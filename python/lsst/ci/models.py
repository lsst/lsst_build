from __future__ import annotations

import json
import os
import os.path
import sys
from dataclasses import dataclass

import pydantic
import requests
from pydantic import BaseModel

from . import tsort

DEFAULT_BRANCH_NAME = "main"
PR_FILE_NAME = "pr_info.json"


class ProductIndex(dict):
    """Container class for a product index with index-related operations,
    such as topological sorting. This class extends `dict` and has the same
    parameters.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.toposorted = False
        self.sorted_groups = []

    def toposort(self) -> ProductIndex:
        """Topologically sort the product index and return a copy.

        Returns
        -------
        ProductIndex
            Topologically sorted product index
        """
        dep_graph = {name: set(product.dependencies) for name, product in self.items()}
        topo_sorted_product_lists = tsort.toposort(dep_graph)
        new_index = ProductIndex()
        new_sorted_groups = []
        for sort_group in topo_sorted_product_lists:
            new_sorted_groups.append(sort_group)
            for product_name in sort_group:
                new_index[product_name] = self[product_name]
        new_index.toposorted = True
        new_index.sorted_groups = new_sorted_groups
        return new_index

    def flat_dependencies(self, product: Product, resolved: set[str] | None = None) -> list[Product]:
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
        list
            A list of the resolved set of dependencies.

        """
        if resolved is None:
            resolved = set()
        for dependency_name in product.dependencies:
            dependency_product: Product = self[dependency_name]
            if dependency_product.name not in resolved:
                resolved.add(dependency_product.name)
                self.flat_dependencies(dependency_product, resolved)
        return [self[resolved_name] for resolved_name in resolved]

    def __setitem__(self, key, value):
        # invalidate the toposort if an item is set
        self.toposorted = False
        self.sorted_groups = []
        super().__setitem__(key, value)


@dataclass
class Product:
    """Class representing an EUPS product to be built.

    Parameters
    ----------
    name
        The name of the product
    sha1
        the SHA-1 git commit hash of the product
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
    version: str | None
    dependencies: list[str]
    optional_dependencies: list[str] | None = None
    ref: Ref | None = None


@dataclass
class Ref:
    """Represents a git reference, colloquially referred as a "ref" in git
    (e.g. `git show-ref`).

    See https://git-scm.com/book/en/v2/Git-Internals-Git-References

    Parameters
    ----------
    sha1
        the SHA-1 git commit hash of the ref
    name
        branch or tag name
    ref_type
        type of ref - branch, tag, or head (if unknown)
    """

    sha1: str
    name: str
    ref_type: str

    HEAD_PREFIX = "refs/heads/"
    TAG_PREFIX = "refs/tags/"

    @staticmethod
    def from_commit_and_ref(sha1: str, ref: str) -> Ref:
        """Take in a commit sha and possibly an unabbreviated ref name.
        If the ref name is unabbreviated, we will use that to determine the
        ref type as well, and shorten it to the abbreviated form.

        Parameters
        ----------
        sha1
            the SHA-1 git commit hash of the ref
        ref
            name of a branch or tag - can be unabbreviated.

        Returns
        -------
        `Ref`
            A new Ref object
        """
        ref_type = "head"
        if Ref.HEAD_PREFIX in ref:
            ref_type = "branch"
            prefix_len = len(Ref.HEAD_PREFIX)
            ref = ref[prefix_len:]
        elif Ref.TAG_PREFIX in ref:
            ref_type = "tag"
            prefix_len = len(Ref.TAG_PREFIX)
            ref = ref[prefix_len:]
        return Ref(name=ref, sha1=sha1, ref_type=ref_type)


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


class GitHubPR(BaseModel):
    """A single pull request for a given product/repository.

    Attributes
    ----------
    product_name : `str`
        The name of the product associated with the PR.
    owner : `str`
        The owner or organization of the repository.
    repo : `str`
        The repository name.
    pr_number : `int`
        The pull request number.
    title : `str`
        The title of the pull request.
    head_ref : `str`
        The branch name that the pull request is based on.
    sha : `str`
        The commit SHA associated with the pull request.
    """

    product_name: str
    owner: str
    repo: str
    pr_number: int
    title: str
    head_ref: str
    sha: str

    def __str__(self) -> str:
        """One liner for logging / printing."""
        return (
            f"PR #{self.pr_number} "
            f"in {self.owner}/{self.repo} "
            f"for product {self.product_name}: {self.title}"
        )

    def post_github_status(self, *, state: str, description: str, agent: str) -> None:
        """Post a single GitHub status to `pr_info` commit.

        Parameters
        ----------
        state : `str`
            The state of the Github status.
        description : `str`
            Description of the current status.
        agent : `str`
            Current agent that is running.
        """
        token = os.getenv("GITHUB_TOKEN")
        if not token:
            print("GITHUB_TOKEN not found in environment variables.")
            return

        url = f"https://api.github.com/repos/{self.owner}/{self.repo}/statuses/{self.sha}"
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }

        build_url = os.getenv(
            "RUN_DISPLAY_URL",
            f"{os.getenv('JENKINS_URL', 'https://rubin-ci.slac.stanford.edu')}"
            "/blue/organizations/jenkins/stack-os-matrix/activity",
        )

        data = {
            "state": state,
            "description": description,
            "context": f"Jenkins Build ({agent})",
            "target_url": build_url,
        }

        print(f"Posting GitHub status to ({self.owner}/{self.repo}): {state} - {description}")

        response = requests.post(url, headers=headers, json=data, timeout=120)
        if response.status_code != 201:
            print(f"Failed to post GitHub status: {response.status_code} - {response.text}", file=sys.stderr)


class AllProductPRCollections(BaseModel):
    """All PR collections for every product.

    Attributes
    ----------
    items : `list` [ `GitHubPR` ]
        A list of PR collections, each representing a product
        and its associated PRs.
    """

    items: list[GitHubPR]

    def __iter__(self):
        """Iterate directly over the contained PRs."""
        return iter(self.items)

    def __len__(self):
        """Length of list."""
        return len(self.items)

    def __bool__(self):
        """Empty collections are False."""
        return bool(self.items)

    def print_summary(self) -> None:
        """Print a summary of all PR collections to console."""
        if not self:
            return

        print("**** PRs Summary ****")
        print(f"Found {len(self)} total matching PR(s) across all products:")
        for pr_col in self:
            print(pr_col)
        print("**** End of summary ****")

    def post_all_status(self, *, agent: str, state: str, description: str) -> None:
        """Iterate over pr_info and post the same status to every PR.

        Parameters
        ----------
        state : `str`
            The state of the Github status.
        description : `str`
            Description of the current status.
        agent : `str`
            Current agent that is running.
        """
        for pr_col in self.items:
            try:
                pr_col.post_github_status(
                    state=state,
                    description=description,
                    agent=agent,
                )

            except requests.Timeout as time_err:
                print(f"Timeout posting to {pr_col}: {time_err}", file=sys.stderr)
                print(f"Skipping {state} posts, but continuing the build.", file=sys.stderr)
                break
            except requests.RequestException as req_err:
                print(f"Network error posting to {pr_col}: {req_err}", file=sys.stderr)
                print(f"Skipping {state} posts, but continuing the build.", file=sys.stderr)
                break

    def save(self, build_dir: str) -> None:
        """Write AllProductPRCollections model to build_dir as pr_info.json.

        Parameters
        ----------
        build_dir : `str`
            Path to the directory where the JSON file will be written.

        Raises
        ------
        `ValueError`
            If `self.build_dir` does not exist or is not a directory.

        Warns
        -----
        Prints to `sys.stderr` if the file cannot be deleted or written.
        """
        # Ensure build_dir exists
        if not os.path.isdir(build_dir):
            raise ValueError(f"build_dir {build_dir} does not exist or is not a directory.")

        path = os.path.join(build_dir, PR_FILE_NAME)

        # Remove any cached pr file before start
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as os_err:
                print(f"WARNING: Failed to remove existing file {path}: {os_err}", file=sys.stderr)

        # Write to build_dir
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.model_dump_json(indent=2))
        except OSError as os_err:
            print(f"Failed to write to {path}: {os_err}", file=sys.stderr)

    @classmethod
    def from_build_dir(cls, build_dir: str) -> AllProductPRCollections:
        """Load the PR info from pr_info.json if it exists,
        otherwise return empty list.

        Parameters
        ----------
        build_dir : `str`
            Path to the directory containing the `pr_info.json` file.

        Returns
        -------
        `AllProductPRCollections`
            The Pydantic model. If `pr_info.json` does not exist or fails
            to parse or validate, returns an empty model
            (`AllProductPRCollections(items=[])`).
        Warns
        -----
        Prints to `sys.stderr` on read, decode, or validation failures.
        """
        path = os.path.join(build_dir, PR_FILE_NAME)
        if not os.path.exists(path):
            print(f"{path} does not exist", file=sys.stderr)
            return cls(items=[])

        try:
            with open(path, encoding="utf-8") as f:
                data = f.read()
        except OSError as os_err:
            print(f"WARNING: Could not read {path}: {os_err}", file=sys.stderr)
            return cls(items=[])

        try:
            return cls.model_validate_json(data)
        except json.JSONDecodeError as decode_err:
            print(f"Invalid JSON in {path}: {decode_err}", file=sys.stderr)
        except pydantic.ValidationError as val_err:
            print(
                f"WARNING: JSON file was valid JSON, but failed Pydantic validation: {val_err}",
                file=sys.stderr,
            )

        return cls(items=[])
