# This file is part of lsst_build.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# Use of this source code is governed by a 3-clause BSD-style
# license that can be found in the LICENSE file.

from lsst.ci.models import Product, ProductIndex

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
