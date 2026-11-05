"""Atomic event time writes keep remote semantics and guard every sent member."""
from copy import deepcopy
from types import SimpleNamespace as NS
import json
from unittest import TestCase
from unittest.mock import Mock, patch

from hero_integration.services import mapped_update_v2 as update
from hero_integration.services.direct_mapping import MappingSpec
from hero_integration.services.change_detection import encode, field_hashes
from hero_integration.services.graphql_contract import parse_mutation
from hero_integration.services.native_projections import values
from hero_integration.tests.test_mapped_update_v2 import SCHEMA, mapping


class TestEventTimeConflicts(TestCase):
    def setUp(self):
        self.options={'confirmed':True,'source_timezone':'UTC','target_timezone':'UTC','all_day_end':'exclusive'}
        self.baseline={'id':7,'title':'Test','start':'2026-11-05T10:00:00+00:00',
                       'end':'2026-11-05T11:00:00+00:00','all_day':False,'is_recurring':False}
        self.remote=deepcopy(self.baseline)
        self.local=NS(doctype='Event',name='E1',modified='v1',starts_on='2026-11-05 10:00:00',
                      ends_on='2026-11-05 11:00:00',all_day=1)
        self.local.get=lambda field:getattr(self.local,field)
        content=mapping('HeroCalendarEvents');content['assignments']=[]
        rules=[]
        for field, path in [('starts_on','start'),('ends_on','end'),('all_day','all_day')]:
            rules.append(MappingSpec('HeroCalendarEvents',path,'Event',field,
                'boolean' if field=='all_day' else 'string',transformation='Identity' if field=='all_day' else 'Native Adapter',definition_id=field))
            content['assignments'].append({'id':field,'source':{'doctype':'Event','field':field},'query_rule_id':field,
                'input_path':'calendar_event.'+path,'read_path':path,
                'resolver':'FIELD' if field=='all_day' else 'EVENT_TIME','options':{}})
        document='mutation($input:EventInput){update_calendar_event(calendar_event:$input){id}}'
        self.ctx=NS(content=content,document=self.local,hero_id='7',query_plan=NS(definitions=rules,adapter_options={}),
            mapping=NS(name='map',mapping_revision=1,operation_name='update_calendar_event'),raw_schema=SCHEMA,
            link=NS(source_id='7'),source_documents={},source_links={},parsed=parse_mutation(document),
            contract=NS(name='contract',contract_revision=1,contract_fingerprint='hash'),service=NS(query_document=document),
            read=NS(mapping=NS(mapping_json=json.dumps({'native_options':self.options})),contract=NS(revision=1),
                    service=NS(query_document='approved read')),proof=NS(fingerprint='proof',source_instance='source-a'))
        self.set_baseline(self.baseline)

    def set_baseline(self, remote):
        native=values({'native_kind':'event'},remote,'7','source-a',self.options,lookup=lambda *_:None)
        hashes=encode(field_hashes({field:native[field] for field in ('starts_on','ends_on','all_day')}))
        self.ctx.link.source_field_hashes=hashes
        self.ctx.link.target_field_hashes=hashes

    def source(self,ctx,item,remote):
        ctx.source_links[item['id']]=ctx.link
        return ctx.document,ctx.document.get(item['source']['field'])

    def prepare(self):
        with patch.object(update,'_read_record',return_value=self.remote),patch.object(update,'_source',side_effect=self.source), \
             patch.object(update.frappe,'get_meta',return_value=NS(get_field=lambda field:NS(label=field))):
            return update._prepare(self.ctx,Mock())

    def test_all_day_toggle_detects_concurrent_remote_time_changes(self):
        self.remote.update(start='2026-11-05T12:00:00+00:00',end='2026-11-05T13:00:00+00:00')
        result=self.prepare()
        self.assertEqual({row['source_field'] for row in result.conflicts},{'starts_on','ends_on'})

    def test_toggle_without_remote_changes_sends_complete_time_group(self):
        result=self.prepare()
        self.assertEqual(result.conflicts,[])
        self.assertEqual(result.variables,{'input':{'id':7,'all_day':True,
            'start':'2026-11-05T00:00:00+00:00','end':'2026-11-06T00:00:00+00:00'}})

    def test_grouped_unchanged_wire_time_still_requires_its_remote_baseline(self):
        self.local.all_day=0
        self.local.starts_on='2026-11-05 09:00:00'
        self.local.ends_on='2026-11-05 13:00:00'
        self.remote['end']='2026-11-05T13:00:00+00:00'
        result=self.prepare()
        self.assertEqual([row['source_field'] for row in result.conflicts],['ends_on'])

    def test_inverse_all_day_toggle_keeps_remote_exclusive_end_semantics(self):
        self.remote.update(start='2026-11-05T00:00:00+00:00',end='2026-11-06T00:00:00+00:00',all_day=True)
        self.set_baseline(self.remote)
        self.local.all_day=0
        result=self.prepare()
        self.assertEqual(result.conflicts,[])
        self.assertEqual(result.variables['input']['end'],'2026-11-05T11:00:00+00:00')

    def test_conflicting_preview_cannot_reserve_or_send(self):
        import hero_integration.api as api
        self.remote.update(start='2026-11-05T12:00:00+00:00',end='2026-11-05T13:00:00+00:00')
        result=self.prepare()
        transport=Mock()
        with patch.object(api,'_assert_mutation_execution_released'),patch.object(update,'_load_context',return_value=self.ctx), \
             patch('hero_integration.services.writeback_source.validate_source'), \
             patch.object(update,'_prepare',return_value=result),patch.object(api,'_reserve_mutation') as reserve:
            with self.assertRaisesRegex(ValueError,'baseline conflicts'):
                update.send('map','Event','E1',client=Mock(),mutation_client=transport,confirmed=True,fingerprint=result.fingerprint)
        reserve.assert_not_called()
        transport.execute_once.assert_not_called()

    def test_selected_aliases_keep_remote_all_day_semantics(self):
        from dataclasses import replace
        paths={'starts_on':'period.begin','ends_on':'period.finish','all_day':'period.whole_day'}
        self.ctx.query_plan.definitions=[replace(rule,source_path=paths[rule.target_field])
                                         for rule in self.ctx.query_plan.definitions]
        for item in self.ctx.content['assignments']:
            item['read_path']=paths[item['source']['field']]
        self.remote={'id':7,'is_recurring':False,'period':{'begin':'2026-11-05T12:00:00+00:00',
            'finish':'2026-11-05T13:00:00+00:00','whole_day':False}}
        result=self.prepare()
        self.assertEqual({row['source_field'] for row in result.conflicts},{'starts_on','ends_on'})
