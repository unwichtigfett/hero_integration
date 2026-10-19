# HERO Desk UI parity matrix

This is a source-level parity record for the native Frappe-v16 workspace. It
does not claim browser acceptance; that remains a separate manual step.

| Legacy UI function | Native hero_integration function | Evidence test | Status |
| --- | --- | --- | --- |
| HERO overview, connection/status cards, tool navigation | `hero_workspace.js:overview` and stored `integration_overview` API | `test_hero_desk_ui_contract` | IMPLEMENTED_SOURCE_TESTED |
| Context reload and schema-update entrypoint on overview | Frappe page secondary/primary actions only on overview | `test_hero_desk_ui_contract` | IMPLEMENTED_SOURCE_TESTED |
| API type analysis, operation roots, fields and details | `analyzer`, `graph_view`, native graph analysis | `test_graphql_graph_contract` | IMPLEMENTED_SOURCE_TESTED |
| Graph query/mutation roots, field/argument relations and custom scalars | Existing `graphViewer` engine extended with pan, zoom, focus, search and relation navigation | `test_graphql_graph_contract`, `test_hero_desk_ui_contract` | IMPLEMENTED_SOURCE_TESTED |
| Graph field to mapping-editor navigation | `setRoute("hero-adapter-tools", ...)` with selected snapshot/field | `test_hero_desk_ui_contract` | IMPLEMENTED_SOURCE_TESTED |
| GraphQL saved-service list, revision, validation and definition detail | `graphqlServices`, `graphql_service_list`, `graphql_service_detail` | `test_hero_desk_ui_contract` | IMPLEMENTED_SOURCE_TESTED |
| Adapter field search and bidirectional suggestions | Existing `hero_adapter_tools.js` native picker APIs | `test_direct_frappe_adapter` in native Frappe harness | IMPLEMENTED_SOURCE_TESTED |
| Draft import, dependencies and one confirmed checklist adoption | Existing `hero_adapter_tools.js` and `adopt_mapping_package` | `test_hero_desk_ui_contract` | IMPLEMENTED_SOURCE_TESTED |

The legacy Process-Core shell and its RPCs are intentionally not ported. All
native routes call `hero_integration.api`; query execution is not exposed by the
Desk workspace and no mutation execution control is included.
