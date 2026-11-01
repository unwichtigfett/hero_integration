"""One-time: HERO values on the Project form appear as ERPNext fields (owner decision 2026-11-01)."""


def execute():
    from hero_integration.integrations.project_form import setup_project_form

    setup_project_form()
