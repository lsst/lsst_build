from typing import Iterator, Iterable, List, Mapping, Set, Tuple, Dict


class GraphError(Exception):
    pass


def to_dep_graph(edges: Iterable[Tuple[str, str]]) -> Dict[str, Set[str]]:
    """Takes an iterable collection of (object, dependency) pairs and returns
    as a graph set"""
    graph = {}
    for node, dep in edges:
        graph.setdefault(node, set()).add(dep)
        # Ensure all deps are added to graph
        graph.setdefault(dep, set())
    return graph


def toposort(edges: Iterable[Tuple[str, str]]) -> Iterator[List]:
    """Takes an iterable collection of (object, dependency) pairs
    and returns an iterator of ordered dependency lists.
    The items in each list can be processed in any order.
    """
    return toposort_mapping(to_dep_graph(edges))


def toposort_dfs(edges: Iterable[Tuple[str, str]]) -> List[str]:
    """Takes an iterable collection of (object, dependency) pairs
    and returns an list of ordered dependencies.
    """
    return toposort_dfs_mapping(to_dep_graph(edges))


def toposort_mapping(graph_set: Dict[str, Set[str]]) -> Iterator[List]:
    """Returns an iterator of ordered dependency lists from a 
    graph dictionary.
    The items in each list can be processed in any order.
    """
    all_dependencies = set()
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
        raise GraphError(f"Cycle among nodes: {remaining_nodes}")


def toposort_dfs_mapping(graph: Mapping[str, Set[str]]) -> List[str]:
    """Topological sort - Depth-first search.
    The results are already flattened
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
        n.proccessing = False
        n.processed = True
        sorted_node_names.append(n.name)

    for node in sorted(node_map.values(), key=lambda n: n.name):
        visit(node)
    return sorted_node_names


def flatten(dependency_lists: Iterable[List]) -> List[str]:
    """Flattens an iterable collection of dependency lists for serial processing"""
    flattened = []
    for sorted_list in dependency_lists:
        flattened.extend(sorted_list)
    return flattened
