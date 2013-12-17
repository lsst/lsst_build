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

Product = collections.namedtuple('Product', ['name', 'sha1', 'version'])

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
				(name, sha1, version) = line.split()[:3]
				self.products[name] = Product(name, sha1, version)

	def build_all(self):
		for name in self.products:
			if not self._build(name):
				return False

	def _build(self, name):
		product = self.products[name]

		# test if the product is already installed, skip build if so.
		e = eups.Eups()
		try:
			e.getProduct(product.name, product.version)
			sys.stderr.write('%15s: ok (already installed).\n' % product.name)
			return True
		except eups.ProductNotFound:
			pass

		# run the pkgbuild sequence for the product
		productdir = os.path.abspath(os.path.join(self.build_dir, product.name))
		buildscript = os.path.join(productdir, '_build.sh')
		logfile = os.path.join(productdir, '_build.log')

		# compute the setup invocations for dependencies
		setups = []
		for dep in self.products.itervalues():
			if dep.name == product.name:
				break
			setups.append("setup --type=build --j %(name)-15s %(version)s" % dep._asdict())

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
			env PRODUCT=%(product)s VERSION=%(version)s   pkgbuild eups_declare
			""" %	{
					'product': product.name,
					'version': product.version,
					'productdir' : productdir,
					'setups': '\n			'.join(setups)
				}
			)

			fp.write(text)

		# Make executable (equivalent of 'chmod +x $buildscript')
		st = os.stat(buildscript)
		os.chmod(buildscript, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

		# Run the build script
		sys.stderr.write('%15s: ' % name)
		with open(logfile, 'w') as logfp:
			# execute the build file from the product directory, capturing the output and return code
			t0 = t = time.time()
			process = subprocess.Popen(buildscript, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=productdir)
			for line in iter(process.stdout.readline,''):
				logfp.write(line)
				
				# throttle progress reporting
				t1 = time.time()
				if (t1 - t) >= 1:
					sys.stderr.write('+')
					t = t1

		retcode = process.poll()
		if retcode:
			print >>sys.stderr, " ERROR (%d sec)." % (time.time() - t0)
			print >>sys.stderr, "*** error building product %s." % product.name
			print >>sys.stderr, "*** exit code = %d" % retcode
			print >>sys.stderr, "*** log is in %s" % logfile
			print >>sys.stderr, "*** last few lines:"

			os.system("tail -n 10 %s | sed -e 's/^/:::::  /'" % pipes.quote(logfile))

			return False
		else:
			print >>sys.stderr, " ok (%.1f sec)." % (time.time() - t0)

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

