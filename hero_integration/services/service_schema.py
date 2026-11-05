"""App-owned service observations; visible labels deliberately have no provider prefix."""
SCALARS = {
    "internal_identifier": ("Data", "Internal Identifier"),
    "is_fixed_net_price": ("Check", "Fixed Price"),
    "quantity": ("Float", "Default Quantity"),
    "time_minutes": ("Float", "Calculated Time (Minutes)"),
    "vat_percent": ("Float", "Source Tax Rate"),
    "created": ("Data", "Source Created"),
    "modified": ("Data", "Source Modified"),
}
ITEM_FIELDS = [{"fieldname": "custom_hero_service_" + source, "label": label, "fieldtype": kind,
                "read_only": 1, "no_copy": 1} for source, (kind, label) in SCALARS.items()] + [
    {"fieldname": "custom_hero_service_calculation", "label": "Service Calculation", "fieldtype": "Table",
     "options": "HERO Service Calculation", "read_only": 1, "no_copy": 1}]
