from typing import List

import eups  # type: ignore
import eups.tags  # type: ignore


class EupsModule:
    """Class for consolidating eups related operations and logic.

    This class exists to distill down eups-related operations around
    dependency information and packages in one place to better facilitate
    reuse within this package (and prevent circular imports).

    Parameters
    ----------
    eups
        eups object
    exclusion_resolver : ExclusionResolver
        object to help exclude products when getting declared dependencies
    """
    def __init__(self, eups: eups.Eups, exclusion_resolver):
        self.eups = eups
        self.exclusion_resolver = exclusion_resolver

    def dependency_file(self, package_name: str) -> str:
        """Return the table file location for a package"""
        return f"ups/{package_name}.table"

    def dependencies(self, product_name: str, table_file_path: str) -> List[str]:
        """Parse the table file to discover explicit dependencies.

        Parameters
        ----------
        product_name
            the name of this product we are investigating
        table_file_path
            the path of the table file with information about dependencies
        Returns
        -------
        list
            the list of optional dependencies.
        """
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
        """Parse the table file to discover optional dependencies.

        Parameters
        ----------
        product_name
            the name of this product we are investigating
        table_file_path
            the path of the table file with information about dependencies
        Returns
        -------
        list
            the list of optional dependencies.
        """
        dependency_names = []
        # Prepare the non-excluded dependencies
        for dep in eups.table.Table(table_file_path).dependencies(self.eups):
            (dependency, is_optional) = dep[0:2]
            # skip excluded optional products, and implicit products
            if is_optional and not self.exclusion_resolver.is_excluded(dependency.name, product_name):
                dependency_names.append(dependency.name)
        return dependency_names
