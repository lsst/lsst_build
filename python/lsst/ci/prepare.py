from __future__ import annotations

import abc
import asyncio
import copy
import hashlib
import logging
import os
import os.path
import re
import requests
import shutil
import sys
import time
from collections.abc import Awaitable, Callable

import eups  # type: ignore
import eups.tags  # type: ignore
import yaml

from . import models
from .eups_module import EupsModule
from .git import Git, GitError

logger = logging.getLogger("lsst.ci")

ASYNC_QUEUE_WORKERS = 8

#1: Determine if the build is running under Jenkins control.  If not, do nothing.
"""
def is_running_under_jenkins():
    label = os.getenv('NODE_LABELS')
    #Check if the script is running under Jenkins control.
    label = label.split(" ")
    if "arm64" in label:
        print("linux aarch64")
    if "mini" in label:
        print("apple arm")
    if "mac" in label:
        print("apple intel")
    if "docker" in label:
        print("linux x86")
    else:
        print("unable to find agent")
"""

def is_running_under_jenkins():
    label = os.getenv('NODE_LABELS')

    #Ask about util.groovy where I set null = "unknown"
    if not label:
        return "error"

    labels = label.split(" ")
    agent = ""
    
    for label in labels:
        match label:
            case "arm64":
                agent = "linux_aarch64"
            case "mini":
                agent = "apple_arm"
            case "osx-13" | "osx-12":  
                agent = "apple_intel"
            case "docker":
                agent = "linux_x86"
    if agent == "":
        return "error"
    return agent
    
print("starting to run is_running_under_jenkins()")
label = is_running_under_jenkins()
print(label)
if label == "error":
    pass #TODO: handle this error

help("modules")

"""
#2: For each GitHub repo that is being checked out in lsst_build/python/lsst/ci/prepare.py 
#(which is known in fetch()) with a non-default git ref, contact the GH API to list all the PRs for that repo.

#PyGithub

#Part of this from demo https://github.com/PyGithub/PyGithub/tree/main 

from github import Github
from github import Auth
# Using an access token - how can I securely put this in?
auth = Auth.Token("access_token")
g = Github(auth=auth)

# Have to find where this is defined below
repos = [list_of_non-default_repos] 

# Loop through each repository in the list
for repo_name in repos:
    # Get a repository 
    repo = g.get_repo(repo_name)
    
    # Get all open PRs for that repository
    open_pulls = repo.get_pulls(state='open')  
    
    # Print information about which repo (may remove this if we want list of PRs only) 
    print(f"Open pull requests for {repo_name}:")
    # Print PR information for that repo 
    for pr in open_pulls:
        print(f"  PR #{pr.number}: {pr.title} (User: {pr.user.login})")
    print()
"""

GITHUB_TOKEN = "github-api-token-sqreadmin"
GITHUB_API_URL = "https://api.github.com"
'''
def list_prs_for_repo(self): 
   # url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls"
    token = os.environ['GITHUB_TOKEN']
    # Use the token in your API calls
    print(f"Using GitHub token: {token}")
    
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

    # Original github url to repos
    url = self.repo_specs[product]

    # Remove .git suffix
    if url.endswith(".git"):
        url = url[:-4]

    # Isolate owner and repo 
    parts = url.split("/")
    owner = parts[3]
    repo = parts[4]
    print("marker3")
    print(owner)
    print(repo)

    api_url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls"
    print(api_url)
    
    response = requests.get(api_url, headers=headers)

    
    if response.status_code == 200:
        print(response) # Debugging PR list
        return response.json()  # Returns a list of PRs
    else:
        print(f"Failed to list PRs for {repo}: {response.content}")
        return None
'''

#3 Find the head-ref in the list of PRs that matches the non-default git ref.


class RemoteError(Exception):
    """Signal that a git repo failed to cloning from all possible remotes

    Parameters
    ----------
    product : `str`
        Name of product being cloned.
    git_errors: `list`
        List of `GitError` objects, one per attempted remote.
    """

    def __init__(self, product, git_errors):
        self.product = product
        self.git_errors = git_errors

    def __str__(self):
        message = f"Failed to clone product {self.product!r} from any of the offered repositories"

        for e in self.git_errors:
            message += "\n" + str(e)

        return message


class Manifest:
    """A representation of topologically ordered list of EUPS products to be
    built.

    Parameters
    ----------
    product_index
        A topologically sorted `ProductIndex`
    build_id
        A unique identifier for this build
    """

    def __init__(self, product_index: models.ProductIndex, build_id: str | None = None):
        self.build_id = build_id
        assert product_index.toposorted
        self.product_index = product_index

    def to_file(self, file_object):
        """Serialize the manifest to a file object"""
        print("# {:<23s} {:<41s} {:<30s}".format("product", "SHA1", "Version"), file=file_object)
        print(f"BUILD={self.build_id}", file=file_object)
        for prod in self.product_index.values():
            deps = ",".join(dep for dep in prod.dependencies)
            print(f"{prod.name:<25s} {prod.sha1:<41s} {prod.version:<40s} {deps}", file=file_object)

    def content_hash(self):
        """Return a hash of the manifest, based on the products it contains."""
        m = hashlib.sha1()
        for prod in self.product_index.values():
            s = f"{prod.name}\t{prod.sha1}\t{prod.version}\n"
            m.update(s.encode("ascii"))

        return m.hexdigest()

    @staticmethod
    def from_file(file_object):
        varre = re.compile(r"^(\w+)=(.*)$")

        product_index = models.ProductIndex()
        build_id = None
        for line in file_object:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                continue

            # Look for variable assignments
            m = varre.match(line)
            if m:
                var_name = m.group(1)
                var_value = m.group(2)
                if var_name == "BUILD":
                    build_id = var_value
                continue

            arr = line.split()
            if len(arr) == 4:
                (name, sha1, version, deps) = arr
                deps = deps.split(",")
            else:
                (name, sha1, version) = arr
                deps = []

            product_index[name] = models.Product(name, sha1, version, deps)
        product_index = product_index.toposort()
        return Manifest(product_index, build_id)


class ProductFetcher:
    """Fetch products from remote git repositories and checks out matching
    refs.

    See `fetch` for further documentation.

    Parameters
    ----------
    build_dir
        The root for product repos. Products are cloned in this directory.
    repos
        The path to the repos.yaml file
    repository_patterns
        A list of str.format() patterns used discover the URL of the remote
        git repository.
    dependency_module
        A module to help with eups-related operations.
    version_db
        VersionDb implementation
    no_fetch
        If true, don't fetch, just checkout the first matching ref.
    out
        FD which to send console output.
    tries
        The number of times to attempt to 'fetch' a product.
    """

    def __init__(
        self,
        build_dir: str | None,
        repos: str,
        repository_patterns: str | None = None,
        dependency_module: EupsModule | None = None,
        version_db: VersionDb | None = None,
        no_fetch: bool = False,
        out=sys.stdout,
        tries=1,
    ):
        self.build_dir = os.path.abspath(build_dir) if build_dir else None
        if repository_patterns:
            self.repository_patterns: list[str] | None = repository_patterns.split("|")
        else:
            self.repository_patterns = None
        self.no_fetch = no_fetch
        if repos:
            if os.path.exists(repos):
                with open(repos, encoding="utf-8") as f:
                    self.repos = yaml.safe_load(f)
            else:
                raise Exception(f"YAML repos file {repos!r} does not exist")
        else:
            self.repos = None
        self.out = out
        self.tries = tries
        self.dependency_module = dependency_module
        self.version_db = version_db
        self.product_index = models.ProductIndex()
        self.lfs_product_names: list[str] = []

        self.repo_specs: dict[str, models.RepoSpec] = {}
        for product, spec in self.repos.items():
            if isinstance(spec, str):
                rs = models.RepoSpec(product, spec)
            elif isinstance(spec, dict):
                # the repos.yaml hash *must* not have keys that are not a
                # RepoSpec constructor args
                rs = models.RepoSpec(product, **spec)
            else:
                raise Exception(
                    "invalid repos.yaml repo specification -- please check the file with repos-lint"
                )
            self.repo_specs[product] = rs

    def _origin_candidates(self, product):
        """Expand repository_patterns into URLs."""
        data = {"product": product}
        locations = []
        repo_spec = self.repo_specs[product]

        print("marker!")
        print(repo_spec)
        print(product)
        
        if repo_spec:
            locations.append(repo_spec.url)
        if self.repository_patterns:
            locations += [pat % data for pat in self.repository_patterns]
        return locations
    """
    def non_default_refs(self, repo_spec: models.RepoSpec, refs: list[str]) -> list[str]:
        Return a list of non-default refs to attempt to checkout.
        
        # Debugging statement
        print(f"Is this even being used??")
        
        # Copy initial refs
        refs = copy.copy(refs)
        print(refs)
        
        # Add the repository-specific ref if defined
        if repo_spec.ref:
            refs.append(repo_spec.ref)

        # Exclude the default branches from the list
        non_default_refs = [
            ref for ref in refs if ref not in (models.DEFAULT_BRANCH_NAME) #?? Where does DEFAULT... come from?
        ]
        print("marker2")
        print(self.repo_specs[product])

        return non_default_refs
    """
    def ref_candidates(self, repo_spec: models.RepoSpec, refs: list[str]) -> list[str]:
        """Generate a list of refs to attempt to checkout."""
        # ref precedence should be:
        # user specified refs > repos.yaml default ref > implicit main
        refs = copy.copy(refs)

        if repo_spec.ref:
            refs.append(repo_spec.ref)

        # Add main branch to list of refs, if not there already
        if models.DEFAULT_BRANCH_NAME not in refs:
            refs.append(models.DEFAULT_BRANCH_NAME)
            
        # Get non-default refs from non_default_refs method
        non_default_refs = self.non_default_refs(repo_spec, refs)

        print(f"Non-default refs {non_default_refs}")
        
        return refs

    async def fetch(self, product: str, refs: list[str]) -> tuple[models.Ref, list[str]]:
        """Clone the product repository and checkout the first matching ref.

        If `self.build_dir`/`product` does not exist, discover the product
        repository by attempting a git clone from the list of URLs
        constructed by running str.format() with { 'product': `product` }
        on self.repository_patterns. Otherwise, intelligently fetch
        any new commits.

        Following this, attempt to check out the refs listed in `refs`, along
        with the default branch for the repo, until the first one succeeds.

        Parameters
        ----------
        product
            the product to fetch
        refs
            the list of user-specified refs to check out

        Returns
        -------
        ref
            A `Ref` object describing the checked out ref (e.g., 'main')
        dependencies
            list of declared dependencies for the product
        """
        # do not handle exceptions unless there will be multiple tries
        for _ in range(self.tries - 1):
            try:
                return await self._fetch(product, refs)
            except (GitError, RemoteError, OSError) as e:
                print("<error>", file=self.out)
                print(e, file=self.out)
                # ensure retry is starting from a clean slate
                assert self.build_dir is not None
                productdir = os.path.join(self.build_dir, product)
                if os.path.exists(productdir):
                    shutil.rmtree(productdir)

            print(f"{product:>20s}: <retrying...>", file=self.out)
            self.out.flush()

            # try to not hammer git remotes with retry attempts
            await asyncio.sleep(3)

        # do not cleanup repo dir on the last "try" so it is available for
        # debugging + allow an exception to propagate from the final attempt
        return await self._fetch(product, refs)

    async def _fetch(self, product: str, refs: list[str]) -> tuple[models.Ref, list[str]]:
        """Retrieve target and dependencies.

        This method should be considered private to fetch(d)
        """
        repo_spec = self.repo_specs[product]
        t0 = time.time()
        print(f"Fetching {product}...", file=self.out)
        self.out.flush()

        assert self.build_dir is not None
        productdir = os.path.join(self.build_dir, product)
        git = Git(productdir)
        
        # lfs credential helper string
        helper = "!f() { cat > /dev/null; echo username=; echo password=; }; f"

        # determine if the repo is likely using lfs.
        # if the repos.yaml url is invalid, and a valid pattern generated
        # origin is found, this will cause lfs support to be enabled
        # for that repo (if it needs it or not).  This should not break non-lfs
        # repos.

        if repo_spec.lfs:
            lfs = True
            self.lfs_product_names.append(product)
        else:
            lfs = False

        # verify the URL of origin hasn't changed
        if os.path.isdir(productdir):
            origin = await git("config", "--get", "remote.origin.url")
            if origin not in self._origin_candidates(product):
                shutil.rmtree(productdir)

        # clone
        if not os.path.isdir(productdir):
            failed = []
            for url in self._origin_candidates(product):
                args = []
                if lfs:
                    # need to work around git-lfs always prompting
                    # for credentials, even when they are not required.

                    # these env vars shouldn't have to removed with the cache
                    # helper we are specifying but it doesn't hurt to be
                    # paranoid
                    if "GIT_ASKPASS" in os.environ:
                        del os.environ["GIT_ASKPASS"]
                    if "SSH_ASKPASS" in os.environ:
                        del os.environ["SSH_ASKPASS"]

                    # lfs will pickup the .gitconfig and pull lfs objects for
                    # the default ref during clone.  Config options set on the
                    # cli during the clone get recorded in `.git/config'
                    args += ["-c", "filter.lfs.required=false"]
                    # We DO NOT want smudge on, because we want to download
                    # only with `git lfs pull`. We do this so we can defer the
                    # download of the LFS files, in case it's not necessary.
                    args += ["-c", "filter.lfs.smudge="]
                    args += ["-c", "filter.lfs.clean=git-lfs clean %f"]
                    args += ["-c", f"credential.helper={helper}"]

                args += [url, productdir]

                try:
                    await Git.clone(*args, cwd=self.build_dir, return_status=False)
                except GitError as e:
                    failed.append(e)
                    continue
                else:
                    break
            else:
                raise RemoteError(product, failed)

        # update from origin
        if not self.no_fetch:
            # in order to avoid broken HEAD in case the checkout branch is
            # removed we detach the HEAD of the local repository.
            await git.checkout("--detach")
            # the line below ensures with a single git command that the
            # refspecs
            #     refs/heads/*
            #     refs/remotes/origin*
            #     refs/tags/*
            # are synchronized between local repository and remote repository
            # this ensures that branches deleted remotely, will also be deleted
            # locally
            await git.fetch(
                "-fp",
                "origin",
                "+refs/heads/*:refs/heads/*",
                "+refs/heads/*:refs/remotes/origin/*",
                "refs/tags/*:refs/tags/*",
            )

            # ensure default branch matches origin
            # (mostly for eups pkgautoversion)
            await git.remote("set-head", "origin", "-a")

        # find a ref that matches, checkout it
        for ref in self.ref_candidates(repo_spec, refs):
            sha1, _ = await git.rev_parse("-q", "--verify", "refs/remotes/origin/" + ref, return_status=True)

            branch = sha1 != ""
            if not sha1:
                sha1, _ = await git.rev_parse("-q", "--verify", "refs/tags/" + ref + "^0", return_status=True)
            if not sha1:
                sha1, _ = await git.rev_parse("-q", "--verify", "__dummy-g" + ref, return_status=True)
            if not sha1:
                continue

            await git.checkout("--force", ref)

            if branch:
                # profiling showed that git-pull took a lot of time; since
                # we know we want the checked out branch to be at the remote
                # sha1 we'll just reset it
                await git.reset("--hard", sha1)

            assert await git.rev_parse("HEAD") == sha1
            break
        else:
            raise Exception(f"None of the specified refs exist in product {product!r}")

        # clean up the working directory (eg., remove remnants of
        # previous builds)
        await git.clean("-d", "-f", "-q", "-x")

        # Find out if we are in a branch or a tag (branches get precedence)
        show_ref_output = await git("show-ref", "--heads")
        heads = [i.split()[1] for i in show_ref_output.splitlines()]
        is_branch = ref in [head[len(models.Ref.HEAD_PREFIX) :] for head in heads]
        target_ref = models.Ref(name=ref, sha1=sha1, ref_type="branch" if is_branch else "tag")

        finish_msg = f"{product} ok [{target_ref.name}] ({time.time() - t0:.1f} sec)."
        print(f"{finish_msg:>80}", file=self.out)
        self.out.flush()

        # Log this ref if it was in the external list
        if self.dependency_module:
            dep_file_path = os.path.join(
                self.build_dir, productdir, self.dependency_module.dependency_file(product)
            )
            dependency_names = self.dependency_module.dependencies(product, dep_file_path)
            optional_dependency_names = self.dependency_module.optional_dependencies(product, dep_file_path)
        else:
            dependency_names = []
            optional_dependency_names = []

        product_obj = models.Product(
            product,
            target_ref.sha1,
            None,
            dependency_names,
            optional_dependencies=optional_dependency_names,
            ref=target_ref,
        )
        self.product_index[product] = product_obj
        return target_ref, dependency_names

    def validate_refs(self, refs: list[str]):
        """Validate that all external refs were found at least once.
        Raises RuntimeError if some have not been found.
        """
        matched_refs = {ref: 0 for ref in refs}
        for product in self.product_index.values():
            assert product.ref is not None
            if product.ref.name in matched_refs:
                matched_refs[product.ref.name] += 1
                print("marker!!!")
                print(matched_refs)
        
        missed = [ref for ref in matched_refs if matched_refs[ref] == 0]
        if missed:
            raise RuntimeError(
                "Did not checkout any products with the following refs: {}".format(",".join(missed))
            )

    async def run_async_tasks(
        self, worker_function: Callable[[asyncio.Queue], Awaitable[None]], queue: asyncio.Queue
    ):
        """Instantiate async worker tasks and process a queue.

        Parameters
        ----------
        worker_function
            A worker function is returns a task, taking `queue` as a parameter
        queue
            The parameter for the worker function to be instantiated.
        """
        tasks = []
        for _ in range(ASYNC_QUEUE_WORKERS):
            task = asyncio.create_task(worker_function(queue))
            tasks.append(task)

        await queue.join()

        for task in tasks:
            task.cancel()

        # Wait until all worker tasks are cancelled.
        await asyncio.gather(*tasks, return_exceptions=True)

    def do_fetch_products(self, products: list[str], refs: list[str]):
        """Perform various product fetching tasks asynchronously while
        building the product index.

        Parameters
        ----------
        products
            List of top-level products we will checkout.
        refs
            List of refs the user specified.
        """
        loop = asyncio.get_event_loop()
        loop.run_until_complete(self.fetch_products(products, refs))

        # We have fetched everything - sort the index
        self.product_index = self.product_index.toposort()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Topologically sorted groups:")
            sort_groups = self.product_index.sorted_groups
            for idx, group in enumerate(sort_groups):
                for dependency in group:
                    deps = " ".join(self.product_index[dependency].dependencies)
                    logger.debug(f"{(' ' * idx + dependency):<46} -> {deps}")

        self.validate_refs(refs)
        if self.version_db:
            loop.run_until_complete(self.resolve_versions())
        if not self.no_fetch and len(self.lfs_product_names):
            loop.run_until_complete(self.lfs_checkout())
        loop.close()

    async def fetch_products(self, product_names: list[str], refs: list[str]) -> set[str]:
        resolved: set[str] = set()
        queued: set[str] = set()
        print("Fetching Products...", file=self.out)
        exceptions = []

        async def fetch_worker(queue):
            while True:
                # Get a "work item" out of the queue.
                (product_name, refs) = await queue.get()
                if product_name in resolved:
                    queue.task_done()
                    continue
                try:
                    ref, dependencies = await self.fetch(product_name, refs)
                    for dependency_name in dependencies:
                        if dependency_name not in resolved and dependency_name not in queued:
                            queue.put_nowait((dependency_name, refs))
                            queued.add(dependency_name)
                    resolved.add(product_name)
                    if product_name in queued:
                        queued.remove(product_name)
                    queue.task_done()
                except Exception as e:
                    print(f"Fetch failed for {product_name}")
                    exceptions.append(e)
                    # Remove from queued and add to resolved
                    # so it's never considered again.
                    resolved.add(product_name)
                    if product_name in queued:
                        queued.remove(product_name)
                    queue.task_done()
                    continue

        queue = asyncio.Queue()  # type: ignore

        for product_name in product_names:
            queued.add(product_name)
            queue.put_nowait((product_name, refs))

        await self.run_async_tasks(fetch_worker, queue)

        if len(exceptions):
            first_exception = exceptions[0]
            logger.error("At least one error occurred during while fetching products")
            raise first_exception
        return resolved

    async def resolve_versions(self):
        """Resolve product versions.

        We do not typically know the git shas of a product's dependencies at
        fetch time, so we wait until we've built the product index to
        resolve versions.
        """
        logger.debug("Resolving product versions")
        exceptions = []

        async def version_worker(queue):
            while True:
                product = await queue.get()
                assert product.ref is not None
                try:
                    assert self.build_dir is not None
                    assert self.version_db is not None
                    repo_dir = os.path.join(self.build_dir, product.name)
                    all_dependencies = self.product_index.flat_dependencies(product)
                    product.version = await self.version_db.version(product, repo_dir, all_dependencies)
                    queue.task_done()
                except Exception as e:
                    print(f"Version resolution failed for {product.name}")
                    exceptions.append(e)
                    queue.task_done()
                    continue

        queue = asyncio.Queue()
        for product in self.product_index.values():
            queue.put_nowait(product)

        await self.run_async_tasks(version_worker, queue)

        if len(exceptions):
            first_exception = exceptions[0]
            logger.error("At least one error occurred during while resolving versions")
            raise first_exception

    async def lfs_checkout(self):
        """Perform parallel LFS checkout on LFS repos.

        This is performed after the git repos are fetched so we can process
        dependency files (e.g. eups Table files) more quickly, and it also
        lets us perform git-lfs pulls in parallel, for better utilization
        on fast connections.
        """
        print("Performing git-lfs pulls...", file=self.out)
        exceptions = []

        async def pull_worker(queue):
            while True:
                lfs_product_name = await queue.get()
                try:
                    print(f"Pulling {lfs_product_name} LFS data...")
                    t0 = time.time()
                    assert self.build_dir is not None
                    repo_dir = os.path.join(self.build_dir, lfs_product_name)
                    git = Git(repo_dir)
                    # pull is equivalent to performing fetch and checkout
                    n_tries = 0
                    success = False
                    while not success:
                        try:
                            await git.lfs("pull")
                            success = True
                        except Exception as e:
                            n_tries += 1
                            if n_tries >= self.tries:
                                raise
                            logger.warning("Failed git-lfs pull for %s, retrying: %s", lfs_product_name, e)
                            await asyncio.sleep(3)

                    # Reconfigure LFS smudge filter after LFS checkout
                    await git("config", "--local", "filter.lfs.smudge", "git-lfs smudge %f")
                    await git("config", "--local", "filter.lfs.required", "true")
                    finish_msg = f"{lfs_product_name} ok ({time.time() - t0:.1f} sec)."
                    print(f"{finish_msg:>80}", file=self.out)
                    queue.task_done()
                except Exception as e:
                    logger.error("Failed git-lfs pull for %s", lfs_product_name)
                    exceptions.append(e)
                    queue.task_done()
                    continue

        queue = asyncio.Queue()
        for lfs_product_name in self.lfs_product_names:
            queue.put_nowait(lfs_product_name)
        await self.run_async_tasks(pull_worker, queue)

        if len(exceptions):
            first_exception = exceptions[0]
            logger.error("At least one error occurred during while performing LFS pulls")
            raise first_exception
        
    def list_non_default_refs_prs(self):
        """List all PRs for non-default git refs"""

        for product_name, product in self.product_index.items():
            assert product.ref is not None
            if product.ref.name != models.DEFAULT_BRANCH_NAME:
                # Get the repository URL
                repo_dir = os.path.join(self.build_dir, product_name)
                git = Git(repo_dir)
                origin_url = git("config", "--get", "remote.origin.url")

                # Parse the GitHub repository owner and name from the origin URL
                repo_info = self._extract_github_repo_info(origin_url)
                if repo_info:
                    owner, repo = repo_info
                    prs = self._get_github_prs(owner, repo)
                    print(f"Pull requests for {owner}/{repo}:")
                    for pr in prs:
                        print(f"#{pr['number']}: {pr['title']}")
                else:
                    print(f"Could not parse GitHub repo info from URL: {origin_url}")

    def _extract_github_repo_info(self, url):
        """Retrieve repo owner and name from URL"""
        
        # Remove the '.git' suffix 
        if url.endswith('.git'):
            url = url[:-4]
        
        #  Handle URLs like 'git@github.com:owner/repo' or 'https://github.com/owner/repo'
        if url.startswith('git@github.com:'):
            path = url[len('git@github.com:'):]
        elif url.startswith('https://github.com/'):
            path = url[len('https://github.com/'):]
        else:
            return None

        parts = path.split('/')
        if len(parts) == 2:
            owner, repo = parts
            return owner, repo
        else:
            return None

    def _get_github_prs(self, owner, repo):
        """Get the list of PRs using the GitHub API."""
    
        # Get token set in util.jenkinsWrapper
        token = os.environ['GITHUB_TOKEN']
    
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
        headers = {
            'Accept': 'application/vnd.github.v3+json',
            'Authorization': f"token {token}"  
        }

        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            prs = response.json()
            print("printing prs!")
            print(prs)
            return prs
        else:
            print(f"Failed to get PRs for {owner}/{repo}: {response.status_code}")
            return []



class VersionDb(metaclass=abc.ABCMeta):
    """Construct a full XXX+YYY version for a product.

    The subclasses of VersionDb determine how +YYY will be computed.
    The XXX part is computed by running EUPS' pkgautoversion.
    """

    @abc.abstractmethod
    def get_suffix(self, product_name: str, product_version: str, dependencies: list[models.Product]) -> str:
        """Return a unique +YYY version suffix for a product given its
        dependencies.

        Parameters
        ----------
        product_name
            name of the product
        product_version
            primary version of the product
        dependencies
            dependency `Product`s used for calculating suffix of product_name

        Returns
        -------
        suffix
            the +YYY suffix (w/o the + sign).
        """
        pass

    @abc.abstractmethod
    def commit(self, manifest: Manifest, build_id: str):
        """Commit the changes to the version database.

        A subclass must override this method to commit to
        permanent storage any changes to the underlying database
        caused by get_suffix() invocations, and to assign the
        build_id to manifest.build_id.

        Parameters
        ----------
        manifest
            a manifest of products from this run
        build_id
            the build identifier
        """
        pass

    async def version(
        self, product: models.Product, productdir: str, dependencies: list[models.Product]
    ) -> str:
        """Return a standardized XXX+YYY EUPS version, that includes the
        dependencies.

        Parameters
        ----------
        product
            the product to version.
        productdir
            the directory with product source code.
        dependencies (list):
            A list of `Product`s that are the immediate dependencies of
            product_name.

        Returns
        -------
        str
            the XXX+YYY version string.
        """
        product_version = "g" + product.sha1[: self.sha_abbrev_len]
        # add +XXXX suffix, if any
        suffix = ""
        if len(dependencies):
            suffix = self.get_suffix(product.name, product_version, dependencies)
        assert suffix.__class__ is str
        suffix = f"+{suffix}" if suffix else ""
        return f"{product_version}{suffix}"


class VersionDbHash(VersionDb):
    """Subclass of `VersionDb` that generates +YYY suffixes by hashing the
    dependency names and versions.
    """

    def __init__(self, sha_abbrev_len: int, eups: eups.Eups):
        self.sha_abbrev_len = sha_abbrev_len
        self.eups = eups

    def hash_dependencies(self, dependencies: list[models.Product]) -> str:
        m = hashlib.sha1()
        for dep in sorted(dependencies, key=lambda d: d.name):
            s = f"{dep.name}\t{dep.sha1}\n"
            m.update(s.encode("ascii"))
        return m.hexdigest()

    def get_suffix(self, product_name: str, product_version: str, dependencies: list[models.Product]) -> str:
        """Return a hash of the sorted list of printed (dep_name, dep_version)
        tuples.
        """
        hash = self.hash_dependencies(dependencies)
        suffix = hash[: self.sha_abbrev_len]
        return suffix

    def __get_build_id(self):
        """Allocate the next unused EUPS tag that matches the bNNNN pattern"""
        tags = eups.tags.Tags()
        tags.loadFromEupsPath(self.eups.path)

        btre = re.compile("^b[0-9]+$")
        btags = [0]
        btags += [int(tag[1:]) for tag in tags.getTagNames() if btre.match(tag)]
        tag = f"b{max(btags) + 1}"

        return tag

    def commit(self, manifest, build_id):
        manifest.build_id = self.__get_build_id() if build_id is None else build_id


class VersionDbGit(VersionDbHash):
    """Subclass of `VersionDb` that generates +DEADBEEF suffixes based on git
    commits, but still tracks manifests in a repository.
    """

    def __init__(self, dbdir: str, sha_abbrev_len: int, eups_obj: eups.Eups):
        super().__init__(sha_abbrev_len, eups_obj)
        self.dbdir = dbdir
        self.eups = eups_obj

    def __shafn(self):
        return os.path.join("manifests", "content_sha.db.txt")

    def __get_build_id(self, manifest_sha: str):
        """Return a build ID unique to this manifest. If a matching manifest
        already exists in the database, its build ID will be used.
        """
        with open(os.path.join(self.dbdir, "manifests", "content_sha.db.txt"), "a+", encoding="utf-8") as fp:
            # Try to find a manifest with existing matching content
            for line in fp:
                (sha1, tag) = line.strip().split()
                if sha1 == manifest_sha:
                    return tag

            # Find the next unused tag that matches the bNNNN pattern
            # and isn't defined in EUPS yet
            git = Git(self.dbdir)
            tags = git.sync_tag("-l", "b[0-9]*").split()
            btre = re.compile("^b[0-9]+$")
            btags = [0]
            btags += [int(t[1:]) for t in tags if btre.match(t)]
            btag = max(btags)

            defined_tags = self.eups.tags.getTagNames()
            while True:
                btag += 1
                tag = f"b{btag}"
                if tag not in defined_tags:
                    break

            return tag

    def commit(self, manifest: Manifest, build_id: str):
        git = Git(self.dbdir)

        manifest_sha = manifest.content_hash()
        manifest.build_id = self.__get_build_id(manifest_sha) if build_id is None else build_id

        # Store a copy of the manifest
        manfn = os.path.join("manifests", f"{manifest.build_id}.txt")
        absmanfn = os.path.join(self.dbdir, manfn)
        with open(absmanfn, "w", encoding="utf-8") as fp:
            manifest.to_file(fp)

        if git.sync_tag("-l", manifest.build_id) == manifest.build_id:
            # If the build_id/manifest are being reused, VersionDB repository
            # must be clean.
            if git.sync_describe("--always", "--dirty=-prljavonakraju").endswith("-prljavonakraju"):
                raise Exception("Trying to reuse the build_id, but the versionDB repository is dirty!")
        else:
            # add the manifest file
            git.sync_add(manfn)

            # add the new manifest<->build_id mapping
            shafn = self.__shafn()
            absshafn = os.path.join(self.dbdir, shafn)
            with open(absshafn, "a+", encoding="utf-8") as fp:
                fp.write(f"{manifest_sha}\t{manifest.build_id}\n")
            git.sync_add(shafn)

            # git-commit
            msg = f"Updates for build {manifest.build_id}."
            git.sync_commit("-m", msg)

            # git-tag
            msg = "Build ID {manifest.build_id}"
            git.sync_tag("-a", "-m", msg, manifest.build_id)


class ExclusionResolver:
    """A class to determine whether a dependency should be excluded from
    build for a product, based on matching against a list of regular
    expression rules.
    """

    def __init__(self, exclusion_patterns):
        self.exclusions = [
            (re.compile(dep_re), re.compile(prod_re)) for (dep_re, prod_re) in exclusion_patterns
        ]

    def is_excluded(self, dep, product):
        """Check if dependency 'dep' is excluded for product 'product'"""
        try:
            rc = self._exclusion_regex_cache
        except AttributeError:
            rc = self._exclusion_regex_cache = {}

        if product not in rc:
            rc[product] = [dep_re for (dep_re, prod_re) in self.exclusions if prod_re.match(product)]

        return any(dep_re.match(dep) for dep_re in rc[product])

    @staticmethod
    def from_file(file_object):
        exclusion_patterns = []

        for line in file_object:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            exclusion_patterns.append(line.split()[:2])

        return ExclusionResolver(exclusion_patterns)


class BuildDirectoryConstructor:
    """A class that, given one or more top level packages, recursively
    clones them to a build directory thus preparing them to be built.
    """

    def __init__(self, build_dir, eups, product_fetcher, version_db, exclusion_resolver):
        self.build_dir = os.path.abspath(build_dir)

        self.eups = eups
        self.product_fetcher = product_fetcher
        self.version_db = version_db
        self.exclusion_resolver = exclusion_resolver

    def construct(self, product_names, refs):
        self.product_fetcher.do_fetch_products(product_names, refs)
        return Manifest(self.product_fetcher.product_index, None)

    @staticmethod
    def run(args):
        #
        # Ensure build directory exists and is writable
        #
        build_dir = args.build_dir
        if not os.access(build_dir, os.W_OK):
            raise Exception(f"Directory {build_dir!r} does not exist or isn't writable.")

        refs = args.ref

        #
        # Wire-up the BuildDirectoryConstructor constructor
        #
        eups_obj = eups.Eups()

        if args.exclusion_map:
            with open(args.exclusion_map, encoding="utf-8") as fp:
                exclusion_resolver = ExclusionResolver.from_file(fp)
        else:
            exclusion_resolver = ExclusionResolver([])

        dependency_module = EupsModule(eups_obj, exclusion_resolver)
        if args.version_git_repo:
            version_db = VersionDbGit(args.version_git_repo, args.sha_abbrev_len, eups_obj)
        else:
            version_db = VersionDbHash(args.sha_abbrev_len, eups_obj)

        product_fetcher = ProductFetcher(
            build_dir,
            args.repos,
            dependency_module=dependency_module,
            version_db=version_db,
            repository_patterns=args.repository_pattern,
            no_fetch=args.no_fetch,
            tries=args.tries,
        )
        p = BuildDirectoryConstructor(build_dir, eups_obj, product_fetcher, version_db, exclusion_resolver)

        #
        # Run the construction
        #
        manifest = p.construct(args.products, refs)
        version_db.commit(manifest, args.build_id)

        #
        # Store the result in build_dir/manifest.txt
        #
        manifest_fn = os.path.join(build_dir, "manifest.txt")
        with open(manifest_fn, "w", encoding="utf-8") as fp:
            manifest.to_file(fp)
