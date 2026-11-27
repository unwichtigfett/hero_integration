"""Apply the migration writer's identity lookup index to existing installations."""
from hero_integration.hero_integration.doctype.hero_integration_link.hero_integration_link import on_doctype_update


def execute():
    on_doctype_update()
