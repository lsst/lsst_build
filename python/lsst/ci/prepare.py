#############################################################################
# Preparer

from builtins import object
from io import open

import os
import os.path
import sys
import eups
import eups.tags
import hashlib
import shutil
import time
import re
import pipes
import subprocess
import collections
import abc
import yaml
import copy

from . import tsort

from .git import Git, GitError


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


class Product(object):
    """Class representing an EUPS product to be built"""
    def __init__(self, name, sha1, version, dependencies):
        self.name = name
        self.sha1 = sha1
        self.version = version
        self.dependencies = dependencies

    def flat_dependencies(self):
        """Return a flat list of dependencies for the product.

            Returns:
                list of `Product`s.
        """
        res = set(self.dependencies)

        for dep in self.dependencies:
            res.update(dep.flat_dependencies())

        return res


class Manifest(object):
    """A representation of topologically ordered list of EUPS products to be built

       :ivar products: topologically sorted list of `Product`s
       :ivar build_id:  unique build identifier
    """

    def __init__(self, products_list, build_id=None):
        """Construct the manifest

        Args:
            products_list (OrderedDict): A topologically sorted dict of `Product`s
            build_id (str): A unique identifier for this build

        """
        self.build_id = build_id
        self.products = products_list

    def to_file(self, file_object):
        """ Serialize the manifest to a file object """
        print(u'# %-23s %-41s %-30s' % ("product", "SHA1", "Version"), file=file_object)
        print(u'BUILD=%s' % self.build_id, file=file_object)
        for prod in self.products.values():
            print(u'%-25s %-41s %-40s %s' % (prod.name, prod.sha1, prod.version,
                                             ','.join(dep.name for dep in prod.dependencies)),
                  file=file_object)

    def content_hash(self):
        """ Return a hash of the manifest, based on the products it contains. """
        m = hashlib.sha1()
        for prod in self.products.values():
            s = '%s\t%s\t%s\n' % (prod.name, prod.sha1, prod.version)
            m.update(s.encode("ascii"))

        return m.hexdigest()

    @staticmethod
    def from_file(file_object):
        varre = re.compile(r'^(\w+)=(.*)$')

        products = collections.OrderedDict()
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
                deps = [products[dep_name] for dep_name in deps.split(',')]
            else:
                (name, sha1, version) = arr
                deps = []

            products[name] = Product(name, sha1, version, deps)

        return Manifest(products, build_id)

    @staticmethod
    def from_product_dict(product_dict):
        """ Create a `Manifest` by topologically sorting the dict of `Product`s

        Args:
            product_dict (dict): A product_name -> `Product` dictionary of products

        Returns:
            The created `Manifest`.
        """
        deps = [(dep.name, prod.name) for prod in product_dict.values() for dep in prod.dependencies]
        topo_sorted_product_names = tsort.tsort(deps)

        # Append top-level products with no dependencies
        _p = set(topo_sorted_product_names)
        for name in set(product_dict.keys()):
            if name not in _p:
                topo_sorted_product_names.append(name)

        products = collections.OrderedDict()
        for name in topo_sorted_product_names:
            products[name] = product_dict[name]
        return Manifest(products, None)


class ProductFetcher(object):
    """ Fetches products from remote git repositories and checks out matching refs.

        See `fetch` for further documentation.

        :ivar build_dir: The product will be cloned to build_dir/product_name
        :ivar repository_patterns: A list of str.format() patterns used discover the URL of the remote git
              repository.
        :ivar refs: A list of refs to attempt to git-checkout
        :ivar no_fetch: If true, don't fetch, just checkout the first matching ref.
        :ivar out: FD which to send console output.
        :ivar tries: The number of times to attempt to 'fetch' a product.
    """
    def __init__(self,
                 build_dir,
                 repos,
                 repository_patterns,
                 refs,
                 no_fetch,
                 out=sys.stdout,
                 tries=1):

        self.build_dir = os.path.abspath(build_dir)
        self.refs = refs
        self.matched_refs = {ref: 0 for ref in refs}
        if repository_patterns:
            self.repository_patterns = repository_patterns.split('|')
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

    def _origin_candidates(self, product):
        """ Expand repository_patterns into URLs. """
        data = {'product': product}
        locations = []
        yaml = self._repos_yaml_lookup(product)

        if yaml:
            locations.append(yaml.url)
        if self.repository_patterns:
            locations += [pat % data for pat in self.repository_patterns]
        return locations

    def _ref_candidates(self, product):
        """ Generate a list of refs to attempt to checkout. """

        # ref precedence should be:
        # user specified refs > repos.yaml default ref > implicit master
        refs = copy.copy(self.refs)
        yaml = self._repos_yaml_lookup(product)

        if yaml.ref:
            refs.append(yaml.ref)

        # Add 'master' to list of refs, if not there already
        if 'master' not in refs:
            refs.append('master')

        return refs

    def _repos_yaml_lookup(self, product):
        """ Return repo specification [if present] from repos.yaml.
            The multiple possible formats in repos.yaml are normalized into a
            single consistent object.  No sanity checking is performed.
        """
        rs = None

        if self.repos and product in self.repos:
            spec = self.repos[product]
            if isinstance(spec, str):
                rs = RepoSpec(product, spec)
            elif isinstance(spec, dict):
                # the repos.yaml hash *must* not have keys that are not a
                # RepoSpec constructor args
                rs = RepoSpec(product, **spec)
            else:
                raise Exception('invalid repos.yaml repo specification'
                                ' -- please check the file with repos-lint')

        return rs

    def _origin_uses_lfs(self, product):
        """ Attempt to determine if this remote url needs git lfs support """
        yaml = self._repos_yaml_lookup(product)
        if yaml:
            # is this an lfs backed repo?
            if yaml.lfs:
                return True
        return False

    def fetch(self, product):
        """ Clone the product repository and checkout the first matching ref.

        Args:
            product (str): the product to fetch

        Returns:
            (ref, sha1) tuple where::

                 ref -- the checked out ref (e.g., 'master')
                 sha1 -- the corresponding commit's SHA1

        If $build_dir/$product does not exist, discovers the product
        repository by attempting a git clone from the list of URLs
        constructed by running str.format() with { 'product': product}
        on self.repository_patterns. Otherwise, intelligently fetches
        any new commits.

        Next, attempts to check out the refs listed in self.ref,
        until the first one succeeds.

        """

        # do not handle exceptions unless there will be multiple tries
        for i in range(self.tries - 1):
            try:
                return self._fetch(product)
            except (GitError, RemoteError, OSError) as e:
                print('<error>', file=self.out)
                print(e, file=self.out)
                # ensure retry is starting from a clean slate
                productdir = os.path.join(self.build_dir, product)
                if os.path.exists(productdir):
                    shutil.rmtree(productdir)

            print("%20s: <retrying...>" % (product), file=self.out)
            self.out.flush()

            # try to not hammer git remotes with retry attempts
            time.sleep(3)

        # do not cleanup repo dir on the last "try" so it is available for
        # debugging + allow an exception to propagate from the final attempt
        return self._fetch(product)

    def _fetch(self, product):
        """This method should be considered private to fetch()"""
        t0 = time.time()
        self.out.write("%20s: " % product)
        self.out.flush()

        productdir = os.path.join(self.build_dir, product)
        git = Git(productdir)

        # lfs credential helper string
        helper = '!f() { cat > /dev/null; echo username=; echo password=; }; f'

        # determine if the repo is likely using lfs.
        # if the repos.yaml url is invalid, and a valid pattern generated
        # origin is found, this will cause lfs support to be enabled
        # for that repo (if it needs it or not).  This should not break non-lfs
        # repos.
        lfs = self._origin_uses_lfs(product)

        # verify the URL of origin hasn't changed
        if os.path.isdir(productdir):
            origin = git('config', '--get', 'remote.origin.url')
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
                    args += ['-c', 'filter.lfs.required']
                    args += ['-c', 'filter.lfs.smudge=git-lfs smudge %f']
                    args += ['-c', 'filter.lfs.clean=git-lfs clean %f']
                    args += ['-c', ('credential.helper=%s' % helper)]

                args += [url, productdir]

                try:
                    Git.clone(*args, return_status=False)
                except GitError as e:
                    failed.append(e)
                    continue
                else:
                    break
            else:
                raise RemoteError(product, failed)

        # update from origin
        if not self.no_fetch:
            # the line below should be equivalent to:
            #     git.fetch("origin", "--force", "--prune")
            #     git.fetch("origin", "--force", "--tags")
            # but avoids the overhead of two (possibly remote) git calls.
            git.fetch("-fup", "origin", "+refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*")

        # find a ref that matches, checkout it
        for ref in self._ref_candidates(product):
            sha1, _ = git.rev_parse("-q", "--verify", "refs/remotes/origin/" + ref, return_status=True)

            branch = sha1 != ""
            if not sha1:
                sha1, _ = git.rev_parse("-q", "--verify", "refs/tags/" + ref + "^0", return_status=True)
            if not sha1:
                sha1, _ = git.rev_parse("-q", "--verify", "__dummy-g" + ref, return_status=True)
            if not sha1:
                continue

            git.checkout("--force", ref)

            if branch:
                # profiling showed that git-pull took a lot of time; since
                # we know we want the checked out branch to be at the remote sha1
                # we'll just reset it
                git.reset("--hard", sha1)

            assert(git.rev_parse("HEAD") == sha1)
            break
        else:
            raise Exception("None of the specified refs exist in product '%s'" % product)

        # clean up the working directory (eg., remove remnants of
        # previous builds)
        git.clean("-d", "-f", "-q", "-x")

        print(" ok (%.1f sec)." % (time.time() - t0), file=self.out)
        self.out.flush()

        # Log this ref if it was in the external list
        if ref in self.matched_refs:
            self.matched_refs[ref] += 1

        return ref, sha1

    def validate_refs(self):
        """Validate that all external refs were found at least once.
        Raises RuntimeError if some have not been found."""
        missed = [ref for ref in self.matched_refs if self.matched_refs[ref] == 0]
        if missed:
            raise RuntimeError("Did not checkout any products with the following refs:"
                               " {}".format(",".join(missed)))


class VersionDb(metaclass=abc.ABCMeta):
    """ Construct a full XXX+YYY version for a product.

        The subclasses of VersionDb determine how +YYY will be computed.
        The XXX part is computed by running EUPS' pkgautoversion.
    """

    @abc.abstractmethod
    def get_suffix(self, product_name, product_version, dependencies):
        """Return a unique +YYY version suffix for a product given its dependencies

            Args:
                product_name (str): name of the product
                product_version (str): primary version of the product
                dependencies (list): A list of `Product`s that are the immediate dependencies of product_name

            Returns:
                str. the +YYY suffix (w/o the + sign).
        """
        pass

    @abc.abstractmethod
    def commit(self, manifest, build_id):
        """Commit the changes to the version database

           Args:
               manifest (`Manifest`): a manifest of products from this run
               build_id (str): the build identifier

           A subclass must override this method to commit to
           permanent storage any changes to the underlying database
           caused by get_suffix() invocations, and to assign the
           build_id to manifest.build_id.
        """
        pass

    def version(self, product_name, productdir, ref, dependencies):
        """ Return a standardized XXX+YYY EUPS version, that includes the dependencies.

            Args:
                product_name (str): name of the product to version
                productdir (str): the directory with product source code
                ref (str): the git ref that has been checked out into productdir (e.g., 'master')
                dependencies (list): A list of `Product`s that are the immediate dependencies of product_name

            Returns:
                str. the XXX+YYY version string.
        """
        q = pipes.quote
        cmd = "cd %s && pkgautoversion %s" % (q(productdir), q(ref))
        product_version = subprocess.check_output(cmd, shell=True).decode("utf-8").strip()

        # add +XXXX suffix, if any
        suffix = self.get_suffix(product_name, product_version, dependencies)
        assert suffix.__class__ == str
        suffix = "+%s" % suffix if suffix else ""
        return "%s%s" % (product_version, suffix)


class VersionDbHash(VersionDb):
    """Subclass of `VersionDb` that generates +YYY suffixes by hashing the dependency names and versions"""

    def __init__(self, sha_abbrev_len, eups):
        self.sha_abbrev_len = sha_abbrev_len
        self.eups = eups

    def _hash_dependencies(self, dependencies):
        def namekey(d):
            return d.name
        m = hashlib.sha1()
        for dep in sorted(dependencies, key=namekey):
            s = '%s\t%s\n' % (dep.name, dep.version)
            m.update(s.encode("ascii"))

        return m.hexdigest()

    def get_suffix(self, product_name, product_version, dependencies):
        """ Return a hash of the sorted list of printed (dep_name, dep_version) tuples """
        hash = self._hash_dependencies(dependencies)
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
    """Subclass of `VersionDb` that generates +YYY suffixes by assigning a unique +N integer to
       each set of dependencies, and tracking the assignments in a git repository.
    """

    class VersionMap(object):
        def __init__(self):
            self.verhash2suffix = dict()  # (version, dep_sha) -> suffix
            self.versuffix2hash = dict()  # (version, suffix) -> depsha

            self.added_entries = dict()	 # (version, suffix) -> [ (dep_name, dep_version) ]

            self.dirty = False

        def __just_add(self, version, hash, suffix):
            assert isinstance(suffix, int)

            self.verhash2suffix[(version, hash)] = suffix
            self.versuffix2hash[(version, suffix)] = hash

        def __add(self, version, hash, suffix, dependencies):
            self.__just_add(version, hash, suffix)

            # Record additions to know what needs to be appended
            self.added_entries[(version, suffix)] = [(product.name, product.version)
                                                     for product in dependencies]

            self.dirty = True

        def suffix(self, version, hash):
            return self.verhash2suffix[(version, hash)]

        def hash(self, version, suffix):
            return self.versuffix2hash[(version, suffix)]

        def new_suffix(self, version, hash, dependencies):
            suffix = 0
            try:
                suffix = max(_suffix for _version, _suffix in self.versuffix2hash if _version == version) + 1
            except ValueError:
                suffix = 0
            self.__add(version, hash, suffix, dependencies)
            return suffix

        def append_additions_to_file(self, file_object_ver, file_object_dep):
            # write (version, hash)<->suffix and dependency table updates
            for (version, suffix), dependencies in self.added_entries.items():
                file_object_ver.write("%s\t%s\t%d\n" % (version, self.hash(version, suffix), suffix))
                for dep_name, dep_version in dependencies:
                    file_object_dep.write("%s\t%d\t%s\t%s\n" % (version, suffix, dep_name, dep_version))

            self.added_entries = []
            self.dirty = False

        @staticmethod
        def from_file(file_object):
            vm = VersionDbGit.VersionMap()
            for line in iter(file_object.readline, ''):
                (version, hash, suffix) = line.strip().split()[:3]
                vm.__just_add(version, hash, int(suffix))

            return vm

    def __init__(self, dbdir, eups_obj):
        super(VersionDbGit, self).__init__(None, None)
        self.dbdir = dbdir
        self.eups = eups_obj

        self.version_maps = dict()

    def __verfn(self, product_name):
        return os.path.join("ver_db", product_name + '.txt')

    def __depfn(self, product_name):
        return os.path.join("dep_db", product_name + '.txt')

    def __shafn(self):
        return os.path.join("manifests", 'content_sha.db.txt')

    def get_suffix(self, product_name, product_version, dependencies):
        hash = self._hash_dependencies(dependencies)

        # Lazy-load/create
        try:
            vm = self.version_maps[product_name]
        except KeyError:
            absverfn = os.path.join(self.dbdir, self.__verfn(product_name))
            try:
                vm = VersionDbGit.VersionMap.from_file(open(absverfn, encoding='utf-8'))
            except IOError:
                vm = VersionDbGit.VersionMap()
            self.version_maps[product_name] = vm

        # get or create a new suffix
        try:
            suffix = vm.suffix(product_version, hash)
        except KeyError:
            suffix = vm.new_suffix(product_version, hash, dependencies)

        assert isinstance(suffix, int)
        if suffix == 0:
            suffix = ""

        return str(suffix)

    def __get_build_id(self, manifest, manifest_sha):
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
                tags = git.tag('-l', 'b[0-9]*').split()
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

    def commit(self, manifest, build_id):
        git = Git(self.dbdir)

        manifest_sha = manifest.content_hash()
        manifest.build_id = self.__get_build_id(manifest, manifest_sha) if build_id is None else build_id

        # Write files
        for (product_name, vm) in self.version_maps.items():
            if not vm.dirty:
                continue

            verfn = self.__verfn(product_name)
            depfn = self.__depfn(product_name)
            absverfn = os.path.join(self.dbdir, verfn)
            absdepfn = os.path.join(self.dbdir, depfn)

            with open(absverfn, 'a', encoding='utf-8') as fp_ver:
                with open(absdepfn, 'a', encoding='utf-8') as fp_dep:
                    vm.append_additions_to_file(fp_ver, fp_dep)

            git.add(verfn, depfn)

        # Store a copy of the manifest
        manfn = os.path.join('manifests', "%s.txt" % manifest.build_id)
        absmanfn = os.path.join(self.dbdir, manfn)
        with open(absmanfn, 'w', encoding='utf-8') as fp:
            manifest.to_file(fp)

        if git.tag("-l", manifest.build_id) == manifest.build_id:
            # If the build_id/manifest are being reused, VersionDB repository must be clean
            if git.describe('--always', '--dirty=-prljavonakraju').endswith("-prljavonakraju"):
                raise Exception("Trying to reuse the build_id, but the versionDB repository is dirty!")
        else:
            # add the manifest file
            git.add(manfn)

            # add the new manifest<->build_id mapping
            shafn = self.__shafn()
            absshafn = os.path.join(self.dbdir, shafn)
            with open(absshafn, 'a+', encoding='utf-8') as fp:
                fp.write(u"%s\t%s\n" % (manifest_sha, manifest.build_id))
            git.add(shafn)

            # git-commit
            msg = "Updates for build %s." % manifest.build_id
            git.commit('-m', msg)

            # git-tag
            msg = "Build ID %s" % manifest.build_id
            git.tag('-a', '-m', msg, manifest.build_id)


class ExclusionResolver(object):
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


class BuildDirectoryConstructor(object):
    """A class that, given one or more top level packages, recursively
    clones them to a build directory thus preparing them to be built."""

    def __init__(self, build_dir, eups, product_fetcher, version_db, exclusion_resolver):
        self.build_dir = os.path.abspath(build_dir)

        self.eups = eups
        self.product_fetcher = product_fetcher
        self.version_db = version_db
        self.exclusion_resolver = exclusion_resolver

    def _add_product_tree(self, products, product_name):
        if product_name in products:
            return products[product_name]

        # Mirror the product into the build directory (clone or git-pull it)
        ref, sha1 = self.product_fetcher.fetch(product_name)

        # Parse the table file to discover dependencies
        dependencies = []
        productdir = os.path.join(self.build_dir, product_name)
        table_fn = os.path.join(productdir, 'ups', '%s.table' % product_name)
        if os.path.isfile(table_fn):
            # Prepare the non-excluded dependencies
            for dep in eups.table.Table(table_fn).dependencies(self.eups):
                (dprod, doptional) = dep[0:2]

                # skip excluded optional products, and implicit products
                if doptional and self.exclusion_resolver.is_excluded(dprod.name, product_name):
                    continue
                if dprod.name == "implicitProducts":
                    continue

                dependencies.append(self._add_product_tree(products, dprod.name))

        # Construct EUPS version
        version = self.version_db.version(product_name, productdir, ref, dependencies)

        # Add the result to products, return it for convenience
        products[product_name] = Product(product_name, sha1, version, dependencies)
        return products[product_name]

    def construct(self, product_names):
        products = dict()
        for name in product_names:
            self._add_product_tree(products, name)

        self.product_fetcher.validate_refs()
        return Manifest.from_product_dict(products)

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

        if args.version_git_repo:
            version_db = VersionDbGit(args.version_git_repo, eups_obj)
        else:
            version_db = VersionDbHash(args.sha_abbrev_len, eups_obj)

        product_fetcher = ProductFetcher(
            build_dir,
            args.repos,
            args.repository_pattern,
            refs,
            args.no_fetch,
            tries=args.tries
        )
        p = BuildDirectoryConstructor(build_dir, eups_obj, product_fetcher, version_db, exclusion_resolver)

        #
        # Run the construction
        #
        manifest = p.construct(args.products)
        version_db.commit(manifest, args.build_id)

        #
        # Store the result in build_dir/manifest.txt
        #
        manifest_fn = os.path.join(build_dir, 'manifest.txt')
        with open(manifest_fn, 'w', encoding='utf-8') as fp:
            manifest.to_file(fp)


class RepoSpec(object):
    """Represents a git repo specification in repos.yaml. """

    def __init__(self, product, url, ref='master', lfs=False):
        self.product = product
        self.url = url
        self.ref = ref
        self.lfs = lfs

    def __str__(self):
        return self.url
