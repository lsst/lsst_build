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
        'Programming Language :: Python :: 2',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.4',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
    ],
    keywords='lsst lsst_build lsstsw eups eupspkg',
    packages=['lsst', 'lsst.ci'],
    package_dir={'lsst': 'python/lsst'},
    install_requires=[
        'pyyaml',
    ],
    setup_requires=[
        'pytest-runner>=2.11.1,<3',
    ],
    tests_require=[
        'pytest>=3,<4',
        'pytest-flake8>=0.8.1,<1',
        'pytest-pythonpath',
        'pytest-mock',
        'flake8==3.7.7'
    ],
    scripts=[
        'bin/lsst-build',
    ],
)
