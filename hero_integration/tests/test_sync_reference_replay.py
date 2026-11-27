"""Replay the independently retained reference archive. No transport or native site is required."""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from zipfile import ZipFile

from graphql import build_client_schema, graphql_sync

from hero_integration.services import mapped_update_v2 as update
from hero_integration.services.mutation_binding_contract import parse_mutation, validate_mutation

ARCHIVE_DIGEST = "05f9a69a249e2f35b45ab384048c3f952f5d10707ec77ca2401ea3ff834d4829"


@skipUnless(os.environ.get("HERO_SYNC_REFERENCE_ARCHIVE"), "separately retained project archive required")
class TestProjectReferenceReplay(TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(os.environ["HERO_SYNC_REFERENCE_ARCHIVE"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != ARCHIVE_DIGEST:
            raise ValueError("The selected project reference archive changed.")
        cls.ex = {}
        with ZipFile(path) as archive:
            cls.identities = json.loads(archive.read("PROJECT_IDENTITIES.json"))
            cls.inventory = json.loads(archive.read("OBJECT_INVENTORY.json"))
            cls.scope = json.loads(archive.read("MUTATION_SCOPE.json"))
            for project in cls.scope["projects"]:
                raw = archive.read(project["name"] + ".json")
                if hashlib.sha256(raw).hexdigest() != project["export_sha256"]:
                    raise ValueError("Project export does not match its scope manifest.")
                cls.ex[project["name"]] = json.loads(raw)["data"]
        config = json.loads((Path(__file__).parents[1] / "config" / "migration_baseline.json").read_text())
        cls.read_services = config["read_services"]
        cls.raw_schema = config["schema_snapshot"]["schema"]
        cls.profiles = {m["content"]["area"]: update.normalise_mapping(m["content"], cls.raw_schema)
                        for m in config["mutation_mappings"] if m["content"]["usage"] == update.USAGE}
        cls.contracts = {row["operation_name"]: parse_mutation(row["query"]) for row in config["mutation_contracts"]}

    def test_every_portable_inventory_reader_materializes_its_approved_response_shape(self):
        from hero_integration.graphql_protocol.graphql_operation_materializer import GraphQLOperationMaterializer
        from hero_integration.services.b8_runtime import ApprovedBoundReader
        from hero_integration.services.direct_runtime import page_variables
        schema = build_client_schema(self.raw_schema)
        for row in self.read_services:
            operation = row["target_operation"]
            with self.subTest(operation=operation):
                contract = SimpleNamespace(**row["mapping"])
                contract.adapter_options = row["mapping"].get("query_adapter")
                request = GraphQLOperationMaterializer.materialize_document(schema, "b"*64, row["query"], page_variables(contract,0))
                request = GraphQLOperationMaterializer.bind_saved_query_result(schema,request,root_name=row["mapping_operation_name"])
                root = contract.records_path.split(".")[0]
                root_value = {root:{"partners":[]} if operation == "HeroEmployees" else {"edges":[]} if root == "FileUpload_FileUploads" else []}
                class Transport:
                    def query(inner, document, variables):
                        response = graphql_sync(schema, document, root_value=root_value, variable_values=variables)
                        self.assertIsNone(response.errors)
                        return response.data
                reader = ApprovedBoundReader.__new__(ApprovedBoundReader)
                reader.plan = SimpleNamespace(sealed=SimpleNamespace(contract=contract),request=request)
                reader.employee_directory = operation == "HeroEmployees"
                reader._client = lambda:Transport()
                if not reader.employee_directory:
                    self.assertTrue(reader.inventory_arguments())
                prefix = ("data", *contract.records_path.split("."))
                projection = {bound.response_path for bound in request.result_bindings}
                self.assertIn((*prefix,contract.identity_path), projection)
                page = reader.inventory_page({},1,0)
                self.assertEqual((page.rows,page.next_offset),((),None))
        self.assertEqual(len(self.read_services), 9)

    def test_nonempty_project_exports_traverse_the_saved_core_queries_in_budgeted_pages(self):
        from hero_integration.graphql_protocol.graphql_operation_materializer import GraphQLOperationMaterializer
        from hero_integration.services.b8_runtime import ApprovedBoundReader
        from hero_integration.services.direct_runtime import page_variables

        records = {"HeroCustomers":{}, "HeroProjectMatches":{}, "HeroTasks":{}, "HeroCalendarEvents":{}}
        for export in self.ex.values():
            for area, rows in (("HeroCustomers", [export["project_match"]["customer"]]),
                               ("HeroProjectMatches", [export["project_match"]]),
                               ("HeroTasks", export["tasks"]), ("HeroCalendarEvents", export["calendar_events"])):
                for row in rows:
                    records[area][row["id"]] = row
        schema = build_client_schema(self.raw_schema)
        for saved in self.read_services:
            area = saved["target_operation"]
            if area not in records:
                continue
            with self.subTest(area=area):
                contract = SimpleNamespace(**saved["mapping"])
                contract.adapter_options = saved["mapping"].get("query_adapter")
                query = GraphQLOperationMaterializer.materialize_document(
                    schema, "b"*64, saved["query"], page_variables(contract, 0))
                query = GraphQLOperationMaterializer.bind_saved_query_result(
                    schema, query, root_name=saved["mapping_operation_name"])
                ordered = sorted(records[area].values(), key=lambda obj:obj["id"])
                calls = []
                def collection(_context, first, offset, **filters):
                    calls.append((first, offset))
                    return ordered[offset:offset+first]
                def reply(document, variables):
                    result = graphql_sync(schema, document, root_value={contract.records_path:collection},
                                          variable_values=variables)
                    self.assertFalse([error.path for error in (result.errors or [])])
                    return result.data
                reader = ApprovedBoundReader.__new__(ApprovedBoundReader)
                reader.plan = SimpleNamespace(sealed=SimpleNamespace(contract=contract), request=query)
                reader._client = lambda:SimpleNamespace(query=reply)
                seen, checkpoint = [], {}
                size = min(3, contract.page_size)
                for _ in range(len(ordered) + 2):
                    page = reader.inventory_page(checkpoint, size, min(1, size-1))
                    seen.extend(row["id"] for row in page.rows)
                    if page.next_offset is None:
                        break
                    checkpoint = page.checkpoint
                self.assertIsNone(page.next_offset)
                self.assertEqual(seen, [r["id"] for r in ordered])
                self.assertEqual(calls[-1][1], len(ordered))
                self.assertTrue(all(first == size for first, _ in calls))
        self.assertEqual([len(records[k]) for k in ("HeroCustomers","HeroProjectMatches","HeroTasks","HeroCalendarEvents")],
                         [4, 5, 32, 17])

    def test_scoped_exports_preserve_project_customer_address_and_assignment_namespaces(self):
        self.assertEqual(len(self.ex), 5)
        indexed = {(obj["type"], str(obj["id"])) for obj in self.inventory["objects"]}
        for identity, scope in zip(self.identities["projects"], self.scope["projects"], strict=True):
            with self.subTest(project=identity["name"]):
                data = self.ex[identity["name"]]
                match = data["project_match"]
                self.assertEqual(match["id"], identity["project_match_id"])
                self.assertIsNone(match["project_id"])
                self.assertIn(("ProjectMatch", str(match["id"])), indexed)
                self.assertEqual((match["customer"]["id"], match["address"]["id"]),
                                 (identity["customer_id"], identity["project_address_id"]))
                self.assertFalse(scope["permission_granted_by_this_file"])
                self.assertEqual(match["partner_id"], identity["responsible_employee"]["partner_id"])
                self.assertNotEqual(identity["responsible_employee"]["user_id"], match["partner_id"])
                for row in data["tasks"]:
                    self.assertEqual(row["target_project_match_id"], match["id"])
                    self.assertIn(("Task", str(row["id"])), indexed)
                for row in data["calendar_events"]:
                    self.assertEqual(row["project_match_id"], match["id"])
                    self.assertIn(("CalendarEvent", str(row["id"])), indexed)
        one, three = (self.ex[key]["project_match"] for key in ("TEST_01", "TEST_03"))
        self.assertEqual(one["customer_id"], three["customer_id"])
        self.assertNotEqual(one["address_id"], three["address_id"])

    def test_saved_task_mutation_round_trips_one_and_multiple_fields_for_each_export(self):
        from copy import deepcopy

        profile = self.profiles["HeroTasks"]
        name = profile["target"]["operation"]
        contract = self.contracts[name]
        schema = build_client_schema(self.raw_schema)
        for label, data in self.ex.items():
            with self.subTest(project=label):
                original = data["tasks"][0]
                remote = deepcopy(original)
                requests = []
                def receive(_root, _info, **args):
                    values = args["task"]
                    self.assertEqual(values["id"], original["id"])
                    requests.append(set(values))
                    remote.update(values)
                    return remote
                schema.mutation_type.fields[name].resolve = receive
                for selected in ({"subject"}, {"subject", "description"}):
                    inputs = {}
                    update._put(inputs, profile["identity"]["input_path"], original["id"])
                    for item in profile["assignments"]:
                        field = item["source"]["field"]
                        if field not in selected:
                            continue
                        local = "Synthetic " + field + " " + str(len(requests))
                        value = update._wire(None, item, SimpleNamespace(), local, remote)
                        update._put(inputs, item["input_path"], value)
                    variables = {contract.argument_variables[key]: value for key, value in inputs.items()}
                    result = graphql_sync(schema, contract.document, variable_values=validate_mutation(
                        self.raw_schema, contract, variables))
                    self.assertIsNone(result.errors)
                    self.assertEqual(result.data[name]["id"], original["id"])
                self.assertEqual(requests, [{"id", "title"}, {"id", "title", "comment"}])
                self.assertNotEqual(remote["title"], original["title"])
                self.assertEqual(remote["target_project_match_id"], original["target_project_match_id"])

    def test_empty_values_and_independent_child_updates_use_the_saved_contract(self):
        from hero_integration.services.change_detection import field_hashes, reconcile
        from hero_integration.services.offset_pagination import advance
        from copy import deepcopy

        base = self.ex["TEST_04"]["project_match"]
        profile = self.profiles["HeroProjectMatches"]
        notes = next(a for a in profile["assignments"] if a["read_path"] == "partner_notes")
        for value in (None, ""):
            self.assertIs(update._wire(None, notes, None, value, base), update.MISSING)
        self.assertNotIn("current_project_match_status_id", {a["read_path"] for a in profile["assignments"]})
        source = self.ex["TEST_05"]["project_match"]
        changed = deepcopy(source)
        changed["project_match_assignments"] = changed["project_match_assignments"][:-1]
        last = advance([base, source], offset=0, size=2, overlap=1)
        overlap = advance([changed], offset=1, size=2, overlap=1, previous=last.checkpoint)
        self.assertIn(str(source["id"]), overlap.changed)
        self.assertEqual(source["modified"], changed["modified"])
        prior = field_hashes({"notes": source["partner_notes"], "name": source["name"]})
        remote = field_hashes({"notes": "remote notes", "name": source["name"]})
        local = field_hashes({"notes": "local notes", "name": "local display"})
        outcome = reconcile(prior, prior, remote, local, tracked=set(prior))
        self.assertEqual((outcome.conflicts, outcome.local), ({"notes"}, {"name"}))

    def test_each_saved_update_profile_materializes_and_executes_its_typed_contract(self):
        from graphql import get_named_type
        schema = build_client_schema(self.raw_schema)
        sent = []
        for area, profile in self.profiles.items():
            name = profile["target"]["operation"]
            contract = self.contracts[name]
            with self.subTest(area=area):
                inputs = {}
                def put(path, value):
                    update._put(inputs, path, update._typed(value, update._input_type(schema, name, path)))
                put(profile["identity"]["input_path"], 37)
                for copy in profile["required_readback"]:
                    path = copy["input_path"]
                    kind = get_named_type(update._input_type(schema, name, path)).name
                    value = "synthetic" if kind == "String" else 43
                    put(path, value)
                candidates = [item for item in profile["assignments"] if item["resolver"] in {"FIELD", "PLAIN_TEXT"}
                              and item["input_path"] not in {entry["input_path"] for entry in profile["required_readback"]}]
                item = next(i for i in candidates if get_named_type(update._input_type(schema,name,i["input_path"])).name
                            in {"String","Boolean","Float","Int"})
                kind = get_named_type(update._input_type(schema, name, item["input_path"])).name
                value = {"String":"synthetic change","Boolean":False,"Int":0,"Float":0.0}[kind]
                put(item["input_path"], update._wire(None, item, SimpleNamespace(), value, {}))
                def receive(_root, _info, **args):
                    if name == "update_file_upload":
                        self.assertNotIn("delete", inputs)
                        self.assertIs(args.pop("delete"), False)  # Schema default, never sent by this mapping.
                    self.assertEqual(args, inputs)
                    sent.append((area, name))
                    return {"id":37}
                schema.mutation_type.fields[name].resolve = receive
                variables = {contract.argument_variables[key]: value for key, value in inputs.items()}
                validated = validate_mutation(self.raw_schema, contract, variables)
                result = graphql_sync(schema, contract.document, variable_values=validated)
                self.assertIsNone(result.errors)
                self.assertEqual(str(result.data[name]["id"]), "37")
        self.assertEqual(len(sent), 9)
        self.assertEqual({area for area, _ in sent}, set(self.profiles))
