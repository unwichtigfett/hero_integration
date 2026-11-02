app_name = "hero_integration"
app_title = "HERO Integration"
app_publisher = "unwichtigfett"
app_description = "Native Frappe v16 integration for HERO"
app_email = "95431041+unwichtigfett@users.noreply.github.com"
app_license = "MIT"
# Omit required_apps: Frappe would install ERPNext before before_install.
# Require an administrator to install ERPNext v16 first.

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
    "Customer": ["public/js/hero_writeback.js", "public/js/hero_customer_send.js"],
    "Project": ["public/js/hero_writeback.js", "public/js/hero_project_send.js"],
    "Task": ["public/js/hero_task_send.js"],
    "HERO Integration Link": ["public/js/hero_diagnostics.js"],
    "HERO Integration Record": ["public/js/hero_diagnostics.js"],
    "HERO Polling Event": ["public/js/hero_diagnostics.js"],
    "HERO GraphQL Mapping": ["public/js/hero_diagnostics.js", "public/js/hero_native_diagnostics.js"],
    "HERO GraphQL Schema Snapshot": ["public/js/hero_diagnostics.js", "public/js/hero_native_diagnostics.js"],
    "HERO Snapshot Mapping Inheritance": ["public/js/hero_diagnostics.js", "public/js/hero_native_diagnostics.js"],
    "HERO Mutation Audit": ["public/js/hero_diagnostics.js", "public/js/hero_native_diagnostics.js"],
    "HERO Integration Run": ["public/js/hero_diagnostics.js", "public/js/hero_native_diagnostics.js"],
    "HERO Mapping Adoption": ["public/js/hero_diagnostics.js", "public/js/hero_native_diagnostics.js"],
    "HERO Integration Settings": ["public/js/hero_diagnostics.js", "public/js/hero_native_diagnostics.js"],
    "HERO Migration Progress Ledger": ["public/js/hero_diagnostics.js", "public/js/hero_native_diagnostics.js"],
}

# Apply the deletion gate only to the listed HERO-owned DocTypes.
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
# Observe only targets identified by Integration Links; no queueing or HERO I/O.
doc_events["*"] = {
    "on_update": "hero_integration.services.change_detection.observe_frappe_target_change",
    # A HERO-bound record is deleted only by the purge or a review rejection.
    "on_trash": "hero_integration.services.guards.protect_hero_bound_target",
}
doc_events["ToDo"] = {
    "on_update": "hero_integration.services.task_assignment_state.observe_local_todo",
    "on_trash": "hero_integration.services.task_assignment_state.observe_local_todo",
}
# Helpdesk and CRM keep plain references to ERPNext Customer/Project.
for _doctype in ("HD Ticket", "CRM Lead", "CRM Deal"):
    doc_events[_doctype] = {"validate": "hero_integration.integrations.app_references.fill_references"}
for _doctype in ("ERPNext HD Settings", "ERPNext CRM Settings"):
    doc_events[_doctype] = {"validate": "hero_integration.integrations.app_references.refuse_record_mirrors"}
# Project IDs ending in -H are reserved for HERO.
doc_events["Project"] = {
    "validate": "hero_integration.services.guards.protect_hero_project_ids",
    "before_rename": "hero_integration.services.guards.protect_hero_project_rename",
}
doc_events["Task"] = {"validate": "hero_integration.services.change_detection.protect_external_task_deadline"}

# Requires the separately distributed Frappe assignment-intent v2 patch.
assignment_intent_before = ["hero_integration.services.task_assignment_state.before_native_assignment"]
assignment_intent_after = ["hero_integration.services.task_assignment_state.after_native_assignment"]
assignment_intent_guard = ["hero_integration.services.task_assignment_state.guard_native_assignment"]

# This app ships DocTypes as source and has no site-specific fixtures.
fixtures = []

permission_query_conditions = {
    "HERO Mapping Draft": "hero_integration.services.mapping_draft_access.personal_mapping_draft_query",
    "HERO Mapping Revision": "hero_integration.services.mapping_draft_access.personal_mapping_revision_query",
}
has_permission = {
    doctype: "hero_integration.services.mapping_draft_access.personal_mapping_permission"
    for doctype in ("HERO Mapping Draft", "HERO Mapping Revision")
}
permission_query_conditions["Version"] = "hero_integration.services.mapping_draft_access.personal_version_query"
has_permission["Version"] = "hero_integration.services.mapping_draft_access.personal_version_permission"
extend_doctype_class = {"Version": ["hero_integration.services.mapping_draft_access.PersonalMappingVersionAccess"]}
