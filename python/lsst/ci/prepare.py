#############################################################################
# Preparer

import os, os.path
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

import tsort

from .git import Git, GitError

class Product(object):
	"""Class representing an EUPS product to be built"""
	def __init__(self, name, sha1, version, dependencies):
		self.name = name
		self.sha1 = sha1
		self.version = version
		self.dependencies = dependencies

class Manifest(object):
	"""A representation of topologically ordered list of EUPS products to be built
	
	   :ivar products: topologically sorted list of `Product`s
	   :ivar buildID:  unique build identifier
	"""

	def __init__(self, productsList, buildID=None):
		"""Construct the manifest
		
		Args:
		    productList (list): A topologically sorted list of `Product`s
		    buildID (str): A unique identifier for this build
		
		"""
		self.buildID = buildID
		self.products = productsList

	def toFile(self, fileObject):
		""" Serialize the manifest to a file object """
		print >>fileObject, '# %-23s %-41s %-30s' % ("product", "SHA1", "Version")
		print >>fileObject, 'BUILD=%s' % self.buildID
		for prod in self.products:
			print >>fileObject, '%-25s %-41s %-40s %s' % (prod.name, prod.sha1, prod.version, ','.join(dep.name for dep in prod.dependencies))

	@staticmethod
	def fromFile(fileObject):
		raise NotImplementedError()

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

		return Manifest( [productDict[name] for name in topoSortedProductNames], None)

class ProductFetcher(object):
	""" Fetches products from remote git repositories and checks out matching refs.

	    See `fetch` for further documentation.
	    
	    :ivar build_dir: The product will be cloned to build_dir/productName
	    :ivar repository_patterns: A list of str.format() patterns used discover the URL of the remote git repository.
	    :ivar refs: A list of refs to attempt to git-checkout
	    :ivar no_fetch: If true, don't fetch, just checkout the first matching ref.
	"""
	def __init__(self, build_dir, repository_patterns, refs, no_fetch):
		self.build_dir = os.path.abspath(build_dir)
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

		If $build_dir/$product does not exist, discovers the product
		repository by attempting a git clone from the list of URLs
		constructed by running str.format() with { 'product': product}
		on self.repository_patterns. Otherwise, intelligently fetches
		any new commits.

		Next, attempts to check out the refs listed in self.ref,
		until the first one succeeds.

		"""

		t0 = time.time()
		sys.stderr.write("%20s: " % product)

		productdir = os.path.join(self.build_dir, product)
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
			#print ref, "branch=", sha1
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

			#print "HEAD=", git.rev_parse("HEAD")
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
	def getSuffix(self, productName, dependencies):
		"""Return a unique +YYY version suffix for a product given its dependencies
		
		    Args:
		        productName (str): name of the product
		        dependencies (list): A list of `Product`s that are the immediate dependencies of productName
		        
		    Returns:
		        str. the +YYY suffix (w/o the + sign).
		"""
		pass

	@abc.abstractmethod
	def getBuildId(self):
		"""Return a unique build ID identifying this build"""
		pass

	@abc.abstractmethod
	def commit(self, manifest):
		"""Commit the changes to the version database
		
		   Args:
		       manifest (`Manifest`): a manifest of products from this run
		       
		   A subclass should override this method to commit to
		   permanent storage any changes to the underlying database
		   caused by getSuffix() invocations.
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
		ver = subprocess.check_output(cmd, shell=True).strip()

		if dependencies:
			deps_sha1 = self.getSuffix(productName, dependencies)
			return "%s+%s" % (ver, deps_sha1)
		else:
			return ver


class VersionDbHash(VersionDb):
	"""Subclass of `VersionDb` that generates +YYY suffixes by hashing the dependency names and versions"""

	def __init__(self, sha_abbrev_len, eups):
		self.sha_abbrev_len = sha_abbrev_len
		self.eups = eups

	def _hash_dependencies(self, dependencies):
		m = hashlib.sha1()
		for dep in sorted(dependencies, lambda a, b: cmp(a.name, b.name)):
			s = '%s\t%s\n' % (dep.name, dep.version)
			m.update(s)

		return m.hexdigest()

	def getSuffix(self, productName, dependencies):
		""" Return a hash of the sorted list of printed (dep_name, dep_version) tuples """
		hash = self._hash_dependencies(dependencies)
		suffix = hash[:self.sha_abbrev_len]
		return suffix

	def getBuildId(self):
		"""Allocate the next unused EUPS tag that matches the bNNNN pattern"""

		tags = eups.tags.Tags()
		tags.loadFromEupsPath(self.eups.path)

		btre = re.compile('^b[0-9]+$')
		btags = [ 0 ]
		btags += [ int(tag[1:]) for tag in tags.getTagNames() if btre.match(tag) ]
		tag = "b%s" % (max(btags) + 1)

		return tag

	def commit(self, manifest):
		pass

class VersionDbGit(VersionDbHash):
	"""Subclass of `VersionDb` that generates +YYY suffixes by assigning a unique +N integer to
	   each set of dependencies, and tracking the assignments in a git repository.	
	"""

	class VersionMap(object):
		def __init__(self):
			self.hash2suffix = dict()
			self.suffix2hash = dict()

			self.added_entries = dict()

			self.dirty = False

		def __just_add(self, hash, suffix):
			assert isinstance(suffix, int)

			self.hash2suffix[hash] = suffix
			self.suffix2hash[suffix] = hash

		def __add(self, hash, suffix, dependencies):
			self.__just_add(hash, suffix)

			# Record additions to know what needs to be appended
			self.added_entries[suffix] = [ (product.name, product.version) for product in dependencies ]

			self.dirty = True

		def suffix(self, hash):
			return self.hash2suffix[hash]

		def hash(self, suffix):
			return self.suffix2hash[suffix]

		def new_suffix(self, hash, dependencies):
			suffix = max(self.suffix2hash.iterkeys()) + 1 if self.suffix2hash else 0
			self.__add(hash, suffix, dependencies)
			return suffix

		def appendAdditionsToFile(self, fileObjectVer, fileObjectDep):
			# write hash<->suffix and dependency table updates
			for suffix, dependencies in self.added_entries.iteritems():
				fileObjectVer.write("%s\t%d\n" % (self.hash(suffix), suffix))
				for name, version in dependencies:
					fileObjectDep.write("%d\t%s\t%s\n" % (suffix, name, version))

			self.added_entries = []
			self.dirty = False

		@staticmethod
		def fromFile(fileObject):
			vm = VersionDbGit.VersionMap()
			for line in iter(fileObject.readline, ''):
				(hash, suffix) = line.strip().split()[:2]
				vm.__just_add(hash, int(suffix))

			return vm

	def __init__(self, dbdir):
		super(VersionDbGit, self).__init__(None, None)
		self.dbdir = dbdir

		self.versionMaps = dict()

	def __verfn(self, productName):
		return os.path.join("ver_db", productName + '.txt')

	def __depfn(self, productName):
		return os.path.join("dep_db", productName + '.txt')

	def getSuffix(self, productName, dependencies):
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
			suffix = vm.suffix(hash)
		except KeyError:
			suffix = vm.new_suffix(hash, dependencies)

		return suffix

	def getBuildId(self):
		"""Find the next unused git tag that matches the bNNNN pattern"""
		git = Git(self.dbdir)

		tags = git.tag('-l', 'b[0-9]*').split()

		btre = re.compile('^b[0-9]+$')
		btags = [ 0 ]
		btags += [ int(tag[1:]) for tag in tags if btre.match(tag) ]
		tag = "b%s" % (max(btags) + 1)

		return tag

	def commit(self, manifest):
		git = Git(self.dbdir)

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
		git.add(manfn)

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
	
	def __init__(self, build_dir, eups, product_fetcher, version_db, exclusion_resolver):
		self.build_dir = os.path.abspath(build_dir)

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
		productdir = os.path.join(self.build_dir, productName)
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
		build_dir = args.build_dir
		if not os.access(build_dir, os.W_OK):
			raise Exception("Directory '%s' does not exist or isn't writable." % build_dir)

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
			version_db = VersionDbGit(args.version_git_repo)
		else:
			version_db = VersionDbHash(args.sha_abbrev_len, eupsObj)

		product_fetcher = ProductFetcher(build_dir, args.repository_pattern, refs, args.no_fetch)
		p = BuildDirectoryConstructor(build_dir, eupsObj, product_fetcher, version_db, exclusion_resolver)

		#
		# Run the construction
		#
		manifest = p.construct(args.products)
		manifest.buildID = version_db.getBuildId() if not args.build_id else args.build_id
		version_db.commit(manifest)

		#
		# Store the result in build_dir/manifest.txt
		#
		manifestFn = os.path.join(build_dir, 'manifest.txt')
		with open(manifestFn, 'w') as fp:
			manifest.toFile(fp)
