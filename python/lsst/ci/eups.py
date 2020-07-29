import asyncio
import os
from typing import List

import re

import eups  # type: ignore
import eups.tags  # type: ignore

from lsst.ci.git import Git


class EupsModule:
    def __init__(self, eups, exclusion_resolver):
        self.eups = eups
        self.exclusion_resolver = exclusion_resolver

    def dependency_file(self, package_name):
        return f"ups/{package_name}.table"

    def dependencies(self, product_name: str, table_file_path: str) -> List[str]:
        # Parse the table file to discover dependencies
        dependency_names = []
        for dep in eups.table.Table(table_file_path).dependencies(self.eups):
            (dependency, is_optional) = dep[0:2]
            # skip excluded optional products, and implicit products
            if is_optional and self.exclusion_resolver.is_excluded(dependency.name, product_name):
                continue
            if dependency.name == "implicitProducts":
                continue
            dependency_names.append(dependency.name)
        return dependency_names

    def optional_dependencies(self, product_name: str, table_file_path: str) -> List[str]:
        # Parse the table file to discover dependencies
        dependency_names = []
        # Prepare the non-excluded dependencies
        for dep in eups.table.Table(table_file_path).dependencies(self.eups):
            (dependency, is_optional) = dep[0:2]
            # skip excluded optional products, and implicit products
            if is_optional and not self.exclusion_resolver.is_excluded(dependency.name, product_name):
                dependency_names.append(dependency.name)
        return dependency_names

    def get_build_id(self):
        """Allocate the next unused EUPS tag that matches the bNNNN pattern"""
        tags = eups.tags.Tags()
        tags.loadFromEupsPath(self.eups.path)
        btre = re.compile("^b[0-9]+$")
        btags = [0]
        btags += [int(tag[1:]) for tag in tags.getTagNames() if btre.match(tag)]
        tag = "b%s" % (max(btags) + 1)
        return tag

    def get_manifest_build_id(self, version_db_path, manifest_sha):
        """Return a build ID unique to this manifest. If a matching manifest already
           exists in the database, its build ID will be used.
        """
        with open(
            os.path.join(version_db_path, "manifests", "content_sha.db.txt"), "a+", encoding="utf-8"
        ) as fp:
            # Try to find a manifest with existing matching content
            for line in fp:
                (sha1, tag) = line.strip().split()
                if sha1 == manifest_sha:
                    return tag

            # Find the next unused tag that matches the bNNNN pattern
            # and isn't defined in EUPS yet
            git = Git(version_db_path)
            tags = asyncio.run(git.tag("-l", "b[0-9]*")).split()
            btre = re.compile("^b[0-9]+$")
            btags = [0]
            btags += [int(t[1:]) for t in tags if btre.match(t)]
            btag = max(btags)

            defined_tags = self.eups.tags.getTagNames()
            while True:
                btag += 1
                tag = "b%s" % btag
                if tag not in defined_tags:
                    break

            return tag
