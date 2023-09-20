#############################################################################
# Builder
import contextlib
import datetime
import os
import pipes
import select
import shutil
import stat
import subprocess
import sys
import textwrap
import time
from typing import List, TextIO

import eups
import eups.tags
import yaml

from . import models
from .prepare import Manifest


def product_representer(dumper, data):
    """Return YAML serialization."""
    obj = {
        "name": str(data.name),
        "sha1": str(data.sha1),
        "version": str(data.version),
    }
    return dumper.represent_mapping("tag:yaml.org,2002:map", obj)


yaml.add_representer(models.Product, product_representer)


def declare_eups_tag(tag, eups_obj):
    """Declare a new EUPS tag.

    FIXME: Not sure if this is the right way to programmatically
           define and persist a new tag. Ask RHL.
    """
    tags = eups_obj.tags
    if tag not in tags.getTagNames():
        tag = str(tag)
        tags.registerTag(tag)
        tags.saveGlobalTags(eups_obj.path[0])


class ProgressReporter:
    """Class that that displays the version string as a progress bar as an
    indicator of liveness.

    Parameters
    ----------
    out
        file this class will write the progress to
    product
        the product which we are reporting progress on
    """

    class ProductProgressReporter:
        """Progress bar helper class."""

        def __init__(self, out_file_obj: TextIO, product: models.Product):
            self.out = out_file_obj
            self.product = product

        def _build_started(self):
            self.out.write("%20s: " % self.product.name)
            self.out.flush()
            self.progress_bar = self.product.version + " "
            self.t0 = self.t = time.time()

        def report_progress(self):
            # throttled progress reporting.
            #
            # Write out the version string as a progress bar, character by
            # character, and then continue with dots.
            #
            # Throttle updates to one character every 2 seconds.
            t1 = time.time()
            while self.t <= t1:
                if self.progress_bar:
                    self.out.write(self.progress_bar[0])
                    self.progress_bar = self.progress_bar[1:]
                else:
                    self.out.write(".")

                self.out.flush()
                self.t += 2

        def report_result(self, retcode, logfile):
            # Make sure we write out the full version string, even if the build
            # ended quickly.
            if self.progress_bar:
                self.out.write(self.progress_bar)
                self.out.flush()

            # If logfile is None, the product was already installed.
            if logfile is None:
                self.out.write("(already installed).\n")
                self.out.flush()
            else:
                elapsed_time = time.time() - self.t0
                if retcode:
                    print("ERROR (%d sec)." % elapsed_time, file=self.out)
                    print("*** error building product %s." % self.product.name, file=self.out)
                    print("*** exit code = %d" % retcode, file=self.out)
                    print("*** log is in %s" % logfile, file=self.out)
                    print("*** last few lines:", file=self.out)

                    os.system("tail -n 10 %s | sed -e 's/^/:::::  /'" % pipes.quote(logfile))
                else:
                    print("ok (%.1f sec)." % elapsed_time, file=self.out)
                self.out.flush()

            self.product = None

        def _finalize(self):
            # Usually called only when an exception is thrown.
            if self.product is not None:
                self.out.write("\n")
                self.out.flush()

    def __init__(self, out_file_obj):
        self.out = out_file_obj

    @contextlib.contextmanager
    def new_build(self, product):
        progress = ProgressReporter.ProductProgressReporter(self.out, product)
        progress._build_started()
        yield progress
        progress._finalize()


class Builder:
    """Class that builds and installs all products in a manifest.

    The result is tagged with the `Manifest`s build ID, if any.

    Parameters
    ----------
    build_dir
        the root directory of the build
    manifest
        the manifest we are building against
    progress
        the `ProgressReporter` reporting for this build
    eups
        an eups object for eups operations (e.g. discovering product info)
    """

    def __init__(self, build_dir: str, manifest: Manifest, progress: ProgressReporter, eups: eups.Eups):
        self.build_dir = build_dir
        self.manifest = manifest
        self.progress = progress
        self.eups = eups
        self.built: List[models.Product] = []
        self.failed_at = None

    def _tag_product(self, name, version, tag):
        if tag:
            self.eups.declare(name, version, tag=str(tag))

    def _build_product(self, product, progress):
        # run the eupspkg sequence for the product
        #
        productdir = os.path.abspath(os.path.join(self.build_dir, product.name))
        buildscript = os.path.join(productdir, "_build.sh")
        logfile = os.path.join(productdir, "_build.log")
        eupsdir = eups.productDir("eups")
        eupspath = os.environ["EUPS_PATH"]

        # construct the tags file with exact dependencies
        setups = [
            "\t%-20s %s" % (dep.name, dep.version)
            for dep in self.manifest.product_index.flat_dependencies(product)
        ]

        # create the buildscript
        with open(buildscript, "w", encoding="utf-8") as fp:
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
                [ "`ls tests/.tests/*.failed 2> /dev/null | wc -l`" -ne 0 ]; then
                echo "*** Failed unit tests.";
                exit 1
            fi
            eupspkg PRODUCT=%(product)s VERSION=%(version)s FLAVOR=generic install

            # declare to EUPS
            eupspkg PRODUCT=%(product)s VERSION=%(version)s FLAVOR=generic decl

            # explicitly append SHA1 to pkginfo
            echo SHA1=%(sha1)s >> $(eups list %(product)s %(version)s -d)/ups/pkginfo
            """
                % {
                    "product": product.name,
                    "version": product.version,
                    "sha1": product.sha1,
                    "productdir": productdir,
                    "setups": "\n            ".join(setups),
                    "eupsdir": eupsdir,
                    "eupspath": eupspath,
                }
            )

            fp.write(text)

        # Make executable (equivalent of 'chmod +x $buildscript')
        st = os.stat(buildscript)
        os.chmod(buildscript, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # Run the build script
        with open(logfile, "w", encoding="utf-8") as logfp:
            # execute the build file from the product directory, capturing the
            # output and return code.
            process = subprocess.Popen(
                buildscript, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=productdir
            )
            select_list = [process.stdout]
            buf = b""
            while True:
                # Wait up to 2 seconds for output
                ready_to_read, _, _ = select.select(select_list, [], [], 2)
                if ready_to_read:
                    c = process.stdout.read(1)
                    buf += c
                    if (c == b"" or c == b"\n") and buf:
                        line = "[%sZ] %s" % (datetime.datetime.utcnow().isoformat(), buf.decode())
                        logfp.write(line)
                        buf = b""
                    # Ready to read but nothing there means end of file
                    if c == b"":
                        break
                progress.report_progress()

        retcode = process.poll()
        if not retcode:
            # copy the log file to product directory
            eups_prod = self.eups.getProduct(product.name, product.version)
            shutil.copy2(logfile, eups_prod.dir)
        else:
            eups_prod = None

        return (eups_prod, retcode, logfile)

    def _build_product_if_needed(self, product):
        # Build a product if it hasn't been installed already
        #
        with self.progress.new_build(product) as progress:
            try:
                # skip the build if the product has been installed
                eups_prod, retcode, logfile = self.eups.getProduct(product.name, product.version), 0, None
            except eups.ProductNotFound:
                eups_prod, retcode, logfile = self._build_product(product, progress)

            if eups_prod is not None and self.manifest.build_id not in eups_prod.tags:
                self._tag_product(product.name, product.version, self.manifest.build_id)

            progress.report_result(retcode, logfile)

        return retcode == 0

    def build(self):
        # Make sure EUPS knows about the build_id tag
        if self.manifest.build_id:
            declare_eups_tag(self.manifest.build_id, self.eups)

        # Build all products
        for product in self.manifest.product_index.values():
            if not self._build_product_if_needed(product):
                self.failed_at = product
                return False
            self.built.append(product)

    def rm_status(self):
        if os.path.isfile(self.status_file()):
            os.remove(self.status_file())

    def status_file(self):
        return os.path.join(self.build_dir, "status.yaml")

    def write_status(self):
        status = {
            "built": self.built,
        }

        if self.failed_at is not None:
            status["failed_at"] = self.failed_at

        with open(self.status_file(), "w", encoding="utf-8") as sf:
            yaml.dump(status, sf, encoding="utf-8", default_flow_style=False)

    @staticmethod
    def run(args):
        # Ensure build directory exists and is writable
        build_dir = args.build_dir
        if not os.access(build_dir, os.W_OK):
            raise Exception("Directory '%s' does not exist or isn't writable." % build_dir)

        # Build products
        eups_obj = eups.Eups()

        progress = ProgressReporter(sys.stdout)

        manifest_fn = os.path.join(build_dir, "manifest.txt")
        with open(manifest_fn, encoding="utf-8") as fp:
            manifest = Manifest.from_file(fp)

        b = Builder(build_dir, manifest, progress, eups_obj)
        b.rm_status()
        retcode = b.build()
        b.write_status()
        sys.exit(retcode == 0)
