#############################################################################
# Builder
import eups

import collections
import subprocess
import textwrap
import os, stat, sys, shutil
import pipes
import time
import eups, eups.tags
import re

Product = collections.namedtuple('Product', ['name', 'sha1', 'version', 'dependencies'])

e = eups.Eups()

class Builder(object):
	def __init__(self, build_dir, manifest):
		# Parse the manifest
		# FIXME: the constructor should taka a _parsed_ manifest
		
		self.build_dir = build_dir
		self.BUILD = None

		self.products = collections.OrderedDict()
		with open(manifest) as fp:
			varre = re.compile('^(\w+)=(.*)$')
			for line in fp:
				line = line.strip()
				if not line:
					continue
				if line.startswith('#'):
					continue

				# Look for variable assignments
				m = varre.match(line)
				if m:
					setattr(self, m.group(1), m.group(2))
					continue

				arr = line.split()
				if len(arr) == 4:
					(name, sha1, version, deps) = arr
					deps = deps.split(',')
				else:
					(name, sha1, version) = arr
					deps = []

				self.products[name] = Product(name, sha1, version, deps)

	def build_all(self):
		# Make sure EUPS knows about the BUILD tag
		if self.BUILD:
			global e
			tags = eups.tags.Tags()
			tags.loadFromEupsPath(e.path)
			if self.BUILD not in tags.getTagNames():
				tags.registerTag(self.BUILD)
				tags.saveGlobalTags(e.path[0])
				e = eups.Eups()			# reload new tags

		# Build all products
		for name in self.products:
			if not self._build(name):
				return False

	def _flatten_dependencies(self, product, res=set()):
		res.update(product.dependencies)
		for dep in product.dependencies:
			self._flatten_dependencies(self.products[dep], res)
		return res

	def _tag_product(self, name, version, tag):
		if tag:
			e.declare(name, version, tag=tag)

	def _build(self, name):
		product = self.products[name]

		# test if the product is already installed, skip build if so.
		try:
			e.getProduct(product.name, product.version)
			sys.stderr.write('%20s: %s (already installed).\n' % (product.name, product.version))
			self._tag_product(product.name, product.version, self.BUILD)
			return True
		except eups.ProductNotFound:
			pass

		# run the eupspkg sequence for the product
		productdir = os.path.abspath(os.path.join(self.build_dir, product.name))
		buildscript = os.path.join(productdir, '_build.sh')
		logfile = os.path.join(productdir, '_build.log')

		# construct the tags file with exact dependencies
		setups = [ 
			"\t%(name)-20s %(version)s" % self.products[dep]._asdict()
				for dep in self._flatten_dependencies(product)
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
			""" %	{
					'product': product.name,
					'version': product.version,
					'sha1' : product.sha1,
					'productdir' : productdir,
					'setups': '\n			'.join(setups)
				}
			)

			fp.write(text)

		# Make executable (equivalent of 'chmod +x $buildscript')
		st = os.stat(buildscript)
		os.chmod(buildscript, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

		# Run the build script
		sys.stderr.write('%20s: ' % name)
		progress_bar = product.version + " "	# cute attack: display the version string as progress bar, character by character
		with open(logfile, 'w') as logfp:
			# execute the build file from the product directory, capturing the output and return code
			t0 = t = time.time()
			process = subprocess.Popen(buildscript, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=productdir)
			for line in iter(process.stdout.readline,''):
				logfp.write(line)
				
				# throttle progress reporting
				t1 = time.time()
				while t <= t1:
					if progress_bar:
						sys.stderr.write(progress_bar[0])
						progress_bar = progress_bar[1:]
					else:
						sys.stderr.write('.')
					t += 2
		if progress_bar:
			sys.stderr.write(progress_bar)

		retcode = process.poll()
		if retcode:
			print >>sys.stderr, "ERROR (%d sec)." % (time.time() - t0)
			print >>sys.stderr, "*** error building product %s." % product.name
			print >>sys.stderr, "*** exit code = %d" % retcode
			print >>sys.stderr, "*** log is in %s" % logfile
			print >>sys.stderr, "*** last few lines:"

			os.system("tail -n 10 %s | sed -e 's/^/:::::  /'" % pipes.quote(logfile))

			return False
		else:
			# copy the log file to product directory
			
			productDir = e.getProduct(product.name, product.version).dir
			shutil.copy2(logfile, productDir)

			self._tag_product(product.name, product.version, self.BUILD)

			print >>sys.stderr, "ok (%.1f sec)." % (time.time() - t0)

		return True

	@staticmethod
	def run(args):
		# Ensure build directory exists and is writable
		build_dir = args.build_dir
		if not os.access(build_dir, os.W_OK):
			raise Exception("Directory '%s' does not exist or isn't writable." % build_dir)

		# Build products
		b = Builder(build_dir, args.manifest)
		b.build_all()

	pass

