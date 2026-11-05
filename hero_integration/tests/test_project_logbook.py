"""Nested logbook pagination and source/parent isolation."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import project_logbook as lb


class Client:
    def __init__(self, projects, histories, cap=500, repeat=False, fail_sizes=()):
        self.projects, self.histories, self.cap = projects, histories, cap
        self.repeat, self.fail_sizes, self.calls = repeat, set(fail_sizes), []

    def query(self, document, variables):
        self.calls.append((document, variables))
        if document == lb.QUERY:
            if variables['first'] in self.fail_sizes:
                raise RuntimeError('HERO rejected large response')
            ids = self.projects[variables['offset']:variables['offset'] + min(variables['first'], 2)]
            return {'project_matches': [dict(id=i, histories=self.histories.get(i, [])[:self.cap]) for i in ids]}
        result = {}
        for index in range(len(variables) // 2):
            identity, offset = variables[f'id{index}'][0], variables[f'offset{index}']
            if self.repeat:
                offset = 0
            result[f'p{index}'] = [dict(id=identity, histories=self.histories.get(identity, [])[offset:offset+self.cap])]
        return result


class TestProjectLogbook(unittest.TestCase):
    def run_sync(self, client, instance='default', known=None, **options):
        added, commits = [], []
        def sql(query, params):
            self.assertIn("source_system='HERO'", query)
            self.assertEqual(params, (instance,))
            if 'LEFT JOIN' in query:
                return list((known or {}).items())
            return [('1', 'P-1' if instance == 'default' else 'B-1'), ('2', 'P-2')]
        fake = SimpleNamespace(db=SimpleNamespace(sql=sql, commit=lambda: commits.append(True)))
        with patch.object(lb, 'frappe', fake), patch.object(lb, '_guard_legacy_outbound'), patch.object(lb, 'enrich_entry'), patch.object(lb, '_metadata', return_value={}), patch.object(lb, 'PAGE_SIZE', 25), patch.object(lb, 'add_entry', side_effect=lambda p,e,u,s: added.append((p,e['id'],s))):
            counts = lb.sync_logbook(client, {}, source_instance=instance, **options)
        return counts, added, commits

    def test_exhausts_both_collections_even_when_server_caps_short_pages(self):
        histories = {1:[{'id': i} for i in range(1, 603)], 2:[{'id':900}], 3:[{'id':1000}]}
        client = Client([1,2,3], histories, cap=123)
        counts, added, commits = self.run_sync(client, known={'900':'P-2'})
        self.assertTrue(counts['complete'])
        self.assertEqual((counts['added'], counts['known'], counts['without_project'], counts['projects']), (602,1,1,3))
        self.assertEqual(len(set(added)),602)
        self.assertEqual(len(commits),2)
        self.assertTrue(any(v.get('offset0') == 492 for d,v in client.calls))
        self.assertTrue(any(d == lb.QUERY and v['offset']==3 for d,v in client.calls))

    def test_retries_failed_initial_page_without_advancing_offset(self):
        client = Client([1], {1:[{'id':10}]}, fail_sizes={25})
        counts, added, _ = self.run_sync(client)
        self.assertEqual([v['offset'] for d,v in client.calls[:2]], [0,0])
        self.assertEqual([v['first'] for d,v in client.calls[:2]], [25,12])
        self.assertEqual(added, [('P-1',10,'default')])
        self.assertTrue(counts['complete'])

    def test_known_identity_is_source_and_parent_scoped(self):
        counts, added, _ = self.run_sync(Client([1], {1:[{'id':10}]}), instance='source-b')
        self.assertEqual(added, [('B-1',10,'source-b')])
        with self.assertRaisesRegex(ValueError,'different Project'):
            self.run_sync(Client([1], {1:[{'id':10}]}), known={'10':'wrong-parent'})

    def test_ignored_history_offset_and_bounds_never_claim_completion(self):
        with self.assertRaisesRegex(ValueError,'repeated an identity'):
            self.run_sync(Client([1], {1:[{'id':10}]}, repeat=True))
        with self.assertRaisesRegex(ValueError,'history page bound'):
            self.run_sync(Client([1], {1:[{'id':10},{'id':11}]}, cap=1), max_history_pages=1)
        with self.assertRaisesRegex(ValueError,'project page bound'):
            self.run_sync(Client([1], {}), max_pages=1)

    def test_query_does_not_switch_to_different_history_root(self):
        document, variables = lb._tail_query({'1':500})
        self.assertIn('histories(first: 100',document)
        self.assertNotIn('project_histories',document)
        self.assertEqual(variables, {'id0':[1], 'offset0':500})

    def test_source_instance_is_required(self):
        with self.assertRaises(ValueError):
            lb.sync_logbook(Client([],{}), source_instance=' ')

if __name__ == '__main__':
    unittest.main()

class TestLogbookMetadata(unittest.TestCase):
    def test_visibility_hides_from_project_without_overwriting_content_or_dates(self):
        row = SimpleNamespace(name='C',link_name='L',subject='Local title',comment_by='Local author',
            custom_hero_project=None,custom_hero_logbook_context=None,reference_doctype='Project',reference_name='P')
        writes=[]
        fake=SimpleNamespace(db=SimpleNamespace(sql=lambda *a,**k:[row],set_value=lambda *a,**k:writes.append((a,k))))
        entry={'id':1,'custom_title':'Source title','author_name':'Source author','role_visibility':',manager,',
               'modified':'2026-01-01','weather_attachment':{'temp':18},'additional_file_uploads':[{'id':2}]}
        with patch.object(lb,'frappe',fake):
            lb.enrich_entry('P',entry,{},'default')
        fields=writes[0][0][2]
        self.assertEqual(fields['reference_doctype'],'HERO Integration Link')
        self.assertEqual(fields['reference_name'],'L')
        self.assertFalse({'content','creation','modified','subject','comment_by'} & fields.keys())
        self.assertIn('weather_attachment',fields['custom_hero_logbook_context'])
        self.assertEqual(writes[0][1],{'update_modified':False})

    def test_previously_restricted_comment_is_never_published_automatically(self):
        row=SimpleNamespace(name='C',link_name='L',subject='Title',comment_by='Author',custom_hero_project='P',
                            custom_hero_logbook_context=None,reference_doctype='HERO Integration Link',reference_name='L')
        write=SimpleNamespace(set_value=lambda *a,**k:None)
        fake=SimpleNamespace(db=SimpleNamespace(sql=lambda *a,**k:[row],set_value=unittest.mock.Mock()))
        with patch.object(lb,'frappe',fake):lb.enrich_entry('P',{'id':1,'role_visibility':''},{},'default')
        self.assertNotIn('reference_doctype',fake.db.set_value.call_args.args[2])
