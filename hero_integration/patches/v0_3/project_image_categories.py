"""Create the image category Link and seed the initial administrator-owned names."""


def execute():
    from hero_integration.services.project_image_categories import install_defaults
    install_defaults()
