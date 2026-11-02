"""One-time: Helpdesk and CRM reference ERPNext Customer/Project."""


def execute():
    from hero_integration.integrations.app_references import setup_reference_access

    setup_reference_access()
