"""Portable mutation profiles accompanying read mappings; import never authorizes execution."""
import json

DIRECTION = 'FRAPPE_TO_MUTATION'


def portable(payload):
    from hero_integration.services import mapping_draft_exchange as exchange
    return {'format_version': exchange.PORTABLE_FORMAT_VERSION,
            'schema_fingerprint': payload['schema_snapshot']['schema_fingerprint'],
            'direction': DIRECTION,
            'schema': {'custom_doctypes': [], 'custom_fields': [], 'relations': []},
            'mappings': payload['mutation_mappings']}


def validate(payload):
    from hero_integration.services import mapping_draft_exchange as exchange
    from hero_integration.services.personal_mapping_content import configured_mutation_helpers
    mappings = payload.get('mutation_mappings', [])
    if not isinstance(mappings, list) or len(mappings) > 10:
        raise ValueError('Configuration mutation mappings must contain at most ten supported profiles.')
    if not mappings:
        return
    exchange._parse_portable(json.dumps(portable(payload)), DIRECTION)
    readers = {row['target_operation'] for row in payload['read_services']}
    seen = set()
    for item in mappings:
        normalise_mapping, _ = configured_mutation_helpers(item['content'].get('usage'))
        content = normalise_mapping(item['content'], payload['schema_snapshot']['schema'])
        area = content['area']
        profile = (area, content['usage'])
        if area not in readers or profile in seen or item['operation_name'] != content['target']['operation']:
            raise ValueError('Every mutation area requires its unique Query mapping in the same file.')
        seen.add(profile)


def summary(payload):
    mappings = payload.get('mutation_mappings', [])
    if not mappings:
        return {}
    return {'mutation_mappings': {'count': len(mappings), 'state': 'Active', 'execution': False,
            'assignments': sum(len(item['content']['assignments']) for item in mappings)}}


def save(payload, source_instance, snapshot_name):
    if not payload.get('mutation_mappings'):
        return {}
    from hero_integration.services import mapping_draft_exchange as exchange
    body = json.dumps(portable(payload), ensure_ascii=False)
    preview = exchange.preview_import(body, source_instance, snapshot_name, DIRECTION)
    if not preview.get('can_save'):
        raise ValueError('Prepared mutation mappings have unresolved draft import conflicts.')
    expected = [{'definition_id': row['definition_id'], **row['expected']} for row in preview['items']]
    result = exchange.save_import(body, source_instance, snapshot_name, DIRECTION, expected,
                                 confirmed=True, preview_fingerprint=preview['preview_fingerprint'])
    from hero_integration.services.simple_mapping_exchange import publish_imported_mappings
    published = publish_imported_mappings(preview['items'])
    return {**summary(payload), 'mutation_import': {**result, 'activation': True, 'mappings': published}}


def export(source_instance, snapshot_name):
    """Reuse the directional exporter so own prepared revisions round-trip too."""
    from hero_integration.services import mapping_draft_exchange as exchange
    result = json.loads(exchange._export_portable(source_instance, snapshot_name, DIRECTION, usage='CONFIGURED_MUTATIONS'))
    return [item for item in result['mappings'] if item['content'].get('usage') in {'MAPPED_UPDATE_V2', 'PROJECT_LOGBOOK_V1'}]
