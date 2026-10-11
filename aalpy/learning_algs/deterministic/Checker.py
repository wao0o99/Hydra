from pathlib import Path
from hydra.runtime import current, append_json
from aalpy.utils.HelperFunctions import (
    extend_set,
    remove_element,
    print_observation_table,
)
from learn_tcp.model_learning import output_dir


class PreGeneratedInputsExhausted(Exception):
    """Every candidate from the configured file has been evaluated."""


class Checker:
    def __init__(self, sul):
        self.sul = sul
        self.equal_set = {}
        self.inputs = PreGeneratedInputs()

    def check(self, observation_table, hypothesis, alphabet):
        for pkt in alphabet:
            if pkt not in self.equal_set:
                self.equal_set[pkt] = [pkt]
        while True:
            current().check()
            result, alphabet = self.check_once(observation_table, hypothesis, alphabet)
            if result != None:
                break
        return alphabet

    def check_once(self, observation_table, hypothesis, alphabet):
        new_input = self.inputs.next_input()
        if new_input is None:
            append_json(
                output_dir.INPUT_FILE, dict(event="pre_generated_inputs_exhausted")
            )
            raise PreGeneratedInputsExhausted()

        observation_table.A.append(tuple([new_input]))
        alphabet.append(new_input)

        rows_to_query = []
        for row in observation_table.S:
            extended_row = row + (new_input,)
            rows_to_query.append(extended_row)
        observation_table.update_obs_table(s_set=rows_to_query)

        new_input_tuple = (new_input,)
        added_suffixes = extend_set(observation_table.E, [new_input_tuple])
        observation_table.update_obs_table(e_set=added_suffixes)

        print_observation_table(observation_table, "det")

        # search for new state
        rows_to_close = observation_table.get_rows_to_close("shortest_first")

        # search for new state transaction
        new_transaction = self.search_new_transaction(
            observation_table, tuple([new_input])
        )

        # search for a known input, which may equal to new input
        equal_inputs = self.search_equal_inputs(observation_table, tuple([new_input]))

        if equal_inputs != []:
            for equal_input in equal_inputs:
                self.equal_set[equal_input[0]].append(new_input)

        append_json(
            output_dir.INPUT_FILE,
            dict(
                event="candidate_evaluated",
                input=new_input,
                new_state=rows_to_close is not None,
                new_transition=bool(new_transaction),
                equivalent_inputs=equal_inputs,
            ),
        )
        current().add("inputs_evaluated")

        if rows_to_close is not None or new_transaction == True or equal_inputs == []:
            with open(output_dir.NEW_INPUT_FILE, "a") as file:
                file.write(str(new_input))
                file.write("\n")

            if rows_to_close is None:
                remove_element(observation_table.E, new_input_tuple)
                for key in observation_table.T:
                    observation_table.T[key] = observation_table.T[key][:-1]

            self.equal_set[new_input] = [new_input]

            return new_input, alphabet

        else:
            with open(output_dir.EQUAL_INPUT_FILE, "a") as file:
                file.write(str(new_input))
                file.write("\n")

            observation_table.A.pop()
            alphabet.pop()
            remove_element(observation_table.E, new_input_tuple)
            for s in rows_to_query:
                del observation_table.T[s]
            for key in observation_table.T:
                observation_table.T[key] = observation_table.T[key][:-1]

            return None, alphabet

    def search_equal_inputs(self, observation_table, new_input):
        equal_inputs = []
        for a in observation_table.A:
            if a in [
                new_input,
                "LISTEN",
                "ACCEPT",
                "RCV",
                "SEND",
                "CLOSE",
                "CLOSECONNECTION",
            ]:
                continue
            equal_flag = 1
            for s in observation_table.S:
                if (
                    observation_table.T[s + a][0]
                    != observation_table.T[s + new_input][0]
                ):
                    equal_flag = 0
                    break
            if equal_flag == 1:
                equal_inputs.append(a)

        return equal_inputs

    def search_new_transaction(self, observation_table, m_input):
        for s in observation_table.S:
            new_transaction = 1
            for a in observation_table.A:
                if observation_table.T[s + a] == observation_table.T[s + m_input]:
                    new_transaction = 0
                    break
            if new_transaction == 1:
                return True
        return False


class PreGeneratedInputs:
    """File-order candidates, without generated fallback inputs."""

    def __init__(self, path=None):
        path = (
            Path(path)
            if path is not None
            else (
                Path(__file__).resolve().parents[3]
                / "learn_tcp/model_learning/PreGeneratedInputs.txt"
            )
        )
        self.candidates = list(
            dict.fromkeys(
                line.strip() for line in path.read_text().splitlines() if line.strip()
            )
        )
        self.position = 0
        current().set(
            pre_generated_total=len(self.candidates), pre_generated_consumed=0
        )

    def next_input(self):
        if self.position == len(self.candidates):
            return None
        value = self.candidates[self.position]
        self.position += 1
        current().set(pre_generated_consumed=self.position)
        return value
