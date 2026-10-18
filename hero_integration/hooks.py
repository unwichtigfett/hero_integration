app_name = "hero_integration"
app_title = "HERO Integration"
app_publisher = "HERO Integration Maintainers"
app_description = "Native Frappe v16 integration for HERO"
app_email = "maintainers@example.invalid"
app_license = "MIT"
# Do not set ``required_apps`` here. Frappe resolves that hook before
# ``before_install`` and would install ERPNext as a side effect. HERO
# Integration instead requires the administrator to install ERPNext v16 first.

before_install = "hero_integration.install.before_install"
after_install = "hero_integration.install.after_install"
after_migrate = "hero_integration.install.after_migrate"

scheduler_events = {
    "hourly": ["hero_integration.tasks.run_scheduled_sync"],
}

# This app ships DocTypes as source and has no site-specific fixtures.
fixtures = []
