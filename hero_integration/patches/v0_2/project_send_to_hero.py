"""One-time: editable Notes and the HERO Writeback role for the Project's Send to HERO button."""


def execute():
    from hero_integration.integrations.project_form import setup_project_form

    setup_project_form()
