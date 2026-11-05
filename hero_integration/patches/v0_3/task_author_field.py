"""Provision the Task author through the shared create-only schema owner."""


def execute():
    from hero_integration.install import ensure_hero_source_fields

    ensure_hero_source_fields()
