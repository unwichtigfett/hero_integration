"""Server-owned policy for the narrowly scoped HERO Task system issuer."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json

_managed = ContextVar('hero_task_policy_write', default=False)


class PolicyError(ValueError):
    pass


@contextmanager
def managed_policy_write():
    token = _managed.set(True)
    try:
        yield
    finally:
        _managed.reset(token)


def policy_write_active():
    return _managed.get()


def validate_policy_payload(payload, site_scope):
    expected = {'source_instance', 'issuer', 'company', 'user_bindings', 'allow_initial', 'allow_transfer'}
    if (not isinstance(payload, dict) or not expected <= set(payload)
            or set(payload) - expected - {'native_effects', 'silent_initial'}
            or not isinstance(site_scope, str) or not site_scope):
        raise PolicyError('Policy requires the exact authorized scope and actions.')
    for field in ('source_instance', 'issuer', 'company'):
        if not isinstance(payload[field], str) or not payload[field] or payload[field] != payload[field].strip():
            raise PolicyError('Policy requires explicit source, issuer and company identities.')
    bindings = payload['user_bindings']
    if (not isinstance(bindings, list) or not bindings or len(bindings) > 100
            or any(not isinstance(item, str) or not item or item != item.strip() for item in bindings)
            or len(bindings) != len(set(bindings))):
        raise PolicyError('Policy requires distinct explicit User binding identities.')
    if type(payload['allow_initial']) is not bool or type(payload['allow_transfer']) is not bool or not payload['allow_initial']:
        raise PolicyError('Policy actions must explicitly allow initial assignment and declare transfer.')
    if 'native_effects' in payload:
        effects = payload['native_effects']
        if (not isinstance(effects, dict) or set(effects) != {'date_rule', 'allow_notifications', 'allow_follow'}
                or effects['date_rule'] != 'LOCAL_TODAY'
                or type(effects['allow_notifications']) is not bool or type(effects['allow_follow']) is not bool):
            raise PolicyError('Native effects require an explicit local date and boolean notification and follow decisions.')
    if 'silent_initial' in payload:
        if (type(payload['silent_initial']) is not bool or
                payload['silent_initial'] and (payload['allow_transfer'] or payload.get('native_effects') != {
                    'date_rule': 'LOCAL_TODAY', 'allow_notifications': False, 'allow_follow': False})):
            raise PolicyError('Silent initial assignment requires explicit no-notification and no-follow effects.')
    return {**payload, 'user_bindings': sorted(bindings), 'site_scope': site_scope, 'source_type': 'Task'}


def _policy_input(value):
    keys = ('source_instance', 'issuer', 'company', 'user_bindings', 'allow_initial', 'allow_transfer')
    result = {key: value[key] for key in keys}
    if 'native_effects' in value:
        result['native_effects'] = value['native_effects']
    if 'silent_initial' in value:
        result['silent_initial'] = value['silent_initial']
    return result


def _fingerprint(payload):
    return sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _require_actor():
    import frappe
    frappe.only_for('System Manager')
    return frappe.session.user


def _check_references(payload):
    import frappe
    if not frappe.db.exists('Company', payload['company']):
        raise PolicyError('Policy company is unavailable.')
    if not frappe.db.get_value('User', payload['issuer'], 'enabled'):
        raise PolicyError('Policy issuer is unavailable or disabled.')
    if payload['issuer'] != 'Administrator' and 'System Manager' not in frappe.get_roles(payload['issuer']):
        raise PolicyError('Policy issuer is not authorized for the existing Task intake.')
    for name in sorted(payload['user_bindings']):
        if not frappe.db.exists('HERO Object Binding', name):
            raise PolicyError('Policy User binding does not exist.')
        binding = frappe.get_doc('HERO Object Binding', name, for_update=True)
        from hero_integration.services.binding_registry import ObjectBinding
        canonical = ObjectBinding.create(site_scope=binding.site_scope, source_instance=binding.source_instance,
            object_type=binding.object_type, external_object_id=binding.external_object_id, target_app=binding.target_app,
            target_doctype=binding.target_doctype, target_name=binding.target_name,
            mapping_definition_id=binding.mapping_definition_id, mapping_revision=int(binding.mapping_revision))
        if canonical.identity_key != binding.identity_key or name != binding.identity_key:
            raise PolicyError('Policy User binding identity has been altered.')
        if (binding.site_scope != payload['site_scope'] or binding.source_instance != payload['source_instance']
                or binding.object_type != 'User' or binding.target_app != 'frappe' or binding.target_doctype != 'User' or binding.lifecycle != 'VALID'
                or not frappe.db.get_value('User', binding.target_name, 'enabled')):
            raise PolicyError('Policy User binding is not valid for this scope.')
        matches = frappe.get_all('HERO Object Binding', filters={'site_scope': payload['site_scope'],
            'source_instance': payload['source_instance'], 'object_type': 'User', 'external_object_id': binding.external_object_id,
            'target_doctype': 'User', 'lifecycle': 'VALID'}, pluck='name', limit=2)
        if matches != [name]:
            raise PolicyError('Policy User binding is ambiguous.')
        employees = frappe.get_all('Employee', filters={'user_id': binding.target_name, 'status': 'Active'}, fields=['company'], limit=2)
        if len(employees) != 1 or employees[0].company != payload['company']:
            raise PolicyError('Policy recipient requires one active Employee in the approved company.')


def create_policy(payload):
    import frappe
    _require_actor()
    value = validate_policy_payload(payload, frappe.local.site)
    _check_references(value)
    with managed_policy_write():
        doc = frappe.get_doc({'doctype': 'HERO Task Assignment Policy', 'site_scope': frappe.local.site,
                              'source_instance': value['source_instance'], 'issuer': value['issuer'],
                              'policy_json': json.dumps(value, sort_keys=True, separators=(',', ':')),
                              'policy_fingerprint': _fingerprint(value), 'policy_revision': 1, 'state': 'DRAFT'})
        doc.insert()
    return doc.name


def approve_policy(name, expected_revision):
    import frappe
    actor = _require_actor()
    doc = frappe.get_doc('HERO Task Assignment Policy', name, for_update=True)
    if doc.site_scope != frappe.local.site or doc.state != 'DRAFT' or int(doc.policy_revision) != int(expected_revision):
        raise PolicyError('Policy is stale, unavailable or outside this site.')
    value = json.loads(doc.policy_json)
    raw = _policy_input(value)
    if (validate_policy_payload(raw, frappe.local.site) != value or _fingerprint(value) != doc.policy_fingerprint
            or doc.source_instance != value['source_instance'] or doc.issuer != value['issuer']):
        raise PolicyError('Policy contents do not match the reviewed scope.')
    _check_references(value)
    with managed_policy_write():
        doc.state = 'APPROVED'; doc.approved_by = actor; doc.approved_revision = doc.policy_revision
        doc.save()
    return doc.name


def revoke_policy(name, expected_revision):
    import frappe
    actor = _require_actor()
    doc = frappe.get_doc('HERO Task Assignment Policy', name, for_update=True)
    if doc.site_scope != frappe.local.site or doc.state != 'APPROVED' or int(doc.policy_revision) != int(expected_revision):
        raise PolicyError('Policy is stale or unavailable for revocation.')
    with managed_policy_write():
        doc.state = 'REVOKED'; doc.revoked_by = actor; doc.policy_revision += 1
        doc.save()
    return doc.name


def approved_initial_policy(source_instance, binding_name):
    """Lock and verify a unique policy; absence never grants authority."""
    import frappe
    rows = frappe.db.sql('SELECT name FROM `tabHERO Task Assignment Policy` WHERE site_scope=%s AND source_instance=%s AND issuer=%s AND state=%s FOR UPDATE',
                         (frappe.local.site, source_instance, frappe.session.user, 'APPROVED'), as_dict=True)
    matches = []
    for row in rows:
        doc = frappe.get_doc('HERO Task Assignment Policy', row.name, for_update=True)
        try:
            payload = json.loads(doc.policy_json)
            raw = _policy_input(payload)
            if (validate_policy_payload(raw, frappe.local.site) != payload or _fingerprint(payload) != doc.policy_fingerprint
                    or doc.source_instance != payload['source_instance'] or doc.issuer != payload['issuer']
                    or not doc.approved_by or int(doc.approved_revision or 0) != int(doc.policy_revision or 0)):
                raise PolicyError('Stored policy approval is stale or invalid.')
            if (payload['source_instance'] == source_instance and payload['issuer'] == frappe.session.user
                    and binding_name in payload['user_bindings'] and payload['allow_initial']):
                _check_references(payload)
                matches.append(doc)
        except (KeyError, TypeError, ValueError):
            raise PolicyError('Stored policy cannot be verified.')
    return matches[0] if len(matches) == 1 else None


def task_has_policy_company(task, policy, frappe_api=None):
    """Require an actual, locked Task project in the policy company."""
    if frappe_api is None:
        import frappe as frappe_api
    if not task.project:
        return False
    try:
        project = frappe_api.get_doc('Project', task.project, for_update=True)
        company = json.loads(policy.policy_json)['company']
    except (frappe_api.DoesNotExistError, KeyError, TypeError, ValueError):
        return False
    return bool(project.company and project.company == company)
