#############################################################################
# Preparer

import os, os.path
import stat
import sys
import eups, eups.tags
import hashlib
import shutil
import time
import re
import pipes
import subprocess
import collections
import contextlib
import textwrap
import cStringIO
import ConfigParser
import getpass

from . import tsort

from .git import Git, GitError, transaction as git_transaction

def load_config(fileObject):
    """ Load the build-tool configuration, returning it in a dict
        
        Config files are ConfigParser parsable, and converted to
        <section>.<key> = <value> keys in the returned dict.
    """

    def str_to_bool(strval):
        if strval.lower() in ["true", "1"]:
            return True
        if strval.lower() in ["false", "0"]:
            return False
        raise Exception("Error converting string '%s' to boolean value." % strval)

    schema = {
        # key : (default value, type_converter)
        'core.exclusions': (None, str),

        'upstream.pattern': (None, str),

        'versiondb.url': (None, str),
        'versiondb.dir': ('.bt/versiondb', str),
        'versiondb.writable': (False, str_to_bool),
        'versiondb.sha-abbrev-len': (10, int),

        'manifestdb.url': (None, str),
        'manifestdb.dir': ('.bt/manifestdb', str),
        'manifestdb.sha-abbrev-len': (10, int),

        'build.prefix': (getpass.getuser(), str)
    }

    config = dict((key, default_value) for (key, (default_value, _)) in schema.iteritems())

    # Load from file
    cp = ConfigParser.ConfigParser()
    cp.readfp(fileObject)
    for section in cp.sections():
        for (key, value) in cp.items(section, raw=True):
            k = "%s.%s" % (section, key)
            _, type_converter = schema[k]
            config[k] = type_converter(value)

    return config

class Product(object):
    """Class representing an EUPS product to be built"""
    def __init__(self, name, dependencies, sha1=None, version=None):
        self.name = name
        self.dependencies = dependencies

        self.sha1 = sha1
        self.version = version

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
       :ivar buildID:  unique build identifier
    """

    def __init__(self, productsList):
        """Construct the manifest
        
        Args:
            productList (OrderedDict): A topologically sorted dict of `Product`s
        
        """
        self.products = productsList

    def toFile(self, fileObject):
        """ Serialize the manifest to a file object """
        print >>fileObject, '# %-23s %-41s %-30s' % ("product", "SHA1", "Version")
        for prod in self.products.itervalues():
            print >>fileObject, '%-25s %-41s %-40s %s' % (prod.name, prod.sha1, prod.version, ','.join(dep.name for dep in prod.dependencies))

    def __repr__(self):
        output = cStringIO.StringIO()
        self.toFile(output)
        return output.getvalue()

    def hash(self):
        """ Return a hash of the manifest, based on the products it contains. """
        m = hashlib.sha1()
        for prod in self.products.itervalues():
            s = '%s\t%s\t%s\n' % (prod.name, prod.sha1, prod.version)
            m.update(s)

        return m.hexdigest()

    def __eq__(self, other):
        return self.hash() == other.hash()
    def __ne__(self, other):
        return not self == other

    @staticmethod
    def fromFile(fileObject):
        varre = re.compile('^(\w+)=(.*)$')
        products = collections.OrderedDict()
        for line in fileObject:
            line = line.strip()
            if not line:
                continue
            if line.startswith('#'):
                continue

            # --- backwards compatibility ---
            # We don't use store variables in manifests any more
            m = varre.match(line)
            if m:
                continue
            # --- backwards compatibility ---

            arr = line.split()
            if len(arr) == 4:
                (name, sha1, version, deps) = arr
                deps = [ products[dep_name] for dep_name in deps.split(',') ]
            else:
                (name, sha1, version) = arr
                deps = []

            products[name] = Product(name, deps, sha1=sha1, version=version)

        return Manifest(products)

    @staticmethod
    def fromProductDict(productDict):
        """ Create a `Manifest` by topologically sorting the dict of `Product`s 
        
        Args:
            productDict (dict): A productName -> `Product` dictionary of products

        Returns:
            The created `Manifest`.
        """
        deps = [ (dep.name, prod.name) for prod in productDict.itervalues() for dep in prod.dependencies ];
        topoSortedProductNames = tsort.tsort(deps)

        # Append top-level products with no dependencies
        _p = set(topoSortedProductNames)
        for name in set(productDict.iterkeys()):
            if name not in _p:
                topoSortedProductNames.append(name)

        products = collections.OrderedDict()
        for name in topoSortedProductNames:
            products[name] = productDict[name]
        return Manifest(products)


class ProductFetcher(object):
    """ Fetches products from remote git repositories and checks out matching refs.

        See `fetch` for further documentation.
        
        :ivar source_dir: The product will be cloned to source_dir/productName
        :ivar repository_patterns: A list of str.format() patterns used discover the URL of the remote git repository.
        :ivar refs: A list of refs to attempt to git-checkout
        :ivar no_fetch: If true, don't fetch, just checkout the first matching ref.
    """
    def __init__(self, source_dir, repository_patterns, refs, no_fetch):
        self.source_dir = os.path.abspath(source_dir)
        self.refs = refs
        self.repository_patterns = repository_patterns.split('|')
        self.no_fetch = no_fetch

    def _origin_candidates(self, product):
        """ Expand repository_patterns into URLs. """
        data = { 'product': product }
        return [ pat % data for pat in self.repository_patterns ]

    def fetch(self, product):
        """ Clone the product repository and checkout the first matching ref.
        
        Args:
            product (str): the product to fetch
            
        Returns:
            (ref, sha1) tuple where::
            
                 ref -- the checked out ref (e.g., 'master')
                 sha1 -- the corresponding commit's SHA1

        If $source_dir/$product does not exist, discovers the product
        repository by attempting a git clone from the list of URLs
        constructed by running str.format() with { 'product': product}
        on self.repository_patterns. Otherwise, intelligently fetches
        any new commits.

        Next, attempts to check out the refs listed in self.ref,
        until the first one succeeds.

        """

        t0 = time.time()
        sys.stderr.write("%20s: " % product)

        productdir = os.path.join(self.source_dir, product)
        git = Git(productdir)

        # verify the URL of origin hasn't changed
        if os.path.isdir(productdir):
            origin = git('config', '--get', 'remote.origin.url')
            if origin not in self._origin_candidates(product):
                shutil.rmtree(productdir)

        # clone
        if not os.path.isdir(productdir):
            for url in self._origin_candidates(product):
                if not Git.clone(url, productdir, return_status=True)[1]:
                    break
            else:
                raise Exception("Failed to clone product '%s' from any of the offered repositories" % product)

        # update from origin
        if not self.no_fetch:
            # the line below should be equivalent to:
            #     git.fetch("origin", "--force", "--prune")
            #     git.fetch("origin", "--force", "--tags")
            # but avoids the overhead of two (possibly remote) git calls.
            git.fetch("-fup", "origin", "+refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*")

        # find a ref that matches, checkout it
        for ref in self.refs:
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
        git.clean("-d", "-f", "-q")

        print >>sys.stderr, " ok (%.1f sec)." % (time.time() - t0)
        return ref, sha1

def get_checked_out_ref_name(git):
    # n.b.: we do it by grepping the reflog and not simply by
    # inspecting the contents of HEAD because the rev used to perform
    # the checkout may have been a tag (in which case HEAD would just
    # record the SHA1)

    # trying to match a pattern like: [[7e60993 HEAD@{10}: checkout: moving from master to u/mjuric/next]]
    r = re.compile(r'^[0-9a-f]+ HEAD@\{\d+\}: checkout: moving from [^ ]+ to ([^ ]+)$')

    for line in git.reflog("HEAD").splitlines():
        m = r.match(line)
        if m:
            return(m.group(1))

    return ""


class BuildIdGenerator(object):
    def __getBuildId(self, manifest):
        """Find and re-use an existing tag with the requested prefix that
           tags the largest subset of this manifest and no products outside
           of it."""

        tags = eups.tags.Tags()
        tags.loadFromEupsPath(self.eups.path)

        # Tags are in the form <prefix><N>; find the maximal <N>
        prefix_len = len(self.build_id_prefix)
        btre = re.compile('^%s[0-9]+$' % self.build_id_prefix)
        btags = [ int(tag[prefix_len:]) for tag in tags.getTagNames() if btre.match(tag) ]
        if btags:
            candidate = max(btags)
        
            # See if the products tagged with the candidate tag are a subset of the manifest
            # If yes, reuse the tag; if not, increment <N> by one and use that.
            products = self.eups.findProducts(tags="%s%s" % (self.build_id_prefix, candidate))
            if len(products) != len(manifest.products):
                for product in products:
                    print "%s-%s" % (product.name, product.version)
                    if product.name not in manifest.products or \
                       product.version != manifest.products[product.name].version:
                        candidate += 1
                        return "%s%s" % (self.build_id_prefix, candidate)
        else:
            candidate = 1
        
        # reuse the tag
        return "%s%s" % (self.build_id_prefix, candidate)

    def __getBuildId(self, manifest, manifestSha):
        """Return a build ID unique to this manifest. If a matching manifest already
           exists in the database, its build ID will be used.
        """
        with open(os.path.join(self.dbdir, 'manifests', 'content_sha.db.txt'), 'a+') as fp:
                # Try to find a manifest with existing matching content
                for line in fp:
                        (sha1, tag) = line.strip().split()
                        if sha1 == manifestSha:
                                return tag

                # Find the next unused tag that matches the ${build_id_prefix}NNNN pattern
                # and isn't defined in EUPS yet
                git = Git(self.dbdir)
                tags = git.tag('-l', '%s[0-9]*' % self.build_id_prefix).split()
                btre = re.compile('^%s[0-9]+$' % self.build_id_prefix)
                prefix_len = len(self.build_id_prefix)
                btags = [ 0 ]
                btags += [ int(tag[prefix_len:]) for tag in tags if btre.match(tag) ]
                btag = max(btags)

                definedTags = self.eups.tags.getTagNames()
                while True:
                    btag += 1
                    tag = "%s%s" % (self.build_id_prefix, btag)
                    if tag not in definedTags:
                        break

                return tag

class VersionDB(object):
    """Subclass of `VersionDb` that generates +YYY suffixes by assigning a unique +N integer to
       each set of dependencies, and tracking the assignments in a git repository.    
    """

    class VersionMap(object):
        def __init__(self):
            self.verhash2suffix = dict()	# (version, dep_sha) -> suffix
            self.versuffix2hash = dict()	# (version, suffix) -> depsha

            self.added_entries = dict()		# (version, suffix) -> [ (depName, depVersion) ]

            self.dirty = False

        def __just_add(self, version, hash, suffix):
            assert isinstance(suffix, int)

            self.verhash2suffix[(version, hash)] = suffix
            self.versuffix2hash[(version, suffix)] = hash

        def __add(self, version, hash, suffix, dependencies):
            self.__just_add(version, hash, suffix)

            # Record additions to know what needs to be appended
            self.added_entries[(version, suffix)] = [ (product.name, product.version) for product in dependencies ]

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

        def appendAdditionsToFile(self, fileObjectVer, fileObjectDep):
            # write (version, hash)<->suffix and dependency table updates
            # returns the number of additions written
            
            for (version, suffix), dependencies in self.added_entries.iteritems():
                fileObjectVer.write("%s\t%s\t%d\n" % (version, self.hash(version, suffix), suffix))
                for depName, depVersion in dependencies:
                    fileObjectDep.write("%s\t%d\t%s\t%s\n" % (version, suffix, depName, depVersion))

            n = len(self.added_entries)
            self.added_entries = []
            self.dirty = False

            return n

        @staticmethod
        def fromFile(fileObject):
            vm = VersionDB.VersionMap()
            for line in iter(fileObject.readline, ''):
                (version, hash, suffix) = line.strip().split()[:3]
                vm.__just_add(version, hash, int(suffix))

            return vm

    def __init__(self, dbdir, eupsObj, writable, sha_abbrev_len, no_fetch):
        self.eups = eupsObj
        self.dbdir = dbdir
        self.writable = writable
        self.sha_abbrev_len = sha_abbrev_len
        self.no_fetch = no_fetch

        self.versionMaps = {}
        self.git = Git(self.dbdir)
        
        if not self.git.isclean():
            raise Exception('versiondb is not clean, refusing to proceed.')

    def __verfn(self, productName):
        return os.path.join("ver_db", productName + '.txt')

    def __depfn(self, productName):
        return os.path.join("dep_db", productName + '.txt')

    def __shafn(self):
        return os.path.join("manifests", 'content_sha.db.txt')

    def _hash_product_list(self, dependencies):
        """ Return a hash of (product name, product version) tuples """
        m = hashlib.sha1()

        for dep in sorted(dependencies, lambda a, b: cmp(a.name, b.name)):
            s = '%s\t%s\n' % (dep.name, dep.version)
            m.update(s)

        return m.hexdigest()

    def _getSuffix(self, productName, productVersion, dependencies):
        hash = self._hash_product_list(dependencies)

        # Lazy-load/create
        try:
            vm = self.versionMaps[productName]
        except KeyError:
            absverfn = os.path.join(self.dbdir, self.__verfn(productName))
            try:
                vm = VersionDB.VersionMap.fromFile(open(absverfn))
            except IOError:
                vm = VersionDB.VersionMap()
            self.versionMaps[productName] = vm

        # get or create a new suffix. If the database is read-only,
        # return the dependency hash as the suffix.
        try:
            suffix = str(vm.suffix(productVersion, hash))
        except KeyError:
            if self.writable:
                suffix = vm.new_suffix(productVersion, hash, dependencies)

                assert isinstance(suffix, int)
                suffix = str(suffix)
            else:
                suffix = hash[:self.sha_abbrev_len]

        if suffix == "0":
            suffix = ""

        return suffix

    def version(self, productName, productdir, git, dependencies):
        """ Return a standardized XXX+YYY EUPS version, that includes the dependencies.
        
            Args:
                productName (str): name of the product to version
                productdir (str): the directory with product source code
                git (Git): the Git object for productdir
                dependencies (list): A list of `Product`s that are the immediate dependencies of productName

            Returns:
                str. the XXX+YYY version string.
        """
        ref = get_checked_out_ref_name(git)

        q = pipes.quote
        cmd ="cd %s && pkgautoversion %s" % (q(productdir), q(ref))
        productVersion = subprocess.check_output(cmd, shell=True).strip()

        # add +XXXX suffix, if any
        suffix = self._getSuffix(productName, productVersion, dependencies)
        assert type(suffix) is str, "suffix must be a string, instead it's a %s" % (suffix.__class__)
        suffix = "+%s" % suffix if suffix else ""
        return "%s%s" % (productVersion, suffix)

    def version_manifest(self, workdir, manifest):
        """ For products in manifest, find or compute sha1, checked out ref,
            and version """

        with git_transaction(self.git, '--ff-only', pull=not self.no_fetch, push=not self.no_fetch):
            for product in manifest.products.itervalues():
                productdir = os.path.join(workdir, product.name)
                git = Git(productdir)

                product.sha1 = git.rev_parse("HEAD")
                product.version = self.version(product.name, productdir, git, product.dependencies)

            self._commit()

    def _commit(self):
        dirty = False
        n = 0
        for (productName, vm) in self.versionMaps.iteritems():
            if not vm.dirty:
                continue

            # if we wrote a new version when the database has been marked
            # as read-only, there's a coding error somewhere in here.
            assert self.writable, "versiondb.writable=false but the entry for %s has been modified" % productName

            verfn = self.__verfn(productName)
            depfn = self.__depfn(productName)
            absverfn = os.path.join(self.dbdir, verfn)
            absdepfn = os.path.join(self.dbdir, depfn)

            with open(absverfn, 'a') as fpVer:
                with open(absdepfn, 'a') as fpDep:
                    n += vm.appendAdditionsToFile(fpVer, fpDep)

            self.git.add(verfn, depfn)
            dirty = True

        if dirty:
            # Commit
            msg = "%d new versions added on %s." % (n, time.strftime("%c"))
            self.git.commit('-m', msg)

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
            rc[product] = [ dep_re for (dep_re, prod_re) in self.exclusions if prod_re.match(product) ]

        for dep_re in rc[product]:
            if dep_re.match(dep):
                return True

        return False

    @staticmethod
    def fromFile(fileObject):
        exclusion_patterns = []

        for line in fileObject:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            exclusion_patterns.append(line.split()[:2])

        return ExclusionResolver(exclusion_patterns)

def make_exclusion_resolver(exclusion_map_fn):
    if not exclusion_map_fn:
        return ExclusionResolver([])

    with open(exclusion_map_fn) as fp:
        return ExclusionResolver.fromFile(fp)

class ProductDependencyLoader(object):
    def __init__(self, source_dir, eupsObj, exclusion_resolver):
        self.exclusion_resolver = exclusion_resolver
        self.source_dir = source_dir
        self.eups = eupsObj
        
    def get_dependencies(self, productName):
        # Parse the table file to discover dependencies
        dependencies = []
        productdir = os.path.join(self.source_dir, productName)
        table_fn = os.path.join(productdir, 'ups', '%s.table' % productName)
        if os.path.isfile(table_fn):
            # Prepare the non-excluded dependencies
            for dep in eups.table.Table(table_fn).dependencies(self.eups):
                (dprod, doptional) = dep[0:2]

                # skip excluded optional products, and implicit products
                if doptional and self.exclusion_resolver.is_excluded(dprod.name, productName):
                    continue;
                if dprod.name == "implicitProducts":
                    continue;

                dependencies.append( dprod.name )
        return dependencies

class ProductDictBuilder(object):
    """A class that, given one or more top level packages, recursively
    clones them to a build directory thus preparing them to be built."""
    
    def __init__(self, source_dir, product_fetcher, dependency_loader):
        self.source_dir = os.path.abspath(source_dir)

        self.fetch_product = product_fetcher.fetch if product_fetcher is not None else lambda x: None;
        self.dependency_loader = dependency_loader

    def _do_walk(self, products, productName):
        if productName in products:
            return products[productName]

        # Load the product components
        self.fetch_product(productName)
        dependencies = [ self._do_walk(products, depName) for depName in self.dependency_loader.get_dependencies(productName) ]

        # Add the result to products, return it for convenience
        products[productName] = Product(productName, dependencies)
        return products[productName]

    def walk(self, productNames):
        products = dict()
        for name in productNames:
            self._do_walk(products, name)
        return products

class BT(object):
    def __init__(self, workdir, btdir, config, products):
        self.workdir = workdir
        self.btdir = btdir
        self.config = config
        self.products = products
        
    def save_products(self, products):
        productsFile = os.path.join(self.btdir, 'products')
        with open(productsFile, 'w') as fp:
            fp.write(' '.join(products))
            fp.write('\n')
        self.products = products

    def save_manifest(self, manifest):
        # Store the result in .bt/manifest
        manifestFn = os.path.join(self.btdir, 'manifest')
        with open(manifestFn, 'w') as fp:
            manifest.toFile(fp)

    def load_manifest(self):
        manifestFn = os.path.join(self.btdir, 'manifest')
        with open(manifestFn, 'r') as fp:
            return Manifest.fromFile(fp)

    @staticmethod
    def fromDir(workdir, btdir=None):
        # Ensure build directory exists, is initialized, and writable
        if btdir is None:
            btdir = os.path.join(workdir, '.bt')

        if not os.access(workdir, os.W_OK):
            raise Exception("Directory '%s' does not exist or isn't writable." % workdir)

        if not os.path.isdir(btdir):
            raise Exception("Directory '%s' doesn't have a .bt subdirectory. Did you forget to run 'bt init'?" % workdir)

        # Load the configuration
        with open(os.path.join(btdir, 'config')) as fp:
            config = load_config(fp)
        
        # Load the current product list, if any
        productsFile = os.path.join(btdir, 'products')
        products = []
        if os.path.exists(productsFile):
            with open(productsFile) as fp:
                products = fp.read().strip().split()

        return BT(workdir, btdir, config, products)


class PullCommand(object):
    @staticmethod
    def run(args):
        bt = BT.fromDir(args.work_dir, args.bt_dir)

        # Use default args.produts if empty
        if args.products:
            products = args.products
            bt.save_products(products)
        else:
            products = bt.products

        if not products:
            raise Exception("Need to specify products the first time you run 'bt pull'.")

        # Apply any command-line overrides
        # -- none so far --

        #
        # Add 'master' to list of refs, if not there already
        #
        refs = args.ref
        if 'master' not in refs:
            refs.append('master')

        #
        # Wire-up the ProductDictBuilder
        #
        eupsObj = eups.Eups()
        exclusion_resolver = make_exclusion_resolver(bt.config['core.exclusions'])
        dependency_loader = ProductDependencyLoader(bt.workdir, eupsObj, exclusion_resolver)
        product_fetcher = ProductFetcher(bt.workdir, bt.config['upstream.pattern'], refs, args.no_fetch)
        p = ProductDictBuilder(bt.workdir, product_fetcher, dependency_loader)

        # Run the construction
        productDict = p.walk(products)
        manifest = Manifest.fromProductDict(productDict)

        # Version the manifest
        versiondb = VersionDB(bt.config["versiondb.dir"], eupsObj, bt.config["versiondb.writable"], bt.config["versiondb.sha-abbrev-len"], args.no_fetch)
        versiondb.version_manifest(bt.workdir, manifest)

        # Save the current manifest into .bt/manifest
        bt.save_manifest(manifest)


#############################################################################
# Builder

def declareEupsTag(tag, eupsObj):
    """ Declare a new EUPS tag
        FIXME: Not sure if this is the right way to programmatically
               define and persist a new tag. Ask RHL.
    """
    tags = eupsObj.tags
    if tag not in tags.getTagNames():
        tags.registerTag(tag)
        tags.saveGlobalTags(eupsObj.path[0])

class ProgressReporter(object):
    # progress reporter: display the version string as progress bar, character by character

    class ProductProgressReporter(object):
        def __init__(self, outFileObj, product):
            self.out = outFileObj
            self.product = product

        def _buildStarted(self):
            self.out.write('%20s: ' % self.product.name)
            self.progress_bar = self.product.version + " "
            self.t0 = self.t = time.time()

        def reportProgress(self):
            # throttled progress reporting
            #
            # Write out the version string as a progress bar, character by character, and
            # then continue with dots.
            #
            # Throttle updates to one character every 2 seconds
            t1 = time.time()
            while self.t <= t1:
                if self.progress_bar:
                    self.out.write(self.progress_bar[0])
                    self.progress_bar = self.progress_bar[1:]
                else:
                    self.out.write('.')

                self.out.flush()
                self.t += 2

        def reportResult(self, retcode, logfile):
            # Make sure we write out the full version string, even if the build ended quickly
            if self.progress_bar:
                self.out.write(self.progress_bar)

            # If logfile is None, the product was already installed
            if logfile is None:
                sys.stderr.write('(already installed).\n')
            else:
                elapsedTime = time.time() - self.t0
                if retcode:
                    print >>self.out, "ERROR (%d sec)." % elapsedTime
                    print >>self.out, "*** error building product %s." % self.product.name
                    print >>self.out, "*** exit code = %d" % retcode
                    print >>self.out, "*** log is in %s" % logfile
                    print >>self.out, "*** last few lines:"

                    os.system("tail -n 10 %s | sed -e 's/^/:::::  /'" % pipes.quote(logfile))
                else:
                    print >>self.out, "ok (%.1f sec)." % elapsedTime

            self.product = None

        def _finalize(self):
            # Usually called only when an exception is thrown
            if self.product is not None:
                self.out.write("\n")

    def __init__(self, outFileObj):
        self.out = outFileObj

    def message(self, msg):
        print >>self.out, msg

    @contextlib.contextmanager
    def newBuild(self, product):
        progress = ProgressReporter.ProductProgressReporter(self.out, product)
        progress._buildStarted()
        yield progress
        progress._finalize()

class ManifestDB(object):
    def __init__(self, dbdir, sha_abbrev_len, no_fetch):
        self.dbdir = dbdir
        self.sha_abbrev_len = sha_abbrev_len
        self.no_fetch = no_fetch

        self.git = Git(self.dbdir)

        if not self.git.isclean():
            raise Exception('manifestdb is not clean, refusing to proceed.')

    def __manifestPath(self, build_id):
        return os.path.abspath(os.path.join(self.dbdir, 'manifests', '%s.manifest.txt' % build_id))

    def add_manifest(self, manifest):
        # generate new build ID
        build_id = 'x' + manifest.hash()[:self.sha_abbrev_len]

        with git_transaction(self.git, '--rebase', pull=not self.no_fetch, push=not self.no_fetch) as tr:
            manifestPath = self.__manifestPath(build_id)

            # is it already in the repository?
            if os.path.exists(manifestPath):
                with open(manifestPath) as fp:
                    if Manifest.fromFile(fp) != manifest:   # quick sanity check
                        raise Exception("Database corruption detected -- stored manifest for %s is not the same as the one you're trying to add." % (build_id))
                return build_id

            # add it
            with open(manifestPath, 'w') as fp:
                manifest.toFile(fp)
            self.git.add(manifestPath)

            # commit locally
            msg = "Manifest for %s added by %s (%d products)." % (build_id, getpass.getuser(), len(manifest.products))
            self.git.commit('-m', msg)

        return build_id

    def get_manifest(self, build_id):
        manifestPath = self.__manifestPath(build_id)
        with open(manifestPath) as fp:
            return Manifest.fromFile(fp)

class Builder(object):
    """Class that builds and installs all products in a manifest.
    
       The result is tagged with the `Manifest`s build ID, if any.
    """
    def __init__(self, source_dir, manifest, build_id, progress, eups):
        self.source_dir = source_dir
        self.manifest = manifest
        self.build_id = build_id
        self.progress = progress
        self.eups = eups

    def _tag_product(self, name, version, tag):
        if tag:
            self.eups.declare(name, version, tag=tag)

    def _build_product(self, product, progress):
        # run the eupspkg sequence for the product
        #
        productdir = os.path.abspath(os.path.join(self.source_dir, product.name))
        buildscript = os.path.join(productdir, '_build.sh')
        logfile = os.path.join(productdir, '_build.log')

        # construct the tags file with exact dependencies
        setups = [ 
            "\t%-20s %s" % (dep.name, dep.version)
                for dep in product.flat_dependencies()
        ]

        # create the buildscript
        with open(buildscript, 'w') as fp:
            text = textwrap.dedent(
            """\
            #!/bin/bash

            # redirect stderr to stdin
            exec 2>&1

            # stop on any error
            set -ex

            cd %(productdir)s

            # clean up the working directory
            git reset --hard
            git clean -d -f -q -x -e '_build.*'

            # prepare
            eupspkg PRODUCT=%(product)s VERSION=%(version)s FLAVOR=generic prep

            # setup the package with its exact dependencies
            cat > _build.tags <<-EOF
            %(setups)s
            EOF
            set +x
            setup --vro=_build.tags -r .
            set -x

            # build
            eupspkg PRODUCT=%(product)s VERSION=%(version)s FLAVOR=generic config
            eupspkg PRODUCT=%(product)s VERSION=%(version)s FLAVOR=generic build
            eupspkg PRODUCT=%(product)s VERSION=%(version)s FLAVOR=generic install

            # declare to EUPS
            eupspkg PRODUCT=%(product)s VERSION=%(version)s FLAVOR=generic decl

            # explicitly append SHA1 to pkginfo
            echo SHA1=%(sha1)s >> $(eups list %(product)s %(version)s -d)/ups/pkginfo
            """ %    {
                    'product': product.name,
                    'version': product.version,
                    'sha1' : product.sha1,
                    'productdir' : productdir,
                    'setups': '\n            '.join(setups)
                }
            )

            fp.write(text)

        # Make executable (equivalent of 'chmod +x $buildscript')
        st = os.stat(buildscript)
        os.chmod(buildscript, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # Run the build script
        with open(logfile, 'w') as logfp:
            # execute the build file from the product directory, capturing the output and return code
            process = subprocess.Popen(buildscript, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=productdir)
            for line in iter(process.stdout.readline, ''):
                logfp.write(line)
                progress.reportProgress()

        retcode = process.poll()
        if not retcode:
            # copy the log file to product directory
            eupsProd = self.eups.getProduct(product.name, product.version)
            shutil.copy2(logfile, eupsProd.dir)
        else:
            eupsProd = None

        return (eupsProd, retcode, logfile)

    def _build_product_if_needed(self, product):
        # Build a product if it hasn't been installed already
        #
        with self.progress.newBuild(product) as progress:
            try:
                # skip the build if the product has been installed
                eupsProd, retcode, logfile = self.eups.getProduct(product.name, product.version), 0, None
            except eups.ProductNotFound:
                eupsProd, retcode, logfile = self._build_product(product, progress)

            if eupsProd is not None and self.build_id not in eupsProd.tags:
                self._tag_product(product.name, product.version, self.build_id)

            progress.reportResult(retcode, logfile)

        return retcode == 0

    def build(self):
        self.progress.message("Building %s (%d products):" % (self.build_id, len(self.manifest.products)) )

        # Make sure EUPS knows about the build_id tag
        if self.build_id is not None:
            declareEupsTag(self.build_id, self.eups)

        # Build all products
        for product in self.manifest.products.itervalues():
            if not self._build_product_if_needed(product):
                return False

    @staticmethod
    def run(args):
        # Get the manifest
        bt = BT.fromDir(args.work_dir, args.bt_dir)
        manifest = bt.load_manifest()

        # Generate new build ID by adding the manifest to the manifestdb database
        bl = ManifestDB(bt.config["manifestdb.dir"], bt.config["manifestdb.sha-abbrev-len"], args.no_fetch)
        build_id = bl.add_manifest(manifest)

        # Build products
        progress = ProgressReporter(sys.stderr)
        eupsObj = eups.Eups()
        b = Builder(bt.workdir, manifest, build_id, progress, eupsObj)
        b.build()

#        # Construct the manifest of products to build
#        exclusion_resolver = make_exclusion_resolver(args.exclusion_map)
#        dependency_loader = ProductDependencyLoader(source_dir, eupsObj, exclusion_resolver)
#        productDict = ProductDictBuilder(source_dir, None, dependency_loader).walk(args.products)
#        manifest = Manifest.fromProductDict(productDict)
#
#        # Version products
#        if args.version_git_repo:
#            version_db = VersionDbGit(args.build_id_prefix, eupsObj, args.version_git_repo)
#        else:
#            version_db = VersionDbHash(args.build_id_prefix, eupsObj, args.sha_abbrev_len)
#        build_id = version_manifest(source_dir, manifest, version_db, args.build_id)
#        print >>sys.stderr, "build id: %s" % build_id


#        manifestFn = os.path.join(source_dir, 'manifest.txt')
#        with open(manifestFn) as fp:
#            manifest = Manifest.fromFile(fp)

