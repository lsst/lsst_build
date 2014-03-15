#!/bin/bash
#
# ********** DONT RUN THIS UNLESS YOU UNDERSTAND WHAT IT DOES ********
# **********             SERIOUS DAMAGE MAY OCCUR             ********
#
# Recursively build all product, starting with top-level ones, build EUPSPKG
# packages, and declare them as current
#
# Expected to be run in a directory with:
#   + A subdirectory named ./build, where the source code will be cloned
#   + A git package named 'versiondb', that keeps the product version database
#
# The versiondb repository should be cloned from:
#
#   git@git.lsstcorp.org/LSST/DMS/devenv/versiondb.git
#
# For creation, use:
#
# 	(mkdir versiondb; cd versiondb; git init; mkdir dep_db ver_db manifests)
#

set -e

# PRODUCTS="lsst_distrib git anaconda"
PRODUCTS="sconsUtils"

#
# Set the type of package to be created, and repository locations
#
export EUPSPKG_SOURCE=package # (use 'package' for public releases, use 'git' for development releases)
export EUPS_PKGROOT=/lsst/home/mjuric/public_html/pkgs
BASE='git://git.lsstcorp.org/LSST'
export REPOSITORY_PATTERN="$BASE/DMS/%(product)s.git|$BASE/DMS/devenv/%(product)s.git|$BASE/DMS/testdata/%(product)s.git|$BASE/external/%(product)s.git"

#
# Prepare build
#
[[ -z $NOPUSH ]] && (cd versiondb && git pull)
./bin/lsst-build prepare --exclusion-map=exclusions.txt ./build --version-git-repo=versiondb $PRODUCTS
[[ -z $NOPUSH ]] && (cd versiondb && git push)
eval "$(grep -E '^BUILD=' ./build/manifest.txt)"
echo "*************** BUILD = $BUILD: "

#
# Execute build
#
./bin/lsst-build build ./build

#
# Create the distribution packages
#
#for product in $PRODUCTS; do
#	eups distrib create --server-dir=$EUPS_PKGROOT -f generic -d eupspkg -t $BUILD $product
#done

#
# Declare the build tag, and declare it current
#
#eups distrib declare --server-dir=$EUPS_PKGROOT -t $BUILD
#sed -r 's|EUPS distribution [^ ]+ version list. Version 1.0|EUPS distribution current version list. Version 1.0|' \
#	$EUPS_PKGROOT/tags/$BUILD.list > $EUPS_PKGROOT/tags/current.list
