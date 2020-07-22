from dataclasses import dataclass
from typing import List, Set, Optional, OrderedDict as ODict
from collections import OrderedDict

from lsst.ci import tsort


class ProductIndex(OrderedDict):
    """
    Container class for a product index with index-related operations.
    """

    def __init__(self, *args, **kwargs):
        """Create a new product index object.

        Parameters
        ----------
        product_index
        """
        super().__init__(*args, **kwargs)
        self.toposorted = False
        self.sorted_groups = []

    def toposort(self) -> ODict[str, "Product"]:
        """Topologically sort the product index and return it.

        This mutates this object.

        Returns
        -------
        OrderedDict[str, `Product`]
            Topologically sorted OrderedDict
        """
        dep_graph = {name: set(product.dependencies) for name, product in self.items()}
        topo_sorted_product_lists = tsort.toposort_mapping(dep_graph)
        ordered_product_list = []
        for group_idx, sort_group in enumerate(topo_sorted_product_lists):
            self.sorted_groups.append(sort_group)
            for product_name in sort_group:
                ordered_product_list.append(self[product_name])
        self.clear()
        for product in ordered_product_list:
            self[product.name] = product
        self.toposorted = True
        return self

    def flat_dependencies(self, product: "Product") -> List["Product"]:
        """Return a flat list of dependencies for this product.

        Parameters
        ----------
        product_index : ordered index (from manifest)


            Returns:
                list of `Product`s.

        """
        resolved: Set[Product] = set()
        self.flat_dependencies_recurse(resolved, product)
        return list(resolved)

    def flat_dependencies_recurse(self, resolved: Set["Product"], product: "Product"):
        """Return a flat list of dependencies for this product.

        Parameters
        ----------
        product_index : ordered index (from manifest)

            Returns:
                list of `Product`s.
        """
        for dependency_name in product.dependencies:
            dependency_product = self[dependency_name]
            if dependency_product not in resolved:
                resolved.add(dependency_product)
                self.flat_dependencies_recurse(resolved, dependency_product)


class Product:
    """Class representing an EUPS product to be built"""

    def __init__(
        self,
        name: str,
        sha1: str,
        version,
        dependencies: List[str],
        optional_dependencies: Optional[List[str]] = None,
        treeish: str = None,
    ):
        self.name = name
        self.sha1 = sha1
        self.version = version
        self.dependencies = dependencies
        self.optional_dependencies = optional_dependencies
        self.treeish = treeish


@dataclass
class Ref:
    """"""

    treeish: str  # Short name (branch name or tag name)
    sha: str  # sha of the git object
    ref: str  # full ref (e.g. ref/heads/<branch>, ref/tags/<tag>)
    ref_type: str  # branch, tag, or head

    HEAD_PREFIX = "refs/heads/"
    TAG_PREFIX = "refs/tags/"

    @staticmethod
    def from_commit_and_ref(commit: str, ref: str) -> "Ref":
        (remote_commit, remote_ref) = (commit, ref)
        ref_type = "head"
        treeish = remote_ref
        if Ref.HEAD_PREFIX in remote_ref:
            ref_type = "branch"
            treeish = remote_ref[len(Ref.HEAD_PREFIX) :]
        elif Ref.TAG_PREFIX in remote_ref:
            ref_type = "tag"
            treeish = remote_ref[len(Ref.HEAD_PREFIX) :]
        return Ref(treeish=treeish, sha=remote_commit, ref=remote_ref, ref_type=ref_type)


class RepoSpec:
    """Represents a git repo specification in repos.yaml. """

    def __init__(self, product: str, url: str, ref: str = "master", lfs: bool = False):
        self.product = product
        self.url = url
        self.ref = ref
        self.lfs = lfs

    def __str__(self):
        return self.url
