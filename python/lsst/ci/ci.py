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
import abc
import contextlib
import textwrap

from . import tsort

from .git import Git, GitError

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
       :ivar buildID:  unique build identifier
    """

    def __init__(self, productsList, buildID=None):
        """Construct the manifest
        
        Args:
            productList (OrderedDict): A topologically sorted dict of `Product`s
            buildID (str): A unique identifier for this build
        
        """
        self.buildID = buildID
        self.products = productsList

    def toFile(self, fileObject):
        """ Serialize the manifest to a file object """
        print >>fileObject, '# %-23s %-41s %-30s' % ("product", "SHA1", "Version")
        print >>fileObject, 'BUILD=%s' % self.buildID
        for prod in self.products.itervalues():
            print >>fileObject, '%-25s %-41s %-40s %s' % (prod.name, prod.sha1, prod.version, ','.join(dep.name for dep in prod.dependencies))

    def content_hash(self):
        """ Return a hash of the manifest, based on the products it contains. """
        m = hashlib.sha1()
        for prod in self.products.itervalues():
            s = '%s\t%s\t%s\n' % (prod.name, prod.sha1, prod.version)
            m.update(s)

        return m.hexdigest()

    @staticmethod
    def fromFile(fileObject):
        varre = re.compile('^(\w+)=(.*)$')

        products = collections.OrderedDict()
        buildId = None
        for line in fileObject:
            line = line.strip()
            if not line:
                continue
            if line.startswith('#'):
                continue

            # Look for variable assignments
            m = varre.match(line)
            if m:
                varName = m.group(1)
                varValue = m.group(2)
                if varName == "BUILD":
                    buildId = varValue
                continue

            arr = line.split()
            if len(arr) == 4:
                (name, sha1, version, deps) = arr
                deps = [ products[dep_name] for dep_name in deps.split(',') ]
            else:
                (name, sha1, version) = arr
                deps = []

            products[name] = Product(name, sha1, version, deps)

        return Manifest(products, buildId)

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
        return Manifest(products, None)

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

class VersionDb(object):
    """ Construct a full XXX+YYY version for a product.
    
        The subclasses of VersionDb determine how +YYY will be computed.
        The XXX part is computed by running EUPS' pkgautoversion.
    """

    __metaclass__ = abc.ABCMeta
    
    @abc.abstractmethod
    def getSuffix(self, productName, productVersion, dependencies):
        """Return a unique +YYY version suffix for a product given its dependencies
        
            Args:
                productName (str): name of the product
                productVersion (str): primary version of the product
                dependencies (list): A list of `Product`s that are the immediate dependencies of productName
                
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
           caused by getSuffix() invocations, and to assign the
           build_id to manifest.buildID.
        """
        pass

    def version(self, productName, productdir, ref, dependencies):
        """ Return a standardized XXX+YYY EUPS version, that includes the dependencies.
        
            Args:
                productName (str): name of the product to version
                productdir (str): the directory with product source code
                ref (str): the git ref that has been checked out into productdir (e.g., 'master')
                dependencies (list): A list of `Product`s that are the immediate dependencies of productName

            Returns:
                str. the XXX+YYY version string.
        """
        q = pipes.quote
        cmd ="cd %s && pkgautoversion %s" % (q(productdir), q(ref))
        productVersion = subprocess.check_output(cmd, shell=True).strip()

        # add +XXXX suffix, if any
        suffix = self.getSuffix(productName, productVersion, dependencies)
        assert suffix.__class__ == str
        suffix = "+%s" % suffix if suffix else ""
        return "%s%s" % (productVersion, suffix)



class VersionDbHash(VersionDb):
    """Subclass of `VersionDb` that generates +YYY suffixes by hashing the dependency names and versions"""

    def __init__(self, build_id_prefix, eups, sha_abbrev_len):
        self.sha_abbrev_len = sha_abbrev_len
        self.build_id_prefix = build_id_prefix
        self.eups = eups

    def _hash_dependencies(self, dependencies):
        m = hashlib.sha1()
        for dep in sorted(dependencies, lambda a, b: cmp(a.name, b.name)):
            s = '%s\t%s\n' % (dep.name, dep.version)
            m.update(s)

        return m.hexdigest()

    def getSuffix(self, productName, productVersion, dependencies):
        """ Return a hash of the sorted list of printed (dep_name, dep_version) tuples """
        hash = self._hash_dependencies(dependencies)
        suffix = hash[:self.sha_abbrev_len]
        return suffix

    def __getBuildId(self):
        """Allocate the next unused EUPS tag that matches the bNNNN pattern"""

        tags = eups.tags.Tags()
        tags.loadFromEupsPath(self.eups.path)

        prefix_len = len(self.build_id_prefix)
        btre = re.compile('^%s[0-9]+$' % self.build_id_prefix)
        btags = [ 0 ]
        btags += [ int(tag[prefix_len:]) for tag in tags.getTagNames() if btre.match(tag) ]
        tag = "%s%s" % (self.build_id_prefix, max(btags) + 1)

        return tag

    def commit(self, manifest, build_id):
        manifest.buildID = self.__getBuildId() if build_id is None else build_id

class VersionDbGit(VersionDbHash):
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
            for (version, suffix), dependencies in self.added_entries.iteritems():
                fileObjectVer.write("%s\t%s\t%d\n" % (version, self.hash(version, suffix), suffix))
                for depName, depVersion in dependencies:
                    fileObjectDep.write("%s\t%d\t%s\t%s\n" % (version, suffix, depName, depVersion))

            self.added_entries = []
            self.dirty = False

        @staticmethod
        def fromFile(fileObject):
            vm = VersionDbGit.VersionMap()
            for line in iter(fileObject.readline, ''):
                (version, hash, suffix) = line.strip().split()[:3]
                vm.__just_add(version, hash, int(suffix))

            return vm

    def __init__(self, build_id_prefix, eupsObj, dbdir):
        super(VersionDbGit, self).__init__(build_id_prefix, eupsObj, None)
        self.dbdir = dbdir

        self.versionMaps = dict()

    def __verfn(self, productName):
        return os.path.join("ver_db", productName + '.txt')

    def __depfn(self, productName):
        return os.path.join("dep_db", productName + '.txt')

    def __shafn(self):
        return os.path.join("manifests", 'content_sha.db.txt')

    def getSuffix(self, productName, productVersion, dependencies):
        hash = self._hash_dependencies(dependencies)

        # Lazy-load/create
        try:
            vm = self.versionMaps[productName]
        except KeyError:
            absverfn = os.path.join(self.dbdir, self.__verfn(productName))
            try:
                vm = VersionDbGit.VersionMap.fromFile(file(absverfn))
            except IOError:
                vm = VersionDbGit.VersionMap()
            self.versionMaps[productName] = vm

        # get or create a new suffix
        try:
            suffix = vm.suffix(productVersion, hash)
        except KeyError:
            suffix = vm.new_suffix(productVersion, hash, dependencies)

        assert isinstance(suffix, int)
        if suffix == 0:
            suffix = ""

        return str(suffix)

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

    def commit(self, manifest, build_id):
        git = Git(self.dbdir)

        manifestSha = manifest.content_hash()
        manifest.buildID = self.__getBuildId(manifest, manifestSha) if build_id is None else build_id

        # Write files
        dirty = False
        for (productName, vm) in self.versionMaps.iteritems():
            if not vm.dirty:
                continue

            verfn = self.__verfn(productName)
            depfn = self.__depfn(productName)
            absverfn = os.path.join(self.dbdir, verfn)
            absdepfn = os.path.join(self.dbdir, depfn)

            with open(absverfn, 'a') as fpVer:
                with open(absdepfn, 'a') as fpDep:
                    vm.appendAdditionsToFile(fpVer, fpDep)

            git.add(verfn, depfn)
            dirty = True

        # Store a copy of the manifest
        manfn = os.path.join('manifests', "%s.txt" % manifest.buildID)
        absmanfn = os.path.join(self.dbdir, manfn)
        with open(absmanfn, 'w') as fp:
            manifest.toFile(fp)

        if git.tag("-l", manifest.buildID) == manifest.buildID:
            # If the buildID/manifest are being reused, VersionDB repository must be clean
            if git.describe('--always', '--dirty=-prljavonakraju').endswith("-prljavonakraju"):
                raise Exception("Trying to reuse the buildID, but the versionDB repository is dirty!")
        else:
            # add the manifest file
            git.add(manfn)

            # add the new manifest<->buildID mapping
            shafn = self.__shafn()
            absshafn = os.path.join(self.dbdir, shafn)
            with open(absshafn, 'a+') as fp:
                fp.write("%s\t%s\n" % (manifestSha, manifest.buildID))
            git.add(shafn)

            # git-commit
            msg = "Updates for build %s." % manifest.buildID
            git.commit('-m', msg)

            # git-tag
            msg = "Build ID %s" % manifest.buildID
            git.tag('-a', '-m', msg, manifest.buildID)

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


class BuildDirectoryConstructor(object):
    """A class that, given one or more top level packages, recursively
    clones them to a build directory thus preparing them to be built."""
    
    def __init__(self, source_dir, eups, product_fetcher, version_db, exclusion_resolver):
        self.source_dir = os.path.abspath(source_dir)

        self.eups = eups
        self.product_fetcher = product_fetcher
        self.version_db = version_db
        self.exclusion_resolver = exclusion_resolver

    def _add_product_tree(self, products, productName):
        if productName in products:
            return products[productName]

        # Mirror the product into the build directory (clone or git-pull it)
        ref, sha1 = self.product_fetcher.fetch(productName)

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

                dependencies.append( self._add_product_tree(products, dprod.name) )

        # Construct EUPS version
        version = self.version_db.version(productName, productdir, ref, dependencies)

        # Add the result to products, return it for convenience
        products[productName] = Product(productName, sha1, version, dependencies)
        return products[productName]

    def construct(self, productNames):
        products = dict()
        for name in productNames:
            self._add_product_tree(products, name)

        return Manifest.fromProductDict(products)

    @staticmethod
    def run(args):
        #
        # Ensure build directory exists and is writable
        #
        source_dir = args.dir
        if not os.access(source_dir, os.W_OK):
            raise Exception("Directory '%s' does not exist or isn't writable." % source_dir)

        #
        # Add 'master' to list of refs, if not there already
        #
        refs = args.ref
        if 'master' not in refs:
            refs.append('master')

        #
        # Wire-up the BuildDirectoryConstructor constructor
        #
        eupsObj = eups.Eups()

        if args.exclusion_map:
            with open(args.exclusion_map) as fp:
                exclusion_resolver = ExclusionResolver.fromFile(fp)
        else:
            exclusion_resolver = ExclusionResolver([])

        if args.version_git_repo:
            version_db = VersionDbGit(args.build_id_prefix, eupsObj, args.version_git_repo)
        else:
            version_db = VersionDbHash(args.build_id_prefix, eupsObj, args.sha_abbrev_len)

        product_fetcher = ProductFetcher(source_dir, args.repository_pattern, refs, args.no_fetch)
        p = BuildDirectoryConstructor(source_dir, eupsObj, product_fetcher, version_db, exclusion_resolver)

        #
        # Run the construction
        #
        manifest = p.construct(args.products)
        version_db.commit(manifest, args.build_id)

        #
        # Store the result in source_dir/manifest.txt
        #
        manifestFn = os.path.join(source_dir, 'manifest.txt')
        with open(manifestFn, 'w') as fp:
            manifest.toFile(fp)

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

    @contextlib.contextmanager
    def newBuild(self, product):
        progress = ProgressReporter.ProductProgressReporter(self.out, product)
        progress._buildStarted()
        yield progress
        progress._finalize()

class Builder(object):
    """Class that builds and installs all products in a manifest.
    
       The result is tagged with the `Manifest`s build ID, if any.
    """
    def __init__(self, source_dir, manifest, progress, eups):
        self.source_dir = source_dir
        self.manifest = manifest
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

            if eupsProd is not None and self.manifest.buildID not in eupsProd.tags:
                self._tag_product(product.name, product.version, self.manifest.buildID)

            progress.reportResult(retcode, logfile)

        return retcode == 0

    def build(self):
        # Make sure EUPS knows about the buildID tag
        if self.manifest.buildID:
            declareEupsTag(self.manifest.buildID, self.eups)

        # Build all products
        for product in self.manifest.products.itervalues():
            if not self._build_product_if_needed(product):
                return False

    @staticmethod
    def run(args):
        # Ensure build directory exists and is writable
        source_dir = args.dir
        if not os.access(source_dir, os.W_OK):
            raise Exception("Directory '%s' does not exist or isn't writable." % source_dir)

        # Build products
        eupsObj = eups.Eups()

        progress = ProgressReporter(sys.stderr)

        manifestFn = os.path.join(source_dir, 'manifest.txt')
        with open(manifestFn) as fp:
            manifest = Manifest.fromFile(fp)

        b = Builder(source_dir, manifest, progress, eupsObj)
        b.build()
