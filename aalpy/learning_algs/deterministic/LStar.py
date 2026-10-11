import time
from hydra.runtime import current
import sys
import logging
from collections import deque

logger = logging.getLogger(__name__)
from aalpy.oracles import RandomWMethodEqOracle, WpMethodEqOracle
from aalpy.base import Oracle, SUL
from aalpy.utils.HelperFunctions import (
    extend_set,
    print_learning_info,
    print_observation_table,
    all_prefixes,
)
from .CounterExampleProcessing import (
    longest_prefix_cex_processing,
    rs_cex_processing,
    counterexample_successfully_processed,
    linear_cex_processing,
    exponential_cex_processing,
)
from .ObservationTable import ObservationTable
from ...base.SUL import CacheSUL
from .Checker import Checker, PreGeneratedInputsExhausted
from learn_tcp.model_learning import output_dir

current_stage = ["NULL"]
learning_stats = {"rounds": 0, "states": 0, "alphabet_size": 0, "cex_count": 0}
learning_events = deque(maxlen=50)

counterexample_processing_strategy = [
    None,
    "rs",
    "longest_prefix",
    "linear_fwd",
    "linear_bwd",
    "exponential_fwd",
    "exponential_bwd",
]
closedness_options = ["suffix_all", "suffix_single"]
print_options = [0, 1, 2, 3]


def run_Lstar(
    alphabet: list,
    sul: SUL,
    automaton_type,
    samples=None,
    closing_strategy="shortest_first",
    cex_processing="rs",
    e_set_suffix_closed=False,
    all_prefixes_in_obs_table=True,
    cache_and_non_det_check=True,
    return_data=False,
    print_level=2,
    max_concurrency=300,
):
    """
    Executes L* algorithm.

    Args:

        alphabet: input alphabet

        sul: system under learning

        automaton_type: type of automaton to be learned. Either 'dfa', 'mealy' or 'moore'.

        samples: input output traces provided to the learning algorithm. They are added to cache and could reduce
        total interaction with the system. Syntax: list of [(input_sequence, output_sequence)] or None

        closing_strategy: closing strategy used in the close method. Either 'longest_first', 'shortest_first' or
            'single' (Default value = 'shortest_first')

        cex_processing: Counterexample processing strategy. Either None, 'rs' (Riverst-Schapire), 'longest_prefix'.
            (Default value = 'rs'), 'longest_prefix', 'linear_fwd', 'linear_bwd', 'exponential_fwd', 'exponential_bwd'

        e_set_suffix_closed: True option ensures that E set is suffix closed,
            False adds just a single suffix per counterexample.

        all_prefixes_in_obs_table: if True, entries of observation table will contain the whole output of the whole
            suffix, otherwise just the last output meaning that all prefixes of the suffix will be added.
            If False, just a single suffix will be added.

        cache_and_non_det_check: Use caching and non-determinism checks (Default value = True)

        return_data: if True, a map containing all information(runtime/#queries/#steps) will be returned
            (Default value = False)

        print_level: 0 - None, 1 - just results, 2 - current round and hypothesis size, 3 - educational/debug
            (Default value = 2)

    Returns:

        automaton of type automaton_type (dict containing all information about learning if 'return_data' is True)

    """

    assert cex_processing in counterexample_processing_strategy
    assert print_level in print_options

    if cache_and_non_det_check or samples is not None:
        # Wrap the sul in the CacheSUL, so that all steps/queries are cached
        sul = CacheSUL(sul)

    rt = current()
    learning_events.clear()
    learning_stats.clear()
    learning_stats.update(rounds=0, states=0, alphabet_size=len(alphabet), cex_count=0)
    learning_rounds = 0
    hypothesis = None

    hypothesis_print_count = 0

    learning_stats["cache_sul"] = sul if cache_and_non_det_check else None

    observation_table = ObservationTable(
        alphabet,
        sul,
        automaton_type,
        all_prefixes_in_obs_table,
        max_concurrency=max_concurrency,
    )
    checker = Checker(sul)

    # Initial update of observation table, for empty row
    observation_table.update_obs_table()

    eq_oracle = WpMethodEqOracle(alphabet, sul, max_concurrency=max_concurrency)
    learning_stats["eq_oracle"] = eq_oracle
    learning_stats["obs_table"] = observation_table
    logger.info(f"Learning started with {len(alphabet)} alphabet symbols")
    learning_events.append((time.time(), "Learning started"))

    while True:
        rt.check()
        cex = None
        while True:
            # Make observation table consistent (iff there is no counterexample processing)
            current_stage[0] = "Make OB Table Consistent"
            rt.set(stage="Make OB Table Consistent")
            if not cex_processing:
                inconsistent_rows = observation_table.get_causes_of_inconsistency()
                while inconsistent_rows is not None:
                    added_suffix = extend_set(observation_table.E, inconsistent_rows)
                    observation_table.update_obs_table(e_set=added_suffix)
                    inconsistent_rows = observation_table.get_causes_of_inconsistency()

            # Close observation table
            current_stage[0] = "Make OB Table Close"
            rt.set(stage="Make OB Table Close")
            rows_to_close = observation_table.get_rows_to_close(closing_strategy)
            while rows_to_close is not None:
                rows_to_query = []
                for row in rows_to_close:
                    observation_table.S.append(row)
                    rows_to_query.extend([row + (a,) for a in alphabet])
                observation_table.update_obs_table(s_set=rows_to_query)
                rows_to_close = observation_table.get_rows_to_close(closing_strategy)

            # Generate hypothesis
            hypothesis = observation_table.gen_hypothesis(
                no_cex_processing_used=cex_processing is None
            )
            hypothesis.save(output_dir.model_path(hypothesis_print_count))

            hypothesis_print_count += 1
            rt.set(
                hypothesis=hypothesis_print_count,
                states=len(hypothesis.states),
                alphabet_size=len(alphabet),
                verification="not_tested",
                model_path=output_dir.model_path(hypothesis_print_count - 1),
            )
            rt.event(
                "hypothesis_generated",
                number=hypothesis_print_count,
                states=len(hypothesis.states),
                alphabet=list(alphabet),
                path=output_dir.model_path(hypothesis_print_count - 1),
            )
            learning_stats["states"] = len(hypothesis.states)
            learning_stats["alphabet_size"] = len(alphabet)
            msg = f"Hypothesis #{hypothesis_print_count} generated ({len(hypothesis.states)} states)"
            logger.info(msg)
            learning_events.append((time.time(), msg))

            # Find counterexample if none has previously been found (first round) and cex is successfully processed
            # (not a counterexample in the current hypothesis)
            if learning_rounds % 5 != 0:
                break

            if cex is None or counterexample_successfully_processed(
                sul, cex, hypothesis
            ):
                current_stage[0] = "Find Counterexample"
                rt.set(stage="Find Counterexample")
                cex = eq_oracle.find_cex(hypothesis)
                if cex is None:
                    break

            # make sure counterexample is a tuple in case oracle returns a list
            cex = tuple(cex)
            learning_stats["cex_count"] = learning_stats.get("cex_count", 0) + 1
            rt.add("counterexamples")
            msg = f"Counterexample found (len={len(cex)}): {cex}"
            logger.info(msg)
            learning_events.append(
                (time.time(), f"Counterexample found (len={len(cex)})")
            )

            # Process counterexample and ask membership queries
            if not cex_processing:
                s_to_update = []
                added_rows = extend_set(observation_table.S, all_prefixes(cex))
                s_to_update.extend(added_rows)
                for p in added_rows:
                    s_to_update.extend([p + (a,) for a in alphabet])

                observation_table.update_obs_table(s_set=s_to_update)
                continue

            elif cex_processing == "longest_prefix":
                cex_suffixes = longest_prefix_cex_processing(
                    observation_table.S + list(observation_table.s_dot_a()),
                    cex,
                    closedness="suffix",
                )
            elif cex_processing == "rs":
                cex_suffixes = rs_cex_processing(
                    sul, cex, hypothesis, e_set_suffix_closed, closedness="suffix"
                )
            else:
                direction = cex_processing[-3:]
                if "linear" in cex_processing:
                    cex_suffixes = linear_cex_processing(
                        sul,
                        cex,
                        hypothesis,
                        e_set_suffix_closed,
                        direction=direction,
                        closedness="suffix",
                    )
                else:
                    cex_suffixes = exponential_cex_processing(
                        sul,
                        cex,
                        hypothesis,
                        e_set_suffix_closed,
                        direction=direction,
                        closedness="suffix",
                    )

            current_stage[0] = "Update OB Table based on Counterexample"

            rt.set(stage="Update OB Table based on Counterexample")
            added_suffixes = extend_set(observation_table.E, cex_suffixes)
            observation_table.update_obs_table(e_set=added_suffixes)

        current_stage[0] = "Check for New Inputs"

        rt.set(stage="Check for New Inputs")
        old_alpha_size = len(alphabet)
        try:
            alphabet = checker.check(observation_table, hypothesis, alphabet)
        except PreGeneratedInputsExhausted:
            rt.reason = "pre_generated_inputs_exhausted"
            logger.info("All pre-generated inputs evaluated; learning completed")
            rt.event("pre_generated_inputs_exhausted")
            break
        if len(alphabet) > old_alpha_size:
            msg = f"Alphabet expanded: {old_alpha_size} -> {len(alphabet)} symbols"
            logger.info(msg)
            rt.event("alphabet_expanded", size=len(alphabet))
            learning_events.append((time.time(), msg))
        learning_rounds += 1
        learning_stats["rounds"] = learning_rounds
        rt.set(rounds=learning_rounds, alphabet_size=len(alphabet))
        msg = f"Round {learning_rounds} complete"
        logger.info(msg)
        learning_events.append((time.time(), msg))
        eq_oracle = RandomWMethodEqOracle(
            alphabet, sul, max_concurrency=max_concurrency
        )
        learning_stats["eq_oracle"] = eq_oracle

    info = rt.snapshot()
    if cache_and_non_det_check:
        info["cache_saved"] = sul.num_cached_queries

    if print_level > 0:
        logger.info("Learning finished: %s", rt.reason)

    if return_data:
        return hypothesis, info

    return hypothesis
