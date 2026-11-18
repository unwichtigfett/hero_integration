"""Add mandatory local Project Address without changing historical records."""
from hero_integration.services.project_address import ensure_fields


def execute():
    ensure_fields()
