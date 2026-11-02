"""Isolated Frappe fallback for offline API contract tests."""

import importlib
import sys
import types


def load_api():
    try:
        import frappe  # noqa: F401
    except ModuleNotFoundError as error:
        if error.name != "frappe":
            raise
        package = importlib.import_module("hero_integration")
        missing = object()
        previous_frappe = sys.modules.get("frappe", missing)
        previous_api = sys.modules.get("hero_integration.api", missing)
        previous_alias = getattr(package, "api", missing)
        stub = types.ModuleType("frappe")
        stub.whitelist = lambda *args, **kwargs: (lambda function: function)
        sys.modules["frappe"] = stub
        try:
            return importlib.import_module("hero_integration.api")
        finally:
            if previous_frappe is missing:
                sys.modules.pop("frappe", None)
            else:
                sys.modules["frappe"] = previous_frappe
            if previous_api is missing:
                sys.modules.pop("hero_integration.api", None)
            else:
                sys.modules["hero_integration.api"] = previous_api
            if previous_alias is missing:
                if hasattr(package, "api"):
                    delattr(package, "api")
            else:
                package.api = previous_alias
    return importlib.import_module("hero_integration.api")
