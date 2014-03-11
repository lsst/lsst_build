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

from .prepare import Product, Manifest

def declareEupsTag(tag, eupsObj):
	""" Declare a new EUPS tag
	    FIXME: Not sure if this is the right way to programmatically
	           define and persist a new tag. Ask RHL.
	"""
	tags = eupsObj.tags
	if tag not in tags.getTagNames():
		tags.registerTag(tag)
		tags.saveGlobalTags(e.path[0])

class Builder(object):
	"""Class that builds and installs all products in a manifest.
	
	   The result is tagged with the `Manifest`s build ID, if any.
	"""
	def __init__(self, build_dir, manifest, eups):
		self.build_dir = build_dir
		self.manifest = manifest
		self.eups = eups

	def _tag_product(self, name, version, tag):
		if tag:
			self.eups.declare(name, version, tag=tag)

	def _build_product(self, product):
		# run the eupspkg sequence for the product
		productdir = os.path.abspath(os.path.join(self.build_dir, product.name))
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
		sys.stderr.write('%20s: ' % product.name)
		progress_bar = product.version + " "	# cute attack: display the version string as progress bar, character by character
		with open(logfile, 'w') as logfp:
			# execute the build file from the product directory, capturing the output and return code
			t0 = t = time.time()
			process = subprocess.Popen(buildscript, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=productdir)
			for line in iter(process.stdout.readline, ''):
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
			
			productDir = self.eups.getProduct(product.name, product.version).dir
			shutil.copy2(logfile, productDir)

			self._tag_product(product.name, product.version, self.manifest.buildID)

			print >>sys.stderr, "ok (%.1f sec)." % (time.time() - t0)

		return True

	def _build_product_if_needed(self, product):
		# test if the product is already installed, skip build if so.
		try:
			self.eups.getProduct(product.name, product.version)
			sys.stderr.write('%20s: %s (already installed).\n' % (product.name, product.version))
			self._tag_product(product.name, product.version, self.manifest.buildID)

			return True
		except eups.ProductNotFound:
			pass

		return self._build_product(product)

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
		build_dir = args.build_dir
		if not os.access(build_dir, os.W_OK):
			raise Exception("Directory '%s' does not exist or isn't writable." % build_dir)

		# Build products
		eupsObj = eups.Eups()

		manifestFn = os.path.join(build_dir, 'manifest.txt')
		with open(manifestFn) as fp:
			manifest = Manifest.fromFile(fp)

		b = Builder(build_dir, manifest, eupsObj)
		b.build()
