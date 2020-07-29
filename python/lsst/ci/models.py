from __future__ import annotations
from dataclasses import dataclass
from typing import List, Set, Optional, Dict

from lsst.ci import tsort

DEFAULT_BRANCH_NAME = "master"


class ProductIndex(dict):
    """Container class for a product index with index-related operations,
    such as topological sorting.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.toposorted = False
        self.sorted_groups = []

    def toposort(self) -> Dict[str, Product]:
        """Topologically sort the product index and return it.

        This mutates this object if it is not already sorted, and it returns
        itself for convenience.

        Returns
        -------
        Dict[str, Product]
            Topologically sorted Dict (self)
        """
        if self.toposorted:
            return self
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

    def flat_dependencies(self, product: Product, resolved: Optional[Set[str]] = None) -> List[Product]:
        """Return and calculate the set of flat dependencies for this product.

        Parameters
        ----------
        product
            The product we are calculating the flat dependencies for.
        resolved
            A set which holds dependency names which we have have previously
            processed, and which we will add to as we process.

        Returns
        -------
            A list of the resolved set of dependencies.

        """
        if resolved is None:
            resolved = set()
        for dependency_name in product.dependencies:
            dependency_product: Product = self[dependency_name]
            if dependency_product.name not in resolved:
                resolved.add(dependency_product.name)
                self.flat_dependencies(dependency_product, resolved)
        return list(self[resolved_name] for resolved_name in resolved)

    def __setitem__(self, key, value):
        # invalidate the toposort if an item is set
        self.toposorted = False
        self.sorted_groups = []
        super(ProductIndex, self).__setitem__(key, value)


@dataclass
class Product:
    """Class representing an EUPS product to be built.

    Parameters
    ----------
    name
        The name of the product
    sha1
        The sha1 of the product
    version
        The version of the product (from VersionDb)
    dependencies
        A list of declared dependencies
    optional_dependencies
        A list of optional declared dependencies
    ref
        Information about `Ref` checked out in `prepare`. This parameter is
        available during the `prepare` phase.
    """

    name: str
    sha1: str
    version: Optional[str]
    dependencies: List[str]
    optional_dependencies: Optional[List[str]] = None
    ref: Optional[Ref] = None


@dataclass
class Ref:
    """Represents a git reference, colloquially referred as a "ref" in git
    (e.g. `git show-ref`).

    See https://git-scm.com/book/en/v2/Git-Internals-Git-References

    Parameters
    ----------
    sha
        sha of the git object
    name
        branch or tag name
    ref_type
        type of ref - branch, tag, or head (if unknown)
    """

    sha: str
    name: str
    ref_type: str

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
            A new Ref object
        """
        ref_type = "head"
        if Ref.HEAD_PREFIX in ref:
            ref_type = "branch"
            ref = ref[len(Ref.HEAD_PREFIX) :]
        elif Ref.TAG_PREFIX in ref:
            ref_type = "tag"
            ref = ref[len(Ref.HEAD_PREFIX) :]
        return Ref(name=ref, sha=sha, ref_type=ref_type)


@dataclass
class RepoSpec:
    """Represents a git repo specification in repos.yaml.

    Parameters
    ----------
    product
        name of the product
    url
        git remote url for the repo
    ref
        default ref name for this branch. This is the fallback used when a
        user-specified ref name is not found for the represented repo.
    """

    product: str
    url: str
    ref: str = DEFAULT_BRANCH_NAME
    lfs: bool = False

    def __str__(self):
        return self.url
