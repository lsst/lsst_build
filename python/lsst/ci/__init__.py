# This file is part of lsst_build.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# Use of this source code is governed by a 3-clause BSD-style
# license that can be found in the LICENSE file.

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("lsst_build")
except PackageNotFoundError:
    # Package is not installed, e.g. when run directly from a source
    # checkout via PYTHONPATH (as when deployed through EUPS).
    __version__ = "0.0.0"
