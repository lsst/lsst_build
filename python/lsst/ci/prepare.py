#############################################################################
# Preparer

import os, os.path
import sys
import eups
import hashlib
import shutil
import time
import re
import pipes
import subprocess

import tsort

from .git import Git, GitError

class Preparer(object):
	def __init__(self, build_dir, refs, repository_patterns, sha_abbrev_len, no_pull, exclusions):
		self.build_dir = os.path.abspath(build_dir)
		self.refs = refs
		self.repository_patterns = repository_patterns.split('|')
		self.sha_abbrev_len = sha_abbrev_len
		self.no_pull = no_pull
		self.exclusions = exclusions

		self.deps = []
		self.versions = {}

	def _origin_candidates(self, product):
		""" Expand repository_patterns into URLs. """
		data = { 'product': product }
		return [ pat % data for pat in self.repository_patterns ]

	def _prepare(self, product):
		try:
			return self.versions[product]
		except KeyError:
			pass

		sys.stderr.write("%20s: " % product)
		t0 = time.time()

		productdir = os.path.join(self.build_dir, product)

		if os.path.isdir(productdir):
			# Check that the remote hasn't changed; remove the clone if it did
			# so it will get cloned again
			git = Git(productdir)
			origin = git('config', '--get', 'remote.origin.url')
			for candidate in self._origin_candidates(product):
				if origin == candidate:
					break
			else:
				shutil.rmtree(productdir)

		# Clone the product, if needed
		if not os.path.isdir(productdir):
			if os.path.exists(productdir):
				raise Exception("%s exists and is not a directory. Cannot clone a git repository there." % productdir)

			for url in self._origin_candidates(product):
				try:
					Git.clone(url, productdir)
					break
				except GitError as e:
					pass
			else:
				raise Exception("Failed to clone product '%s' from any of the offered repositories" % product)

		git = Git(productdir)

		if not self.no_pull:
			# fetch updates from the remote
			git("fetch", "origin", "--prune", "--tags")

		# Check out the first matching requested ref
		for ref in self.refs:
			try:
				# Presume the ref is a tag
				sha1 = git('rev-parse', '--verify', '-q', 'refs/tags/%s' % ref)
			except GitError:
				try:
					# Presume it's a branch
					sha1 = git('rev-parse', '--verify', '-q', 'origin/%s' % ref)
					ref = sha1
				except GitError:
					try:
						# Presume it's a straight SHA1
						sha1 = git('rev-parse', '--verify', '-q', 'dummy-g%s' % ref)
					except GitError:
						continue

			# avoid checking out if already checked out (for speed)
			try:
				checkout = git('rev-parse', 'HEAD') != sha1
			except GitError:
				checkout = True

			if checkout:
				git('checkout', '-f', ref)
			break
		else:
			raise Exception("None of the specified refs exist in product '%s'" % product)

		# Reset and clean up the working directory
		git("reset", "--hard")
		git("clean", "-d", "-f", "-q")

		print >>sys.stderr, " ok (%.1f sec)." % (time.time() - t0)

		# Parse the table file to discover dependencies
		dep_vers = []
		table_fn = os.path.join(productdir, 'ups', '%s.table' % product)
		if os.path.isfile(table_fn):
			# Choose which dependencies to prepare
			product_deps = []
			for dep in eups.table.Table(table_fn).dependencies(eups.Eups()):
				if dep[1] == True and self._is_excluded(dep[0].name, product):	# skip excluded optionals
					continue;
				if dep[0].name == "implicitProducts": continue;		# skip implicit products
				product_deps.append(dep[0].name)

			# Recursively prepare the chosen dependencies
			for dep_product in product_deps:
				dep_ver = self._prepare(dep_product)[0]
				dep_vers.append(dep_ver)
				self.deps.append((dep_product, product))

		# Construct EUPS version
		version = self._construct_version(productdir, ref, dep_vers)

		# Store the result
		self.versions[product] = (version, sha1)

		return self.versions[product]

	def _construct_version(self, productdir, ref, dep_versions):
		""" Return a standardized XXX+YYY EUPS version, that includes the dependencies. """
		q = pipes.quote
		cmd ="cd %s && pkgbuild -f git_version %s" % (q(productdir), q(ref))
		ver = subprocess.check_output(cmd, shell=True).strip()

		if dep_versions:
			deps_sha1 = self._depver_hash(dep_versions)
			return "%s+%s" % (ver, deps_sha1)
		else:
			return ver

	def _is_excluded(self, dep, product):
		""" Check if dependency 'dep' is excluded for product 'product' """
		try:
			rc = self.exclusion_regex_cache
		except AttributeError:
			rc = self.exclusion_regex_cache = dict()

		if product not in rc:
			rc[product] = [ dep_re for (dep_re, prod_re) in self.exclusions if prod_re.match(product) ]
		
		for dep_re in rc[product]:
			if dep_re.match(dep):
				return True

		return False

	def _depver_hash(self, versions):
		""" Return a standardized hash of the list of versions """
		return hashlib.sha1('\n'.join(sorted(versions))).hexdigest()[:self.sha_abbrev_len]

	@staticmethod
	def run(args):
		# Ensure build directory exists and is writable
		build_dir = args.build_dir
		if not os.access(build_dir, os.W_OK):
			raise Exception("Directory '%s' does not exist or isn't writable." % build_dir)

		# Add 'master' to list of refs, if not there already
		refs = args.ref
		if 'master' not in refs:
			refs.append('master')

		# Load exclusion map
		exclusions = []
		if args.exclusion_map:
			with open(args.exclusion_map) as fp:
				for line in fp:
					line = line.strip()
					if not line or line.startswith("#"):
						continue
					(dep_re, prod_re) = line.split()[:2]
					exclusions.append((re.compile(dep_re), re.compile(prod_re)))

		# Prepare products
		p = Preparer(build_dir, refs, args.repository_pattern, args.sha_abbrev_len, args.no_pull, exclusions)
		for product in args.products:
			p._prepare(product)

		# Topologically sort the result
		products = tsort.tsort(p.deps)
		print '# %-23s %-41s %-30s' % ("product", "SHA1", "Version")
		for product in products:
			print '%-25s %-41s %-30s' % (product, p.versions[product][1], p.versions[product][0])

