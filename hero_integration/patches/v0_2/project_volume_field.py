"""One-time: read-only Project Volume (euros) field on the Project form."""


def execute():
    from hero_integration.integrations.project_form import setup_project_form

    setup_project_form()
