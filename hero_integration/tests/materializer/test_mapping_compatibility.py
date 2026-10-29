import copy
import json

from hero_integration.services.mapping_compatibility import COMPATIBLE, CONFLICT, INCOMPLETE_SOURCE, canonical_hash, project_contract_v1, validate

def mapping(index):
 return {"definition_id":f"d-{index}","operation":"HeroCustomers","source_path":"id","source_type":"int","target_doctype":"Customer","target_field":f"custom_{index}","purpose":"Field","transformation":"Identity","relation":"","priority":1,"schema_dependencies":["type:Customer"],"mapping_revision":1}
def fixture():
 mappings=[mapping(index) for index in range(76)]; fp="a"*64
 state_rows=[{"definition_id":item["definition_id"],"mapping_revision":1,"definition_hash":canonical_hash(item)} for item in mappings]
 state={"snapshot":{"schema_fingerprint":fp},"read_mappings":state_rows,"dynamic_graphql_mappings":[{"name":f"g-{i}","schema_fingerprint":fp,"mapping_revision":1} for i in range(7)],"mutation_contracts":[{"name":f"m-{i}","schema_fingerprint":fp,"mapping_json":{}} for i in range(2)],"graphql_services":[{"name":f"s-{i}","service_revision":1,"approved_service_revision":1,"execution_status":"Approved","query_document":"q"} for i in range(7)]}
 return {"schema_fingerprint":fp,"mappings":mappings},state
def test_complete_synthetic_parity_is_compatible_and_deterministic():
 package,state=fixture(); first=validate(package,state); assert first==validate(copy.deepcopy(package),copy.deepcopy(state)); assert first["classification"]==COMPATIBLE and first["execution_authority"]=="UNVERIFIED"
def test_duplicate_missing_revision_fingerprint_hash_and_transform_conflict():
 package,state=fixture(); package["mappings"][1]["definition_id"]="d-0"; package["mappings"][2]["mapping_revision"]=2; package["mappings"][3]["transformation"]="Unsupported"; package["schema_fingerprint"]="b"*64; result=validate(package,state); assert result["classification"]==CONFLICT; assert {"duplicate_or_missing_definition_id","changed_mapping_revision","unsupported_transformation","schema_fingerprint_mismatch"} <= set(result["conflicts"])
def test_incomplete_payload_is_not_mistaken_for_restoration_ready():
 package,state=fixture(); del state["graphql_services"][0]["query_document"]; del state["mutation_contracts"][0]["mapping_json"]; package["mappings"][0]["schema_dependencies"]=[]; state["read_mappings"][0]["definition_hash"]=canonical_hash(package["mappings"][0]); result=validate(package,state); assert result["classification"]==INCOMPLETE_SOURCE; assert set(result["missing_restoration_data"]) >= {"graphql_services.query_document","mutation_contracts.mapping_json","mapping.schema_dependencies"}


def capture_fixture():
 package,state=fixture(); fp=package["schema_fingerprint"]
 state["snapshot"]={"name":"SNAPSHOT-1","schema_fingerprint":fp,"schema_version":1,"source_instance":"hero-primary","endpoint_fingerprint":"endpoint-1","analysis_status":"Validated","execution_contract_status":"Disabled"}
 state["technical_dependencies"]={"schema_fingerprint":fp,"snapshot_name":"SNAPSHOT-1","source_instance":"hero-primary","endpoint_fingerprint":"endpoint-1"}
 state["read_mappings"]=[{"name":f"READ-{i}","definition_id":x["definition_id"],"operation":x["operation"],"priority":x["priority"],"schema_fingerprint":fp,"schema_snapshot":"SNAPSHOT-1","status":"Active","mapping_revision":x["mapping_revision"]} for i,x in enumerate(package["mappings"])]
 state["dynamic_graphql_mappings"]=[{"name":f"MAP-{i}","operation_kind":"query","operation_name":f"Read{i}","direction":"Read","source_instance":"hero-primary","status":"Active","mapping_revision":1,"schema_fingerprint":fp,"schema_snapshot":"SNAPSHOT-1"} for i in range(7)]
 state["graphql_services"]=[{"name":f"SERVICE-{i}","service_name":f"Read{i}","operation_name":f"Read{i}","read_mapping":f"MAP-{i}","schema_snapshot":"SNAPSHOT-1","schema_fingerprint":fp,"service_revision":1,"approved_service_revision":1,"approved_mapping_revision":1,"status":"Validated","execution_status":"Approved"} for i in range(7)]
 state["mutation_contracts"]=[{"name":f"CONTRACT-{i}","graphql_service":f"SERVICE-{i}","operation_name":f"Read{i}","schema_snapshot":"SNAPSHOT-1","schema_fingerprint":fp,"contract_revision":1,"contract_fingerprint":f"contract-{i}","status":"Draft"} for i in range(2)]
 records={"hero_frappe_mappings":[{**x,"name":f"READ-{i}","schema_dependencies":json.dumps({"ids":x["schema_dependencies"]}),"schema_dependencies_ids":list(x["schema_dependencies"]),"schema_manifest":"{}","schema_snapshot":"SNAPSHOT-1","schema_fingerprint":fp,"status":"Active","validation_summary":""} for i,x in enumerate(package["mappings"])],"hero_graphql_mappings":[{**x,"mapping_json":"{}","suggestion_reason":"","confidence":1.0} for x in state["dynamic_graphql_mappings"]],"hero_graphql_services":[{**x,"query_document":"query Read { id }","validation_summary":""} for x in state["graphql_services"]],"hero_mutation_contracts":[{**x,"mapping_json":"{}","validation_summary":""} for x in state["mutation_contracts"]],"hero_graphql_schema_snapshots":[{**state["snapshot"],"snapshot_json":"{}","captured_at":"2026-10-24T00:00:00Z","structural_diff_json":"","analysis_detail":""}]}
 return package,state,{"capture_provenance":"CURRENT_CAPTURE","records":records}

def test_current_capture_projects_contract_v1_without_persistence_or_approval_inference():
 package,state,supplement=capture_fixture(); first=project_contract_v1(package,state,supplement)
 assert first==project_contract_v1(copy.deepcopy(package),copy.deepcopy(state),copy.deepcopy(supplement)); assert first["classification"]==COMPATIBLE and first["execution_authority"]=="UNVERIFIED"
 assert [len(first["projection"][key]) for key in ("read_mappings","graphql_mappings","graphql_services","mutation_contracts")]==[76,7,7,2]; assert first["projection"]["read_mappings"][0]["schema_dependencies"]==["type:Customer"]
 supplement["records"]["hero_graphql_services"][0]["status"]="Active"; supplement["records"]["hero_graphql_services"][0]["execution_status"]="Active"; state["graphql_services"][0]["status"]="Active"; state["graphql_services"][0]["execution_status"]="Active"
 result=project_contract_v1(package,state,supplement); assert result["classification"]==COMPATIBLE; assert result["projection"]["approval"]["approved_services"]==tuple(f"SERVICE-{i}" for i in range(1,7))

def test_contract_v1_projection_rejects_dependency_payload_and_checkpoint_drift():
 package,state,supplement=capture_fixture(); supplement["records"]["hero_frappe_mappings"][0]["schema_dependencies"]="[]"; malformed=project_contract_v1(package,state,supplement); assert malformed["classification"]==CONFLICT and "unsupported_schema_dependencies" in malformed["conflicts"]
 package,state,supplement=capture_fixture(); del supplement["records"]["hero_graphql_services"][0]["query_document"]; incomplete=project_contract_v1(package,state,supplement); assert incomplete["classification"]==INCOMPLETE_SOURCE and "graphql_services.query_document" in incomplete["missing_restoration_data"]
 package,state,supplement=capture_fixture(); supplement["records"]["hero_graphql_mappings"][0]["mapping_revision"]=2; stale=project_contract_v1(package,state,supplement); assert stale["classification"]==CONFLICT and "graphql_mappings.mapping_revision" in stale["conflicts"]
