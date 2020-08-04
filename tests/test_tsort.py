from lsst.ci import tsort

edgeless_data_edges = [
    ("a", ""),  # No edge
    ("b", None),  # No edge
    ("z", "y"),
    ("z", "x"),
    ("y", "x"),
]

edgeless_data = tsort.to_dep_graph(edgeless_data_edges)

good_data_edges = [
    ("z", "y"),  # z depends on y
    ("b", "a"),  # b depends on a
    ("c", "a"),  # c depends on a
    ("c", "b"),  # c depends on b
    ("z", "x"),  # z depends on x
    ("y", "x"),  # y depends on x
]
good_data = tsort.to_dep_graph(good_data_edges)

good_data_start_edges = list(good_data_edges)
good_data_start_edges.append(("start", "c"))  # start depends on c
good_data_start_edges.append(("start", "z"))  # start depends on x

good_data_start = tsort.to_dep_graph(good_data_start_edges)


# c depends on a
# a depends on c
bad_data_edges = [
    ("b", "a"),
    ("c", "a"),
    ("c", "b"),
    ("y", "x"),
    ("z", "x"),
    ("a", "c"),
]

bad_data = tsort.to_dep_graph(bad_data_edges)

multigraph_data_edges = [
    ("b", "a"),
    ("c", "a"),
    ("c", "b"),
    ("j", "i"),
    ("y", "x"),
    ("z", "x"),
    ("z", "y"),
    ("start", "b"),
    ("start", "z"),
    ("start", "j"),
]

multigraph_data = tsort.to_dep_graph(multigraph_data_edges)


def test_toposort():
    sorted_lists = list(tsort.toposort(edgeless_data))
    assert sorted_lists == [["a", "b", "x"], ["y"], ["z"]]

    sorted_lists = list(tsort.toposort(good_data))
    assert sorted_lists == [["a", "x"], ["b", "y"], ["c", "z"]]

    sorted_lists = list(tsort.toposort(good_data_start))
    assert sorted_lists == [["a", "x"], ["b", "y"], ["c", "z"], ["start"]]

    sorted_lists = list(tsort.toposort(multigraph_data))
    assert sorted_lists == [["a", "i", "x"], ["b", "j", "y"], ["c", "z"], ["start"]]


def test_toposort_dfs():
    dfs_sorted = tsort.toposort_dfs(good_data)
    assert dfs_sorted == ["a", "b", "c", "x", "y", "z"]

    dfs_sorted = tsort.toposort_dfs(good_data_start)
    assert dfs_sorted == ["a", "b", "c", "x", "y", "z", "start"]

    dfs_sorted = list(tsort.toposort_dfs(multigraph_data))
    assert dfs_sorted == ["a", "b", "c", "i", "j", "x", "y", "z", "start"]


def test_flatten():
    sorted_lists = tsort.toposort(good_data)
    flattened = tsort.flatten(sorted_lists)
    assert flattened == ["a", "x", "b", "y", "c", "z"]

    sorted_lists = tsort.toposort(good_data_start)
    flattened = tsort.flatten(sorted_lists)
    assert flattened == ["a", "x", "b", "y", "c", "z", "start"]


def test_graph_error():
    try:
        tsort.toposort(bad_data)
        assert False
    except tsort.GraphError as e:
        assert str(e) == "Cycle among nodes: ['a', 'b', 'c']"
