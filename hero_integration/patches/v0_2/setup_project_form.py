"""One-time: HERO values on the Project form appear as ERPNext fields."""


def execute():
    from hero_integration.integrations.project_form import setup_project_form

    setup_project_form()
