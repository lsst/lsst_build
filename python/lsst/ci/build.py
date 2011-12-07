#############################################################################
# Builder
import collections
import textwrap
import os

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
			self._build(name)

	def _build(self, name):
		product = self.products[name]

		# run the pkgbuild sequence for the product
		buildfile = os.path.join(self.build_dir, product.name, '_build.sh')

		# compute the setup invocations for dependencies
		setups = []
		for dep in self.products.itervalues():
			if dep.name == product.name:
				break
			setups.append("setup --type=build --j %(name)-15s %(version)s" % dep._asdict())

		with open(buildfile, 'w') as fp:
			text = textwrap.dedent(
			"""\
			#!/bin/bash
			
			env PRODUCT=%(product)s VERSION=%(version)s   pkgbuild prep
			
			%(setups)s
			
			setup -j -r .
			
			env PRODUCT=%(product)s VERSION=%(version)s   pkgbuild config
			env PRODUCT=%(product)s VERSION=%(version)s   pkgbuild build
			env PRODUCT=%(product)s VERSION=%(version)s   pkgbuild install
			""" %	{
					'product': product.name,
					'version': product.version,
					'setups': '\n			'.join(setups)
				}
			)

			fp.write(text)

		# execute the build file
		TODO

		print buildfile, "constructed."

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

