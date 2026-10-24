import copy
from hero_integration.services.mapping_compatibility import COMPATIBLE, CONFLICT, INCOMPLETE_SOURCE, canonical_hash, validate

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
