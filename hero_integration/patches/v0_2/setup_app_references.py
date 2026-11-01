"""One-time: Helpdesk and CRM reference ERPNext Customer/Project (user decision 2026-11-01)."""


def execute():
    from hero_integration.integrations.app_references import setup_reference_access

    setup_reference_access()
