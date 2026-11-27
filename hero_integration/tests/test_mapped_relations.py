"""Native relation imports are explicit, source-scoped and preserve local ownership."""
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch

from hero_integration.services import mapped_relations as mr

BANK = {'kind': 'customer_bank_account', 'identity_path': 'accounting.id',
        'fields': {'iban': 'accounting.iban', 'bank_name': 'accounting.bank_name', 'swift_number': 'accounting.bic'}}
ADDRESS = {'kind': 'project_address', 'identity_path': 'address.id',
           'fields': {'address_line1': 'address.street', 'address_line2': 'address.line_2', 'city': 'address.city', 'pincode': 'address.zipcode'},
           'country_code_path': 'address.country.code', 'parent_field': 'custom_hero_project_address', 'address_type': 'Other'}


class Doc:
    def __init__(self, site, values):
        self.site = site
        self.values = {'links': [], 'is_company_account': 0, 'account': None, 'company': None, **values}
        self.values['links'] = [NS(**x) if isinstance(x, dict) else x for x in self.values['links']]
    def __getattr__(self, key): return self.values.get(key)
    def get(self, key, default=None): return self.values.get(key, default)
    def set(self, key, value): self.values[key] = value
    def update(self, values): self.values.update(values)
    def append(self, key, value): self.values.setdefault(key, []).append(NS(**value))
    def insert(self, **kwargs):
        if self.doctype == self.site.fail_insert: raise RuntimeError('synthetic native write failure')
        if not self.name:
            self.values['name'] = self.bank_name if self.doctype == 'Bank' else self.doctype + '-' + str(len(self.site.docs))
        self.site.docs[(self.doctype, self.name)] = self
        self.site.inserted.append((self.doctype, kwargs))
        return self
    def save(self, **kwargs): self.site.docs[(self.doctype, self.name)] = self; return self


class Site:
    def __init__(self):
        self.docs = {}; self.inserted = []; self.fail_insert = ''; self.saved = {}; self.rollbacks = []; self.permissions = []
        self.db = NS(exists=lambda dt, name: (dt, name) in self.docs, set_value=self.set_value,
                     savepoint=self.savepoint, rollback=self.rollback, sql=self.locked_links)
        self.utils = NS(now_datetime=lambda: '2026-01-01 00:00:00')
        self.get_meta = lambda dt: NS(get_field=lambda field: NS(fieldtype='Link', options='Address'))
        for dt, name, extra in [('Customer', 'C1', {}), ('Project', 'P1', {}), ('Project', 'P2', {}), ('Country', 'Germany', {'code': 'de'})]:
            self.docs[(dt, name)] = Doc(self, {'doctype': dt, 'name': name, **extra})
    def locked_links(self, query, params, *, as_dict):
        assert as_dict and "FROM `tabHERO Integration Link`" in query and "FOR UPDATE" in query
        kind, source, identity = params
        return self.get_all("HERO Integration Link", {"source_system": "HERO", "source_type": kind,
                            "source_instance": source, "source_id": identity}, ["*"], 2)

    def get_doc(self, *args, for_update=False):
        return Doc(self, args[0]) if isinstance(args[0], dict) else self.docs[(args[0], args[1])]
    def get_all(self, dt, filters, fields, limit_page_length=0):
        def matches(doc, key, value):
            if isinstance(value, list) and value[0] == '!=':
                return doc.get(key) != value[1]
            return doc.get(key) == value
        rows = [doc for (kind, _), doc in self.docs.items() if kind == dt
                and all(matches(doc, key, value) for key, value in filters.items())]
        return rows[:limit_page_length] if limit_page_length else rows
    def has_permission(self, dt, operation, **kw): self.permissions.append((dt, operation)); return True
    def set_value(self, dt, name, fields, **kw): self.docs[(dt, name)].update(fields)
    def savepoint(self, name): self.saved[name] = {key: deepcopy(doc.values) for key, doc in self.docs.items()}
    def rollback(self, save_point):
        self.rollbacks.append(save_point)
        self.docs = {key: Doc(self, values) for key, values in self.saved[save_point].items()}


class TestNativeRelations(TestCase):
    def setUp(self):
        lock = patch('hero_integration.hero_integration.doctype.hero_frappe_mapping.hero_frappe_mapping._lock_active_target')
        self.bank_lock = lock.start()
        self.addCleanup(lock.stop)
        self.site = Site()
        fresh = patch.object(mr, '_committed_bank_accounts', side_effect=lambda iban: {
            account.name for account in self.docs('Bank Account') if account.iban == iban})
        fresh.start()
        self.addCleanup(fresh.stop)
        self.bank_record = {'id': 1, '_hero_source_instance': 'source-a', 'accounting': {
            'id': 44, 'iban': 'DE89 3704 0044 0532 0130 00', 'bic': 'COBADEFFXXX', 'bank_name': 'Synthetic Bank'}}
        self.address_record = {'id': 2, '_hero_source_instance': 'source-a', 'address': {
            'id': 9, 'street': 'Synthetic street 1', 'line_2': '', 'city': 'Synthetic City', 'zipcode': '12345', 'country': {'code': 'DE'}}}
    def sync(self, relation, record, parent=None, preview=False):
        parent_type = mr.PARENTS[relation['kind']]
        with patch.object(mr, 'frappe', self.site):
            return mr.sync(NS(adapter_options={'native_relations': [relation]}, mapping_version='test-v1'),
                           record, parent_type, parent or ('C1' if parent_type == 'Customer' else 'P1'), preview=preview)
    def docs(self, dt): return [doc for (kind, _), doc in self.site.docs.items() if kind == dt]

    def test_company_metadata_on_proven_customer_account_survives_preview_and_replay(self):
        self.sync(BANK, self.bank_record)
        account = self.docs('Bank Account')[0]
        account.set('company', 'Synthetic Company')
        self.assertEqual(self.sync(BANK, self.bank_record, preview=True)['conflicts'], 0)
        self.assertEqual(self.sync(BANK, self.bank_record)['unchanged'], 1)
        self.assertEqual(len(self.docs('Bank Account')), 1)
        self.assertEqual(account.company, 'Synthetic Company')

    def test_preview_leaves_iban_unlocked_and_deadlock_reaches_existing_worker_retry(self):
        self.sync(BANK, self.bank_record, preview=True)
        self.bank_lock.assert_not_called()
        class Deadlock(Exception):
            pass
        self.site.QueryDeadlockError = Deadlock
        with patch.object(mr, '_apply', side_effect=Deadlock('synthetic transaction rollback')):
            with self.assertRaises(Deadlock):
                self.sync(BANK, self.bank_record)
        self.bank_lock.assert_called_once_with('customer-bank-account','Bank Account','DE89370400440532013000')
        self.assertEqual(self.site.rollbacks, [])

    def test_real_company_account_ledger_owner_and_duplicate_target_still_block(self):
        self.sync(BANK, self.bank_record)
        account = self.docs('Bank Account')[0]
        account.set('company', 'Synthetic Company')
        for field, value in [('is_company_account', 1), ('account', 'Bank GL'), ('party', 'C2'), ('party_type', 'Supplier')]:
            original = account.get(field)
            account.set(field, value)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'ownership'):
                self.sync(BANK, self.bank_record, preview=True)
            account.set(field, original)
        link = self.docs('HERO Integration Link')[0]
        self.site.get_doc({**link.values, 'name': 'other-binding', 'source_id': 'other'}).insert()
        with self.assertRaisesRegex(ValueError, 'multiple.*bindings'):
            self.sync(BANK, self.bank_record, preview=True)

    def test_bank_account_import_is_native_and_repeated_without_duplicates(self):
        self.assertEqual(self.sync(BANK, self.bank_record)['created'], 1)
        self.assertEqual(self.sync(BANK, self.bank_record)['unchanged'], 1)
        account = self.docs('Bank Account')[0]
        self.assertEqual((account.party_type, account.party, account.is_company_account, account.is_default), ('Customer', 'C1', 0, 0))
        self.assertEqual(account.iban, 'DE89370400440532013000')
        self.assertEqual(len(self.docs('Bank Account')), 1)
        self.assertEqual(len(self.docs('Bank')), 1)
        self.assertNotIn('Bank Transaction', [dt for dt, kw in self.site.inserted])
        self.assertFalse(any(kw for dt, kw in self.site.inserted if dt in {'Bank', 'Bank Account'}))

    def test_exact_existing_bic_reuses_bank_without_mutating_it(self):
        Doc(self.site, {'doctype': 'Bank', 'name': 'Existing', 'bank_name': 'Existing native label', 'swift_number': 'COBADEFFXXX'}).insert()
        self.sync(BANK, self.bank_record)
        self.assertEqual(self.docs('Bank Account')[0].bank, 'Existing')
        self.assertEqual(self.docs('Bank')[0].bank_name, 'Existing native label')

    def test_name_only_collision_and_unbound_iban_are_not_adopted(self):
        Doc(self.site, {'doctype': 'Bank', 'bank_name': 'Synthetic Bank', 'swift_number': 'OTHERBIC'}).insert()
        with self.assertRaisesRegex(ValueError, 'institution'): self.sync(BANK, self.bank_record)
        self.assertEqual(self.docs('Bank Account'), [])
        self.site = Site()
        Doc(self.site, {'doctype': 'Bank Account', 'name': 'local', 'iban': 'DE89370400440532013000', 'party_type': 'Customer', 'party': 'C1'}).insert()
        with self.assertRaisesRegex(ValueError, 'reconciliation'): self.sync(BANK, self.bank_record)
        self.assertEqual(len(self.docs('Bank Account')), 1)

    def test_missing_bic_is_reusable_only_through_its_owned_account(self):
        self.bank_record['accounting']['bic'] = None
        self.sync(BANK, self.bank_record)
        self.assertEqual(self.sync(BANK, self.bank_record)['unchanged'], 1)
        self.assertEqual(len(self.docs('Bank')), 1)

    def test_local_account_change_is_retained_and_foreign_party_blocks(self):
        self.sync(BANK, self.bank_record); account = self.docs('Bank Account')[0]
        account.set('is_default', 1)
        self.assertEqual(self.sync(BANK, self.bank_record)['conflicts'], 1)
        self.assertEqual(account.is_default, 1)
        account.set('party', 'Foreign Customer')
        with self.assertRaisesRegex(ValueError, 'ownership'): self.sync(BANK, self.bank_record)

    def test_bank_creation_rolls_back_if_native_account_validation_fails(self):
        self.site.fail_insert = 'Bank Account'
        with self.assertRaisesRegex(RuntimeError, 'synthetic'): self.sync(BANK, self.bank_record)
        self.assertEqual(self.docs('Bank'), [])
        self.assertEqual(self.docs('HERO Integration Link'), [])
        self.assertEqual(self.site.rollbacks, ['hero_native_relations'])

    def test_native_address_reuses_identity_across_projects_and_preserves_local_edits(self):
        self.sync(ADDRESS, self.address_record)
        address = self.docs('Address')[0]
        self.assertEqual(address.country, 'Germany')
        self.assertEqual(self.site.docs[('Project', 'P1')].custom_hero_project_address, address.name)
        self.sync(ADDRESS, self.address_record, parent='P2')
        self.assertEqual(len(self.docs('Address')), 1)
        self.assertEqual({x.link_name for x in address.links}, {'P1', 'P2'})
        address.set('city', 'Local correction')
        self.address_record['address']['city'] = 'Changed remotely'
        self.assertEqual(self.sync(ADDRESS, self.address_record)['conflicts'], 1)
        self.assertEqual(address.city, 'Local correction')

    def test_existing_project_address_is_never_overwritten_or_duplicated(self):
        self.site.docs[('Project', 'P1')].set('custom_hero_project_address', 'Local address')
        with self.assertRaisesRegex(ValueError, 'locally owned'): self.sync(ADDRESS, self.address_record)
        self.assertEqual(self.docs('Address'), [])

    def test_project_address_uses_germany_only_for_empty_source_country(self):
        plan = NS(adapter_options={'native_relations': [ADDRESS]})
        record = deepcopy(self.address_record)
        record['address']['country'] = None
        self.assertEqual(mr.prepare(plan, record, 'Project', 'P1')[0]['values']['country_code'], 'de')
        record['address']['country'] = {'code': 'FR'}
        self.assertEqual(mr.prepare(plan, record, 'Project', 'P1')[0]['values']['country_code'], 'fr')

    def test_preview_and_empty_relations_write_nothing(self):
        self.assertEqual(self.sync(BANK, self.bank_record, preview=True)['planned'], 1)
        self.assertEqual(self.sync(ADDRESS, self.address_record, preview=True)['planned'], 1)
        self.assertEqual(self.site.inserted, [])
        self.assertEqual(self.site.saved, {})
        self.bank_record['accounting'] = None
        self.assertEqual(self.sync(BANK, self.bank_record)['planned'], 0)

    def test_bad_iban_missing_source_wrong_scope_and_protected_targets_fail(self):
        broken = deepcopy(self.bank_record); broken['accounting']['iban'] = 'DE00370400440532013000'
        with self.assertRaisesRegex(ValueError, 'valid IBAN'): self.sync(BANK, broken)
        broken = deepcopy(self.bank_record); del broken['accounting']['bic']
        with self.assertRaisesRegex(ValueError, 'absent'): self.sync(BANK, broken)
        bad = deepcopy(BANK); bad['fields']['is_default'] = 'accounting.id'
        with self.assertRaises(ValueError): mr.validate_config([bad])
        with self.assertRaises(ValueError):
            mr.prepare(NS(adapter_options={'native_relations': [BANK]}), self.bank_record, 'Project', 'P1')
        self.assertEqual(mr.required_paths([ADDRESS]), {'address.id', 'address.street', 'address.line_2', 'address.city', 'address.zipcode', 'address.country.code'})

    def test_identical_bank_values_update_link_version_without_native_save(self):
        self.sync(BANK, self.bank_record)
        with patch.object(Doc, 'save', side_effect=AssertionError('unnecessary native save')), patch.object(mr, 'frappe', self.site):
            result = mr.sync(NS(adapter_options={'native_relations': [BANK]}, mapping_version='next-version'),
                self.bank_record, 'Customer', 'C1')
        self.assertEqual(result['unchanged'], 1)
        self.assertEqual(self.docs('HERO Integration Link')[0].mapping_version, 'next-version')
