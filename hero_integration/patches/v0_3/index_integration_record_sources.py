"""Index a writer receipt's original run and source identity on existing sites."""
from hero_integration.hero_integration.doctype.hero_integration_record.hero_integration_record import on_doctype_update


def execute():
    on_doctype_update()
