# HERO workspace compatibility matrix

The retained workspace, overview, settings and CSS are source-port copies from
the read-only historic Frappe source. The Desk host is native to this app; no
old application is imported at runtime. `hero-integration` is the one visible
HERO Desk workspace. The old Desk URLs are retained as redirects with a
section deep link, not as independent renderers.

| Source file | SHA-256 read from the read-only source |
| --- | --- |
| `hero_tools.js` | `4d284ae80791844dfd71e58db2bea3ac22a8a7539649b090160db425b42a929e` |
| `hero_tools.css` | `3f24fac3cbc9b7bfaedebc5f6d25e0b9f101543b56fc18d382519b716251b6ec` |
| `hero_adapter.js/css` | `fcaca634a5cb19206eb74aaf76e36e35c8a54d133f55086a9000ff0890901969` / `dbe55064b5cced2bdb2fbbe850f1099ec654576a1c52f31f034204ec46248e3d` |
| `hero_overview.js/css` | `1127ab2297d80ea389ebdd6c51da4d06b77ed5b44336d277e50864b483cac485` / `c18c908006926cf614d1520c4fb906078ad4227f4e7306497e98287782ee348f` |
| `hero_settings.js/css` | `e63180ad7b73583b0f266dbed309d8aed38938f33f49ae4dd6d3e03c7b7087c2` / `e8d5b7e240792eb74f6abe65cc55a4b1a221cd432630bd5d989ba9ebbeb7e82e` |

## Visual and interaction components

| Original file and component | Native implementation | Source test | Status |
| --- | --- | --- | --- |
| `hero_tools.js` `Workspace`, graph cache and renderer | `public/js/hero_tools.js` with only its finite RPC call seam replaced | `test_hero_desk_ui_contract.py` asset and interaction checks | IMPLEMENTED_SOURCE_TESTED |
| `hero_tools.css` graph, analyzer, inspector and service editor | `public/css/hero_tools.css` | packaged-assets check | IMPLEMENTED_SOURCE_TESTED |
| `hero_adapter.js` Desk embedding, modal and route shell | native `public/js/hero_workspace.js` single Frappe host and compatibility facade | route/adapter check | IMPLEMENTED_SOURCE_TESTED |
| `hero_adapter.css` host, controls and modal styles | `public/css/hero_adapter.css` | packaged-assets check | IMPLEMENTED_SOURCE_TESTED |
| `hero_overview.js/css` cards, actions and navigation | `public/js/hero_overview.js`, `public/css/hero_overview.css` | native-platform adapter check | IMPLEMENTED_SOURCE_TESTED |
| `hero_settings.js/css` masked credential form | `public/js/hero_settings.js`, `public/css/hero_settings.css`, central Settings section | token-redaction and native settings API check | IMPLEMENTED_SOURCE_TESTED |

## Unified Desk navigation

`hero-integration` opens the preserved Adapter host and contains Overview /
Connection, API Analyzer, Graph Viewer, GraphQL Services, ERPNext Mapping,
Migration / Synchronisation, and Settings. `hero-api-analyzer`,
`hero-graph-viewer`, `hero-graphql-services`, `hero-adapter-tools`,
`hero-migration-center`, and `hero-settings` remain installed only so existing
bookmarks resolve to the corresponding section. Technical DocTypes remain
available to their System Manager permission checks and direct records, but are
not linked from the normal HERO workspace navigation.

The CSS/DOM source port has not been visually inspected in a browser in this
step. Visual parity is therefore a manual step, not a claimed result.

## RPC inventory and bounded mapping

The source contains **19 distinct RPC names**, rather than the requested 18:
17 in the tool workspace plus `get_settings` and `save_settings`; `get_context`
is shared by the tool and overview. The extra observed name is not omitted.

| Original RPC | Classification | Native result |
| --- | --- | --- |
| `get_context` | SAFE_READ | `workspace_context` from saved snapshots |
| `get_schema` | SAFE_READ | `workspace_schema` full saved introspection projection |
| `get_graph` | SAFE_READ | `workspace_graph` complete saved graph projection |
| `list_services`, `get_query` | SAFE_READ | saved `HERO GraphQL Service` records |
| `save_query`, `validate_saved_query`, `preview_query`, `delete_query` | SAFE_LOCAL_SERVICE_MANAGEMENT | permission-checked local read-only service definitions |
| `get_settings` | SAFE_READ | redacted native settings read |
| `save_settings` | SAFE_LOCAL_SERVICE_MANAGEMENT | native DocType validation; token is never returned |
| `execute_service` | UNSUPPORTED_HERO_MUTATION | visible Execute control disabled; no remote execution endpoint is mapped |
| `refresh_schema`, `synchronize_connection` | LEGACY_PROCESS_ONLY | visible overview actions disabled; they require old binding/transport state |
| `get_analyzer_service_draft`, `save_analyzer_service` | LEGACY_PROCESS_ONLY | visible Analyzer save control disabled; no inferred query contract is created |
| `list_candidates`, `mark_candidate`, `remove_candidate` | LEGACY_PROCESS_ONLY | visible native Mapping Editor hand-off; no parallel candidate persistence |

The source workspace still displays Query and Mutation modes. Mutation mode is
disabled in the native host, and every server-side stored service is validated
with the existing read-only GraphQL guard.

## Complete stored schema source

The latest read-only source snapshot is `HERO Tool Record`
`schema-f8a45313b07848d6b010eb953874306be688273e3eb11342bb637bfc5ee30c3f`
(modified 2026-09-30 10:36:07 UTC). Its source fingerprint is
`1f467d54bf2b9b03abb5f1d68abb8ecfaa339fa7da15a66e3f6ac84ad3f37955`.
The source payload was transferred only to an untracked temporary location for
projection validation, not to Git. The actual source reports 300 types, 65
queries, 86 mutations and 2,492 type/input fields. Thus the historical
2,286-field baseline does not describe this latest stored snapshot.
