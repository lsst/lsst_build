from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("lsst_build")
except PackageNotFoundError:
    # Package is not installed, e.g. when run directly from a source
    # checkout via PYTHONPATH (as when deployed through EUPS).
    __version__ = "0.0.0"
