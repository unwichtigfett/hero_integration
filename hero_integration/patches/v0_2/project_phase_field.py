"""One-time: Phase shows the HERO status step (as HERO does); the status group name is hidden."""


def execute():
    from hero_integration.integrations.project_form import setup_project_form

    setup_project_form()
