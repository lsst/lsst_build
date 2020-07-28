from __future__ import annotations
from dataclasses import dataclass
from typing import List, Set, Optional, Dict

from lsst.ci import tsort


class ProductIndex(dict):
    """
    Container class for a product index with index-related operations.
    """

    def __init__(self, *args, **kwargs):
        """Create a new product index object.

        """
        super().__init__(*args, **kwargs)
        self.toposorted = False
        self.sorted_groups = []

    def toposort(self) -> Dict[str, Product]:
        """Topologically sort the product index and return it.

        This mutates this object, and it returns itself for convenience.

        Returns
        -------
        Dict[str, Product]
            Topologically sorted Dict (self)
        """
        dep_graph = {name: set(product.dependencies) for name, product in self.items()}
        topo_sorted_product_lists = tsort.toposort(dep_graph)
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

    def flat_dependencies(self, product: Product, resolved: Optional[Set[Product]] = None) -> List[Product]:
        """Return and calculate the set of flat dependencies for this product.

        Parameters
        ----------
        product
            The product we are calculating the flat dependencies for.
        resolved
            A set which holds dependencies which we have have previously
            processed, and which we will add to as we process.

        Returns
        -------
            A list of the resolved set of dependencies.

        """
        if resolved is None:
            resolved = set()
        for dependency_name in product.dependencies:
            dependency_product = self[dependency_name]
            if dependency_product not in resolved:
                resolved.add(dependency_product)
                self.flat_dependencies(dependency_product, resolved)
        return list(resolved)


class Product:
    """Class representing an EUPS product to be built."""

    def __init__(
        self,
        name: str,
        sha1: str,
        version,
        dependencies: List[str],
        optional_dependencies: Optional[List[str]] = None,
        ref: Optional[Ref] = None,
    ):
        self.name = name
        self.sha1 = sha1
        self.version = version
        self.dependencies = dependencies
        self.optional_dependencies = optional_dependencies
        self.ref = ref


@dataclass
class Ref:
    """A Ref object includes information about a checked-out git ref.

    This includes information about the type of reference (branch or tag)
    and the unabbreviated object name.
    """

    sha: str  # sha of the git object
    name: str  # branch or tag name
    ref_type: str  # branch, tag, or head (if unknown)

    HEAD_PREFIX = "refs/heads/"
    TAG_PREFIX = "refs/tags/"

    @staticmethod
    def from_commit_and_ref(sha: str, ref: str) -> Ref:
        """Take in a commit sha and possibly an unabbreviated ref name.
        If the ref name is unabbreviated, we will use that to determine the
        ref type as well, and shorten it to the abbreviated form.

        Parameters
        ----------
        sha
            git commit sha
        ref
            name of a branch or tag - can be unabbreviated.

        Returns
        -------

        """
        ref_type = "head"
        if Ref.HEAD_PREFIX in ref:
            ref_type = "branch"
            ref = ref[len(Ref.HEAD_PREFIX) :]
        elif Ref.TAG_PREFIX in ref:
            ref_type = "tag"
            ref = ref[len(Ref.HEAD_PREFIX) :]
        return Ref(name=ref, sha=sha, ref_type=ref_type)


class RepoSpec:
    """Represents a git repo specification in repos.yaml. """

    def __init__(self, product: str, url: str, ref: str = "master", lfs: bool = False):
        self.product = product
        self.url = url
        self.ref = ref
        self.lfs = lfs

    def __str__(self):
        return self.url
