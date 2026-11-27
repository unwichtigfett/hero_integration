"""The full test migration reads from its project and catalog cohorts at the wire boundary."""

from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from graphql import build_schema, graphql_sync, introspection_from_schema, parse, value_from_ast_untyped

from hero_integration.services import (
    direct_runtime as runtime,
    migration_testing,
    scoped_test_reads as scoped,
)
from hero_integration.services.graphql_schema_cache import compiled_schema
from hero_integration.services.mapping_contract_v1 import StoredSnapshot
from hero_integration.sources.hero import GraphQLServiceSource

SCHEMA = build_schema("""
    type Query {
        project_matches(first:Int!, offset:Int!, ids:[Int], orderBy:String):[Project!]!
        project_match(project_match_id:Int!):Project
        tasks(first:Int!, offset:Int!, project_match_id:Int, ids:[Int], orderBy:String):[Task!]!
        supply_product_versions(first:Int!, offset:Int!, product_ids:[String], orderBy:String):[Product!]!
        supply_services(first:Int!, offset:Int!, service_ids:[Int], orderBy:String):[Service!]!
    }
    type Project {
        id:Int! is_deleted:Boolean! customer_id:Int project_match_assignments:[Assignment!]!
        file_uploads(first:Int!,offset:Int!,orderBy:String):[Upload!]!
        customer_documents(first:Int!,offset:Int!,orderBy:String):[CustomerDocument!]!
        histories(first:Int!,offset:Int!):[History!]!
    }
    type Assignment { user_id:Int! }
    type History { id:Int! }
    type Task { id:Int! target_project_match_id:Int author_user_id:Int target_user_id:Int }
    type Upload { id:Int! uuid:String! category:String }
    type CustomerDocument { id:Int! file_upload:Upload }
    type Product { product_id:String! name:String }
    type Service { id:Int! name:String }
""")
raw = introspection_from_schema(SCHEMA)
SNAPSHOT = StoredSnapshot("scoped-fixture", raw, compiled_schema(raw).fingerprint, "source")
ROOTS = {
    "HeroProjectMatches": (
        "project_matches",
        "id customer_id is_deleted project_match_assignments { user_id }",
    ),
    "HeroTasks": ("tasks", "id target_project_match_id author_user_id target_user_id"),
    "HeroProducts": ("supply_product_versions", "product_id name"),
    "HeroServices": ("supply_services", "id name"),
}


def saved_read(operation, *, plan=None):
    root, fields = ROOTS[operation]
    key = "product_id" if operation == "HeroProducts" else "id"
    document = (
        "query Saved($first:Int!, $offset:Int!) { "
        + root
        + '(first:$first, offset:$offset, orderBy:"'
        + key
        + '") {'
        + fields
        + "} }"
    )
    return runtime.DirectRead(
        operation,
        plan or SimpleNamespace(source_identity=lambda row: str(row[key])),
        document,
        root,
        {"first": "$page_size", "offset": "$offset"},
        17,
        "approved",
        "mapping",
        SNAPSHOT,
        service_revision=3,
    )


class Client:
    def __init__(self, root):
        self.root = root
        self.calls = []

    def query(self, document, variables):
        self.calls.append((document, variables))
        result = graphql_sync(SCHEMA, document, root_value=self.root, variable_values=variables)
        if result.errors:
            raise AssertionError(str(result.errors))
        return result.data


def inventory_client(inventory):
    root = {}
    for operation, (field, _) in ROOTS.items():
        key = "product_id" if operation == "HeroProducts" else "id"
        arg = {"HeroProducts": "product_ids", "HeroServices": "service_ids"}.get(operation, "ids")

        def resolve(info, first, offset, *, rows=inventory.get(operation, ()), key=key, arg=arg, **kwargs):
            selected = rows
            if arg in kwargs:
                selected = [r for r in rows if r[key] in kwargs[arg]]
            if "project_match_id" in kwargs:
                selected = [r for r in selected if r["target_project_match_id"] == kwargs["project_match_id"]]
            return selected[offset : offset + first]

        root[field] = resolve
    return Client(root)


class TestScopedReads(TestCase):
    def test_projects_are_selected_without_loading_histories_then_relations_by_exact_ids(self):
        projects = [
            dict(id=i, customer_id=90, is_deleted=False, project_match_assignments=[{"user_id": 7}])
            for i in range(1, 19)
        ]
        client = inventory_client({"HeroProjectMatches": projects})
        read = saved_read("HeroProjectMatches")
        lean = replace(read, projection_paths=("project_matches.id", "project_matches.is_deleted"))
        budget = SimpleNamespace(limit=10)
        rows = next(scoped.project_sample_pages(client, lean, budget, Mock()))
        self.assertEqual(rows, [{"id": i, "is_deleted": False} for i in range(1, 9)])
        rows = list(scoped.project_sample_pages(client, read, budget, Mock(), ids=["16", "7"]))
        self.assertEqual(rows, [[projects[15], projects[6]]])
        self.assertEqual(len(client.calls), 2)

    def test_task_discovery_matches_legacy_members_without_unrelated_rows(self):
        tasks = [
            dict(id=i, target_project_match_id=2 if i % 4 == 0 else 9, author_user_id=1, target_user_id=3)
            for i in range(1, 41)
        ]
        client = inventory_client({"HeroTasks": tasks})
        read = saved_read("HeroTasks")
        actual = scoped.discovery_batch(client, read, "HeroTasks", {"2": 0, "4": 0})
        self.assertEqual(actual, {"2": [t for t in tasks if t["target_project_match_id"] == 2], "4": []})
        self.assertEqual(len(client.calls), 1)
        pages = scoped.discovery_batch(client, read, "HeroTasks", {"9": 17, "2": 4})
        self.assertEqual(pages["2"], actual["2"][4:])
        self.assertEqual(pages["9"], [t for t in tasks if t["target_project_match_id"] == 9][17:])

    def test_catalog_projects_only_ids_and_fetches_only_requested_details(self):
        client = inventory_client(
            {"HeroProducts": [dict(product_id=str(i), name="Part") for i in range(300)]}
        )
        read = saved_read("HeroProducts")
        page, _ = scoped.catalog_pages(client, read, Mock())
        self.assertEqual(page(200, 3), ["200", "201", "202"])
        field = parse(client.calls[0][0]).definitions[0].selection_set.selections[0]
        self.assertEqual([leaf.name.value for leaf in field.selection_set.selections], ["product_id"])
        self.assertEqual(
            [r["product_id"] for r in scoped.exact_rows(client, read, ["201", "3"])], ["201", "3"]
        )
        self.assertEqual(len(client.calls), 2)
        with self.assertRaisesRegex(ValueError, "no longer"):
            scoped.exact_rows(client, read, ["9999999"])
        self.assertTrue(all("product_ids:" in doc for doc, _ in client.calls[1:]))
        with self.assertRaisesRegex(ValueError, "ordering"):
            scoped.catalog_pages(
                client, replace(read, document=read.document.replace('"product_id"', '"name"')), None
            )

    def test_file_predicate_preserves_approved_category_and_refuses_conflicting_uuid_filter(self):
        query = '{FileUpload_FileUploads(filters:{category:{equals:"images"}}){edges{node{id uuid}}}}'
        restricted = scoped.narrow(query, "HeroPictures", "FileUpload_FileUploads", ["one", "two"])
        field = parse(restricted).definitions[0].selection_set.selections[0]
        self.assertEqual(
            value_from_ast_untyped(field.arguments[0].value),
            {"category": {"equals": "images"}, "uuid": {"equalsAny": ["one", "two"]}},
        )
        with self.assertRaisesRegex(ValueError, "intersect"):
            scoped.narrow(restricted, "HeroPictures", "FileUpload_FileUploads", ["three"])
        with self.assertRaisesRegex(ValueError, "foreign"):
            with patch.object(runtime, "fetch_page", return_value=[{"id": 1, "uuid": "wrong"}]):
                read = replace(
                    saved_read("HeroTasks"),
                    document=query,
                    operation="HeroPictures",
                    records_path="FileUpload_FileUploads.edges.node",
                )
                scoped.exact_rows(Mock(), read, ["1"], uuids={"1": "one"}, allow_absent=True)
        with patch.object(runtime, "fetch_page", side_effect=[[{"id": 2, "uuid": "two"}], []]) as fetch:
            rows = scoped.exact_rows(
                Mock(), read, ["1", "2"], uuids={"1": "one", "2": "two"}, allow_absent=True
            )
        self.assertEqual(rows, [{"id": 2, "uuid": "two"}])
        self.assertEqual(fetch.call_count, 2)

    def test_native_writer_source_keeps_local_offset_out_of_wire_filter(self):
        tasks = [
            dict(id=i, target_project_match_id=2, author_user_id=1, target_user_id=3) for i in range(1, 80)
        ]
        client = inventory_client({"HeroTasks": tasks})
        read = saved_read("HeroTasks")
        contract = SimpleNamespace(
            target_operation="HeroTasks",
            records_path="tasks",
            identity_path="id",
            variables=read.variables,
            page_size=2,
        )
        source = GraphQLServiceSource(
            client,
            SimpleNamespace(query_document=read.document),
            contract,
            "source",
            revalidate=lambda: contract,
        )
        source._stored_snapshot = lambda: SNAPSHOT
        cohort = {
            "version": 2,
            "project_ids": ["2"],
            "task_ids": ["79", "3", "51"],
            "scoped_project_reads": True,
        }
        selection = migration_testing.RelatedProjectSource(source, 1, cohort)
        self.assertEqual([r["id"] for r in selection.projection(0).records], [3, 51])
        self.assertEqual([r["id"] for r in selection.projection(2).records], [79])
        self.assertIsNone(selection.projection(3).next_offset)
        self.assertEqual([variables["offset"] for _, variables in client.calls], [0, 0])
        self.assertTrue(all("ids:" in document for document, _ in client.calls))

    def test_history_followups_remain_pinned_to_the_frozen_project(self):
        contract = SimpleNamespace(
            target_operation="HeroProjectMatches",
            records_path="project_matches",
            identity_path="id",
            page_size=1,
            variables={"first": "$page_size", "offset": "$offset", "history_offset": 0},
            adapter_options={
                "logbook": {
                    "path": "histories",
                    "identity_path": "id",
                    "limit": 1,
                    "offset_variable": "history_offset",
                    "max_pages": 5,
                }
            },
        )
        document = (
            "query Q($first:Int!,$offset:Int!,$history_offset:Int!){project_matches(first:$first,"
            "offset:$offset){id histories(first:1,offset:$history_offset){id}}}"
        )

        def projects(info, first, offset, ids):
            self.assertEqual((first, offset, ids), (1, 0, [23]))
            return [
                {
                    "id": 23,
                    "histories": lambda info, first, offset: [{"id": 1}, {"id": 2}][offset : offset + first],
                }
            ]

        client = Client({"project_matches": projects})
        source = GraphQLServiceSource(
            client, SimpleNamespace(query_document=document), contract, "source", revalidate=lambda: contract
        )
        source._stored_snapshot = lambda: SNAPSHOT
        source.scoped_test_ids = ("19", "23")
        page = source.projects(1)
        self.assertEqual(page.records[0]["histories"], [{"id": 1}, {"id": 2}])
        self.assertTrue(page.records[0]["_hero_logbook_complete"])
        self.assertEqual([var["history_offset"] for _, var in client.calls], [0, 1, 2])

    def test_progress_reports_frozen_reader_phase_before_first_child(self):
        from hero_integration.services import file_migration, run_progress

        row = SimpleNamespace(
            action=file_migration.FULL_ACTION,
            name="full",
            state="Running",
            execution_budget_run=None,
            source_instance="fixture",
        )
        with (
            patch.object(run_progress, "_card", return_value={"active": True}),
            patch.object(
                file_migration,
                "_full",
                return_value={
                    "scoped_project_reads": True,
                    "related_test_discovery": {"operation_index": 4, "offset": 35, "rows_read": 37},
                },
            ),
        ):
            result = run_progress.migration(row)
        self.assertEqual(
            (result["phase"], result["checkpoint"], result["counts"]["rows_read"]), ("file_uploads", 35, 37)
        )
        self.assertEqual(result["counts"]["processed"], 0)

    def test_project_upload_join_duplicates_within_and_across_pages(self):
        one = {"id": 1, "uuid": "first", "category": "images"}
        two = {"id": 2, "uuid": "second", "category": "images"}
        self.assertEqual(scoped._unique_upload_rows([one, dict(one), two], 0, None), [one, two])
        self.assertEqual(scoped._unique_upload_rows([one, one], 1, one), [])
        self.assertEqual(scoped._unique_upload_rows([one, two], 1, one), [two])
        for rows in ([dict(one, uuid="changed")], [dict(one, category="documents")], [two, one]):
            with self.assertRaises(ValueError):
                scoped._unique_upload_rows(rows, 1, one)

    def test_nested_project_branches_keep_offsets_separate_and_share_file_identity(self):
        file = {"id": 17, "uuid": "shared", "category": "images"}

        def project(info, project_match_id):
            return {
                "id": project_match_id,
                "file_uploads": lambda info, first, offset, orderBy: [file][offset : offset + first],
                "customer_documents": lambda info, first, offset, orderBy: [{"id": 3, "file_upload": file}][
                    offset : offset + first
                ],
            }

        client = Client({"project_match": project})
        read = saved_read("HeroProjectMatches")
        uploads = scoped.discovery_batch(client, read, "file_uploads", {"1": 0, "2": 0})
        documents = scoped.discovery_batch(client, read, "customer_documents", {"1": 1, "2": 0})
        self.assertEqual(uploads, {"1": [file], "2": [file]})
        self.assertEqual(documents, {"1": [], "2": [{"id": 3, "file_upload": file}]})
        self.assertEqual(len(client.calls), 2)

    def test_new_parent_freezes_independent_principals_before_child_dispatch(self):
        from hero_integration.services import execution_budget, file_migration as fm, file_transfer
        from contextlib import ExitStack

        projects = [
            {"id": i, "customer_id": 71, "is_deleted": i == 1, "project_match_assignments": []}
            for i in range(1, 8)
        ]
        client = inventory_client(
            {
                "HeroProjectMatches": projects,
                "HeroProducts": [{"product_id": str(i)} for i in (14, 15, 16)],
                "HeroServices": [{"id": i} for i in (43, 44, 45)],
            }
        )
        budget = Mock(limit=2)
        budget.incremental_ids.side_effect = lambda op, kind, pages: self.take(pages, 2)
        budget.incremental_group.side_effect = lambda op, kind, pages, catalog: {"ids": catalog[0](0, 2)}
        config = SimpleNamespace(source_binding_revision=3, source_instance="source")
        run = SimpleNamespace(
            name="full", budget_contract_version=3, testing_enabled=1, testing_max_actions=2
        )
        plan = SimpleNamespace(
            source_identity=lambda row: str(row["id"]),
            definitions=[
                SimpleNamespace(
                    source_path="customer_id",
                    transformation="HERO Customer Link",
                    purpose="Customer Relation",
                )
            ],
        )
        with ExitStack() as stack:
            for seam in (
                patch.object(fm, "bound_configuration", return_value=config),
                patch.object(fm, "_full", return_value={"scoped_project_reads": True}),
                patch.object(execution_budget, "for_run", return_value=budget),
                patch.object(fm, "frappe", SimpleNamespace(db=Mock(get_value=Mock(return_value="Running")))),
                patch.object(file_transfer, "_client_and_downloader", return_value=(client, None, "source")),
                patch.object(
                    runtime, "approved_read", side_effect=lambda op, instance: saved_read(op, plan=plan)
                ),
            ):
                stack.enter_context(seam)
            result = fm.prepare_related_test(run)
        self.assertEqual(result["project_ids"], ["2", "3"])
        self.assertEqual(result["customer_ids"], ["71"])
        self.assertEqual(
            result["additional_cohorts"], {"supply_product": ["14", "15"], "supply_service": ["43", "44"]}
        )

    @staticmethod
    def take(pages, count):
        from itertools import chain, islice

        return list(islice(chain.from_iterable(pages), count))
