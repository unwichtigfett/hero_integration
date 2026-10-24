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
