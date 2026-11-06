"""Collection imports preserve native identities, local ownership and transaction scope."""
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch

from hero_integration.services import mapped_collection_relations as collection
from hero_integration.services import mapped_relations as relations
from hero_integration.tests.test_mapped_relations import Doc, Site

ADDRESSES = {'kind': 'customer_addresses', 'collection_path': 'customer_addresses',
    'identity_path': 'address.id', 'primary_identity_path': 'address.id', 'deleted_path': 'is_deleted',
    'fields': {'address_line1': 'address.street', 'city': 'address.city', 'pincode': 'address.zipcode'},
    'country_code_path': 'address.country.code', 'address_type': 'Other'}
CONTACTS = {'kind':'customer_contacts','collection_path':'contacts','identity_path':'id','parent_identity_path':'parent_customer_id','parent_source_identity_path':'id','deleted_path':'is_deleted','contact_person_path':'is_contact_person','fields':{'first_name':'first_name','last_name':'last_name','email_id':'email','mobile_no':'phone_mobile','phone':'phone_home','salutation':'title','designation':'position'},'salutation_map':{'herr':'Herr'}}
PRICES = {'kind': 'sales_prices', 'collection_path': 'sales_prices', 'identity_path': 'id',
    'price_list_identity_path': 'supply_sales_price_id', 'fields': {'price_list_rate': 'net_price_per_unit'},
    'price_lists': {'10': {'name': 'Standard Selling', 'currency': 'EUR', 'create': False},
                   '20': {'name': 'Wholesale', 'currency': 'EUR', 'create': True}}}


class CollectionSite(Site):
    def __init__(self):
        super().__init__()
        for dt, name, extra in [('Item', 'I1', {'stock_uom': 'Nos'}), ('Currency', 'EUR', {}),
            ('Price List', 'Standard Selling', {'currency': 'EUR', 'enabled': 1, 'selling': 1, 'buying': 0})]:
            self.docs[(dt, name)] = Doc(self, {'doctype': dt, 'name': name, **extra})
    def get_all(self, dt, filters, fields, limit_page_length=0):
        if dt=='Dynamic Link':
            return [NS(parent=name) for (kind,name),doc in self.docs.items() if kind==filters.get('parenttype')
                and any(row.link_doctype==filters.get('link_doctype') and row.link_name==filters.get('link_name') for row in doc.get('links',[]))]
        return super().get_all(dt,filters,fields,limit_page_length)
    def get_doc(self, *args, **kwargs):
        if isinstance(args[0], dict) and args[0].get('doctype') == 'Price List':
            args = ({**args[0], 'name': args[0]['price_list_name']},)
        return super().get_doc(*args)


class TestMappedCollectionRelations(TestCase):
    def setUp(self):
        self.site = CollectionSite()
        self.address = {'address': {'id': 1}, '_hero_source_instance': 'source-a', 'customer_addresses': [
            {'is_deleted': False, 'address': {'id': 1}},
            {'is_deleted': False, 'address': {'id': 2, 'street': 'Example 2', 'city': 'Example City',
                'zipcode': '12345', 'country': {'code': 'DE'}}},
            {'is_deleted': True, 'address': {'id': 3}}]}
        self.contacts={'id':100,'_hero_source_instance':'source-a','contacts':[{'id':101,'parent_customer_id':100,'is_contact_person':True,'is_deleted':False,'first_name':'Example','last_name':'Contact','email':'contact@example.invalid','phone_mobile':'+491234','phone_home':'+495678','title':None,'position':'Office'}]}
        self.prices = {'_hero_source_instance': 'source-a', 'sales_prices': [
            {'id': 'p1', 'supply_sales_price_id': 10, 'net_price_per_unit': 12.5},
            {'id': 'p2', 'supply_sales_price_id': 20, 'net_price_per_unit': 9.5}]}
    def sync(self, config, record, preview=False):
        dt = collection.PARENTS[config['kind']]
        with patch.object(relations, 'frappe', self.site), patch.object(collection, 'frappe', self.site):
            return relations.sync(NS(adapter_options={'native_relations': [config]}, mapping_version='test-v2'),
                record, dt, 'C1' if dt == 'Customer' else 'I1', preview=preview)
    def docs(self, dt): return [doc for (kind, _), doc in self.site.docs.items() if kind == dt]

    def test_incomplete_address_leaves_customer_imported_and_reports_pending_relation(self):
        from hero_integration.services import migration
        plan = NS(adapter_options={'native_relations': [ADDRESSES]}, target_doctype='Customer',
                  source_identity=lambda record: str(record['id']))
        result = migration.Result('UPDATED', 'Customer imported.', 'Customer', 'C1')
        run = NS(mode='Execute', name='RUN')
        with patch.object(relations, 'sync', side_effect=collection.IncompleteCustomerAddress('Country missing')), \
             patch.object(migration, 'record_output') as report:
            actual = migration._sync_mapped_relations(plan, {'id': 10}, result, run)
        self.assertEqual(actual.code, 'UPDATED')
        self.assertTrue(actual.pending_relation)
        self.assertEqual(report.call_args.kwargs['result'], 'PENDING_REVIEW')
        self.assertIn('Country missing', report.call_args.kwargs['detail'])

    def test_address_defaults_empty_country_to_germany_but_keeps_explicit_country(self):
        record = deepcopy(self.address)
        record['customer_addresses'][1]['address']['country'] = None
        self.assertEqual(collection.prepare(ADDRESSES, record, 'Customer', 'C1')[0]['values']['country_code'], 'de')
        record['customer_addresses'][1]['address']['country'] = {'code': 'FR'}
        self.assertEqual(collection.prepare(ADDRESSES, record, 'Customer', 'C1')[0]['values']['country_code'], 'fr')
        del record['customer_addresses'][1]['address']['country']
        with self.assertRaisesRegex(ValueError, 'absent'):
            collection.prepare(ADDRESSES, record, 'Customer', 'C1')

    def test_product_and_price_list_form_a_stable_explicit_price_identity(self):
        config = {**PRICES, 'parent_source_identity_path': 'product_id'}
        row = {'product_id': 'product-A', '_hero_source_instance': 'source-a', 'sales_prices': [
            {'id': None, 'supply_sales_price_id': 10, 'net_price_per_unit': 12.5}]}
        first = collection.prepare(config, row, 'Item', 'I1')[0]
        row['sales_prices'][0]['id'] = 123
        second = collection.prepare(config, row, 'Item', 'I1')[0]
        self.assertEqual(first['source_id'], second['source_id'])
        row['product_id'] = 'product-B'
        self.assertNotEqual(first['source_id'], collection.prepare(config, row, 'Item', 'I1')[0]['source_id'])
        self.assertIn('product_id', collection.required_paths(config))

    def test_composite_price_identity_rejects_duplicate_lists(self):
        config = {**PRICES, 'parent_source_identity_path': 'product_id'}
        row = {'product_id': 'product-A', '_hero_source_instance': 'source-a', 'sales_prices': [
            {'id': None, 'supply_sales_price_id': 10, 'net_price_per_unit': 12.5},
            {'id': 4, 'supply_sales_price_id': 10, 'net_price_per_unit': 12.5}]}
        with self.assertRaisesRegex(ValueError, 'multiple prices'):
            collection.prepare(config, row, 'Item', 'I1')

    def test_secondary_addresses_skip_primary_and_deleted_and_repeat_without_duplicates(self):
        self.assertEqual(self.sync(ADDRESSES, self.address)['created'], 1)
        self.assertEqual(self.sync(ADDRESSES, self.address)['unchanged'], 1)
        addresses = self.docs('Address')
        self.assertEqual(len(addresses), 1)
        self.assertEqual((addresses[0].country, addresses[0].links[0].link_name), ('Germany', 'C1'))
        self.assertFalse(self.site.docs['Customer', 'C1'].customer_primary_address)
        self.assertEqual(self.docs('HERO Integration Link')[0].source_type, 'customer_address')

    def test_equal_source_addresses_share_existing_bound_native_address(self):
        self.assertEqual(self.sync(ADDRESSES, self.address)['created'], 1)
        native = self.docs('Address')[0]
        native.set('address_type', 'Billing')
        alias = deepcopy(self.address)
        alias['customer_addresses'][1]['address']['id'] = 4
        self.assertEqual(self.sync(ADDRESSES, alias)['created'], 1)
        self.assertEqual(len(self.docs('Address')), 1)
        self.assertEqual(native.address_type, 'Billing')
        links = self.docs('HERO Integration Link')
        self.assertEqual({link.source_id for link in links}, {'2', '4'})
        self.assertEqual({link.target_name for link in links}, {native.name})

    def test_partial_address_match_is_not_automatically_aliased(self):
        self.sync(ADDRESSES, self.address)
        alias = deepcopy(self.address)
        alias['customer_addresses'][1]['address'].update(id=4, zipcode='99999')
        with self.assertRaises(collection.UnboundCustomerAddress):
            self.sync(ADDRESSES, alias)
        self.assertEqual(len(self.docs('Address')), 1)

    def test_address_local_edit_is_not_overwritten_and_foreign_owner_blocks(self):
        self.sync(ADDRESSES, self.address)
        address = self.docs('Address')[0]; address.set('city', 'Local correction')
        self.address['customer_addresses'][1]['address']['city'] = 'Remote correction'
        self.assertEqual(self.sync(ADDRESSES, self.address)['conflicts'], 1)
        self.assertEqual(address.city, 'Local correction')
        address.values['links'] = [NS(link_doctype='Customer', link_name='Other')]
        with self.assertRaisesRegex(ValueError, 'does not belong'): self.sync(ADDRESSES, self.address)

    def test_existing_unbound_address_requires_reconciliation(self):
        Doc(self.site, {'doctype': 'Address', 'name': 'local-address', 'address_line1': 'Example 2',
            'city': 'Example City', 'country': 'Germany', 'links': [{'link_doctype': 'Customer', 'link_name': 'C1'}]}).insert()
        with self.assertRaisesRegex(ValueError, 'reconciliation'): self.sync(ADDRESSES, self.address)
        self.assertEqual(len(self.docs('Address')), 1)

    def test_sales_prices_create_only_explicit_lists_and_native_prices(self):
        self.assertEqual(self.sync(PRICES, self.prices)['created'], 2)
        self.assertEqual(self.sync(PRICES, self.prices)['unchanged'], 2)
        self.assertEqual(len(self.docs('Item Price')), 2)
        self.assertEqual(len(self.docs('Price List')), 2)
        self.assertEqual({p.price_list for p in self.docs('Item Price')}, {'Standard Selling', 'Wholesale'})
        self.assertEqual(len([x for x in self.docs('HERO Integration Link') if x.source_type == 'sales_price_list']), 1)
        self.assertEqual(set(dt for dt, _ in self.site.inserted), {'Price List', 'Item Price', 'HERO Integration Link'})
        self.assertFalse(any(kw for dt, kw in self.site.inserted if dt in {'Price List', 'Item Price'}))

    def test_local_price_change_and_new_dimensional_restriction_are_conflicts(self):
        self.sync(PRICES, self.prices)
        price = self.docs('Item Price')[0]; price.set('price_list_rate', 77)
        self.assertEqual(self.sync(PRICES, self.prices)['conflicts'], 1)
        self.assertEqual(price.price_list_rate, 77)
        price.set('price_list_rate', 12.5); price.set('customer', 'Private customer')
        self.assertEqual(self.sync(PRICES, self.prices)['conflicts'], 1)
        self.assertEqual(price.customer, 'Private customer')

    def test_existing_erp_price_is_never_adopted_or_overwritten(self):
        Doc(self.site, {'doctype': 'Item Price', 'name': 'local-price', 'item_code': 'I1',
            'price_list': 'Standard Selling', 'uom': 'Nos', 'price_list_rate': 88}).insert()
        with self.assertRaisesRegex(ValueError, 'reconciliation'): self.sync(PRICES, self.prices)
        self.assertEqual(self.docs('Item Price')[0].price_list_rate, 88)
        self.assertEqual(len(self.docs('Item Price')), 1)

    def test_unknown_source_price_list_missing_field_and_nonfinite_rate_fail_before_writes(self):
        for update in ({'supply_sales_price_id': 999}, {'net_price_per_unit': float('nan')}, {'net_price_per_unit': -1}):
            record = deepcopy(self.prices); record['sales_prices'][1].update(update)
            with self.assertRaises(ValueError): self.sync(PRICES, record)
        record = deepcopy(self.prices); del record['sales_prices'][1]['net_price_per_unit']
        with self.assertRaisesRegex(ValueError, 'absent'): self.sync(PRICES, record)
        self.assertEqual(self.docs('Item Price'), [])
        self.assertEqual(self.site.saved, {})

    def test_all_writes_roll_back_if_later_list_is_incompatible(self):
        Doc(self.site, {'doctype': 'Price List', 'name': 'Wholesale', 'currency': 'USD',
            'selling': 1, 'buying': 0, 'enabled': 1}).insert()
        with self.assertRaisesRegex(ValueError, 'approved currency'): self.sync(PRICES, self.prices)
        self.assertEqual(self.docs('Item Price'), [])
        self.assertEqual(self.docs('HERO Integration Link'), [])
        self.assertEqual(self.site.rollbacks, ['hero_native_relations'])

    def test_preview_has_no_savepoint_or_writes(self):
        self.assertEqual(self.sync(PRICES, self.prices, preview=True)['planned'], 2)
        self.assertEqual(self.sync(ADDRESSES, self.address, preview=True)['planned'], 1)
        self.assertEqual(self.site.saved, {})
        self.assertEqual(self.site.inserted, [])

    def test_configuration_forbids_duplicate_destination_lists_and_unsafe_fields(self):
        invalid = deepcopy(PRICES); invalid['price_lists']['20']['name'] = 'Standard Selling'
        with self.assertRaisesRegex(ValueError, 'same ERPNext'): relations.validate_config([invalid])
        invalid = deepcopy(ADDRESSES); invalid['fields']['is_primary_address'] = 'id'
        with self.assertRaises(ValueError): relations.validate_config([invalid])
        self.assertIn('sales_prices.supply_sales_price_id', relations.required_paths([PRICES]))
        self.assertIn('customer_addresses.address.country.code', relations.required_paths([ADDRESSES]))
        self.assertIn('address.id', relations.required_paths([ADDRESSES]))

    def test_duplicate_rows_are_idempotent_but_conflicting_duplicates_are_rejected(self):
        record = deepcopy(self.prices); record['sales_prices'].append(deepcopy(record['sales_prices'][0]))
        self.assertEqual(self.sync(PRICES, record)['created'], 2)
        record['sales_prices'][-1]['net_price_per_unit'] = 0
        with self.assertRaisesRegex(ValueError, 'conflicting'): self.sync(PRICES, record)

    def test_null_price_does_not_zero_or_delete_managed_price(self):
        self.sync(PRICES, self.prices)
        self.prices['sales_prices'][0]['net_price_per_unit'] = None
        self.assertEqual(self.sync(PRICES, self.prices)['planned'], 1)
        self.assertEqual(self.docs('Item Price')[0].price_list_rate, 12.5)

    def test_managed_price_lookup_exposes_exact_row_identity_and_requires_baseline_proof(self):
        self.sync(PRICES, self.prices)
        with patch.object(collection, 'frappe', self.site):
            result = collection.lookup_managed_price('I1', 'Standard Selling', 'source-a')
            self.assertEqual(result['source_id'], 'p1')
            self.assertEqual(collection._hash(result['observed']), result['target_payload_hash'])
            result['document'].set('price_list_rate', 25)
            with self.assertRaisesRegex(ValueError, 'changed locally'):
                collection.lookup_managed_price('I1', 'Standard Selling', 'source-a')
            changed = collection.lookup_managed_price('I1', 'Standard Selling', 'source-a', allow_rate_change=True)
            self.assertEqual(collection._hash({**changed['observed'], 'price_list_rate': 12.5}), changed['target_payload_hash'])
            changed['document'].set('currency', 'USD')
            tampered = collection.lookup_managed_price('I1', 'Standard Selling', 'source-a', allow_rate_change=True)
            self.assertNotEqual(collection._hash({**tampered['observed'], 'price_list_rate': 12.5}), tampered['target_payload_hash'])
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                collection.lookup_managed_price('I1', 'Standard Selling', 'another-source')

    def test_contact_persons_create_native_contacts_not_customers_or_duplicate_primary(self):
        self.assertEqual(self.sync(CONTACTS,self.contacts)['created'],1)
        self.assertEqual(self.sync(CONTACTS,self.contacts)['unchanged'],1)
        contact=self.docs('Contact')[0]
        self.assertEqual(len(self.docs('Contact')),1)
        self.assertEqual((contact.first_name,contact.links[0].link_name),('Example','C1'))
        self.assertEqual(contact.email_ids[0].email_id,'contact@example.invalid')
        self.assertEqual(len(contact.phone_nos),2)
        self.assertEqual(self.docs('HERO Integration Link')[0].source_type,'customer_contact')
        self.assertFalse(self.site.docs['Customer','C1'].customer_primary_contact)

    def test_contacts_reject_wrong_parent_wrong_kind_and_skip_deleted(self):
        for change in ({'parent_customer_id':999},{'is_contact_person':False}):
            record=deepcopy(self.contacts);record['contacts'][0].update(change)
            with self.assertRaises(ValueError):self.sync(CONTACTS,record)
        self.contacts['contacts'][0]['is_deleted']=True
        self.assertEqual(self.sync(CONTACTS,self.contacts)['planned'],0)
        self.assertEqual(self.docs('Contact'),[])

    def test_contact_local_phone_edit_is_preserved_and_shared_contact_blocks(self):
        self.sync(CONTACTS,self.contacts);contact=self.docs('Contact')[0]
        contact.phone_nos[0].phone='+499999'
        self.assertEqual(self.sync(CONTACTS,self.contacts)['conflicts'],1)
        self.assertEqual(contact.phone_nos[0].phone,'+499999')
        contact.append('links',{'link_doctype':'Customer','link_name':'Other'})
        with self.assertRaisesRegex(ValueError,'shared'):self.sync(CONTACTS,self.contacts)

    def test_equal_unbound_contact_email_is_not_source_identity_proof(self):
        Doc(self.site,{'doctype':'Contact','name':'local-contact','first_name':'Example','last_name':'Contact',
            'links':[{'link_doctype':'Customer','link_name':'C1'}]}).insert()
        with self.assertRaisesRegex(ValueError,'reconciliation'):self.sync(CONTACTS,self.contacts)
        self.assertEqual(len(self.docs('Contact')),1)

    def test_primary_contact_reuse_requires_exact_source_customer_identity(self):
        self.sync(CONTACTS,self.contacts);contact=self.docs('Contact')[0]
        # Deliberately remove the contact binding, retain a proven parent/primary relationship.
        self.site.docs={key:doc for key,doc in self.site.docs.items() if key[0]!='HERO Integration Link'}
        self.site.docs['Customer','C1'].set('customer_primary_contact',contact.name)
        self.contacts['contacts'][0]['id']=100
        with self.assertRaisesRegex(ValueError,'exact imported Customer'):self.sync(CONTACTS,self.contacts)
        Doc(self.site,{'doctype':'HERO Integration Link','source_system':'HERO','source_instance':'source-a',
            'source_type':'customer','source_id':'100','target_doctype':'Customer','target_name':'C1'}).insert()
        self.assertEqual(self.sync(CONTACTS,self.contacts)['created'],1)
        self.assertEqual(len(self.docs('Contact')),1)
        self.assertEqual(self.sync(CONTACTS,self.contacts)['unchanged'],1)

    def shared_primary_contact(self, config=CONTACTS):
        person = self.contacts['contacts'][0]
        person.update(id=100, phone_home='+491234', phone_mobile='+491234')
        contact = Doc(self.site, {'doctype': 'Contact', 'name': 'shared-primary',
            'first_name': person['first_name'], 'last_name': person['last_name'],
            'designation': person['position'], 'email_ids': [NS(email_id=person['email'], is_primary=1)],
            'phone_nos': [NS(phone='+491234', is_primary_phone=1, is_primary_mobile_no=1)],
            'links': [{'link_doctype': 'Customer', 'link_name': 'C1'}]}).insert()
        self.site.docs['Customer', 'C1'].set('customer_primary_contact', contact.name)
        Doc(self.site, {'doctype': 'HERO Integration Link', 'source_system': 'HERO', 'source_instance': 'source-a',
            'source_type': 'customer', 'source_id': '100', 'target_doctype': 'Customer', 'target_name': 'C1'}).insert()
        self.assertEqual(self.sync(config, self.contacts)['created'], 1)
        self.assertEqual(len(contact.phone_nos), 1)
        return contact

    def test_shared_primary_phone_row_splits_when_source_numbers_diverge(self):
        contact = self.shared_primary_contact()
        self.contacts['contacts'][0].update(phone_home='+495678', phone_mobile='+499876')
        self.assertEqual(self.sync(CONTACTS, self.contacts)['updated'], 1)
        self.assertEqual(collection._contact_value(contact, 'phone'), '+495678')
        self.assertEqual(collection._contact_value(contact, 'mobile_no'), '+499876')
        self.assertEqual(len(contact.phone_nos), 2)
        self.assertEqual(self.sync(CONTACTS, self.contacts)['unchanged'], 1)
        self.assertEqual(len(contact.phone_nos), 2)

    def test_shared_phone_update_preserves_unmapped_mobile_number(self):
        config = deepcopy(CONTACTS)
        del config['fields']['mobile_no']
        contact = self.shared_primary_contact(config)
        self.contacts['contacts'][0]['phone_home'] = '+495678'
        self.assertEqual(self.sync(config, self.contacts)['updated'], 1)
        self.assertEqual(collection._contact_value(contact, 'phone'), '+495678')
        self.assertEqual(collection._contact_value(contact, 'mobile_no'), '+491234')

    def test_contact_save_mismatch_rolls_back_without_confirming_new_baseline(self):
        contact = self.shared_primary_contact()
        links = [doc for doc in self.docs('HERO Integration Link') if doc.source_type == 'customer_contact']
        before = links[0].target_payload_hash
        self.contacts['contacts'][0].update(phone_home='+495678', phone_mobile='+499876')
        save = Doc.save

        def corrupt(document, **kwargs):
            if document.doctype == 'Contact':
                document.phone_nos[0].phone = 'unexpected'
            return save(document, **kwargs)

        with patch.object(Doc, 'save', corrupt), self.assertRaisesRegex(ValueError, 'communication differs'):
            self.sync(CONTACTS, self.contacts)
        link = next(doc for doc in self.docs('HERO Integration Link') if doc.source_type == 'customer_contact')
        self.assertEqual(link.target_payload_hash, before)
        self.assertEqual(collection._contact_value(self.docs('Contact')[0], 'phone'), '+491234')

    def test_shared_phone_preview_does_not_split_or_change_rows(self):
        contact = self.shared_primary_contact()
        self.contacts['contacts'][0].update(phone_home='+495678', phone_mobile='+499876')
        self.assertEqual(self.sync(CONTACTS, self.contacts, preview=True)['planned'], 1)
        self.assertEqual(len(contact.phone_nos), 1)
        self.assertEqual(collection._contact_value(contact, 'phone'), '+491234')
        self.assertEqual(collection._contact_value(contact, 'mobile_no'), '+491234')

    def test_contact_preview_has_no_writes_and_unknown_salutation_blocks(self):
        self.assertEqual(self.sync(CONTACTS,self.contacts,preview=True)['planned'],1)
        self.assertEqual(self.docs('Contact'),[])
        self.assertEqual(self.site.saved,{})
        self.contacts['contacts'][0]['title']='Unknown'
        with self.assertRaisesRegex(ValueError,'salutation'):self.sync(CONTACTS,self.contacts)

    def test_identical_collection_values_do_not_save_native_documents(self):
        for config, record in ((ADDRESSES, self.address), (PRICES, self.prices), (CONTACTS, self.contacts)):
            with self.subTest(kind=config['kind']):
                self.sync(config, record)
                with patch.object(Doc, 'save', side_effect=AssertionError('unnecessary native save')):
                    result = self.sync(config, record)
                self.assertGreater(result['unchanged'], 0)
                self.assertEqual(result['updated'], 0)

    def test_unpopulated_price_with_null_identity_is_not_a_price_write(self):
        record = {'_hero_source_instance': 'source-a', 'sales_prices': [
            {'id': None, 'supply_sales_price_id': 10, 'net_price_per_unit': None}]}
        self.assertEqual(self.sync(PRICES, record)['planned'], 0)
        self.assertFalse(self.docs('Item Price'))
        record['sales_prices'][0]['net_price_per_unit'] = 0
        with self.assertRaisesRegex(ValueError, 'stable source identity'):
            self.sync(PRICES, record)
