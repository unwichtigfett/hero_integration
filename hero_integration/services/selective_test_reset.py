"""Plan and apply the selective reset through the existing test-mode service."""
from collections import Counter, defaultdict
from hashlib import file_digest, sha256
from pathlib import Path
import json
import re

from hero_integration.services.binding_registry import matches_incarnation

RUN = "HERO Integration Run"
RECORD = "HERO Integration Record"
LINK = "HERO Integration Link"
OBJECT = "HERO Object Binding"
FIELD = "HERO Field Binding"
PENDING = "HERO Pending Reference"
AUDIT = "HERO Mutation Audit"
PRIVATE_BLOB = re.compile(r"/private/files/hero-[0-9]+-([a-f0-9]{32})-[\w.\-]+")
REFERENCE_FIELD_TYPES = frozenset({"Attach", "Attach Image", "Code", "Data", "HTML Editor", "JSON",
                                   "Long Text", "Markdown Editor", "Small Text", "Text", "Text Editor"})
PROOF = "hero_test_creation"
SYNC_PROOF = "hero_test_sync"
BLOB_CLEANUP = "hero_test_blob_cleanup"
NATIVE_HISTORY = frozenset({"hero_to_frappe", "service_component_import", "service_number_repair",
                            "historical_service_material"})
NATIVE = frozenset({"Address", "Contact", "Customer", "Employee", "Event", "File",
                    "Item", "Item Price", "Project", "Task", "ToDo"})


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _row_state(doc, frappe):
    # Include native child rows, including custom fields and writes without modified timestamps.
    state = doc.as_dict()
    fields = set(frappe.db.get_table_columns(doc.doctype))
    result = {key: state.get(key) for key in fields}
    for table in doc.meta.get_table_fields():
        result[table.fieldname] = [_row_state(row, frappe) for row in doc.get(table.fieldname) or ()]
    return result


def capture_creation(doc, method=None):
    """Record native test ownership and carry it through provable test saves."""
    if doc.doctype not in NATIVE | {LINK, OBJECT, FIELD}:
        return
    import frappe
    from hero_integration.services.execution_budget import current_budget
    budget = current_budget()
    if not budget or budget.scope != "migration":
        return
    origin = frappe.db.get_value(RUN, budget.run_id, ["testing_enabled", "source_instance"], as_dict=True)
    if not origin or not origin.testing_enabled or origin.source_instance != budget.source_instance:
        return

    if method != "after_insert":
        # Production saves never refresh test proof. A test save may advance a
        # proof only from the exact previously confirmed native preimage.
        if doc.doctype not in NATIVE:
            return
        prior = doc.get_doc_before_save()
        rows = _all(frappe, RECORD, ["name", "run", "native_state_json"], {
            "technical_action": PROOF, "target_doctype": doc.doctype, "target_name": doc.name,
            "source_instance": budget.source_instance,
        })
        for row in rows:
            if not frappe.db.get_value(RUN, row.run, "testing_enabled"):
                continue
            proof = _ack_state(row)
            if proof.get("creation") != str(doc.creation):
                continue
            if prior:
                if proof.get("digest") != _digest(_row_state(prior, frappe)):
                    continue
            elif not doc.flags.in_insert or row.run != budget.run_id:
                continue
            stored = frappe.get_doc(doc.doctype, doc.name)
            proof["digest"] = _digest(_row_state(stored, frappe))
            frappe.db.set_value(RECORD, row.name, "native_state_json", json.dumps(proof), update_modified=False)
        return

    from hero_integration.services.integration_events import record_output
    stored = frappe.get_doc(doc.doctype, doc.name)
    record_output(run=budget.run_id, source_type=doc.doctype, source_id=doc.name,
                  source_instance=budget.source_instance, result="CREATED", action=PROOF,
                  target_doctype=doc.doctype, target_name=doc.name,
                  native_state={"creation": str(stored.creation), "digest": _digest(_row_state(stored, frappe))})


def _all(frappe, doctype, fields, filters=None):
    return frappe.get_all(doctype, fields=fields, filters=filters or {}, order_by="", limit_page_length=0)


def _related(frappe, doctype, field, values, fields=("name",)):
    if not values:
        return []
    return _all(frappe, doctype, fields, {field: ["in", sorted(values)]})


def _ack_state(row):
    try:
        data = json.loads(row.native_state_json or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _delete_order(candidates, dependencies, blocked):
    """Emit dependents first, retaining cycles and parents of retained records."""
    remaining = dict(candidates)
    result = []
    while remaining:
        ready = sorted(key for key in remaining if key not in blocked
                       and dependencies[key].isdisjoint(remaining.keys() | blocked))
        if not ready:
            break
        for key in ready:
            result.append(key)
            remaining.pop(key)
    return result, remaining


def _blob_path(frappe, url):
    if PRIVATE_BLOB.fullmatch(url or ""):
        return Path(frappe.get_site_path("private", "files", url.rsplit("/", 1)[-1]))
    return None


def _blob_unshared(frappe, url, file_name=""):
    match = PRIVATE_BLOB.fullmatch(url or "")
    if not match:
        return False
    # The storage identifier survives absolute URLs, query strings and escaping
    # of filename characters. Treat any occurrence as a possible shared use.
    reference = f"%{match[1]}%"
    if frappe.db.sql("SELECT 1 FROM tabSingles WHERE value LIKE %s LIMIT 1", (reference,)):
        return False
    for doctype, fields in _file_reference_fields(frappe):
        filters = {}
        if doctype == "File" and file_name:
            filters["name"] = ["!=", file_name]
        if doctype == RECORD:
            # Our own deferred cleanup receipt contains the URL by design.
            filters["technical_action"] = ["!=", BLOB_CLEANUP]
        if frappe.get_all(doctype, fields=["name"], filters=filters, order_by="", limit_page_length=1,
                          or_filters=[[field, "like", reference] for field in fields]):
            return False
    return True


def _same_blob(path, size, content_hash):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != size or not content_hash:
        return False
    with path.open("rb") as stream:
        return file_digest(stream, "md5").hexdigest() == content_hash


def _attachment(frappe, doc):
    url = doc.file_url or ""
    path = _blob_path(frappe, url)
    if doc.is_folder or not doc.is_private or doc.thumbnail_url or path is None:
        return None
    if doc.content_hash and frappe.db.exists("File", {"name": ["!=", doc.name], "content_hash": doc.content_hash}):
        return None
    if _same_blob(path, doc.file_size, doc.content_hash) and _blob_unshared(frappe, url, doc.name):
        return url, doc.file_size, doc.content_hash
    return None


def _reclaim_blob(frappe, url, size, content_hash, *, receipts):
    from hero_integration.services.file_transfer import _discard_unreferenced
    path = _blob_path(frappe, url)
    if path is None:
        return
    if _same_blob(path, size, content_hash) and _blob_unshared(frappe, url):
        _discard_unreferenced(url)
    if path.exists() or path.is_symlink():
        return
    # The file is gone. Retire only its cleanup receipts, without committing
    # work from other callbacks on the request connection.
    from hero_integration.client.settings import _configuration_connection
    db = _configuration_connection()
    try:
        db.sql("DELETE FROM `tabHERO Integration Record` WHERE name IN %s AND technical_action=%s",
               (tuple(receipts), BLOB_CLEANUP))
        db.commit()
    finally:
        db.close()


def _file_reference_fields(frappe):
    for dt in _all(frappe, "DocType", ["name", "issingle", "is_virtual"]):
        if dt.issingle or dt.is_virtual or not frappe.db.table_exists(dt.name):
            continue
        persisted = set(frappe.db.get_table_columns(dt.name))
        fields = [field.fieldname for field in frappe.get_meta(dt.name).fields
                  if field.fieldtype in REFERENCE_FIELD_TYPES and field.fieldname in persisted]
        if fields:
            yield dt.name, fields


def _references(frappe, doc):
    from frappe.model.delete_doc import get_dynamic_linked_docs, get_linked_docs

    refs = {(row["reference_doctype"], row["reference_docname"])
            for row in (*get_linked_docs(doc), *get_dynamic_linked_docs(doc))}
    # Native deletion ignores or asynchronously clears several references.
    for dt, kind, target in (
        ("File", "attached_to_doctype", "attached_to_name"),
        ("ToDo", "reference_type", "reference_name"),
        ("Comment", "reference_doctype", "reference_name"),
        ("Communication", "reference_doctype", "reference_name"),
        ("Activity Log", "reference_doctype", "reference_name"),
        ("Activity Log", "timeline_doctype", "timeline_name"),
        ("Notification Log", "document_type", "document_name"),
        ("Document Follow", "ref_doctype", "ref_docname"),
        ("DocShare", "share_doctype", "share_name"),
        ("Email Unsubscribe", "reference_doctype", "reference_name"),
        ("Submission Queue", "ref_doctype", "ref_docname"),
        ("Version", "ref_doctype", "docname"),
        ("View Log", "reference_doctype", "reference_name"),
    ):
        if frappe.db.table_exists(dt) and frappe.db.has_column(dt, target) and frappe.db.has_column(dt, kind):
            refs.update((dt, row.name) for row in _all(frappe, dt, ["name"], {kind: doc.doctype, target: doc.name}))
    if (doc.doctype == "Event" and frappe.db.table_exists("Cockpit Calendar Plan")
            and frappe.db.has_column("Cockpit Calendar Plan", "event")):
        refs.update(("Cockpit Calendar Plan", row.name) for row in
                    _all(frappe, "Cockpit Calendar Plan", ["name"], {"event": doc.name}))
    if doc.doctype == "Item":
        refs.update(("Bin", row.name) for row in _all(frappe, "Bin", ["name"], {"item_code": doc.name}))
    return refs


def _native_blockers(frappe, doc):
    # These native hooks mutate linked business records during deletion.
    if doc.doctype == "Employee" and doc.get("user_id"):
        yield ("User", doc.user_id)
    if doc.doctype == "Customer" and doc.get("lead_name"):
        yield ("Lead", doc.lead_name)
    if doc.doctype == "Task":
        for field, doctype in (("parent_task", "Task"), ("project", "Project")):
            if doc.get(field):
                yield (doctype, doc.get(field))
    if doc.doctype == "ToDo" and doc.get("reference_name"):
        if (doc.reference_type == "Task"
                and frappe.db.get_value("Task", doc.reference_name, "cockpit_area")):
            yield ("Cockpit task responsibility", doc.reference_name)
        yield (doc.reference_type, doc.reference_name)
    # These Contact/Address rows themselves are subject to shared party links.
    if doc.doctype in {"Contact", "Address"}:
        yield from ((row.link_doctype, row.link_name) for row in doc.get("links") or ())


def _cohort(frappe):
    runs = _all(frappe, RUN, ["name", "source_instance", "started_on", "creation", "testing_enabled",
                             "execution_budget_run", "mode", "finished_on"])
    roots = {r.name for r in runs if r.testing_enabled}
    selected = {r.name: r for r in runs if r.name in roots or r.execution_budget_run in roots}
    rows = _related(frappe, RECORD, "run", selected, [
        "name", "run", "source_type", "source_id", "target_doctype", "target_name",
        "technical_action", "result", "creation", "native_state_json",
    ])
    return selected, rows


def plan(frappe, *, lock=False):
    selected, records = _cohort(frappe)
    findings, candidates, dependencies = {}, {}, defaultdict(set)
    bindings = {LINK: set(), OBJECT: set(), FIELD: set()}
    protected_runs = set()
    evidence = defaultdict(list)
    for row in records:
        if row.target_doctype and row.target_name and row.technical_action in NATIVE_HISTORY | {PROOF}:
            evidence[row.target_doctype, row.target_name].append(row)

    audits = _all(frappe, AUDIT, ["name", "origin_event", "bound_object_key", "pending_target_key",
                                 "pending_object_key", "reservation_key"])
    audit_events = {r.origin_event for r in audits if r.origin_event}
    audit_objects = {r.bound_object_key for r in audits if r.bound_object_key}
    active_audit = any(r.pending_target_key or r.pending_object_key or r.reservation_key == "active" for r in audits)
    outputs = defaultdict(set)
    for row in records:
        if row.technical_action in NATIVE_HISTORY and row.target_name and row.source_id:
            src = selected[row.run].source_instance
            outputs[row.target_doctype, row.target_name].add((src, row.source_type, str(row.source_id)))

    test_identity_evidence = defaultdict(set)
    for row in records:
        if row.source_id and row.source_type and row.technical_action in NATIVE_HISTORY | {"identity_checkpoint"}:
            test_identity_evidence[selected[row.run].source_instance, row.source_type].add(str(row.source_id))
    evidence_groups = _group(evidence)
    target_bindings = defaultdict(set)
    binding_sources = {}
    for kind, type_key, identity_key in ((LINK, "source_type", "source_id"), (OBJECT, "object_type", "external_object_id")):
        columns = ["name", "target_doctype", "target_name", "target_incarnation", "source_instance", "creation",
                   type_key, identity_key, "source_system" if kind == LINK else "site_scope"]
        related = {row.name: row for row in _related(frappe, kind, "name", evidence_groups.get(kind, ()), columns)}
        for dt, names in evidence_groups.items():
            if dt in NATIVE:
                related.update((r.name, r) for r in _all(frappe, kind, columns,
                               {"target_doctype": dt, "target_name": ["in", sorted(names)]}))
        if kind == LINK:
            for (source, object_type), ids in test_identity_evidence.items():
                for r in _all(frappe, LINK, columns, {"source_instance": source, "source_system": "HERO",
                              "source_type": object_type, "source_id": ["in", sorted(ids)]}):
                    related[r.name] = r
                    if (r.target_doctype, r.target_name) not in evidence:
                        findings[r.target_doctype, r.target_name] = "Testidentität belegt, native Herkunft offen"
                        findings[LINK, r.name] = "historische Bindungsherkunft nicht eindeutig"
        for row in related.values():
            target = (row.target_doctype, row.target_name)
            key = (kind, row.name)
            relevant = (row.source_system == "HERO" if kind == LINK else row.site_scope == frappe.local.site)
            binding_sources[key] = row
            target_bindings[target].add(key)
            proof = evidence.get(key, ())
            if relevant and any(_ack_state(p).get("creation") == str(row.creation)
                                for p in proof if p.technical_action == PROOF):
                bindings[kind].add(row.name)
            elif relevant and target[0] in NATIVE:
                identity = (row.source_instance, row.get(type_key), str(row.get(identity_key)))
                observations = evidence.get(target, ())
                if identity in outputs[target] or any(str(p.source_id) == identity[2] for p in observations
                                                      if p.technical_action in NATIVE_HISTORY):
                    birth = frappe.db.get_value(target[0], target[1], "creation")
                    if birth and matches_incarnation(row.target_incarnation, birth) and any(
                            p.technical_action in NATIVE_HISTORY and p.result == "CREATED"
                            and selected[p.run].mode == "Execute" and selected[p.run].source_instance == identity[0]
                            and str(p.source_id) == identity[2] and p.source_type == identity[1]
                            and (selected[p.run].started_on or selected[p.run].creation) <= birth <= row.creation <= p.creation
                            for p in observations):
                        bindings[kind].add(row.name)
                    else:
                        findings[key] = "historische Bindungsherkunft nicht eindeutig"


    # A native production run using the same target keeps its links and baselines.
    regular_targets = set()
    for dt, names in _group(evidence.keys()).items():
        if dt not in NATIVE or not names:
            continue
        history = _all(frappe, RECORD, ["target_name", "run", "technical_action", "result"],
                       {"target_doctype": dt, "target_name": ["in", sorted(names)],
                        "technical_action": "hero_to_frappe"})
        regular_targets.update((dt, r.target_name) for r in history if r.run and r.run not in selected
                               and r.result not in {"SKIPPED_REJECTED", "BLOCKED_RECORD", "UNAVAILABLE"})

    for target in regular_targets:
        for kind, name in target_bindings[target]:
            bindings[kind].discard(name)
            findings[kind, name] = "regulärer HERO-Lauf verwendet dieselbe Identität"
    # Never leave half of a paired provenance binding behind. A historical
    # object binding can attest the incarnation of a legacy link with no birth field.
    for target, keys in target_bindings.items():
        for kind, name in sorted(keys):
            if kind != OBJECT or name not in bindings[OBJECT]:
                continue
            obj = binding_sources[kind, name]
            paired = {n for dt, n in keys if dt == LINK and binding_sources[dt, n].source_system == "HERO"
                      and binding_sources[dt, n].source_instance == obj.source_instance
                      and str(binding_sources[dt, n].source_id) == str(obj.external_object_id)}
            for link_name in paired - bindings[LINK]:
                link = binding_sources[LINK, link_name]
                if not link.target_incarnation and any(
                        p.technical_action == "hero_to_frappe" and p.result == "CREATED"
                        and p.source_type == link.source_type and str(p.source_id) == str(link.source_id)
                        and selected[p.run].source_instance == link.source_instance
                        and (selected[p.run].started_on or selected[p.run].creation) <= link.creation <= p.creation
                        for p in evidence.get(target, ())):
                    bindings[LINK].add(link_name)
                    findings.pop((LINK, link_name), None)
            if paired - bindings[LINK]:
                bindings[OBJECT].discard(name)
                findings[OBJECT, name] = "zugehörige Herkunftsbindung geschützt"
        for kind, name in keys:
            if kind != LINK or name not in bindings[LINK]:
                continue
            link = binding_sources[kind, name]
            if any(dt == OBJECT and n not in bindings[OBJECT]
                   and binding_sources[dt, n].source_instance == link.source_instance
                   and str(binding_sources[dt, n].external_object_id) == str(link.source_id) for dt, n in keys):
                bindings[LINK].discard(name)
                findings[LINK, name] = "zugehörige Objektbindung geschützt"

    policy_bindings = set()
    if frappe.db.table_exists("HERO Task Assignment Policy"):
        for rule in _all(frappe, "HERO Task Assignment Policy", ["policy_json"]):
            try:
                payload = json.loads(rule.policy_json or "{}")
            except ValueError:
                continue  # No binding identity can be obtained from an invalid document.
            if isinstance(payload, dict) and isinstance(payload.get("user_bindings"), list):
                policy_bindings.update(x for x in payload["user_bindings"] if isinstance(x, str))
    audited_targets = set()
    for name in audit_objects:
        if row := binding_sources.get((OBJECT, name)):
            audited_targets.add((row.target_doctype, row.target_name))
            for kind, bound in target_bindings[row.target_doctype, row.target_name]:
                if bound in bindings[kind]:
                    bindings[kind].remove(bound)
                    findings[kind, bound] = "externer Mutationsnachweis"

    for name in bindings[OBJECT] & policy_bindings:
        row = binding_sources[OBJECT, name]
        for kind, bound in target_bindings[row.target_doctype, row.target_name]:
            if bound in bindings[kind]:
                bindings[kind].remove(bound)
                findings[kind, bound] = "bestehende Zuweisungskonfiguration verweist auf Bindung"

    field_columns = ["name", "object_binding", "creation", "target_doctype", "target_name"]
    associated = {r.name: r for r in _related(frappe, FIELD, "object_binding", bindings[OBJECT], field_columns)}
    associated.update((r.name, r) for r in _related(frappe, FIELD, "name", evidence_groups.get(FIELD, ()), field_columns))
    for row in associated.values():
        key = (FIELD, row.name)
        if row.object_binding in bindings[OBJECT]:
            bindings[FIELD].add(row.name)
        elif ((row.target_doctype, row.target_name) not in regular_targets | audited_targets
                and row.object_binding not in audit_objects | policy_bindings
                and any(_ack_state(p).get("creation") == str(row.creation) for p in evidence.get(key, ())
                        if p.technical_action == PROOF)):
            bindings[FIELD].add(row.name)

    artifacts = defaultdict(set)
    owned_keys = {f"hero_full_migration:{name}" for name in selected}
    owned_keys.update(f"hero_full_migration_child:{name}" for name in selected)
    owned_keys.update(f"hero_employee_migration:{name}" for name in selected)
    for row in _all(frappe, "DefaultValue", ["name", "defkey"], {"parent": "__default"}):
        namespaced = row.defkey.split(":")
        if row.defkey in owned_keys or (len(namespaced) == 3 and namespaced[0] == "hero_testing_migration"
                                        and namespaced[1] in selected):
            artifacts["DefaultValue"].add(row.name)

    monitors = _all(frappe, "HERO Polling Event", ["name", "status"], {"manual_approval": ["is", "set"]})
    sync = _all(frappe, "HERO Sync Run Ledger", ["name", "sync_run_id", "state"],
                {"request_fingerprint": ["is", "set"], "site_scope": frappe.local.site, "automatic": 0})
    sync_proof = _all(frappe, RECORD, ["name", "source_id"], {"technical_action": SYNC_PROOF})
    proven_sync = {row.source_id for row in sync_proof}
    sync_ids = {row.sync_run_id for row in sync if row.sync_run_id in proven_sync}
    for row in sync:
        if row.sync_run_id not in sync_ids:
            findings["HERO Sync Run Ledger", row.name] = "historischer Einzellauf ohne Testmodus-Beleg"
    artifacts[RECORD].update(row.name for row in sync_proof if row.source_id in sync_ids and row.name not in audit_events)
    artifacts["HERO Polling Event"].update(row.name for row in monitors if row.status != "Started")
    artifacts["HERO Sync Run Ledger"].update(row.name for row in sync if row.sync_run_id in sync_ids)
    for row in _all(frappe, "HERO Polling Event", ["name", "run", "sync_run"]):
        if row.run in selected or row.sync_run in sync_ids:
            artifacts["HERO Polling Event"].add(row.name)
    for row in _all(frappe, "HERO Execution Budget", ["name", "scope", "run_id"]):
        if row.run_id in {"migration": selected, "monitor": artifacts["HERO Polling Event"], "sync": sync_ids}.get(row.scope, {}):
            artifacts["HERO Execution Budget"].add(row.name)
    artifacts["HERO Budget Outcome"].update(row.name for row in
        _related(frappe, "HERO Budget Outcome", "budget", artifacts["HERO Execution Budget"]))
    artifacts["HERO Test Cohort Series"].update(row.name for row in
        _all(frappe, "HERO Test Cohort Series", ["name"]))
    artifacts[PENDING].update(row.name for row in _related(frappe, PENDING, "origin_run", selected))
    artifacts["HERO Pending Dependency"].update(row.name for row in
        _related(frappe, "HERO Pending Dependency", "parent", artifacts[PENDING]))
    if frappe.db.table_exists("HERO Task Assignment State"):
        artifacts["HERO Task Assignment State"].update(row.name for row in
            _related(frappe, "HERO Task Assignment State", "task_link", bindings[LINK]))

    fields = bindings[FIELD]
    for row in _all(frappe, "HERO Sync Change Event", ["name", "object_binding", "open_field", "run_id"]):
        if row.name not in audit_events and (row.object_binding in bindings[OBJECT] or row.open_field in fields or row.run_id in sync_ids):
            artifacts["HERO Sync Change Event"].add(row.name)
    for row in _related(frappe, RECORD, "correlation_id", bindings[OBJECT]):
        if row.name not in audit_events:
            artifacts[RECORD].add(row.name)

    covered = set((kind, name) for kind, names in (bindings | artifacts).items() for name in names)
    file_urls = {}
    for key, proofs in sorted(evidence.items()):
        dt, name = key
        if dt in {LINK, OBJECT, FIELD}:
            if key not in covered:
                findings.setdefault(key, "Bindung behalten")
            continue
        if dt not in NATIVE:
            findings[key] = "geschützter nativer Typ"
            continue
        if not frappe.db.exists(dt, name):
            continue
        doc = frappe.get_doc(dt, name, for_update=lock)
        birth = [_ack_state(p) for p in proofs if p.technical_action == PROOF]
        if not birth:
            if key in regular_targets:
                findings[key] = "gemeinsam verwendet durch regulären HERO-Lauf"
            elif all(doc.creation < (selected[p.run].started_on or selected[p.run].creation) for p in proofs):
                findings[key] = "vor dem Test vorhanden"
            else:
                findings[key] = "kein revisionssicherer nativer Erstellungsbeleg"
            continue
        if key in regular_targets | audited_targets or any(b not in covered for b in target_bindings[key]):
            findings[key] = "gemeinsam verwendet oder Bindungsherkunft offen"
            continue
        if doc.docstatus != 0 or not doc.has_permission("delete") or not any(
                b.get("creation") == str(doc.creation) and b.get("digest") == _digest(_row_state(doc, frappe)) for b in birth):
            findings[key] = "native Daten seit Erstellungsbeleg geändert oder Löschung nicht zulässig"
            continue
        if dt == "File":
            attachment = _attachment(frappe, doc)
            if attachment is None:
                findings[key] = "Datei gemeinsam verwendet oder Speicherherkunft unklar"
                continue
            file_urls[key] = attachment
        candidates[key] = doc
        # The named target's test artifacts will be removed before the native graph.
        dependencies[key].update(_references(frappe, doc) - covered - {key})
    # Incoming references determine the order. Outgoing side effects of native
    # hooks require those referenced documents to be in the same deletion set.
    prerequisites = {}
    for key, doc in candidates.items():
        prerequisites[key] = set(_native_blockers(frappe, doc)) - {key}
        if key[0] == "File" and doc.attached_to_name:
            prerequisites[key].add((doc.attached_to_doctype, doc.attached_to_name))
    blocked = {key for key in candidates if (prerequisites[key] | dependencies[key]) - candidates.keys()}
    while True:
        order, remaining = _delete_order(candidates, dependencies, blocked)
        affected = {key for key in order if not prerequisites[key].isdisjoint(remaining)}
        if not affected:
            break
        blocked.update(affected)
    for key in remaining:
        findings[key] = "geschützte oder zyklische native Abhängigkeit"
    for key in findings:
        protected_runs.update(r.run for r in evidence.get(key, ()))
    # Historical page receipts can be the only evidence connecting a retained
    # link to its test run. They have a source identity but no native target.
    retained_link_identities = {
        (link.source_instance, link.source_type, str(link.source_id))
        for (kind, name), link in binding_sources.items()
        if kind == LINK and link.source_system == "HERO" and name not in bindings[LINK]
    }
    for row in records:
        retained_checkpoint = (row.technical_action == "identity_checkpoint"
                               and (selected[row.run].source_instance, row.source_type, str(row.source_id))
                               in retained_link_identities)
        if row.name in audit_events or retained_checkpoint or ((row.target_doctype, row.target_name) in findings
                                       and row.technical_action in NATIVE_HISTORY | {PROOF}):
            protected_runs.add(row.run)
            artifacts[RECORD].discard(row.name)
        else:
            artifacts[RECORD].add(row.name)
    artifacts[RUN].update(set(selected) - protected_runs)
    # Retain parent provenance while a child with unresolved evidence survives.
    artifacts[RUN].difference_update(r.execution_budget_run for r in selected.values() if r.name in protected_runs)
    retained_runs = sorted(set(selected) - artifacts[RUN])

    for kind, names in bindings.items():
        artifacts[kind].update(names)
    files = {key: file_urls[key] for key in order if key in file_urls}
    retries = defaultdict(list)
    for row in _all(frappe, RECORD, ["name", "native_state_json"], {"technical_action": BLOB_CLEANUP}):
        data = _ack_state(row)
        url, size, content_hash = (data.get(k) for k in ("url", "size", "content_hash"))
        path = _blob_path(frappe, url)
        if path is None:
            findings[RECORD, row.name] = "unklare Datei-Nachverarbeitung"
        elif not path.exists() and not path.is_symlink():
            artifacts[RECORD].add(row.name)
        elif _same_blob(path, size, content_hash) and _blob_unshared(frappe, url):
            retries[url, size, content_hash].append(row.name)
        else:
            findings[RECORD, row.name] = "Datei-Nachverarbeitung wegen Änderung oder weiterer Referenz offen"
    removed = {dt: sorted(names) for dt, names in sorted(artifacts.items()) if names}
    native_state = [(dt, name, _digest(_row_state(candidates[dt, name], frappe))) for dt, name in order]
    dangling = sorted((key, sorted(dependencies[key] | prerequisites[key])) for key in remaining)
    manifest = {"artifacts": removed, "native": order, "native_state": native_state, "files": list(files.items()),
                "review": sorted(findings.items()), "dependencies": dangling, "retained_runs": retained_runs,
                "blob_retries": [(file, sorted(retries[file])) for file in sorted(retries)]}
    return manifest, {
        "test_runs": len(selected), "deletable_records": dict(sorted(Counter(dt for dt, _ in order).items())),
        "removable_artifacts": {dt: len(names) for dt, names in removed.items()},
        "file_count": len(files) + len(retries),
        "file_bytes": sum(size for _, size, _ in files.values()) + sum(size for _, size, _ in retries),
        "protected": _report_findings(findings), "unresolved_dependencies": len(dangling),
        "retained_mutation_audits": len(audits), "active_mutations": active_audit,
        "retained_test_runs": len(selected) - len(removed.get(RUN, ())),
    }


def _group(keys):
    groups = defaultdict(set)
    for doctype, name in keys:
        groups[doctype].add(name)
    return groups


def _report_findings(findings):
    result = defaultdict(Counter)
    for (dt, _), reason in findings.items():
        result[dt][reason] += 1
    return dict(result)


def fingerprint(manifest):
    return _digest(manifest)


def apply(frappe, manifest):
    """Delete the admitted data in the caller's transaction, then reclaim private blobs."""
    from functools import partial
    from hero_integration.services.integration_events import record_output
    from hero_integration.services.guards import assert_deletion_allowed

    assert_deletion_allowed(action="selective HERO test reset")
    removed = manifest["artifacts"]
    ordered = ("HERO Pending Dependency", "HERO Task Assignment State", "HERO Sync Change Event", FIELD,
               OBJECT, LINK, PENDING, "HERO Budget Outcome", "HERO Execution Budget", "HERO Test Cohort Series",
               "HERO Polling Event", "HERO Sync Run Ledger", "DefaultValue", RECORD, RUN)
    if set(removed) - set(ordered):
        raise ValueError("Unexpected reset artifact.")
    for doctype in ordered:
        if names := removed.get(doctype):
            frappe.db.delete(doctype, {"name": ["in", names]})
    if removed.get("DefaultValue"):
        from frappe.cache_manager import clear_defaults_cache
        frappe.db.after_commit.add(partial(clear_defaults_cache, "__default"))
    for run in manifest["retained_runs"]:
        frappe.db.set_value(RUN, run, {"testing_results_json": '{"reset_retained":true}',
            "testing_cohort_json": "", "employee_progress_json": "", "page_checkpoint_json": "",
            "checkpoint_contract_json": "", "progress_counts_json": "", "resume_phase": "",
            "resume_offset": 0, "resume_record_index": 0, "lease_expires_on": None}, update_modified=False)
    for file, receipts in manifest["blob_retries"]:
        frappe.db.after_commit.add(partial(_reclaim_blob, frappe, *file, receipts=receipts))
    urls = dict(manifest["files"])
    for doctype, name in manifest["native"]:
        if (doctype, name) in urls:
            file = frappe.get_doc(doctype, name, for_update=True)
            current = _attachment(frappe, file)
            if current != urls[doctype, name]:
                raise ValueError("File ownership changed during reset.")
            file.validate_protected_file()
            # File.on_trash unlinks bytes before commit and writes to the parent timeline.
            frappe.delete_doc(doctype, name, ignore_on_trash=True, delete_permanently=True)
            url, size, content_hash = current
            receipt = record_output(source_type="File", source_id=name, result="DEFERRED", action=BLOB_CLEANUP,
                                    native_state={"url": url, "size": size, "content_hash": content_hash})
            frappe.db.after_commit.add(partial(_reclaim_blob, frappe, url, size, content_hash, receipts=[receipt]))
        else:
            frappe.delete_doc(doctype, name, delete_permanently=True)
