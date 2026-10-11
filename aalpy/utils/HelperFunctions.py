import string
from collections import defaultdict
from learn_tcp.model_learning import output_dir


def extend_set(list_to_extend: list, new_elements: list) -> list:
    """
    Helper function to extend a list while maintaining set property.
    They are stored as lists, so with this function set property is maintained.
    :return

    Returns:

        list of elements that were added to the set

    """
    set_repr = set(list_to_extend)
    added_elements = [s for s in new_elements if s not in set_repr]
    list_to_extend.extend(added_elements)
    return added_elements


def remove_element(list_to_modify: list, element_to_remove) -> bool:
    list_to_modify.remove(element_to_remove)
    return True


def all_prefixes(li):
    """
    Returns all prefixes of a list.

    Args:
      li: list from which to compute all prefixes

    Returns:
      list of all prefixes

    """
    return [tuple(li[: i + 1]) for i in range(len(li))]


def all_suffixes(li):
    """
    Returns all suffixes of a list.

    Args:
      li: list from which to compute all suffixes

    Returns:
      list of all suffixes

    """
    return [tuple(li[len(li) - i - 1 :]) for i in range(len(li))]


def profile_function(function: callable, sort_key="cumtime"):
    """

    Args:
      function: callable:
      sort_key:  (Default value = 'cumtime')

    Returns:
        prints the profiling results
    """
    import cProfile

    pr = cProfile.Profile()
    pr.enable()
    function()
    pr.disable()
    pr.print_stats(sort=sort_key)


def random_string_generator(size=10, chars=string.ascii_lowercase + string.digits):
    """

    Args:

      size:  (Default value = 10)
      chars:  (Default value = string.ascii_lowercase + string.digits)

    Returns:

        a random string of length size
    """
    import random

    return "".join(random.choice(chars) for _ in range(size))


def print_learning_info(info: dict):
    """
    Print learning statistics.
    """
    print("-----------------------------------")
    print("Learning Finished.")
    print("Learning Rounds:  {}".format(info["learning_rounds"]))
    print("Number of states: {}".format(info["automaton_size"]))
    print("Time (in seconds)")
    print("  Total                : {}".format(info["total_time"]))
    print("  Learning algorithm   : {}".format(info["learning_time"]))
    print("  Conformance checking : {}".format(info["eq_oracle_time"]))
    print("Learning Algorithm")
    print(" # Membership Queries  : {}".format(info["queries_learning"]))
    if "cache_saved" in info.keys():
        print(" # MQ Saved by Caching : {}".format(info["cache_saved"]))
    print(" # Steps               : {}".format(info["steps_learning"]))
    print("Equivalence Query")
    print(" # Membership Queries  : {}".format(info["queries_eq_oracle"]))
    print(" # Steps               : {}".format(info["steps_eq_oracle"]))
    print("-----------------------------------")



def print_observation_table(ot, table_type):
    """
    Save the complete observation table to the configured output file.

    Args:
        ot: Observation table with attributes such as S, E, and T.
        table_type: Table type, such as 'det', 'non-det', or 'stoc'.
    """
    if table_type == "det":
        s_set, extended_s, e_set, table = ot.S, ot.s_dot_a(), ot.E, ot.T
    elif table_type == "non-det":
        s_set, extended_s, e_set = ot.S, ot.get_extended_S(), ot.E
        table = ot.sul.cache.get_table(s_set + extended_s, e_set)
    elif table_type == "abstracted-non-det":
        s_set, extended_s, e_set, table = ot.S, ot.S_dot_A, ot.E, ot.T
    else:
        s_set, extended_s, e_set, table = ot.S, ot.get_extended_s(), ot.E, ot.T

    headers = [str(e) for e in e_set]
    s_rows = []
    extended_rows = []
    headers.insert(0, "Prefixes / E set")
    for s in s_set:
        row = [str(s)]
        if table_type == "det":
            row.extend(str(e) for e in table[s])
        else:
            row.extend(str(table[s][e]) for e in e_set)
        s_rows.append(row)
    for s in extended_s:
        row = [str(s)]
        if table_type == "det":
            row.extend(str(e) for e in table[s])
        else:
            row.extend(str(table[s][e]) for e in e_set)
        extended_rows.append(row)

    table_data = [headers] + s_rows
    columns = defaultdict(int)
    for i in table_data + extended_rows:
        for index, el in enumerate(i):
            columns[index] = max(columns[index], len(el))

    output_lines = []
    row_len = 0

    for row in table_data:
        formatted_row = "|".join(
            element.ljust(columns[ind] + 1) for ind, element in enumerate(row)
        )
        separator = "-" * len(formatted_row)
        output_lines.append(separator)
        output_lines.append(formatted_row)
        row_len = len(formatted_row)

    output_lines.append("=" * row_len)

    for row in extended_rows:
        formatted_row = "|".join(
            element.ljust(columns[ind] + 1) for ind, element in enumerate(row)
        )
        separator = "-" * len(formatted_row)
        output_lines.append(separator)
        output_lines.append(formatted_row)

    output_lines.append("-" * row_len)

    try:
        with open(output_dir.OB_TABLE_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(output_lines) + "\n")
    except IOError as e:
        print(f"\n--- Error saving to file ob.txt: {e} ---")


def is_suffix_of(suffix, trace) -> bool:
    """

    Args:
      suffix: target suffix
      trace: trace in question

    Returns:

        True if suffix is the suffix of trace.
    """
    if len(trace) < len(suffix):
        return False
    else:
        return trace[-len(suffix) :] == suffix


def get_cex_prefixes(cex, automaton_type):
    """
    Returns all prefixes of the stochastic automaton.

    Args:
        cex: counterexample
        automaton_type: `mdp` or `smm`

    Returns:

        all prefixes of the counterexample based on the `automaton_type`
    """
    if automaton_type == "mdp":
        return [tuple(cex[: i + 1]) for i in range(0, len(cex), 2)]
    return [tuple(cex[:i]) for i in range(0, len(cex) + 1, 2)]


def convert_i_o_traces_for_RPNI(sequences):
    """
    Converts a list of input-output sequences to RPNI format.
    Eg. [[(1,'a'), (2,'b'), (3,'c')], [(6,'7'), (4,'e'), (3,'c')]] to
    [((1,), 'a'), ((1, 2), 'b'), ((1, 2, 3), 'c'), ((6,), '7'), ((6, 4), 'e'), ((6, 4, 3), 'c')]
    """
    rpni_sequences = set()

    for s in sequences:
        for i in range(len(s)):
            inputs = tuple([io[0] for io in s[: i + 1]])
            rpni_sequences.add((inputs, s[i][1]))

    return list(rpni_sequences)


def visualize_classification_tree(root_node):
    from pydot import Dot, Node, Edge

    graph = Dot("classification_tree", graph_type="digraph")
    root_node_dot = Node(
        id(root_node),
        shape="box",
        label=f"Distinguishing String:\n{root_node.distinguishing_string}",
    )
    graph.add_node(root_node_dot)

    queue = [(root_node, root_node_dot)]

    while queue:
        origin_node, origin_node_dot = queue.pop(0)

        for key, child in origin_node.children.items():
            if child.is_leaf():
                destination_dot = Node(
                    id(child), label=f"Access String:\n{child.access_string}"
                )
            else:
                destination_dot = Node(
                    id(child),
                    shape="box",
                    label=f"Distinguishing String:\n{child.distinguishing_string}",
                )
                queue.append((child, destination_dot))
            graph.add_node(destination_dot)
            graph.add_edge(Edge(origin_node_dot, destination_dot, label=key))

    graph.write(path="classification_tree.pdf", format="pdf")
