from abc import ABC, abstractmethod

from aalpy.base.CacheTree import CacheTree, CacheDict

import threading
import time
import logging
from hydra.runtime import current

logger = logging.getLogger(__name__)


class SUL(ABC):
    """
    System Under Learning (SUL) abstract class. Defines the interaction between the learning algorithm and the system
    under learning. All systems under learning have to implement this class, as it is
    passed to the learning algorithm and the equivalence oracle.
    """

    def __init__(self):
        self.num_queries = 0
        self.num_steps = 0
        self.num_cached_queries = 0
        self.num_retries = 0
        self.write_lock = threading.Lock()

    def query(self, word: tuple) -> list:
        """
        Performs a membership query on the SUL. Before the query, pre() method is called and after the query post()
        method is called. Each letter in the word (input in the input sequence) is executed using the step method.

        Args:

            word: membership query (word consisting of letters/inputs)

        Returns:

            list of outputs, where the i-th output corresponds to the output of the system after the i-th input

        """
        rt = current()
        max_retries = 5
        for attempt in range(max_retries + 1):
            SUL_port1 = self.pre()
            if len(word) == 0:
                out1 = None
            else:
                out1 = [self.step(SUL_port1, letter) for letter in word]
            self.post(SUL_port1)

            SUL_port2 = self.pre()
            if len(word) == 0:
                out2 = None
            else:
                out2 = [self.step(SUL_port2, letter) for letter in word]
            self.post(SUL_port2)

            SUL_port = self.pre()
            if len(word) == 0:
                out = None
            else:
                out = [self.step(SUL_port, letter) for letter in word]
            self.post(SUL_port)

            rt.add("query_executions", 3)
            rt.add("input_steps", 3 * len(word))
            if (out == out1) and (out1 == out2):
                break
            else:
                self.num_retries += 1
                rt.add("consistency_retries")
                if attempt >= 3:
                    logging.warning(
                        "Query outputs differ on attempt %d/6: %r", attempt + 1, word
                    )
                rt.event(
                    "query_inconsistent",
                    attempt=attempt + 1,
                    word=word,
                    outputs=[out1, out2, out],
                    ports=[SUL_port1, SUL_port2, SUL_port],
                )
                if attempt >= max_retries:
                    rt.add("consistency_fallbacks")
                    logging.info(
                        f"SUL consistency check exhausted {max_retries} retries, "
                        f"using last result for word={word}"
                    )
                    break
                time.sleep(3)

        logging.debug(f"Sequence query succeed, with port: {SUL_port}, {word}, {out}")

        self.num_queries += 1
        self.num_steps += len(word)

        return out

    def io_query(self, word: tuple):
        return list(zip(word, self.query(word)))

    @abstractmethod
    def pre(self):
        """
        Resets the system. Called after post method in the equivalence query.
        """
        pass

    @abstractmethod
    def post(self, port):
        """
        Performs additional cleanup on the system in necessary. Called before pre method in the equivalence query.
        """
        pass

    @abstractmethod
    def step(self, port, letter):
        """
        Executes an action on the system under learning and returns its result.

        Args:

            letter: Single input that is executed on the SUL.

        Returns:

            Output received after executing the input.

        """
        pass


class CacheSUL(SUL):
    """
    System under learning that keeps a multiset of all queries in memory.
    This multiset/cache is encoded as a tree.
    """

    def __init__(self, sul: SUL, cache_type="tree"):
        super().__init__()
        self.sul = sul
        self.cache = CacheTree() if cache_type == "tree" else CacheDict()
        self.cache_lock = threading.Lock()
        self.num_sul_queries = 0  # queries that actually reached the real SUL

    def query(self, word):
        """
        Performs a membership query on the SUL if and only if `word` is not a prefix of any trace in the cache.
        Before the query, pre() method is called and after the query post()
        method is called. Each letter in the word (input in the input sequence) is executed using the step method.

        Args:

            word: membership query (word consisting of letters/inputs)

        Returns:

            list of outputs, where the i-th output corresponds to the output of the system after the i-th input

        """
        cached_query = self.cache.in_cache(word)
        if cached_query:
            current().add("cache_hits")
            self.num_cached_queries += 1
            return cached_query

        # get outputs using default query method
        current().add("cache_misses")
        out = self.sul.query(word)
        self.num_sul_queries += 1

        # add input/outputs to tree
        with self.cache_lock:
            self.cache.reset()
            for i, o in zip(word, out):
                self.cache.step_in_cache(i, o)

        self.num_queries += 1
        self.num_steps += len(word)
        return out

    def pre(self):
        """
        Reset the system under learning and current node in the cache tree.
        """
        self.cache.reset()
        return self.sul.pre()

    def post(self, port):
        self.sul.post(port)

    def step(self, port, letter):
        """
        Executes an action on the system under learning, adds it to the cache and returns its result.

        Args:

           letter: Single input that is executed on the SUL.

        Returns:

           Output received after executing the input.

        """
        out = self.sul.step(port, letter)
        return out
