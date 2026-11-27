"""Archive selection belongs to an admitted migration, never to polling or writeback."""
from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

# The file connection has no lifecycle field. Its verified FileUpload detail does.
FIELDS = {
    "HeroCustomers": "is_deleted", "HeroProjectMatches": "is_deleted",
    "HeroTasks": "is_deleted", "HeroCalendarEvents": "deleted",
    "HeroProducts": "is_deleted", "HeroServices": "is_deleted",
    "HeroDocuments": "is_deleted", "HeroPictures": "is_deleted",
}
SHOW_DELETED_ROOTS = {"contacts", "tasks", "calendar_events"}
_policy = ContextVar("hero_migration_include_archived", default=None)


@contextmanager
def selection(include):
    token = _policy.set(None if include is None else bool(include))
    try:
        yield
    finally:
        _policy.reset(token)


def run_selection(run):
    if getattr(run, "inventory_sync_run", None):
        return selection(None)
    return selection(int(getattr(run, "migrate_archived_records", 0) or 0))


def for_run(fn):
    """Use the full run’s frozen selection in independently scheduled discovery jobs."""
    @wraps(fn)
    def execute(run, *args, **kwargs):
        with run_selection(run):
            return fn(run, *args, **kwargs)
    return execute


def active():
    return _policy.get() is not None


def includes_archived():
    return _policy.get() is True


def excluded(operation, record):
    return _policy.get() is False and operation in FIELDS and record.get(FIELDS[operation]) is True


def primary_address_excluded(record, address_id):
    if _policy.get() is not False:
        return False
    # The Address type has no archive flag. CustomerAddress carries it on the relation.
    links = [link for link in (record.get("customer_addresses") or ()) if isinstance(link, dict)
             and isinstance(link.get("address"), dict) and link["address"].get("id") == address_id]
    return bool(links) and all(excluded("HeroCustomers", link) for link in links)


@dataclass(frozen=True)
class CatalogPage:
    identities: list[str]
    eligible: list[str]


def list_flag(read):
    """Resolve the root record's Boolean flag on this reader's actual schema."""
    from graphql import get_named_type
    from hero_integration.services.graphql_schema_cache import compiled_schema
    name = FIELDS.get(read.operation)
    field = compiled_schema(read.snapshot.snapshot_json).core.query_type.fields.get(read.records_path)
    member = get_named_type(field.type).fields.get(name) if field else None
    return name if member and get_named_type(member.type).name == "Boolean" else None


def request(snapshot, document, variables, *, root, include_for_identity=False, **projection):
    """Change only the archive predicate of migration queries. Validate the resulting wire query."""
    from hero_integration.services.mapping_contract_v1 import materialize_saved_query

    if active() and root in SHOW_DELETED_ROOTS:
        from graphql import get_named_type, parse, print_ast
        from hero_integration.services.graphql_schema_cache import compiled_schema
        from graphql.language.ast import ArgumentNode, BooleanValueNode, NameNode, VariableNode
        from hero_integration.services.scoped_test_reads import root_of

        root_field = compiled_schema(snapshot.snapshot_json).core.query_type.fields.get(root)
        argument = root_field.args.get("show_deleted") if root_field else None
        if argument is None or get_named_type(argument.type).name != "Boolean":
            return materialize_saved_query(snapshot, document, variables, **projection)
        tree = parse(document)
        field = root_of(tree, root)
        include = includes_archived() or include_for_identity
        existing = next((arg for arg in field.arguments if arg.name.value == "show_deleted"), None)
        if existing and isinstance(existing.value, VariableNode):
            from graphql import Visitor, visit
            name = existing.value.name.value
            uses = []
            class Uses(Visitor):
                def enter_variable(self, node, *_):
                    if node.name.value == name:
                        uses.append(node)
            visit(tree.definitions[0].selection_set, Uses())
            if len(uses) == 1:
                return materialize_saved_query(snapshot, document, {**variables, name: include}, **projection)
        if existing is None or isinstance(existing.value, (VariableNode, BooleanValueNode)):
            value = BooleanValueNode(value=include)
            if existing:
                existing.value = value
            else:
                field.arguments = (*field.arguments, ArgumentNode(name=NameNode(value="show_deleted"), value=value))
            document = print_ast(tree)
        else:
            raise ValueError("Unsupported archive predicate in the approved migration query.")
    return materialize_saved_query(snapshot, document, variables, **projection)
