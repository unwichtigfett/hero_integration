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

# A one-minute tick only checks mode, window and interval; the configured
# polling interval (1..1440 minutes) decides whether anything starts.
scheduler_events = {
    "cron": {"* * * * *": [
        "hero_integration.tasks.run_scheduled_sync",
        "hero_integration.tasks.run_scheduled_schema_refresh",
    ]},
}

# Native Desk extension only.  It asks the server for eligible, linked fields
# on every form refresh and never observes or sends values by itself.
doctype_js = {
    "Customer": "public/js/hero_writeback.js",
    "Project": "public/js/hero_writeback.js",
}

# The gate intentionally covers only HERO-owned documents. It cannot block a
# user from deleting arbitrary ERPNext records, but it does cover every native
# app RPC, background path and direct DocType deletion route.
_HERO_OWNED_DOCTYPES = (
    "HERO Frappe Mapping",
    "HERO GraphQL Mapping",
    "HERO GraphQL Schema Snapshot",
    "HERO GraphQL Service",
    "HERO Integration Link",
    "HERO Integration Record",
    "HERO Integration Run",
    "HERO Integration Settings",
    "HERO Integration Sync State",
    "HERO Mapping Adoption",
    "HERO Mutation Audit",
    "HERO Mutation Contract",
    "HERO Polling Event",
    "HERO Task Assignment Policy",
    "HERO Task Assignment State",
    "HERO Snapshot Mapping Inheritance",
)
doc_events = {
    doctype: {"on_trash": "hero_integration.services.guards.assert_app_document_deletion_allowed"}
    for doctype in _HERO_OWNED_DOCTYPES
}
# Target DocTypes are intentionally not hard-coded.  The observer only acts
# when an app-owned Integration Link identifies this document as a mapped
# target; it never starts polling, queues work, or sends a HERO request.
doc_events["*"] = {"on_update": "hero_integration.services.change_detection.observe_frappe_target_change"}
doc_events["ToDo"] = {
    "on_update": "hero_integration.services.task_assignment_state.observe_local_todo",
    "on_trash": "hero_integration.services.task_assignment_state.observe_local_todo",
}
doc_events["Task"] = {"validate": "hero_integration.services.change_detection.protect_external_task_deadline"}

# Requires the separately distributed Frappe assignment-intent v2 patch.
assignment_intent_before = ["hero_integration.services.task_assignment_state.before_native_assignment"]
assignment_intent_after = ["hero_integration.services.task_assignment_state.after_native_assignment"]
assignment_intent_guard = ["hero_integration.services.task_assignment_state.guard_native_assignment"]

# This app ships DocTypes as source and has no site-specific fixtures.
fixtures = []

permission_query_conditions = {
    doctype: "hero_integration.services.mapping_draft_access.personal_mapping_query"
    for doctype in ("HERO Mapping Draft", "HERO Mapping Revision")
}
has_permission = {
    doctype: "hero_integration.services.mapping_draft_access.personal_mapping_permission"
    for doctype in ("HERO Mapping Draft", "HERO Mapping Revision")
}
permission_query_conditions["Version"] = "hero_integration.services.mapping_draft_access.personal_version_query"
has_permission["Version"] = "hero_integration.services.mapping_draft_access.personal_version_permission"
extend_doctype_class = {"Version": ["hero_integration.services.mapping_draft_access.PersonalMappingVersionAccess"]}
