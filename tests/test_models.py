import sys
from lsst.ci.models import ProductIndex, Product

from unittest.mock import Mock

sys.modules['eups'] = Mock()
sys.modules['eups.tags'] = Mock()

dependency_nodes = [
    ("z", "y"),  # z depends on y
    ("b", "a"),  # b depends on a
    ("c", "a"),  # c depends on a
    ("c", "b"),  # c depends on b
    ("y", "x"),  # y depends on x
    # z depends implicitly on x through y
]


def test_flatten_dependencies():
    """Verify transitive dependencies are in the flat dependencies
    list.
    """
    product_index = ProductIndex()

    # `z` has a transitive dependency on `x` which we will test
    graph = {"z": {"y"}, "y": {"x"}, "b": {"a"}, "a": set(), "c": {"a", "b"}, "x": set()}

    sha1 = "c0ffeecafe"
    version = "0.0"

    for product_name, dependency_set in graph.items():
        product = Product(product_name, sha1, version, list(dependency_set))
        product_index[product_name] = product

    z = product_index["z"]
    # Note: toposort only required to order output
    assert product_index["x"] in product_index.flat_dependencies(z)


def test_dependants():
    product_index = ProductIndex()
    graph = {"z": {"y"}, "y": {"x"}, "b": {"a"}, "a": set(), "c": {"a", "b"}, "x": set()}

    sha1 = "c0ffeecafe"
    version = "0.0"

    for product_name, dependency_set in graph.items():
        product = Product(product_name, sha1, version, list(dependency_set))
        product_index[product_name] = product

    set_1 = set([d.name for d in product_index.dependants(product_index["a"])])
    assert {"b", "c"}.issubset(set_1)


def test_remove():

    # remove_products is a convenience function to reduce the complexity
    # of ProductIndex for now.
    from lsst.ci.prepare import remove_products

    product_index = ProductIndex()
    graph = {"z": {"y"}, "y": {"x"}, "b": {"a"}, "a": set(), "c": {"a", "b"}, "x": set()}

    sha1 = "c0ffeecafe"
    version = "0.0"

    for product_name, dependency_set in graph.items():
        product = Product(product_name, sha1, version, list(dependency_set))
        product_index[product_name] = product

    product_index = remove_products(product_index, ["a"])
    assert "a" not in product_index
    for dependency in product_index.values():
        assert "a" not in dependency.dependencies
        if dependency.optional_dependencies:
            assert "a" not in dependency.optional_dependencies
