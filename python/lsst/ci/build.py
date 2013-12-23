#############################################################################
# Builder
import eups

import collections
import subprocess
import textwrap
import os, stat, sys
import pipes
import time
import eups

Product = collections.namedtuple('Product', ['name', 'sha1', 'version', 'dependencies'])

class Builder(object):
	def __init__(self, build_dir, manifest):
		# Parse the manifest
		# FIXME: the constructor should taka a _parsed_ manifest
		
		self.build_dir = build_dir

		self.products = collections.OrderedDict()
		with open(manifest) as fp:
			for line in fp:
				line = line.strip()
				if line.startswith('#'):
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
		for name in self.products:
			if not self._build(name):
				return False

	def _flatten_dependencies(self, product, res=set()):
		res.update(product.dependencies)
		for dep in product.dependencies:
			self._flatten_dependencies(self.products[dep], res)
		return res

	def _build(self, name):
		product = self.products[name]

		# test if the product is already installed, skip build if so.
		e = eups.Eups()
		try:
			e.getProduct(product.name, product.version)
			sys.stderr.write('%20s: %s (already installed).\n' % (product.name, product.version))
			return True
		except eups.ProductNotFound:
			pass

		# run the pkgbuild sequence for the product
		productdir = os.path.abspath(os.path.join(self.build_dir, product.name))
		buildscript = os.path.join(productdir, '_build.sh')
		logfile = os.path.join(productdir, '_build.log')

		# compute the setup invocations for dependencies
		setups = [ 
			"setup --type=build -j %(name)-20s %(version)s" % self.products[dep]._asdict()
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
			git clean -d -f -q -e '_build.*'

			# prepare
			env PRODUCT=%(product)s VERSION=%(version)s   pkgbuild prep

			# setup dependencies (if any)
			%(setups)s
			setup -j -r .

			# build
			env PRODUCT=%(product)s VERSION=%(version)s   pkgbuild config
			env PRODUCT=%(product)s VERSION=%(version)s   pkgbuild build
			env PRODUCT=%(product)s VERSION=%(version)s   pkgbuild install

			# declare to EUPS
			env PRODUCT=%(product)s VERSION=%(version)s   pkgbuild eups_declare

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

