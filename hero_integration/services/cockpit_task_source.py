"""Keep the HERO task observation distinct from editable native Task values."""

from datetime import datetime, timezone
import json


FIELD = 'custom_hero_cockpit_source'
DATES = ('due_date', 'done_date', 'created', 'modified', 'start', 'end')


def task_values(record):
    project = record.get('target_project_match')
    nested = project.get('id') if isinstance(project, dict) else project
    scalar = record.get('target_project_match_id', nested)
    identifiers = ('id', 'author_user_id', 'target_user_id')
    required = {*identifiers, 'is_deleted', 'title', 'comment', *DATES}
    if not required.issubset(record) or not {'target_project_match', 'target_project_match_id'}.intersection(record):
        return None
    if nested is not None and nested != scalar:
        raise ValueError('HERO task project identifiers disagree.')
    for value in (scalar, nested, *(record[key] for key in identifiers)):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError('Invalid HERO task identifier.')
    if not record['id'] or type(record['is_deleted']) is not bool:
        raise ValueError('Invalid HERO task identity or deletion flag.')
    if any(record[key] is not None and not isinstance(record[key], str) for key in ('title', 'comment')):
        raise ValueError('Invalid HERO task text.')
    task = {key: record[key] for key in (*identifiers, 'title', 'comment', 'is_deleted')}
    task['target_project_match'] = scalar
    for key in DATES:
        value = record[key]
        if value is not None:
            instant = datetime.fromisoformat(value)
            if instant.utcoffset() is None:
                raise ValueError('HERO task timestamps require a UTC offset.')
            value = instant.astimezone(timezone.utc).isoformat()
        task[key] = value
    return task


def store(record, instance, expected_name=None):
    import frappe

    if not frappe.get_meta('Task').has_field(FIELD):
        return
    task = task_values(record)
    if task is None:
        return
    link = frappe.db.get_value('HERO Integration Link', {
        'source_system': 'HERO', 'source_instance': instance, 'source_type': 'Task',
        'source_id': str(task['id']), 'target_doctype': 'Task',
    }, ['target_name', 'target_incarnation', 'creation'], as_dict=True, for_update=True)
    if not link or (expected_name and link.target_name != expected_name):
        return
    row = frappe.db.get_value('Task', link.target_name, ['creation', FIELD], as_dict=True, for_update=True)
    if not row or row.creation > link.creation:
        return
    if link.target_incarnation and row.creation.isoformat(timespec='microseconds') != link.target_incarnation:
        return
    value = {'instance': instance, 'task': task}
    if row.get(FIELD):
        old = json.loads(row[FIELD])
        if old['instance'] != instance or old['task']['id'] != task['id']:
            raise ValueError('Cockpit task source identity changed.')
        previous = old['task']['modified']
        if old == value or (previous and (not task['modified'] or
            datetime.fromisoformat(task['modified']) < datetime.fromisoformat(previous))):
            return
    frappe.db.set_value('Task', link.target_name, FIELD, json.dumps(value, ensure_ascii=False), update_modified=False)
