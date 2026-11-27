from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from unittest import TestCase

from hero_integration.services.binding_registry import BindingConflict, ObjectBinding, compatible_binding
from hero_integration.services.direct_mapping import MappingSpec
from hero_integration.services.employee_binding_revision import compatible_rules
from hero_integration.services.mapping_runtime import MappingPlan


class EmployeeRevisionTests(TestCase):
    def setUp(self):
        old = MappingSpec('HeroEmployees','first_name','Employee','first_name','string','Field','Trim','',10,'first')
        added = replace(old, definition_id='extra', source_path='company_id', target_field='custom_hero_company')
        plan = MappingPlan('employees', (old,), 'HeroEmployees', {'version':2})
        self.plan = replace(plan, definitions=(old, added), adapter_options={'version':2,'source_history':'partner_absences'})
        self.query = 'query Employees { partners { id first_name } }'
        self.new_query = self.query.replace('first_name','first_name company_id')
        self.old = self.config(plan, self.query)
        self.current = self.config(self.plan, self.new_query)

    @staticmethod
    def config(plan, query):
        return {'target_operation':'HeroEmployees','records_path':'partners','query_adapter':plan.adapter_options,
                'mapping_definition_ids':[r.definition_id for r in plan.definitions], 'query_adapter_seal':{
                'version':2,'source_instance':'test','read_shape_sha256':'shape','variables_sha256':'variables',
                'schema_fingerprint':'schema','mapping_version':plan.mapping_version,
                'document_sha256':sha256(query.encode()).hexdigest()}}

    def test_additions_keep_proven_fields(self):
        self.assertEqual(set(compatible_rules(self.plan,self.old,self.current,self.query,self.new_query)), {'first'})

    def test_changed_transform_or_query_arguments_require_review(self):
        rule, extra = self.plan.definitions
        for plan, query in [(replace(self.plan,definitions=(replace(rule,transformation='Identity'),extra)), self.new_query),
                            (self.plan,self.new_query.replace('partners','partners(first: 1)'))]:
            with self.assertRaises(BindingConflict):
                compatible_rules(plan,self.old,self.config(plan,query),self.query,query)

    def test_changed_field_options_and_removed_fields_require_review(self):
        changed=deepcopy(self.old)
        changed['query_adapter']['transform_options']={'first':{'target_type':'Int'}}
        for old in (changed,dict(self.old,mapping_definition_ids=['missing'])):
            with self.assertRaises(BindingConflict):
                compatible_rules(self.plan,old,self.current,self.query,self.new_query)

    def test_verified_birth_key_survives_only_for_the_same_current_binding(self):
        args=dict(site_scope='site',source_instance='default',object_type='Partner',external_object_id='1',
                  target_app='erpnext',target_doctype='Employee',target_name='E1',mapping_definition_id='employee')
        initial=ObjectBinding.create(**args,mapping_revision=3)
        current=replace(initial,mapping_revision=4,identity_mapping_revision=3,revision=2)
        self.assertEqual(compatible_binding(current,ObjectBinding.create(**args,mapping_revision=4)),current)
        for wrong in (ObjectBinding.create(**args,mapping_revision=3),
                      ObjectBinding.create(**dict(args,target_name='E2'),mapping_revision=4)):
            with self.assertRaises(BindingConflict):
                compatible_binding(current,wrong)
