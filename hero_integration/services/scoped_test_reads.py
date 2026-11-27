"""Bound HERO test reads to frozen IDs or an explicit project relationship."""

from dataclasses import replace

from graphql import parse, print_ast
from graphql.language import OperationType
from graphql.language.ast import (
    ArgumentNode,
    FieldNode,
    IntValueNode,
    ListValueNode,
    NameNode,
    ObjectFieldNode,
    ObjectValueNode,
    OperationDefinitionNode,
    StringValueNode,
    VariableNode,
)

from hero_integration.services import migration_archive
from hero_integration.services import direct_runtime
from hero_integration.services.mapping_contract_v1 import materialize_saved_query
from hero_integration.services.project_identity_read import CollectionIdentityReader


def root_of(tree, root):
    if len(tree.definitions) != 1:
        raise ValueError("Scoped test reads require one saved query.")
    op = tree.definitions[0]
    if (
        not isinstance(op, OperationDefinitionNode)
        or op.operation != OperationType.QUERY
        or len(op.selection_set.selections) != 1
    ):
        raise ValueError("Invalid scoped test query.")
    field = op.selection_set.selections[0]
    if not isinstance(field, FieldNode) or field.name.value != root or field.directives:
        raise ValueError("Unexpected root in scoped test query.")
    return field


def narrow(document, operation, root, values, *, by_project=False):
    """Retain every saved argument and selection. Never send an unfiltered fallback."""
    tree = parse(document)
    field = root_of(tree, root)
    if not values:
        raise ValueError("Empty identity predicate.")
    if by_project:
        if (operation, root) not in (
            ("HeroTasks", "tasks"),
            ("HeroCalendarEvents", "calendar_events"),
        ) or len(values) != 1:
            raise ValueError("Unsupported project predicate.")
        name, value = "project_match_id", IntValueNode(value=str(int(values[0])))
    elif operation in ("HeroDocuments", "HeroPictures") and root == "FileUpload_FileUploads":
        items = tuple(StringValueNode(value=str(v)) for v in values)
        predicate = ObjectFieldNode(
            name=NameNode(value="uuid"),
            value=ObjectValueNode(
                fields=(ObjectFieldNode(name=NameNode(value="equalsAny"), value=ListValueNode(values=items)),)
            ),
        )
        existing = next((arg for arg in field.arguments if arg.name.value == "filters"), None)
        if existing:
            if not isinstance(existing.value, ObjectValueNode) or any(
                f.name.value == "uuid" for f in existing.value.fields
            ):
                raise ValueError("Cannot intersect the approved file filters.")
            existing.value.fields = (*existing.value.fields, predicate)
            return print_ast(tree)
        name, value = "filters", ObjectValueNode(fields=(predicate,))
    else:
        spec = (
            ("ids", "id", "Int")
            if (operation, root) == ("HeroProjectMatches", "project_matches")
            else CollectionIdentityReader.ROOTS.get((operation, root))
        )
        if not spec:
            raise ValueError("Missing a verified identity argument for " + operation)
        name, _, scalar = spec
        items = (
            StringValueNode(value=str(v)) if scalar == "String" else IntValueNode(value=str(int(v)))
            for v in values
        )
        value = ListValueNode(values=tuple(items))
    if any(arg.name.value == name for arg in field.arguments):
        raise ValueError("Cannot replace an existing identity predicate.")
    field.arguments = (*field.arguments, ArgumentNode(name=NameNode(value=name), value=value))
    return print_ast(tree)


def exact_rows(client, read, identities, *, verify=None, uuids=None, allow_absent=False):
    """Return the exact ordered sample. Missing/foreign rows never initiate a list scan."""
    identities = list(map(str, identities))
    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate frozen test identity.")
    ordered = []
    for start in range(0, len(identities), read.page_size):
        batch = identities[start : start + read.page_size]
        keys = [uuids[key] for key in batch] if uuids is not None else batch
        restricted = replace(
            read, document=narrow(read.document, read.operation, read.records_path.split(".")[0], keys)
        )
        found = {}
        while len(found) < len(batch):
            if verify:
                verify()
            rows = direct_runtime.fetch_page(client, restricted, len(found), len(batch) - len(found))
            if verify:
                verify()
            if not rows:
                if allow_absent:
                    break
                raise ValueError("A frozen test identity is no longer returned by its approved reader.")
            for row in rows:
                key = str(read.plan.source_identity(row))
                if key not in batch or key in found or (uuids is not None and row.get("uuid") != uuids[key]):
                    raise ValueError("HERO returned a foreign or repeated frozen identity.")
                found[key] = row
        ordered.extend(found[key] for key in batch if key in found)
    return ordered


def catalog_pages(client, read, verify):
    """Read only the ordered identity column. Resume using a checked source position."""
    from hero_integration.services.migration_testing import project_reader_fingerprint

    root = read.records_path
    key = "product_id" if read.operation == "HeroProducts" else "id"
    field = root_of(parse(read.document), root)
    args = {arg.name.value: arg.value for arg in field.arguments}
    order = args.get("orderBy")
    if isinstance(order, VariableNode):
        order = read.variables.get(order.name.value)
    else:
        order = getattr(order, "value", None)
    if order != key or any(
        not isinstance(args.get(k), VariableNode) or read.variables.get(args[k].name.value) != v
        for k, v in [("first", "$page_size"), ("offset", "$offset")]
    ):
        raise ValueError("Catalog selection requires approved identity ordering, limit and offset.")
    flag = migration_archive.list_flag(read) if migration_archive.active() and not migration_archive.includes_archived() else None
    columns = (key, flag) if flag else (key,)
    minimal = replace(read, projection_paths=tuple(root + "." + col for col in columns), projection_preserve_paths=())

    def page(offset, size):
        if verify:
            verify()
        rows = direct_runtime.fetch_page(client, minimal, offset, size)
        if verify:
            verify()
        ids, eligible = [], []
        for row in rows:
            value = row.get(key)
            if type(value) not in (str, int) or not str(value).strip():
                raise ValueError("Catalog selection returned an invalid identity.")
            if flag and flag not in row:
                raise ValueError("Catalog response omitted the archive field.")
            ids.append(str(value))
            if flag and not migration_archive.excluded(read.operation, row):
                eligible.append(str(value))
        return migration_archive.CatalogPage(ids, eligible) if flag else ids

    return page, project_reader_fingerprint(read, version=2)


# One small batch of independently pageable project branches per checkpoint.
PROJECT_BATCH = 8
SCOPED_STEPS = (
    "HeroTasks",
    "HeroCalendarEvents",
    "HeroEmployees",
    "HeroCustomers",
    "file_uploads",
    "customer_documents",
)


def send_branches(client, read, branches):
    """Combine schema-checked branches without sharing filter or pagination variables."""
    from graphql import Visitor, visit
    from graphql.language.ast import DocumentNode, SelectionSetNode

    declarations, selections, variables = [], [], {}
    for alias, request in branches:

        class BranchVariables(Visitor):
            def enter_variable(self, node, *_):
                return VariableNode(name=NameNode(value=alias + "_" + node.name.value))

        branch = visit(parse(request.document), BranchVariables()).definitions[0]
        branch.selection_set.selections[0].alias = NameNode(value=alias)
        selections.extend(branch.selection_set.selections)
        declarations.extend(branch.variable_definitions)
        variables.update({alias + "_" + k: v for k, v in request.variables.items()})
    op = OperationDefinitionNode(
        operation=OperationType.QUERY,
        name=NameNode(value="ScopedProjectRelations"),
        variable_definitions=tuple(declarations),
        selection_set=SelectionSetNode(selections=tuple(selections)),
    )
    document = print_ast(DocumentNode(definitions=(op,)))
    batch = materialize_saved_query(read.snapshot, document, variables)
    result = client.query(batch.document, batch.variables)
    if not isinstance(result, dict) or set(result) != {alias for alias, _ in branches}:
        raise ValueError("Incomplete response to scoped test query.")
    return result


def project_sample_pages(client, read, budget, verify, *, ids=None):
    """Batch the saved one-project reader while omitting expensive project histories."""
    width = min(PROJECT_BATCH, budget.limit)
    previous = None
    for start in range(0, len(ids) if ids is not None else 1000 * width, width):
        branches = []
        count = min(width, len(ids) - start) if ids is not None else width
        for index in range(start, start + count):
            document = read.document
            if ids is not None:
                document = narrow(document, read.operation, read.records_path, [ids[index]])
            request = materialize_saved_query(
                read.snapshot,
                document,
                direct_runtime.page_variables(read, 0 if ids is not None else index, 1),
                selected_paths=read.projection_paths,
            )
            branches.append(("p" + str(index), request))
        verify()
        result = send_branches(client, read, branches)
        verify()
        rows = []
        ended = False
        for index in range(start, start + count):
            part = result["p" + str(index)]
            if not isinstance(part, list) or len(part) > 1:
                raise ValueError("Invalid one-project response.")
            if not part and ids is None:
                ended = True
                continue
            if ended or not part or not isinstance(part[0], dict):
                raise ValueError("Project missing from the selected page.")
            key = part[0].get("id")
            if type(key) is not int or (ids is not None and str(key) != str(ids[index])):
                raise ValueError("Project reader returned a foreign identity.")
            if ids is None and previous is not None and key <= previous:
                raise ValueError("Project list no longer has stable ascending identities.")
            previous = key
            rows.append(part[0])
        yield rows
        if ended:
            return
    if ids is None:
        raise ValueError("Incremental project selection exceeded its bounded page allowance.")


def discovery_batch(client, read, operation, cursors):
    branches = []
    nested = operation in ("file_uploads", "customer_documents")
    size = min(100, read.page_size) if not nested else 100
    for project, offset in cursors.items():
        alias = "p" + str(int(project))
        if nested:
            select = (
                "id file_upload { id uuid category }"
                if operation == "customer_documents"
                else "id uuid category"
            )
            document = (
                "query ProjectFiles($id:Int!, $first:Int!, $offset:Int!) { "
                + alias
                + ": project_match(project_match_id:$id) { id "
                + operation
                + '(first:$first,offset:$offset,orderBy:"id") { '
                + select
                + " } } }"
            )
            request = materialize_saved_query(
                read.snapshot, document, dict(id=int(project), first=size, offset=offset)
            )
        else:
            root = read.records_path
            document = narrow(read.document, operation, root, [project], by_project=True)
            leaves = (
                ("id", "target_project_match_id", "author_user_id", "target_user_id")
                if operation == "HeroTasks"
                else ("id", "project_match_id", "partners.id")
            )
            if migration_archive.active() and (flag := migration_archive.list_flag(read)):
                leaves = (*leaves, flag)
            request = migration_archive.request(
                read.snapshot,
                document,
                direct_runtime.page_variables(read, offset, size), root=root,
                selected_paths=tuple(root + "." + k for k in leaves),
            )
        branches.append((alias, request))
    result = send_branches(client, read, branches)
    pages = {}
    for project in cursors:
        value = result.get("p" + str(int(project)))
        if nested:
            if not isinstance(value, dict) or str(value.get("id")) != project:
                raise ValueError("Missing or foreign project in scoped file discovery.")
            value = value.get(operation)
        if (
            not isinstance(value, list)
            or len(value) > size
            or any(not isinstance(row, dict) for row in value)
        ):
            raise ValueError("Invalid project relationship page.")
        pages[project] = value
    return pages


def _unique_upload_rows(rows, last_id, last_row):
    """HERO can return multiple identical join rows for a single project upload."""
    unique = []
    for row in rows:
        identity = row.get("id")
        if type(identity) is not int or identity <= 0 or identity < last_id:
            raise ValueError("Invalid or unordered project upload identity.")
        if identity == last_id:
            if row != last_row:
                raise ValueError("Conflicting project upload identity at the same source position.")
            continue
        last_id, last_row = identity, row
        unique.append(row)
    return unique


def discover(client, instance, state, cohort, page_budget, verify_run):
    from hero_integration.services import migration_testing as test
    from hero_integration.services.file_transfer import details, RELATION_PATHS

    progress = state.setdefault("related_test_discovery", {"operation_index": 0, "offset": 0, "pages": 0})
    fingerprints = cohort["projection_reader_fingerprints"]
    reads = {}

    def reader(op):
        if op not in reads:
            rd = direct_runtime.approved_read(op, instance)
            if rd is None:
                raise ValueError("Missing approved project cohort reader: " + op)
            stamp = test.project_reader_fingerprint(rd, version=2)
            expected = (
                cohort["project_reader_fingerprint"]
                if op == "HeroProjectMatches"
                else fingerprints.setdefault(op, stamp)
            )
            if stamp != expected:
                raise ValueError("Project cohort reader changed: " + op)
            reads[op] = rd
        return reads[op]

    projects = cohort["project_ids"]
    if not projects:
        verify_run()
        return True
    for _ in range(page_budget):
        idx = progress["operation_index"]
        if idx == len(SCOPED_STEPS):
            break
        op = SCOPED_STEPS[idx]
        if op in ("HeroEmployees", "HeroCustomers"):
            rd = reader(op)
            if op == "HeroEmployees":
                prefix = rd.records_path + "."
                required = (prefix + "id",)
                optional = (prefix + "user_id", prefix + "user.id")
                identity_read = replace(rd, projection_paths=required, projection_preserve_paths=optional)
                employees = direct_runtime.fetch_page(client, identity_read, 0)
                if any("user_id" not in row and "user" not in row for row in employees):
                    raise ValueError("Employee reader lacks the user relationship.")
                test.collect_project_employees(cohort, employees)
                progress["rows_read"] = progress.get("rows_read", 0) + len(employees)
            progress.update(operation_index=idx + 1, offset=0)
            continue
        nested = op in ("file_uploads", "customer_documents")
        rd = reader("HeroProjectMatches" if nested else op)
        if nested:
            reader("HeroPictures")
            reader("HeroDocuments")
        cursors = progress.setdefault("project_offsets", dict.fromkeys(projects, 0))
        tails = progress.setdefault("last_ids", {})
        if not cursors:
            progress.pop("project_offsets")
            progress.pop("last_ids")
            progress.pop("last_upload_rows", None)
            progress.update(operation_index=idx + 1, offset=0)
            continue
        if progress["pages"] >= 10000:
            raise ValueError("Project dependency discovery exceeded its bounded page allowance.")
        verify_run()
        pages = discovery_batch(client, rd, op, dict(list(cursors.items())[:PROJECT_BATCH]))
        progress["rows_read"] = progress.get("rows_read", 0) + sum(len(rows) for rows in pages.values())
        candidates = {}
        for project, rows in pages.items():
            if not rows:
                del cursors[project]
                continue
            raw_count = len(rows)
            if op == "file_uploads":
                upload_tails = progress.setdefault("last_upload_rows", {})
                final_row = rows[-1]
                rows = _unique_upload_rows(rows, tails.get(project, 0), upload_tails.get(project))
                upload_tails[project] = final_row
            ids = [row.get("id") for row in rows]
            if (
                any(type(i) is not int or i <= 0 for i in ids)
                or ids != sorted(set(ids))
                or (ids and ids[0] <= tails.get(project, 0))
            ):
                raise ValueError("Scoped project page returned invalid, repeated or unordered IDs.")
            if ids:
                tails[project] = ids[-1]
            if nested:
                for row in rows:
                    item = row["file_upload"] if op == "customer_documents" else row
                    if item is None:
                        continue
                    if (
                        not isinstance(item, dict)
                        or not item.get("uuid")
                        or type(item.get("id")) is not int
                        or "category" not in item
                    ):
                        raise ValueError("Invalid file identity on selected project.")
                    if candidates.setdefault(item["uuid"], item) != item:
                        raise ValueError("Conflicting file identity on selected projects.")
            else:
                path = "target_project_match_id" if op == "HeroTasks" else "project_match_id"
                if any(str(r.get(path)) != project for r in rows):
                    raise ValueError("HERO ignored the requested project filter.")
                test.collect_related_projection(cohort, op, rows)
            cursors[project] += raw_count
            progress["offset"] += raw_count
        for file_op in ("HeroDocuments", "HeroPictures") if candidates else ():
            selected = []
            known = cohort["files"].get(file_op, {})
            for row in candidates.values():
                if direct_runtime.belongs(file_op, row):
                    proof = known.get(str(row["id"]))
                    if proof is None:
                        selected.append(row)
                    elif proof["uuid"] != row["uuid"]:
                        raise ValueError("Conflicting UUID for a project attachment.")
            if selected:
                found = details(
                    client, selected, reader(file_op), instance, allow_missing=True, paths=RELATION_PATHS
                )
                test.collect_project_files(cohort, file_op, selected, found)
                for row in selected:
                    if str(row["id"]) not in cohort["files"][file_op] and not migration_archive.excluded(file_op, found.get(row["uuid"]) or {}):
                        issue = [
                            "file_upload",
                            str(row["id"]),
                            "project",
                            "Project attachment lacks a verified detail relationship",
                        ]
                        if issue not in cohort["unresolved"]:
                            cohort["unresolved"].append(issue)
        progress["pages"] += 1
        if len(cohort["task_ids"]) + len(cohort["event_ids"]) > 50000:
            raise ValueError("Too many related tasks and events in scoped discovery.")
        verify_run()
    verify_run()
    for op, rd in reads.items():
        current = direct_runtime.approved_read(op, instance)
        if not current or test.project_reader_fingerprint(
            current, version=2
        ) != test.project_reader_fingerprint(rd, version=2):
            raise ValueError("Project cohort reader changed during discovery: " + op)
    return progress["operation_index"] == len(SCOPED_STEPS)
