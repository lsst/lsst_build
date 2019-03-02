"""LSST build automation

See:
https://developer.lsst.io/
"""

from setuptools import setup
from codecs import open
from os import path

here = path.abspath(path.dirname(__file__))
with open(path.join(here, 'README.md'), encoding='utf-8') as f:
    long_description = f.read()

f8 = 'flake8>=3.7.7,<4'

setup(
    name='lsst_build',
    version='1.0.0',
    description='LSST build automation',
    long_description=long_description,
    url='https://github.com/lsst/lsst_build',
    author='Large Synoptic Survey Telescope Data Management Team',
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Intended Audience :: Developers',
        'Topic :: Software Development :: Build Tools',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3.6',
    ],
    keywords='lsst lsst_build lsstsw eups eupspkg',
    packages=['lsst', 'lsst.ci'],
    package_dir={'lsst': 'python/lsst'},
    install_requires=[
        'pyyaml>=3.13,<4',
    ],
    setup_requires=[
        'pytest-runner>=4.4,<5',
    ],
    tests_require=[
        'pytest>=4.3,<5',
        'pytest-flake8>=1.0.4,<2',
        'pytest-pythonpath>=0.7,<1',
        'pytest-mock>=1.10,<2',
        f8,
    ],
    extras_require={
        'travis': [
            f8
        ],
    },
    scripts=[
        'bin/lsst-build',
    ],
)
