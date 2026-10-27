from copy import deepcopy
import json
import unittest
from hero_integration.services.personal_mapping_content import bounded, validate_edits
from hero_integration.services.mapping_draft_service import MappingDraftError

class TestPersonalMappingContent(unittest.TestCase):
    def test_new_assignments_have_bounded_explicit_shape_and_unique_identity(self):
        prior = {'source': {'field': 'existing'}, 'extra': {'keep': 3}}
        item = {'id': '8a6c58ba-08fb-4da4-87e6-684bb1b16e19',
                'source': {'doctype': 'Customer', 'field': 'customer_name'},
                'target': {'operation': 'update_contact', 'argument': 'contact', 'input_type': 'CustomerInput', 'field': 'full_name'},
                'transformation': 'Identity'}
        submitted = {**deepcopy(prior), 'additional_mappings': [item]}
        self.assertEqual(submitted, validate_edits(prior, submitted))
        for invalid in ([item, item], [{**item, 'actor': 'Administrator'}], [{**item, 'id': 'not-an-id'}],
                        [{**item, 'source': {'doctype': 'Customer', 'field': ''}}]):
            with self.assertRaises(MappingDraftError):
                validate_edits(prior, {**deepcopy(prior), 'additional_mappings': invalid})

    def test_canonical_utf8_size_boundary(self):
        value={'x':'a'*65528}
        self.assertEqual(65536,len(json.dumps(value,separators=(',', ':')).encode()))
        self.assertEqual(value,bounded(value))
        with self.assertRaises(MappingDraftError): bounded({'x':'a'*65529})

    def test_immutable_values_preserve_json_types_and_presence(self):
        prior={'extra':{'flag':False,'zero':0,'empty':'','list':[]},'source':{'field':'before','extension':{'keep':False}}}
        changed=deepcopy(prior);changed['source']['field']='after'
        self.assertEqual(changed,validate_edits(prior,changed))
        for value in ({'extra':{'flag':0,'zero':0,'empty':'','list':[]},'source':prior['source']},
                      {'source':prior['source']},
                      {**prior,'actor':'Administrator'}):
            with self.assertRaises(MappingDraftError): validate_edits(prior,value)

    def test_browser_integral_float_roundtrip_retains_original_subtrees(self):
        prior={'extra':{'whole':1.0,'negative_zero':-0.0},'source':{'field':'before','extension':{'whole':1.0}}}
        submitted={'extra':{'whole':1,'negative_zero':0},'source':{'field':'after','extension':{'whole':1}}}
        result=validate_edits(prior,submitted)
        expected=deepcopy(prior);expected['source']['field']='after'
        self.assertEqual(json.dumps(expected,sort_keys=True),json.dumps(result,sort_keys=True))
        self.assertIsInstance(result['extra']['whole'],float)
