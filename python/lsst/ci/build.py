from __future__ import print_function
#############################################################################
# Builder
import eups

import subprocess
import textwrap
import os, stat, sys, shutil
import pipes
import time
import eups.tags
import contextlib
import datetime

from .prepare import Manifest

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
                    print("ERROR (%d sec)." % elapsedTime, file=self.out)
                    print("*** error building product %s." % self.product.name, file=self.out)
                    print("*** exit code = %d" % retcode, file=self.out)
                    print("*** log is in %s" % logfile, file=self.out)
                    print("*** last few lines:", file=self.out)

                    os.system("tail -n 10 %s | sed -e 's/^/:::::  /'" % pipes.quote(logfile))
                else:
                    print("ok (%.1f sec)." % elapsedTime, file=self.out)

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
    def __init__(self, build_dir, manifest, progress, eups):
        self.build_dir = build_dir
        self.manifest = manifest
        self.progress = progress
        self.eups = eups

    def _tag_product(self, name, version, tag):
        if tag:
            self.eups.declare(name, version, tag=tag)

    def _build_product(self, product, progress):
        # run the eupspkg sequence for the product
        #
        productdir = os.path.abspath(os.path.join(self.build_dir, product.name))
        buildscript = os.path.join(productdir, '_build.sh')
        logfile = os.path.join(productdir, '_build.log')
        eupsdir = eups.productDir("eups")
        eupspath = os.environ["EUPS_PATH"]

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

            # define the setup command, but preserve EUPS_PATH
            . "%(eupsdir)s/bin/setups.sh"
            export EUPS_PATH="%(eupspath)s"

            cd "%(productdir)s"

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
            echo "Setting up environment with EUPS"
            setup --vro=_build.tags -r .
            set -x

            # build
            eupspkg PRODUCT=%(product)s VERSION=%(version)s FLAVOR=generic config
            eupspkg PRODUCT=%(product)s VERSION=%(version)s FLAVOR=generic build
            if [ -d  tests/.tests ] && \
                [ "`ls tests/.tests/*\.failed 2> /dev/null | wc -l`" -ne 0 ]; then
                echo "*** Failed unit tests."; 
                exit 1 
            fi
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
                    'setups': '\n            '.join(setups),
                    'eupsdir': eupsdir,
                    'eupspath': eupspath,
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
                line = "[%sZ] %s" % (datetime.datetime.utcnow().isoformat(), line)
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
        build_dir = args.build_dir
        if not os.access(build_dir, os.W_OK):
            raise Exception("Directory '%s' does not exist or isn't writable." % build_dir)

        # Build products
        eupsObj = eups.Eups()

        progress = ProgressReporter(sys.stderr)

        manifestFn = os.path.join(build_dir, 'manifest.txt')
        with open(manifestFn) as fp:
            manifest = Manifest.fromFile(fp)

        b = Builder(build_dir, manifest, progress, eupsObj)
        retcode = b.build()
        exit(retcode == 0)
