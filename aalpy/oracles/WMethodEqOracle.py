from itertools import product
from random import shuffle, choice, randint

from aalpy.base.Oracle import Oracle
from aalpy.base.SUL import SUL

import threading
import copy
import ast
import json
from hydra.runtime import current, append_json
import time

import logging

logger = logging.getLogger(__name__)
from learn_tcp.model_learning import output_dir


class WMethodEqOracle(Oracle):
    def __init__(
        self, alphabet: list, sul: SUL, max_number_of_states, shuffle_test_set=True
    ):

        super().__init__(alphabet, sul)


class WpMethodEqOracle(Oracle):
    """
    Equivalence oracle based on characterization set/ W-set. From 'Tsun S. Chow.   Testing software design modeled by
    finite-state machines'.
    """

    def __init__(self, alphabet: list, sul: SUL, max_concurrency=300):

        super().__init__(alphabet, sul)
        self.max_concurrency = max_concurrency

        self.inconsistency = ()
        self.bounded_lock = threading.BoundedSemaphore(value=max_concurrency)
        self.test_lock = threading.Lock()

        self.num_eq_queries = 0
        self.total_eq_queries = 0

    def find_cex(self, hypothesis):

        current().set(
            eq_scheduled=0,
            eq_completed=0,
            eq_failed=0,
            eq_cancelled=0,
            verification="in_progress",
        )
        self.ProcessKnowCex(hypothesis)
        if self.inconsistency != ():
            inconsistency = self.inconsistency
            self.inconsistency = ()
            current().set(verification="counterexample_found")
            current().event("counterexample_found", sequence=inconsistency)
            return inconsistency

        if not hypothesis.characterization_set:
            hypothesis.characterization_set = hypothesis.compute_characterization_set()

        state_identifiers = hypothesis.compute_state_identifiers(
            hypothesis.characterization_set
        )  # Modified method for Wp-method

        # covers every transition of the specification at least once.
        transition_cover = [state.prefix for state in hypothesis.states]

        middle = []
        for i in range(1, 3):
            middle.extend(list(product(self.alphabet, repeat=i)))

        transition_cover_len = len(transition_cover)
        middle_len = len(middle)

        # Pre-calculate total EQ queries for progress tracking
        self.num_eq_queries = 0
        total = 0
        for seq in product(transition_cover, middle):
            full_seq = list(seq[0]) + list(seq[1])
            cur = hypothesis.initial_state
            for sym in full_seq:
                cur = cur.transitions.get(sym)
            si = state_identifiers[cur]
            total += max(len(si), 1)
        self.total_eq_queries = total
        current().set(
            eq_total=total,
            eq_scheduled=0,
            eq_completed=0,
            eq_failed=0,
            eq_cancelled=0,
            verification="in_progress",
        )
        current().event("eq_started", total=total, method=type(self).__name__)
        logger.info(f"EQ (WpMethod): starting {total} test cases")

        for seq in product(transition_cover, middle):
            full_sequence = list(seq[0]) + list(seq[1])

            current_state = hypothesis.initial_state

            for input_symbol in full_sequence:
                next_state = current_state.transitions.get(input_symbol)
                current_state = next_state

            state_identifier = state_identifiers[current_state]

            if len(state_identifier) == 0:
                test_case = tuple(full_sequence)

                self.num_eq_queries += 1

                if self.inconsistency == ():
                    current().check()
                    self.bounded_lock.acquire()
                    current().add("eq_scheduled")
                    thread = threading.Thread(
                        target=self.CexRequest,
                        args=(
                            test_case,
                            hypothesis,
                        ),
                        name="hydra-query-eq",
                    )
                    thread.start()
                    time.sleep(0.005)
                    continue
                else:
                    for i in range(self.max_concurrency):
                        self.bounded_lock.acquire()
                    for i in range(self.max_concurrency):
                        self.bounded_lock.release()
                    inconsistency = self.inconsistency
                    self.inconsistency = ()
                    logger.info(
                        f"EQ (WpMethod): cex found at test #{self.num_eq_queries}, len={len(inconsistency)}"
                    )
                    current().set(verification="counterexample_found")
                    current().event("counterexample_found", sequence=inconsistency)
                    return inconsistency

            for identifier in state_identifier:
                test_case = tuple(full_sequence + list(identifier))

                self.num_eq_queries += 1

                if self.inconsistency == ():
                    current().check()
                    self.bounded_lock.acquire()
                    current().add("eq_scheduled")
                    thread = threading.Thread(
                        target=self.CexRequest,
                        args=(
                            test_case,
                            hypothesis,
                        ),
                        name="hydra-query-eq",
                    )
                    thread.start()
                    time.sleep(0.005)
                    continue
                else:
                    for i in range(self.max_concurrency):
                        self.bounded_lock.acquire()
                    for i in range(self.max_concurrency):
                        self.bounded_lock.release()
                    inconsistency = self.inconsistency
                    self.inconsistency = ()
                    logger.info(
                        f"EQ (WpMethod): cex found at test #{self.num_eq_queries}, len={len(inconsistency)}"
                    )
                    current().set(verification="counterexample_found")
                    current().event("counterexample_found", sequence=inconsistency)
                    return inconsistency

        for i in range(self.max_concurrency):
            self.bounded_lock.acquire()
        for i in range(self.max_concurrency):
            self.bounded_lock.release()
        if self.inconsistency != ():
            inconsistency = self.inconsistency
            self.inconsistency = ()
            logger.info(
                f"EQ (WpMethod): cex found at test #{self.num_eq_queries}, len={len(inconsistency)}"
            )
            current().set(verification="counterexample_found")
            current().event("counterexample_found", sequence=inconsistency)
            return inconsistency
        current().set(
            verification="incomplete"
            if current().snapshot().get("eq_failed", 0)
            else "passed_test_suite"
        )
        current().event(
            "eq_completed",
            tests=self.num_eq_queries,
            failed=current().snapshot().get("eq_failed", 0),
        )
        logger.info(
            f"EQ (WpMethod): {self.num_eq_queries} tests done, no counterexample"
        )

    def ProcessKnowCex(self, hypothesis):

        with open(output_dir.CEX_KNOWN_FILE, "r") as file:
            for line in file:
                line = line.strip()
                try:
                    items = json.loads(line)
                except json.JSONDecodeError:
                    items = ast.literal_eval(line)
                if isinstance(items, dict):
                    items = items["sequence"]
                inp_seq = tuple(items)

                self.bounded_lock.acquire()
                current().add("eq_scheduled")
                self.CexRequest(inp_seq, hypothesis)

                if self.inconsistency != ():
                    return

    def CexRequest(self, inp_seq, hypothesis):
        started = time.monotonic()
        try:
            hypothesis_copy = copy.deepcopy(hypothesis)
            hypothesis_copy.reset_to_initial()

            outputs = tuple(self.sul.query(inp_seq))
            outputs_hyp = []

            for ind, letter in enumerate(inp_seq):
                out_hyp = hypothesis_copy.step(letter)
                outputs_hyp.append(out_hyp)
                if out_hyp != outputs[ind]:
                    with self.test_lock:
                        if self.inconsistency != ():
                            break

                        append_json(
                            output_dir.CEX_FILE,
                            dict(
                                sequence=inp_seq,
                                expected=outputs_hyp,
                                observed=outputs,
                                distinguishing_prefix=inp_seq[: ind + 1],
                            ),
                        )

                        self.inconsistency = inp_seq[: ind + 1]

                    break
            current().add("eq_completed")
        except Exception as e:
            current().add("eq_failed")
            logging.error(f"EQ CexRequest failed: {e} (seq={inp_seq[:3]}...)")
        finally:
            current().record_latency(time.monotonic() - started)
            self.bounded_lock.release()


class RandomWMethodEqOracle(Oracle):
    """
    Randomized version of the W-Method equivalence oracle.
    Random walks stem from fixed prefix (path to the state). At the end of the random
    walk an element from the characterization set is added to the test case.
    """

    def __init__(
        self,
        alphabet: list,
        sul: SUL,
        walks_per_state=300,
        walk_len=5,
        max_concurrency=300,
    ):

        super().__init__(alphabet, sul)
        self.walks_per_state = walks_per_state
        self.random_walk_len = walk_len
        self.freq_dict = dict()
        self.max_concurrency = max_concurrency

        self.inconsistency = ()
        self.bounded_lock = threading.BoundedSemaphore(value=max_concurrency)
        self.test_lock = threading.Lock()

        self.num_eq_queries = 0
        self.total_eq_queries = 0

    def find_cex(self, hypothesis):
        if not hypothesis.characterization_set:
            hypothesis.characterization_set = hypothesis.compute_characterization_set()

        state_identifiers = hypothesis.compute_state_identifiers(
            hypothesis.characterization_set
        )  # Modified method for Wp-method

        states_to_cover = []

        for state in hypothesis.states:
            if state.prefix is None:
                state.prefix = hypothesis.get_shortest_path(
                    hypothesis.initial_state, state
                )

            states_to_cover.extend([state] * self.walks_per_state)

        # Pre-calculate total EQ queries for progress tracking
        self.num_eq_queries = 0
        total = 0
        for state in states_to_cover:
            total += max(len(state_identifiers.get(state, [])), 1)
        self.total_eq_queries = total
        current().set(
            eq_total=total,
            eq_scheduled=0,
            eq_completed=0,
            eq_failed=0,
            eq_cancelled=0,
            verification="in_progress",
        )
        current().event("eq_started", total=total, method=type(self).__name__)
        logger.info(f"EQ (RandomWMethod): starting {total} test cases")

        shuffle(states_to_cover)

        for state in states_to_cover:
            prefix = state.prefix

            middle = list(
                choice(self.alphabet) for _ in range(randint(1, self.random_walk_len))
            )

            full_sequence = list(prefix) + middle

            current_state = hypothesis.initial_state
            for input_symbol in full_sequence:
                next_state = current_state.transitions.get(input_symbol)
                current_state = next_state

            state_identifier = state_identifiers[current_state]

            for identifier in state_identifier:
                test_case = tuple(full_sequence + list(identifier))

                if self.inconsistency == ():
                    self.num_eq_queries += 1

                    current().check()
                    self.bounded_lock.acquire()
                    current().add("eq_scheduled")
                    thread = threading.Thread(
                        target=self.CexRequest,
                        args=(
                            test_case,
                            hypothesis,
                        ),
                        name="hydra-query-eq",
                    )
                    thread.start()
                    time.sleep(0.005)
                else:
                    for i in range(self.max_concurrency):
                        self.bounded_lock.acquire()
                    for i in range(self.max_concurrency):
                        self.bounded_lock.release()
                    inconsistency = self.inconsistency
                    self.inconsistency = ()
                    logger.info(
                        f"EQ (RandomWMethod): cex found at test #{self.num_eq_queries}, len={len(inconsistency)}"
                    )
                    current().set(verification="counterexample_found")
                    current().event("counterexample_found", sequence=inconsistency)
                    return inconsistency

        for i in range(self.max_concurrency):
            self.bounded_lock.acquire()
        for i in range(self.max_concurrency):
            self.bounded_lock.release()
        if self.inconsistency != ():
            inconsistency = self.inconsistency
            self.inconsistency = ()
            logger.info(
                f"EQ (RandomWMethod): cex found at test #{self.num_eq_queries}, len={len(inconsistency)}"
            )
            current().set(verification="counterexample_found")
            current().event("counterexample_found", sequence=inconsistency)
            return inconsistency
        current().set(
            verification="incomplete"
            if current().snapshot().get("eq_failed", 0)
            else "passed_test_suite"
        )
        current().event(
            "eq_completed",
            tests=self.num_eq_queries,
            failed=current().snapshot().get("eq_failed", 0),
        )
        logger.info(
            f"EQ (RandomWMethod): {self.num_eq_queries} tests done, no counterexample"
        )

    def CexRequest(self, inp_seq, hypothesis):
        started = time.monotonic()
        try:
            hypothesis_copy = copy.deepcopy(hypothesis)
            hypothesis_copy.reset_to_initial()

            outputs = tuple(self.sul.query(inp_seq))
            outputs_hyp = []

            for ind, letter in enumerate(inp_seq):
                out_hyp = hypothesis_copy.step(letter)
                outputs_hyp.append(out_hyp)
                if out_hyp != outputs[ind]:
                    with self.test_lock:
                        if self.inconsistency != ():
                            break
                        else:
                            append_json(
                                output_dir.CEX_FILE,
                                dict(
                                    sequence=inp_seq,
                                    expected=outputs_hyp,
                                    observed=outputs,
                                    distinguishing_prefix=inp_seq[: ind + 1],
                                ),
                            )
                            self.inconsistency = inp_seq[: ind + 1]
                            break
            current().add("eq_completed")
        except Exception as e:
            current().add("eq_failed")
            logging.error(f"EQ CexRequest failed: {e} (seq={inp_seq[:3]}...)")
        finally:
            current().record_latency(time.monotonic() - started)
            self.bounded_lock.release()
