from __future__ import annotations
import asyncio

import os
import os.path
import sys

from typing import Dict, Optional, List, Callable, Set, Tuple, Awaitable

import eups  # type: ignore
import eups.tags  # type: ignore
import hashlib
import shutil
import time
import re
import pipes
import subprocess
import abc
import yaml
import copy

from .eups_module import EupsModule, InstallException

from .git import Git, GitError
from . import models

import logging
logger = logging.getLogger("lsst.ci")

ASYNC_QUEUE_WORKERS = 8
USE_PREBUILT_VERSIONS = True
INSTALL_IGNORES_OPTIONAL_DEPENDENCIES = False
REWRITE_PRODUCTS_INDEX_ON_SKIPPED_OPTIONALS = False


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
        message = "Failed to clone product '%s' from any of the offered " \
            "repositories" % self.product

        for e in self.git_errors:
            message += "\n" + str(e)

        return message


class Manifest:
    """A representation of topologically ordered list of EUPS products to be built

    Parameters
    ----------
    product_index
        A topologically sorted `ProductIndex`
    build_id
        A unique identifier for this build
    """

    def __init__(self, product_index: models.ProductIndex, build_id: Optional[str] = None):
        self.build_id = build_id
        assert product_index.toposorted
        self.product_index = product_index

    def to_file(self, file_object):
        """ Serialize the manifest to a file object """
        print(u'# %-23s %-41s %-30s' % ("product", "SHA1", "Version"), file=file_object)
        print(u'BUILD=%s' % self.build_id, file=file_object)
        for prod in self.product_index.values():
            print(u'%-25s %-41s %-40s %s' % (prod.name, prod.sha1, prod.version,
                                             ','.join(dep for dep in prod.dependencies)),
                  file=file_object)

    def content_hash(self):
        """ Return a hash of the manifest, based on the products it contains. """
        m = hashlib.sha1()
        for prod in self.product_index.values():
            s = '%s\t%s\t%s\n' % (prod.name, prod.sha1, prod.version)
            m.update(s.encode("ascii"))

        return m.hexdigest()

    @staticmethod
    def from_file(file_object):
        varre = re.compile(r'^(\w+)=(.*)$')

        product_index = models.ProductIndex()
        build_id = None
        for line in file_object:
            line = line.strip()
            if not line:
                continue
            if line.startswith('#'):
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
                deps = [dep_name for dep_name in deps.split(',')]
            else:
                (name, sha1, version) = arr
                deps = []

            product_index[name] = models.Product(name, sha1, version, deps)
        product_index = product_index.toposort()
        return Manifest(product_index, build_id)


class ProductFetcher:
    """Fetch products from remote git repositories and checks out matching refs.

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
    def __init__(self,
                 build_dir: Optional[str],
                 repos: str,
                 repository_patterns: Optional[str] = None,
                 dependency_module: Optional[EupsModule] = None,
                 version_db: Optional[VersionDb] = None,
                 no_fetch: bool = False,
                 out=sys.stdout,
                 tries=1):
        self.build_dir = os.path.abspath(build_dir) if build_dir else None
        if repository_patterns:
            self.repository_patterns: Optional[List[str]] = repository_patterns.split('|')
        else:
            self.repository_patterns = None
        self.no_fetch = no_fetch
        if repos:
            if os.path.exists(repos):
                with open(repos, 'r', encoding='utf-8') as f:
                    self.repos = yaml.safe_load(f)
            else:
                raise Exception("YAML repos file '%s' does not exist" % repos)
        else:
            self.repos = None
        self.out = out
        self.tries = tries
        self.dependency_module = dependency_module
        self.version_db = version_db
        self.product_index = models.ProductIndex()
        self.lfs_product_names: List[str] = []
        self.installed_name_set: Set[str] = set()
        self.skipped_optionals: Set[str] = set()

        self.repo_specs: Dict[str, models.RepoSpec] = {}
        for product, spec in self.repos.items():
            if isinstance(spec, str):
                rs = models.RepoSpec(product, spec)
            elif isinstance(spec, dict):
                # the repos.yaml hash *must* not have keys that are not a
                # RepoSpec constructor args
                rs = models.RepoSpec(product, **spec)
            else:
                raise Exception('invalid repos.yaml repo specification'
                                ' -- please check the file with repos-lint')
            self.repo_specs[product] = rs

    def _origin_candidates(self, product):
        """Expand repository_patterns into URLs."""
        data = {'product': product}
        locations = []
        repo_spec = self.repo_specs[product]

        if repo_spec:
            locations.append(repo_spec.url)
        if self.repository_patterns:
            locations += [pat % data for pat in self.repository_patterns]
        return locations

    def ref_candidates(self, repo_spec: models.RepoSpec, refs: List[str]) -> List[str]:
        """Generate a list of refs to attempt to checkout."""

        # ref precedence should be:
        # user specified refs > repos.yaml default ref > implicit master
        refs = copy.copy(refs)

        if repo_spec.ref:
            refs.append(repo_spec.ref)

        # Add main branch to list of refs, if not there already
        if models.DEFAULT_BRANCH_NAME not in refs:
            refs.append(models.DEFAULT_BRANCH_NAME)

        return refs

    async def fetch(self, product: str, refs: List[str]) -> Tuple[models.Ref, List[str]]:
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
            A `Ref` object describing the checked out ref (e.g., 'master')
        dependencies
            list of declared dependencies for the product
        """

        # do not handle exceptions unless there will be multiple tries
        for i in range(self.tries - 1):
            try:
                return await self._fetch(product, refs)
            except (GitError, RemoteError, OSError) as e:
                print('<error>', file=self.out)
                print(e, file=self.out)
                # ensure retry is starting from a clean slate
                assert self.build_dir is not None
                productdir = os.path.join(self.build_dir, product)
                if os.path.exists(productdir):
                    shutil.rmtree(productdir)

            print("%20s: <retrying...>" % (product), file=self.out)
            self.out.flush()

            # try to not hammer git remotes with retry attempts
            time.sleep(3)

        # do not cleanup repo dir on the last "try" so it is available for
        # debugging + allow an exception to propagate from the final attempt
        return await self._fetch(product, refs)

    async def _fetch(self, product: str, refs: List[str]) -> Tuple[models.Ref, List[str]]:
        """This method should be considered private to fetch(d)"""
        repo_spec = self.repo_specs[product]
        t0 = time.time()
        print("Fetching %s..." % product, file=self.out)
        self.out.flush()

        assert self.build_dir is not None
        productdir = os.path.join(self.build_dir, product)
        git = Git(productdir)

        # lfs credential helper string
        helper = '!f() { cat > /dev/null; echo username=; echo password=; }; f'

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
            origin = await git('config', '--get', 'remote.origin.url')
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
                    if 'GIT_ASKPASS' in os.environ:
                        del os.environ['GIT_ASKPASS']
                    if 'SSH_ASKPASS' in os.environ:
                        del os.environ['SSH_ASKPASS']

                    # lfs will pickup the .gitconfig and pull lfs objects for
                    # the default ref during clone.  Config options set on the
                    # cli during the clone get recorded in `.git/config'
                    args += ['-c', 'filter.lfs.required=false']
                    # We DO NOT want smudge on, because we want to download
                    # only with `git lfs pull`. We do this so we can defer the
                    # download of the LFS files, in case it's not necessary.
                    args += ['-c', 'filter.lfs.smudge=']
                    args += ['-c', 'filter.lfs.clean=git-lfs clean %f']
                    args += ['-c', ('credential.helper=%s' % helper)]

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
            # in order to avoid broken HEAD in case the checkout branch is removed
            # we detach the HEAD of the local repository.
            await git.checkout("--detach")
            # the line below ensures with a single git command that the refspecs
            #     refs/heads/*
            #     refs/remotes/origin*
            #     refs/tags/*
            # are synchronized between local repository and remote repository
            # this ensures that branches deleted remotely, will also be deleted locally
            await git.fetch("-fp", "origin",
                            "+refs/heads/*:refs/heads/*",
                            "+refs/heads/*:refs/remotes/origin/*",
                            "refs/tags/*:refs/tags/*")

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
                # we know we want the checked out branch to be at the remote sha1
                # we'll just reset it
                await git.reset("--hard", sha1)

            assert(await git.rev_parse("HEAD") == sha1)
            break
        else:
            raise Exception("None of the specified refs exist in product '%s'" % product)

        # clean up the working directory (eg., remove remnants of
        # previous builds)
        await git.clean("-d", "-f", "-q", "-x")

        # Find out if we are in a branch or a tag (branches get precedence)
        show_ref_output = await git("show-ref", "--heads")
        heads = [i.split()[1] for i in show_ref_output.splitlines()]
        is_branch = ref in [head[len(models.Ref.HEAD_PREFIX):] for head in heads]
        target_ref = models.Ref(name=ref, sha1=sha1, ref_type="branch" if is_branch else "tag")

        finish_msg = f"{product} ok [{target_ref.name}] ({time.time() - t0:.1f} sec)."
        print(f"{finish_msg:>80}", file=self.out)
        self.out.flush()

        # Log this ref if it was in the external list
        if self.dependency_module:
            dep_file_path = os.path.join(
                self.build_dir,
                productdir,
                self.dependency_module.dependency_file(product)
            )
            dependency_names = self.dependency_module.dependencies(product, dep_file_path)
            optional_dependency_names = self.dependency_module.optional_dependencies(product, dep_file_path)
        else:
            dependency_names = []
            optional_dependency_names = []

        product_obj = models.Product(product, target_ref.sha1, None, dependency_names,
                                     optional_dependencies=optional_dependency_names, ref=target_ref)
        self.product_index[product] = product_obj
        return target_ref, dependency_names

    def validate_refs(self, refs: List[str]):
        """Validate that all external refs were found at least once.
        Raises RuntimeError if some have not been found."""
        matched_refs = {ref: 0 for ref in refs}
        for product in self.product_index.values():
            assert product.ref is not None
            if product.ref.name in matched_refs:
                matched_refs[product.ref.name] += 1

        missed = [ref for ref in matched_refs if matched_refs[ref] == 0]
        if missed:
            raise RuntimeError("Did not checkout any products with the following refs:"
                               " {}".format(",".join(missed)))

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
        for i in range(ASYNC_QUEUE_WORKERS):
            task = asyncio.create_task(worker_function(queue))
            tasks.append(task)

        await queue.join()

        for task in tasks:
            task.cancel()

        # Wait until all worker tasks are cancelled.
        await asyncio.gather(*tasks, return_exceptions=True)

    def do_fetch_products(self, products: List[str], refs: List[str]):
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
        if USE_PREBUILT_VERSIONS:
            loop.run_until_complete(self.install_prebuilt_dependencies())
        if not self.no_fetch and len(self.lfs_product_names):
            loop.run_until_complete(self.lfs_checkout())
        loop.close()

    async def fetch_products(self, product_names: List[str], refs: List[str]) -> Set[str]:
        resolved: Set[str] = set()
        queued: Set[str] = set()
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
                    product.version = await self.version_db.version(
                        product.name, repo_dir, product.ref.name, all_dependencies
                    )
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

    async def install_prebuilt_dependencies(self):
        """Install available prebuilt packages.

        This is performed after the git repos are fetched so we can process
        dependency files (e.g. eups Table files) more quickly, but before
        the lfs_checkout occurs, in case the lfs repo is an optional
        dependency and we can skip it's checkout.
        """
        print("Checking for prebuilt products...", file=self.out)
        install_t0 = time.time()

        exceptions = []
        assert self.product_index.toposorted
        # reverse so it's we pop with no params
        sorted_group_stack = self.product_index.sorted_groups.copy()
        sorted_group_stack.reverse()
        index_optionals = self.product_index.optionals()

        async def install_worker(queue: asyncio.Queue):
            while True:
                product_name = await queue.get()
                try:
                    product: models.Product = self.product_index[product_name]
                    # Find the uninstalled dependencies
                    missing_set = set(product.dependencies).difference(self.installed_name_set)
                    # if the feature is active and all uninstalled dependencies are optional
                    if INSTALL_IGNORES_OPTIONAL_DEPENDENCIES and missing_set.issubset(index_optionals):
                        logger.debug(f"{product_name} may be installable")
                    elif missing_set:
                        # We can't install this dependency - skip it
                        missing_names = [d for d in product.dependencies if d in missing_set]
                        logger.debug(f"Skipping {product_name} - missing dependencies: {missing_names}")
                        queue.task_done()
                        continue

                    print(f"Checking if {product_name} {product.version} is available...", file=self.out)
                    status = "ok"
                    t0 = time.time()
                    try:
                        await self.dependency_module.install_prebuilt(product)
                        self.installed_name_set.add(product_name)
                    except InstallException as e:
                        status = "not installed"
                        logger.debug(f"Prebuilt install for {product_name} not installed")
                        logger.debug(e)

                    finish_msg = f"{product_name} {status} ({time.time() - t0:.1f} sec)."
                    print(f"{finish_msg:>80}", file=self.out)
                    queue.task_done()

                except Exception as e:
                    logger.error(f"Failed prebuilt install for for {product_name}")
                    exceptions.append(e)
                    queue.task_done()
                    continue

        queue = asyncio.Queue()
        group = 0
        while len(sorted_group_stack):
            group_t0 = time.time()
            next_group = sorted_group_stack.pop()
            for product_name in next_group:
                queue.put_nowait(product_name)
            await self.run_async_tasks(install_worker, queue)
            logger.debug(f"group {group} finished in ({time.time() - group_t0:.1f} sec).")
            group += 1

        if len(exceptions):
            first_exception = exceptions[0]
            logger.error("At least one error occurred during while performing LFS pulls")
            raise first_exception

        logger.debug(f"Prebuilt installation finished in ({time.time() - install_t0:.1f} sec).")

    async def lfs_checkout(self):
        """Perform parallel LFS checkout on LFS repos.

        This is performed after the git repos are fetched so we can process
        dependency files (e.g. eups Table files) more quickly, and it also
        let's us perform git-lfs pulls in parallel, for better utilization
        on fast connections.
        """
        print("Performing git-lfs pulls...", file=self.out)
        exceptions = []
        index_optionals = self.product_index.optionals()

        async def pull_worker(queue):
            while True:
                lfs_product_name = await queue.get()
                try:
                    if INSTALL_IGNORES_OPTIONAL_DEPENDENCIES:
                        if lfs_product_name in index_optionals:
                            # might be skippable if dependants are installed
                            lfs_product = self.product_index[lfs_product_name]
                            dependants = set(d.name for d in self.product_index.dependants(lfs_product))
                            if dependants.issubset(self.installed_name_set):
                                print(f"Skipping {lfs_product_name} checkout - all dependants installed")
                                self.skipped_optionals.add(lfs_product_name)
                                queue.task_done()
                                continue
                    print(f"Pulling {lfs_product_name} LFS data...")
                    t0 = time.time()
                    assert self.build_dir is not None
                    repo_dir = os.path.join(self.build_dir, lfs_product_name)
                    git = Git(repo_dir)
                    # pull is equivalent to performing fetch and checkout
                    await git.lfs("pull")
                    # Reconfigure LFS smudge filter after LFS checkout
                    await git("config", "--local", "filter.lfs.smudge", "git-lfs smudge %f")
                    await git("config", "--local", "filter.lfs.required", "true")
                    finish_msg = f"{lfs_product_name} ok ({time.time() - t0:.1f} sec)."
                    print(f"{finish_msg:>80}", file=self.out)
                    queue.task_done()
                except Exception as e:
                    logger.error(f"Failed git-lfs pull for {lfs_product_name}")
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


def remove_products(product_index: models.ProductIndex, product_names: List[str]) -> models.ProductIndex:
    """Remove products from a product index.

    This mutates the underlying `Products`s in the index, as well as
    topologically sorting the index before returning it.

    Parameters
    ----------
    product_index
        product index we are to modify
    product_names
        products to remove from the index and the `Product`s in it

    Returns
    -------
    ProductIndex
        the modified index.
    """
    for product_name in product_names:
        lfs_product = product_index[product_name]
        del product_index[product_name]
        dependants: List[models.Product] = product_index.dependants(lfs_product)
        for dependant in dependants:
            dependant.dependencies.remove(product_name)
            if dependant.optional_dependencies:
                dependant.optional_dependencies.remove(product_name)
    # sort it to rebuild for the manifest
    return product_index.toposort()


class VersionDb(metaclass=abc.ABCMeta):
    """ Construct a full XXX+YYY version for a product.

        The subclasses of VersionDb determine how +YYY will be computed.
        The XXX part is computed by running EUPS' pkgautoversion.
    """

    @abc.abstractmethod
    def get_suffix(
            self,
            product_name: str,
            product_version: str,
            dependencies: List[models.Product]
    ) -> str:
        """Return a unique +YYY version suffix for a product given its dependencies

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
            self,
            product_name: str,
            productdir: str,
            ref: str,
            dependencies: List[models.Product]
    ) -> str:
        """Return a standardized XXX+YYY EUPS version, that includes the dependencies.

        Parameters
        ----------
        product_name
            name of the product to version
        productdir
            the directory with product source code
        ref
            the git ref that has been checked out into productdir (e.g., 'master')
        dependencies (list):
            A list of `Product`s that are the immediate dependencies of product_name

        Returns
        -------
        str
            the XXX+YYY version string.
        """
        q = pipes.quote
        cmd = "cd %s && pkgautoversion %s" % (q(productdir), q(ref))
        process = await asyncio.create_subprocess_shell(cmd, stdout=subprocess.PIPE)
        (stdout, stderr) = await process.communicate()
        product_version = stdout.decode().strip()
        if process.returncode != 0:
            print(f"{product_name} failed for {ref} in {productdir}")
        # add +XXXX suffix, if any
        suffix = ""
        if len(dependencies):
            suffix = self.get_suffix(product_name, product_version, dependencies)
        assert suffix.__class__ == str
        suffix = "+%s" % suffix if suffix else ""
        return "%s%s" % (product_version, suffix)


class VersionDbHash(VersionDb):
    """Subclass of `VersionDb` that generates +YYY suffixes by hashing the dependency names and versions"""

    def __init__(self, sha_abbrev_len: int, eups: eups.Eups):
        self.sha_abbrev_len = sha_abbrev_len
        self.eups = eups

    def hash_dependencies(self, dependencies: List[models.Product]) -> str:
        m = hashlib.sha1()
        for dep in sorted(dependencies, key=lambda d: d.name):
            s = '%s\t%s\n' % (dep.name, dep.sha1)
            m.update(s.encode("ascii"))
        return m.hexdigest()

    def get_suffix(
            self,
            product_name: str,
            product_version: str,
            dependencies: List[models.Product]
    ) -> str:
        """Return a hash of the sorted list of printed (dep_name, dep_version) tuples"""
        hash = self.hash_dependencies(dependencies)
        suffix = hash[:self.sha_abbrev_len]
        return suffix

    def __get_build_id(self):
        """Allocate the next unused EUPS tag that matches the bNNNN pattern"""

        tags = eups.tags.Tags()
        tags.loadFromEupsPath(self.eups.path)

        btre = re.compile('^b[0-9]+$')
        btags = [0]
        btags += [int(tag[1:]) for tag in tags.getTagNames() if btre.match(tag)]
        tag = "b%s" % (max(btags) + 1)

        return tag

    def commit(self, manifest, build_id):
        manifest.build_id = self.__get_build_id() if build_id is None else build_id


class VersionDbGit(VersionDbHash):
    """Subclass of `VersionDb` that generates +DEADBEEF suffixes based on git commits,
    but still tracks manifests in a repository.
    """

    def __init__(self, dbdir: str, sha_abbrev_len: int, eups_obj: eups.Eups):
        super(VersionDbGit, self).__init__(sha_abbrev_len, eups_obj)
        self.dbdir = dbdir
        self.eups = eups_obj

    def __shafn(self):
        return os.path.join("manifests", 'content_sha.db.txt')

    def __get_build_id(self, manifest_sha: str):
        """Return a build ID unique to this manifest. If a matching manifest already
           exists in the database, its build ID will be used.
        """
        with open(os.path.join(self.dbdir, 'manifests', 'content_sha.db.txt'), 'a+', encoding='utf-8') as fp:
            # Try to find a manifest with existing matching content
            for line in fp:
                (sha1, tag) = line.strip().split()
                if sha1 == manifest_sha:
                    return tag

            # Find the next unused tag that matches the bNNNN pattern
            # and isn't defined in EUPS yet
            git = Git(self.dbdir)
            tags = git.sync_tag('-l', 'b[0-9]*').split()
            btre = re.compile('^b[0-9]+$')
            btags = [0]
            btags += [int(t[1:]) for t in tags if btre.match(t)]
            btag = max(btags)

            defined_tags = self.eups.tags.getTagNames()
            while True:
                btag += 1
                tag = "b%s" % btag
                if tag not in defined_tags:
                    break

            return tag

    def commit(self, manifest: Manifest, build_id: str):
        git = Git(self.dbdir)

        manifest_sha = manifest.content_hash()
        manifest.build_id = self.__get_build_id(manifest_sha) if build_id is None else build_id

        # Store a copy of the manifest
        manfn = os.path.join('manifests', "%s.txt" % manifest.build_id)
        absmanfn = os.path.join(self.dbdir, manfn)
        with open(absmanfn, 'w', encoding='utf-8') as fp:
            manifest.to_file(fp)

        if git.sync_tag("-l", manifest.build_id) == manifest.build_id:
            # If the build_id/manifest are being reused, VersionDB repository must be clean
            if git.sync_describe('--always', '--dirty=-prljavonakraju').endswith("-prljavonakraju"):
                raise Exception("Trying to reuse the build_id, but the versionDB repository is dirty!")
        else:
            # add the manifest file
            git.sync_add(manfn)

            # add the new manifest<->build_id mapping
            shafn = self.__shafn()
            absshafn = os.path.join(self.dbdir, shafn)
            with open(absshafn, 'a+', encoding='utf-8') as fp:
                fp.write(u"%s\t%s\n" % (manifest_sha, manifest.build_id))
            git.sync_add(shafn)

            # git-commit
            msg = "Updates for build %s." % manifest.build_id
            git.sync_commit('-m', msg)

            # git-tag
            msg = "Build ID %s" % manifest.build_id
            git.sync_tag('-a', '-m', msg, manifest.build_id)


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
        """ Check if dependency 'dep' is excluded for product 'product' """
        try:
            rc = self._exclusion_regex_cache
        except AttributeError:
            rc = self._exclusion_regex_cache = dict()

        if product not in rc:
            rc[product] = [dep_re for (dep_re, prod_re) in self.exclusions if prod_re.match(product)]

        for dep_re in rc[product]:
            if dep_re.match(dep):
                return True

        return False

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
    clones them to a build directory thus preparing them to be built."""

    def __init__(self, build_dir, eups, product_fetcher, version_db, exclusion_resolver):
        self.build_dir = os.path.abspath(build_dir)

        self.eups = eups
        self.product_fetcher = product_fetcher
        self.version_db = version_db
        self.exclusion_resolver = exclusion_resolver

    def construct(self, product_names, refs):
        self.product_fetcher.do_fetch_products(product_names, refs)
        product_index = self.product_fetcher.product_index
        if INSTALL_IGNORES_OPTIONAL_DEPENDENCIES:
            print(f"Skipped install/checkout for {self.product_fetcher.skipped_optionals}")
        if REWRITE_PRODUCTS_INDEX_ON_SKIPPED_OPTIONALS:
            product_index = remove_products(product_index, self.product_fetcher.skipped_optionals)
        return Manifest(product_index, None)

    @staticmethod
    def run(args):
        #
        # Ensure build directory exists and is writable
        #
        build_dir = args.build_dir
        if not os.access(build_dir, os.W_OK):
            raise Exception("Directory '%s' does not exist or isn't writable." % build_dir)

        refs = args.ref

        #
        # Wire-up the BuildDirectoryConstructor constructor
        #
        eups_obj = eups.Eups()

        if args.exclusion_map:
            with open(args.exclusion_map, encoding='utf-8') as fp:
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
            tries=args.tries
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
        manifest_fn = os.path.join(build_dir, 'manifest.txt')
        with open(manifest_fn, 'w', encoding='utf-8') as fp:
            manifest.to_file(fp)
