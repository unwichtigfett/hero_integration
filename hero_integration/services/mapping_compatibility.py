"""Pure deterministic compatibility checks; never import or persist mapping data."""
from __future__ import annotations
import hashlib, json
from collections.abc import Mapping
from hero_integration.services.direct_mapping import TRANSFORMATIONS

COMPATIBLE="COMPATIBLE"; INCOMPLETE_SOURCE="INCOMPLETE_SOURCE"; CONFLICT="CONFLICT"
FIELDS=("definition_id","operation","source_path","source_type","target_doctype","target_field","purpose","transformation","relation","priority","schema_dependencies","mapping_revision")
def canonical_hash(value): return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
def rows(value,name,missing):
 if not isinstance(value,list): missing.append(name); return []
 return [item for item in value if isinstance(item,dict)]
def validate(package:Mapping[str,object],state:Mapping[str,object])->dict[str,object]:
 """Return deterministic compatibility evidence without I/O side effects."""
 missing=[]; conflicts=[]; mappings=rows(package.get("mappings"),"package.mappings",missing); state_rows=rows(state.get("read_mappings"),"state.read_mappings",missing); dynamic=rows(state.get("dynamic_graphql_mappings"),"state.dynamic_graphql_mappings",missing); mutations=rows(state.get("mutation_contracts"),"state.mutation_contracts",missing); services=rows(state.get("graphql_services"),"state.graphql_services",missing); snapshot=state.get("snapshot") if isinstance(state.get("snapshot"),Mapping) else {}; fingerprint=package.get("schema_fingerprint")
 if fingerprint!=snapshot.get("schema_fingerprint"): conflicts.append("schema_fingerprint_mismatch")
 ids=[item.get("definition_id") for item in mappings]
 if len(mappings)!=76: conflicts.append("read_mapping_count")
 if len(ids)!=len(set(ids)) or any(not isinstance(value,str) or not value for value in ids): conflicts.append("duplicate_or_missing_definition_id")
 by_id={item.get("definition_id"):item for item in state_rows}
 for item in mappings:
  state_item=by_id.get(item.get("definition_id"))
  if state_item is None: conflicts.append("missing_definition_id"); continue
  if item.get("mapping_revision")!=state_item.get("mapping_revision"): conflicts.append("changed_mapping_revision")
  if item.get("transformation") not in TRANSFORMATIONS: conflicts.append("unsupported_transformation")
  if state_item.get("definition_hash") and state_item["definition_hash"]!=canonical_hash({key:item.get(key) for key in FIELDS}): conflicts.append("changed_canonical_mapping_hash")
 for label,items,count in (("dynamic",dynamic,7),("mutation",mutations,2)):
  names=[item.get("name") for item in items]
  if len(items)!=count or len(names)!=len(set(names)) or any(not name for name in names): conflicts.append(label+"_identity_or_count")
  if any(item.get("schema_fingerprint")!=fingerprint for item in items): conflicts.append("schema_fingerprint_mismatch")
 for item in services:
  if item.get("execution_status")=="Approved" and item.get("approved_service_revision")!=item.get("service_revision"): conflicts.append("status_or_approval_mismatch")
  if "query_document" not in item: missing.append("graphql_services.query_document")
 if any("mapping_json" not in item for item in mutations): missing.append("mutation_contracts.mapping_json")
 if any(not item.get("schema_dependencies") for item in mappings): missing.append("mapping.schema_dependencies")
 conflicts=sorted(set(conflicts)); missing=sorted(set(missing))
 return {"classification":CONFLICT if conflicts else INCOMPLETE_SOURCE if missing else COMPATIBLE,"conflicts":conflicts,"missing_restoration_data":missing,"coverage":{"read_mappings":len(mappings),"dynamic_mappings":len(dynamic),"mutation_contracts":len(mutations),"read_services":len(services)},"execution_authority":"UNVERIFIED"}


PROJECTION_METADATA = {
 "read_mappings": ("name", "definition_id", "operation", "priority", "schema_fingerprint", "schema_snapshot", "status", "mapping_revision"),
 "graphql_mappings": ("name", "operation_kind", "operation_name", "direction", "source_instance", "status", "mapping_revision", "schema_fingerprint", "schema_snapshot"),
 "graphql_services": ("name", "service_name", "operation_name", "read_mapping", "schema_snapshot", "schema_fingerprint", "service_revision", "approved_service_revision", "approved_mapping_revision", "status", "execution_status"),
 "mutation_contracts": ("name", "graphql_service", "operation_name", "schema_snapshot", "schema_fingerprint", "contract_revision", "contract_fingerprint", "status"),
}
PROJECTION_RECORDS = {
 "read_mappings": "hero_frappe_mappings",
 "graphql_mappings": "hero_graphql_mappings",
 "graphql_services": "hero_graphql_services",
 "mutation_contracts": "hero_mutation_contracts",
}

def _projection_rows(value, missing, name):
 if not isinstance(value, list):
  missing.append(name)
  return []
 return [item for item in value if isinstance(item, Mapping)]

def _projection_json(value, missing, conflicts, name, required=True):
 if value in (None, ""):
  if required: missing.append(name)
  return None
 if not isinstance(value, str):
  conflicts.append("unsupported_"+name.replace(".", "_"))
  return None
 try:
  return json.loads(value)
 except json.JSONDecodeError:
  conflicts.append("invalid_"+name.replace(".", "_"))
  return None

def _dependency_ids(value, conflicts):
 if not isinstance(value, str):
  conflicts.append("unsupported_schema_dependencies")
  return None
 try:
  decoded=json.loads(value)
 except json.JSONDecodeError:
  conflicts.append("unsupported_schema_dependencies")
  return None
 if not isinstance(decoded, Mapping) or set(decoded)!={"ids"}:
  conflicts.append("unsupported_schema_dependencies")
  return None
 ids=decoded["ids"]
 if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
  conflicts.append("unsupported_schema_dependencies")
  return None
 return ids

def _project_result(conflicts, missing, projection=None):
 conflicts=sorted(set(conflicts)); missing=sorted(set(missing))
 return {
  "classification": CONFLICT if conflicts else INCOMPLETE_SOURCE if missing else COMPATIBLE,
  "conflicts": conflicts,
  "missing_restoration_data": missing,
  "projection": projection,
  "execution_authority": "UNVERIFIED",
  "restoration_execution": "NOT_RUN",
 }

def project_contract_v1(package:Mapping[str,object], state:Mapping[str,object], supplement:Mapping[str,object])->dict[str,object]:
 """Project a verified current technical capture without importing, activating, or persisting."""
 conflicts=[]; missing=[]
 if supplement.get("capture_provenance")!="CURRENT_CAPTURE": conflicts.append("capture_provenance")
 records=supplement.get("records")
 if not isinstance(records, Mapping):
  return _project_result(["capture_records"], missing)
 mappings=_projection_rows(records.get("hero_frappe_mappings"), missing, "hero_frappe_mappings")
 dynamic=_projection_rows(records.get("hero_graphql_mappings"), missing, "hero_graphql_mappings")
 services=_projection_rows(records.get("hero_graphql_services"), missing, "hero_graphql_services")
 mutations=_projection_rows(records.get("hero_mutation_contracts"), missing, "hero_mutation_contracts")
 snapshots=_projection_rows(records.get("hero_graphql_schema_snapshots"), missing, "hero_graphql_schema_snapshots")
 baseline=_projection_rows(package.get("mappings"), missing, "package.mappings")
 fingerprint=package.get("schema_fingerprint")
 if len(mappings)!=76 or len(baseline)!=76: conflicts.append("read_mapping_count")
 baseline_by_id={item.get("definition_id"):item for item in baseline if isinstance(item.get("definition_id"), str)}
 current_by_id={item.get("definition_id"):item for item in mappings if isinstance(item.get("definition_id"), str)}
 if len(baseline_by_id)!=76 or set(current_by_id)!=set(baseline_by_id): conflicts.append("read_mapping_identity")
 projected_mappings=[]
 for definition_id in sorted(baseline_by_id):
  original=baseline_by_id[definition_id]; current=current_by_id.get(definition_id)
  if current is None: continue
  ids=_dependency_ids(current.get("schema_dependencies"), conflicts)
  exported=current.get("schema_dependencies_ids")
  if not isinstance(exported, list) or not all(isinstance(item, str) for item in exported):
   conflicts.append("unsupported_schema_dependencies")
  elif ids is not None and (ids!=exported or ids!=original.get("schema_dependencies")):
   conflicts.append("schema_dependencies_drift")
  for field in FIELDS:
   if field=="schema_dependencies": continue
   if current.get(field)!=original.get(field): conflicts.append("read_mappings."+field)
  _projection_json(current.get("schema_manifest"), missing, conflicts, "read_mappings.schema_manifest")
  projected_mappings.append({field:current.get(field) for field in FIELDS if field!="schema_dependencies"}|{"schema_dependencies": list(ids or []) ,"schema_manifest":current.get("schema_manifest"),"schema_snapshot":current.get("schema_snapshot"),"schema_fingerprint":current.get("schema_fingerprint"),"status":current.get("status")})
 def parity(label, current, expected):
  current_by_name={item.get("name"):item for item in current if isinstance(item.get("name"), str)}
  expected_by_name={item.get("name"):item for item in expected if isinstance(item, Mapping) and isinstance(item.get("name"), str)}
  if set(current_by_name)!=set(expected_by_name): conflicts.append(label+".identity")
  for name in sorted(set(current_by_name)&set(expected_by_name)):
   for field in PROJECTION_METADATA[label]:
    if current_by_name[name].get(field)!=expected_by_name[name].get(field): conflicts.append(label+"."+field)
  return current_by_name
 expected_read=_projection_rows(state.get("read_mappings"), missing, "state.read_mappings")
 parity("read_mappings", mappings, expected_read)
 expected_dynamic=_projection_rows(state.get("dynamic_graphql_mappings"), missing, "state.dynamic_graphql_mappings")
 expected_services=_projection_rows(state.get("graphql_services"), missing, "state.graphql_services")
 expected_mutations=_projection_rows(state.get("mutation_contracts"), missing, "state.mutation_contracts")
 dynamic_by_name=parity("graphql_mappings", dynamic, expected_dynamic)
 services_by_name=parity("graphql_services", services, expected_services)
 mutations_by_name=parity("mutation_contracts", mutations, expected_mutations)
 if len(dynamic)!=7: conflicts.append("graphql_mappings.count")
 if len(services)!=7: conflicts.append("graphql_services.count")
 if len(mutations)!=2: conflicts.append("mutation_contracts.count")
 snapshot_by_name={item.get("name"):item for item in snapshots if isinstance(item.get("name"), str)}
 expected_snapshot=state.get("snapshot") if isinstance(state.get("snapshot"), Mapping) else {}
 if len(snapshot_by_name)!=1 or set(snapshot_by_name)!={expected_snapshot.get("name")}: conflicts.append("snapshot.identity")
 for field in ("name","schema_fingerprint","schema_version","source_instance","endpoint_fingerprint","analysis_status","execution_contract_status"):
  if snapshot_by_name.get(expected_snapshot.get("name"),{}).get(field)!=expected_snapshot.get(field): conflicts.append("snapshot."+field)
 technical=state.get("technical_dependencies") if isinstance(state.get("technical_dependencies"), Mapping) else {}
 for field, value in (("schema_fingerprint", expected_snapshot.get("schema_fingerprint")),("snapshot_name", expected_snapshot.get("name")),("source_instance", expected_snapshot.get("source_instance")),("endpoint_fingerprint", expected_snapshot.get("endpoint_fingerprint"))):
  if technical.get(field)!=value: conflicts.append("technical_dependencies."+field)
 for item in dynamic:
  _projection_json(item.get("mapping_json"), missing, conflicts, "graphql_mappings.mapping_json")
 for item in services:
  if not isinstance(item.get("query_document"), str) or not item.get("query_document"): missing.append("graphql_services.query_document")
  if item.get("schema_snapshot") not in snapshot_by_name: conflicts.append("graphql_services.snapshot_reference")
  link=item.get("read_mapping")
  if link not in (None,"") and link not in dynamic_by_name: conflicts.append("graphql_services.read_mapping_reference")
  if item.get("execution_status")=="Approved":
   if item.get("approved_service_revision")!=item.get("service_revision") or item.get("approved_mapping_revision")!=dynamic_by_name.get(link,{}).get("mapping_revision"):
    conflicts.append("graphql_services.approval_revision")
 for item in mutations:
  _projection_json(item.get("mapping_json"), missing, conflicts, "mutation_contracts.mapping_json")
  if item.get("schema_snapshot") not in snapshot_by_name: conflicts.append("mutation_contracts.snapshot_reference")
  if item.get("graphql_service") not in services_by_name: conflicts.append("mutation_contracts.service_reference")
 for item in snapshots:
  _projection_json(item.get("snapshot_json"), missing, conflicts, "schema_snapshots.snapshot_json")
  _projection_json(item.get("structural_diff_json"), missing, conflicts, "schema_snapshots.structural_diff_json", required=False)
 for item in mappings+dynamic+services+mutations:
  if item.get("schema_snapshot") not in snapshot_by_name: conflicts.append("schema_snapshot_reference")
 if conflicts or missing: return _project_result(conflicts, missing)
 approved=tuple(sorted(item["name"] for item in services if item.get("execution_status")=="Approved"))
 projection={"format_version":"hero-mapping-contract-v1-projection","capture_provenance":"CURRENT_CAPTURE","schema_fingerprint":fingerprint,"read_mappings":tuple(projected_mappings),"graphql_mappings":tuple(dynamic),"graphql_services":tuple(services),"mutation_contracts":tuple(mutations),"schema_snapshots":tuple(snapshots),"approval":{"approved_services":approved}}
 return _project_result(conflicts, missing, projection)
