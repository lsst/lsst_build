from typing import Iterator, Iterable, List, Mapping, Set, Tuple, Dict, Optional


class GraphError(Exception):
    pass


def to_dep_graph(edges: Iterable[Tuple[str, Optional[str]]]) -> Dict[str, Set[str]]:
    """Take an iterable collection of (node, dependency) pairs and return
    a mapping of nodes to the set of dependencies for that node.

    Parameters
    ----------
    edges
        An iterable of tuples of (node, dependency). If the dependency
        is None or an empty string, a node will be added.

    Returns
    -------
        The values in the graph dictionary associated as name to the set of
        dependencies.
    """
    graph: Dict[str, Set] = {}
    for node, dep in edges:
        node_set = graph.setdefault(node, set())
        if dep:  # has an edge
            node_set.add(dep)
            # Ensure all deps are added to graph
            graph.setdefault(dep, set())
    return graph


def toposort(graph_set: Dict[str, Set[str]]) -> Iterator[List]:
    """Perform a topological sort based on Kahn's algorithm.

    This function produces an iterator of topologically sorted dependency
    lists from a graph dictionary.

    This algorithm sorts by removing childless nodes first, adds them to a
    list which it will sort and yield, and removes references to the nodes
    which were removed from the remaining dependency in the graph dictionary.

    Each list that is yielded is, by definition, independent of the other
    items in that list, which means each list may be processed in any order
    (or in parallel).

    see http://en.wikipedia.org/wiki/Topological_sorting

    Parameters
    ----------
    graph_set
        A dependency graph dictionary, with names associated to the set of
        dependencies of a product.

    Returns
    -------
        An iterator which produces lists of dependencies in a bottom-up
        topological sort
    """
    all_dependencies: Set[str] = set()
    self_including_nodes = []
    for node, dependencies in graph_set.items():
        if node in dependencies:
            self_including_nodes.append(node)
        all_dependencies.union(dependencies)
    if self_including_nodes:
        raise GraphError(f"Self-including nodes: {self_including_nodes}")
    missing_nodes = list(sorted(all_dependencies - set(graph_set.keys())))
    if missing_nodes:
        raise GraphError(f"Missing nodes in mapping: {missing_nodes}")
    remaining_nodes = graph_set.copy()
    while True:
        # Visit all nodes, remove nodes without dependencies
        childless_nodes = set(node for node, dependencies in remaining_nodes.items() if not dependencies)
        if not childless_nodes:
            break
        yield list(sorted(childless_nodes))
        more_nodes = {}
        # Remove nodes we returned from dependency lists
        for node, dependencies in remaining_nodes.items():
            if node not in childless_nodes:
                more_nodes[node] = dependencies - childless_nodes
        remaining_nodes = more_nodes
    if remaining_nodes:
        remaining_nodes_list = sorted(remaining_nodes)
        raise GraphError(f"Cycle among nodes: {remaining_nodes_list}")


def toposort_dfs(graph: Mapping[str, Set[str]]) -> List[str]:
    """Perform a depth-first search topological sort on the mapping of
    dependencies and return an ordered list.

    This uses a postorder tree-traversal in sorted order of
    the child nodes.

    Compared to the flattened version of toposort_mapping output, this
    sort produces a list of dependencies which will go deeper, faster,
    on the sorted list of dependencies for a product.

    In practical terms, this may be useful in producing builds that build
    products with more dependencies earlier in the build cycle.

    Parameters
    ----------
    graph_set
        A dependency graph dictionary, with names associated to the set of
        dependencies of a product.

    Returns
    -------
        bottom-up topologically sorted list of products
    """

    class Node:
        def __init__(self, name, dependencies):
            self.name = name
            self.dependencies = sorted(set(dependencies))
            self.processing = False
            self.processed = False

    sorted_node_names = []
    node_map = {name: Node(name, dependencies) for name, dependencies in graph.items()}

    def visit(n: Node):
        if n.processed:
            return
        if n.processing:
            raise GraphError(f"Cycle: {n.name}")
        n.processing = True
        for dep in sorted(n.dependencies):
            try:
                visit(node_map[dep])
            except GraphError as e:
                # unroll cycle
                raise GraphError(e.args[0] + f" <- {n.name}")
        n.processing = False
        n.processed = True
        sorted_node_names.append(n.name)

    for node in sorted(node_map.values(), key=lambda n: n.name):
        visit(node)
    return sorted_node_names


def flatten(dependency_lists: Iterable[List]) -> List[str]:
    """Flatten an iterable collection of dependency lists for serial
    processing.

    `toposort` returns an iterable of lists, this is a convenience function
    to flatten the list for serial processing.
    """
    flattened = []
    for sorted_list in dependency_lists:
        flattened.extend(sorted_list)
    return flattened
