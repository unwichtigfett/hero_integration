"""One-time: index the target lookup that every document save and delete performs."""


def execute():
    from hero_integration.hero_integration.doctype.hero_integration_link.hero_integration_link import on_doctype_update

    on_doctype_update()
