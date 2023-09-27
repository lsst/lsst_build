"""Test ProductFetcher class"""

import asyncio
import os
import sys
from unittest.mock import Mock

import pytest

# Pretend eups is available.
sys.modules["eups"] = Mock()
sys.modules["eups.tags"] = Mock()

import lsst.ci.git  # noqa: E402
from lsst.ci.git import GitError  # noqa: E402
from lsst.ci.models import DEFAULT_BRANCH_NAME  # noqa: E402
from lsst.ci.prepare import ProductFetcher, RemoteError  # noqa: E402


@pytest.fixture
def fixture_dir():
    """Return the data directory."""
    d = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(d, "data")


@pytest.fixture
def repos_yaml_good(fixture_dir):
    """Return path to good yaml file."""
    return os.path.join(fixture_dir, "good", "repos.yaml")


@pytest.fixture
def repos_yaml_bad(fixture_dir):
    """Return path to bad yaml file."""
    return os.path.join(fixture_dir, "bad", "repos.yaml")


@pytest.fixture
def test_product():
    """Return representative product name."""
    return "base"


def test_fetch(tmpdir, repos_yaml_good, test_product):
    """Clone git repo from a valid repos.yaml"""
    refs = [DEFAULT_BRANCH_NAME]
    product_fetcher = ProductFetcher(tmpdir, repos_yaml_good, None, no_fetch=False)

    ref, sha1 = asyncio.run(product_fetcher.fetch(test_product, refs))
    assert os.path.exists(os.path.join(tmpdir, test_product, ".git"))
    assert ref is not None
    assert sha1 is not None


def test_fetch_bad_remote(tmpdir, repos_yaml_bad, test_product):
    """Fail to clone when there isn't a valid remote in repos.yaml"""
    refs = [DEFAULT_BRANCH_NAME]
    product_fetcher = ProductFetcher(tmpdir, repos_yaml_bad, None, no_fetch=False)

    with pytest.raises(RemoteError) as e:
        asyncio.run(product_fetcher.fetch(test_product, refs))

    assert len(e.value.git_errors) == 1


def test_fetch_bad_git_checkout(tmpdir, repos_yaml_good, mocker, test_product):
    """Fail when git command errors on top of an existing clone"""
    refs = [DEFAULT_BRANCH_NAME]
    product_fetcher = ProductFetcher(tmpdir, repos_yaml_good, None, no_fetch=False)

    # first call is to setup a pre-existing clone
    ref, sha1 = asyncio.run(product_fetcher.fetch(test_product, refs))
    assert ref is not None
    assert sha1 is not None
    assert os.path.exists(os.path.join(tmpdir, test_product, ".git"))

    # Note that we are mocking out the import into lsst.ci.prepare
    mocker.patch("lsst.ci.prepare.Git.checkout")
    lsst.ci.prepare.Git.checkout.side_effect = GitError(42, "cmd", "stdout", "stderr")

    with pytest.raises(GitError) as e:
        asyncio.run(product_fetcher.fetch(test_product, refs))

    assert e.value.returncode == 42


def test_fetch_bad_remote_retry(tmpdir, repos_yaml_bad, mocker, test_product):
    """Verify that cloning is retried when upon failure"""
    tries = 3
    refs = [DEFAULT_BRANCH_NAME]
    product_fetcher = ProductFetcher(tmpdir, repos_yaml_bad, None, no_fetch=False, tries=tries)

    # this is not BDDish and dependent on internal implimentation details
    mocker.spy(product_fetcher, "_fetch")

    with pytest.raises(RemoteError) as e:
        asyncio.run(product_fetcher.fetch(test_product, refs))

    # No matter the number of tries, the exception from the last attempt is
    # propegated. In this case, the RemoteError records the number of remotes
    # failed for the most recent iteration only, which should always be 1 when
    # repos.yaml is in use.
    assert len(e.value.git_errors) == 1
    assert product_fetcher._fetch.call_count == tries


def test_fetch_bad_git_checkout_retry(tmpdir, repos_yaml_good, mocker, test_product):
    """Verify that repo is recloned after checkout on an existing clone
    fails.
    """
    tries = 3
    refs = [DEFAULT_BRANCH_NAME]

    product_fetcher = ProductFetcher(tmpdir, repos_yaml_good, None, no_fetch=False, tries=tries)

    # first call is to setup a pre-existing clone
    ref, sha1 = asyncio.run(product_fetcher.fetch(test_product, refs))
    assert ref is not None
    assert sha1 is not None
    assert os.path.exists(os.path.join(tmpdir, test_product, ".git"))

    # this is not BDDish and dependent on internal implimentation details
    mocker.spy(product_fetcher, "_fetch")

    # Note that we are mocking out the import into lsst.ci.prepare
    mocker.patch("lsst.ci.prepare.Git.checkout")
    lsst.ci.prepare.Git.checkout.side_effect = GitError(42, "cmd", "stdout", "stderr")

    mocker.spy(lsst.ci.prepare.Git, "clone")

    with pytest.raises(GitError) as e:
        asyncio.run(product_fetcher.fetch(test_product, refs))

    assert e.value.returncode == 42
    assert product_fetcher._fetch.call_count == tries
    # clone is not called on the first iteration as the repo already exists
    assert lsst.ci.prepare.Git.clone.call_count == (tries - 1)
    assert lsst.ci.prepare.Git.checkout.call_count == tries


def test_fetch_products(tmpdir, repos_yaml_good, test_product):
    """Clone git repo from a valid repos.yaml"""
    refs = [DEFAULT_BRANCH_NAME]
    product_fetcher = ProductFetcher(tmpdir, repos_yaml_good, None, no_fetch=False)

    asyncio.run(product_fetcher.fetch_products([test_product], refs))
    assert os.path.exists(os.path.join(tmpdir, test_product, ".git"))
