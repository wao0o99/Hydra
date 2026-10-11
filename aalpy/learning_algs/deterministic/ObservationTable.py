from collections import defaultdict

from aalpy.base import Automaton, SUL
from aalpy.automata import (
    Dfa,
    DfaState,
    MealyState,
    MealyMachine,
    MooreMachine,
    MooreState,
)

import threading
import time
from hydra.runtime import current

import logging

logger = logging.getLogger(__name__)

aut_type = ["dfa", "mealy", "moore"]
closing_options = ["shortest_first", "longest_first", "single", "single_longest"]


class ObservationTable:
    def __init__(
        self,
        alphabet: list,
        sul: SUL,
        automaton_type,
        prefixes_in_cell=False,
        max_concurrency=300,
    ):
        """
        Constructor of the observation table. Initial queries are asked in the constructor.

        Args:

            alphabet: input alphabet
            sul: system under learning
            automaton_type: automaton type, one of ['dfa', 'mealy', 'moore']

        Returns:

        """
        assert automaton_type in aut_type
        assert alphabet is not None and sul is not None
        self.automaton_type = automaton_type

        # If True add prefixes of each element of E set to a cell, else only add the output
        self.prefixes_in_cell = prefixes_in_cell

        self.empty_word = tuple()
        self.A = [tuple([a]) for a in alphabet]
        self.S = list()  # prefixes of S
        self.S.append(self.empty_word)
        self.E = []
        self.E.append(self.empty_word)
        self.T = defaultdict(tuple)

        self.sul = sul

        self.num_mq_queries = 0
        self.mq_batch_total = 0
        self.mq_batch_progress = 0

        # DFAs and Moore machines use empty word for identification of accepting states/state outputs
        if self.automaton_type == "dfa" or self.automaton_type == "moore":
            self.E.insert(0, empty_word)  # noqa: F821 - Preserve the original non-Mealy branch.

        self.max_concurrency = max_concurrency
        self.bounded_lock = threading.BoundedSemaphore(value=max_concurrency)
        self.T_lock = threading.Lock()

        self.tmp_output_dict = {}
        self.tmp_lock = threading.Lock()

    def get_rows_to_close(self, closing_strategy="longest_first"):
        """
        Get rows for that need to be closed. Row selection is done according to closing_strategy.
        The length of the row is defined by the length of the prefix corresponding to the row in the S set.
        longest_first -> get all rows that need to be closed and ask membership queries for the longest row first
        shortest_first -> get all rows that need to be closed and ask membership queries for the shortest row first
        single -> find and ask membership query for the single row
        single_longest -> returns single longest row to close

        Args:

            closing_strategy: one of ['shortest_first', 'longest_first', 'single'] (Default value = 'longest_first')

        Returns:

            list if non-closed exist, None otherwise: rows that will be moved to S set and closed

        """
        assert closing_strategy in closing_options
        rows_to_close = []
        row_values = set()

        s_rows = {self.T[s] for s in self.S}

        for t in self.s_dot_a():
            row_t = self.T[t]
            if row_t[1:] not in row_values and row_t[1:] not in {
                row[1:] for row in s_rows
            }:
                rows_to_close.append(t)
                row_values.add(row_t[1:])

                if closing_strategy == "single":
                    return rows_to_close

        if not rows_to_close:
            return None

        if "longest" in closing_strategy:
            rows_to_close.sort(key=len, reverse=True)
            if closing_strategy == "longest_first":
                return rows_to_close
            if closing_strategy == "single_longest":
                return [rows_to_close[0]]

        return rows_to_close

    def get_causes_of_inconsistency(self):
        """
        If the two rows in the S set are the same, but their one letter extensions are not, this method founds
        the cause of inconsistency and returns it.
        :return:

        Returns:

            a+e values that are the causes of inconsistency

        """
        for i, s1 in enumerate(self.S):
            for s2 in self.S[i + 1 :]:
                if self.T[s1] == self.T[s2]:
                    for a in self.A:
                        if self.T[s1 + a] != self.T[s2 + a]:
                            for index, e in enumerate(self.E):
                                if self.T[s1 + a][index] != self.T[s2 + a][index]:
                                    return [(a + e)]

        return None

    def s_dot_a(self):
        """
        Helper generator function that returns extended S, or S.A set.
        """
        s_set = set(self.S)
        for s in self.S:
            for a in self.A:
                if s + a not in s_set:
                    yield s + a

    def update_obs_table(self, s_set: list = None, e_set: list = None):
        """
        Perform the membership queries.

        Args:

            s_set: Prefixes of S set on which to preform membership queries. If None, then whole S set will be used.

            e_set: Suffixes of E set on which to perform membership queries. If None, then whole E set will be used.

        Returns:

        """

        update_S = s_set if s_set else list(self.S) + list(self.s_dot_a())
        update_E = e_set if e_set else self.E

        # This could save few queries
        update_S.reverse()

        self.mq_batch_total = len(update_S) * len(update_E)
        self.mq_batch_progress = 0

        current().set(
            mq_scheduled=0,
            mq_completed=0,
            mq_failed=0,
            mq_cancelled=0,
            mq_total=self.mq_batch_total,
        )
        for s in update_S:
            for e in update_E:
                current().check()
                self.mq_batch_progress += 1
                # Run queries in multiple threads
                self.bounded_lock.acquire()
                if len(self.T[s]) != len(self.E):
                    s_plus_e = s + e
                    current().add("mq_scheduled")
                    thread = threading.Thread(
                        target=self.MqRequest,
                        args=(
                            s_plus_e,
                            s,
                            e,
                        ),
                        name="hydra-query-mq",
                    )
                    thread.start()
                    time.sleep(0.02)
            # Wait for all threads to finish
        for i in range(self.max_concurrency):
            self.bounded_lock.acquire()
        for i in range(self.max_concurrency):
            self.bounded_lock.release()
        sent = 0
        cached = 0
        timeout_count = 0
        for s in update_S:
            for e in update_E:
                entry = self.tmp_output_dict[s][e]
                self.T[s] = self.T[s] + entry
                if entry == ("timeout",):
                    timeout_count += 1
                sent += 1
        cached = self.mq_batch_total - sent
        logger.info(
            f"MQ batch done: {sent} queries"
            f" ({timeout_count} timeout, {sent - timeout_count} response)"
            + (f", {cached} skipped" if cached else "")
        )
        self.tmp_output_dict = {}

    def MqRequest(self, s_plus_e, s, e):
        started = time.monotonic()
        try:
            if s_plus_e == self.empty_word:
                obs_table_entry = ("timeout",)
            else:
                output = tuple(self.sul.query(s_plus_e))
                obs_table_entry = (output[-1],)
            with self.tmp_lock:
                current().add("mq_completed")
                self.num_mq_queries += 1
                if s not in self.tmp_output_dict:
                    self.tmp_output_dict[s] = {}
                self.tmp_output_dict[s][e] = obs_table_entry
        except Exception as e_exc:
            current().add("mq_failed")
            logging.error(f"MQ request failed: {e_exc} (seq={s_plus_e[:3]}...)")
        finally:
            current().record_latency(time.monotonic() - started)
            self.bounded_lock.release()

    def gen_hypothesis(self, no_cex_processing_used=False) -> Automaton:
        """
        Generate automaton based on the values found in the observation table.
        :return:

        Args:

            check_for_duplicate_rows:  (Default value = False)

        Returns:

            Automaton of type `automaton_type`

        """
        state_distinguish = dict()
        states_dict = dict()
        initial_state = None
        automaton_class = {"dfa": Dfa, "mealy": MealyMachine, "moore": MooreMachine}

        s_set = self.S
        # Added check for the algorithm without counterexample processing
        if no_cex_processing_used:
            s_set = self._get_row_representatives()

        # create states based on S set
        stateCounter = 0
        for prefix in s_set:
            state_id = f"s{stateCounter}"

            if self.automaton_type == "dfa":
                states_dict[prefix] = DfaState(state_id)
                states_dict[prefix].is_accepting = self.T[prefix][0]
            elif self.automaton_type == "moore":
                states_dict[prefix] = MooreState(state_id, output=self.T[prefix][0])
            else:
                states_dict[prefix] = MealyState(state_id)

            states_dict[prefix].prefix = prefix
            row_prefix = self.T[prefix]
            state_distinguish[tuple(row_prefix[1:])] = states_dict[prefix]

            if not prefix:
                initial_state = states_dict[prefix]
            stateCounter += 1

        # add transitions based on extended S set
        for prefix in s_set:
            for a in self.A:
                row_prefix_plus_a = self.T[prefix + a]
                state_in_S = state_distinguish[row_prefix_plus_a[1:]]
                states_dict[prefix].transitions[a[0]] = state_in_S
                states_dict[prefix].output_fun[a[0]] = self.T[prefix + a][0]

        automaton = automaton_class[self.automaton_type](
            initial_state, list(states_dict.values())
        )
        automaton.characterization_set = self.E

        return automaton

    def _get_row_representatives(self):
        self.S.sort(key=len)
        representatives = defaultdict(list)
        for prefix in self.S:
            representatives[self.T[prefix]].append(prefix)

        return [r[0] for r in representatives.values()]
