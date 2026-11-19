"""Run module-level pytest cases separately from unittest and site suites."""
import sys

import pytest


class ModuleFunctionsOnly:
    def pytest_collection_modifyitems(self, items):
        items[:] = [item for item in items if isinstance(item.parent, pytest.Module)]


if __name__ == "__main__":
    sys.exit(pytest.main(["hero_integration/tests", *sys.argv[1:]], plugins=[ModuleFunctionsOnly()]))
