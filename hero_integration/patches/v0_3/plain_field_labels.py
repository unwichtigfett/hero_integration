"""One-time: app fields on ERPNext documents get plain labels without "HERO"; the origin stays in the field metadata."""


def execute():
    from hero_integration.integrations.field_labels import relabel_fields

    relabel_fields()
