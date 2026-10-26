from copy import deepcopy
import json
import unittest
from hero_integration.services.personal_mapping_content import bounded, validate_edits
from hero_integration.services.mapping_draft_service import MappingDraftError

class TestPersonalMappingContent(unittest.TestCase):
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
