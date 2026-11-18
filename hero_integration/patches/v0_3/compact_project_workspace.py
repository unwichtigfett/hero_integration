"""Compact the existing Project workspace and add local ERPNext responsibility."""


def execute():
    from hero_integration.services.project_workspace_layout import apply_compact_details
    from hero_integration.services.project_image_categories import ensure_upload_folders
    from hero_integration.services.design_mode import ensure_field
    ensure_field()
    apply_compact_details()
    ensure_upload_folders()
