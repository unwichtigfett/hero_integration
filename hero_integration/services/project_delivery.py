"""One explicit, actor-bound confirmation for Project fields and native activities."""
from copy import deepcopy
import hmac
import json

import frappe

from hero_integration.services import mapped_send, project_logbook_writeback as logbook
from hero_integration.services.localized_error import LocalizedError


class ProjectDeliveryError(LocalizedError):
    pass


class PreviewReader:
    """Memoize identical reads only within one preview; sends always read afresh."""
    def __init__(self, client):
        self.client, self.cache = client, {}

    def query(self, document, variables):
        key = json.dumps([document, variables], sort_keys=True, default=str)
        if key not in self.cache:
            self.cache[key] = self.client.query(document, variables)
        return deepcopy(self.cache[key])


def _sign(plan):
    from hero_integration.services.mapping_draft_exchange import _preview_binding
    return _preview_binding({'purpose': 'project-delivery-v1'}, plan)


def _access(project):
    if not mapped_send._roles():
        raise ProjectDeliveryError('Only System Managers and HERO Writeback users may send to HERO.')
    document = frappe.get_doc('Project', project)
    frappe.has_permission('Project', 'write', doc=document, throw=True)
    return document


def preview(project, *, phase_id=None, client=None):
    project_document = _access(project)
    if client is None:
        from hero_integration.client.settings import get_configuration
        from hero_integration.client.graphql import HeroGraphQLClient
        config = get_configuration(fresh=True)
        client = HeroGraphQLClient(config.endpoint, lambda: config.api_token, config.timeout_seconds, max_retries=0)
    reader = PreviewReader(client)
    data = mapped_send.preview('Project', project, phase_id=phase_id, client=reader)
    activities = []
    for activity in logbook.candidates(project)['comments']:
        item = logbook.preview(activity['name'], activity_doctype=activity['activity_doctype'], client=reader)
        if item['project'] != project:
            raise ProjectDeliveryError('The Project send preview changed; open it again.')
        activities.append({**item, 'creation': str(activity['creation'])})
    plan = {'project': project, 'project_modified': str(project_document.modified), 'phase_id': phase_id or None, 'data': data['fingerprint'],
            'activities': [{'name': item['comment'], 'doctype': item['activity_doctype'],
                            'fingerprint': item['delivery_fingerprint']} for item in activities]}
    changes = data['changes'] + [change for item in activities for change in item['changes']]
    return {**data, 'project': project, 'activities': activities, 'data_changes': data['changes'],
            'changes': changes, 'delivery': plan, 'fingerprint': _sign(plan),
            'can_send': bool(changes) and not data['conflicts'] and data['sending_enabled']
                        and all(item['can_send'] for item in activities)}


def send(project, *, fingerprint, delivery, confirmed=False, phase_id=None, client=None, mutation_client=None):
    _access(project)
    from hero_integration.api import _assert_mutation_execution_released
    _assert_mutation_execution_released()
    if isinstance(delivery, str):
        from hero_integration.services.personal_mapping_content import unique_json_object
        if len(delivery) > 500_000:
            raise ProjectDeliveryError('The Project send preview changed; open it again.')
        try:
            delivery = json.loads(delivery, object_pairs_hook=unique_json_object)
        except (ValueError, TypeError):
            raise ProjectDeliveryError('The Project send preview changed; open it again.') from None
    if (confirmed not in (True, 1, '1', 'true') or not isinstance(delivery, dict)
            or delivery.get('project') != project or delivery.get('phase_id') != (phase_id or None)
            or not isinstance(fingerprint, str) or not hmac.compare_digest(_sign(delivery), fingerprint)):
        raise ProjectDeliveryError('The Project send preview changed; open it again.')
    current = preview(project, phase_id=phase_id, client=client)
    if not hmac.compare_digest(current['fingerprint'], fingerprint) or not current['can_send']:
        raise ProjectDeliveryError('The Project send preview changed; open it again.')
    result = {'status': 'SENT', 'changes': [], 'activities': [], 'failed_step': None}
    stamp = None
    try:
        if current['data_changes']:
            result['failed_step'] = 'PROJECT'
            data = mapped_send.send('Project', project, fingerprint=delivery['data'], confirmed=True,
                                    phase_id=phase_id, client=client, mutation_client=mutation_client)
            result['changes'] = data['changes']
            stamp = data.get('project_stamp_receipt')
            if stamp is not None and stamp['before'] != delivery['project_modified']:
                raise ProjectDeliveryError('The Project send preview changed; open it again.')
        # Only the explicitly reviewed activities; never discover new ones after a send.
        for activity in delivery['activities']:
            result['failed_step'] = activity['doctype'] + ':' + activity['name']
            fresh = logbook.preview(activity['name'], activity_doctype=activity['doctype'], client=client,
                                    **({'delivery_project_stamp': stamp} if stamp is not None else {}))
            if fresh['project'] != project or fresh['delivery_fingerprint'] != activity['fingerprint']:
                raise ProjectDeliveryError('The Project send preview changed; open it again.')
            sent = logbook.send(activity['name'], activity_doctype=activity['doctype'],
                                fingerprint=fresh['fingerprint'], confirmed=True, client=client, mutation_client=mutation_client)
            result['activities'].append({'name': activity['name'], 'doctype': activity['doctype'], 'hero_id': sent['hero_id']})
    except Exception:
        # Native senders preserve their own durable receipts/reservations. Do not
        # retry or claim that completed external mutations can be rolled back.
        frappe.log_error(title='Project delivery stopped', message='Project delivery stopped; consult native mutation audits and durable activity reservations.')
        return {**result, 'status': 'STOPPED', 'message': 'Sending stopped. Confirmed changes remain sent; review the result before sending again.'}
    return {**result, 'failed_step': None}
