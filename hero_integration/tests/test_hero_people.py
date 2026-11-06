import unittest
from types import SimpleNamespace
from unittest import mock

from hero_integration.integrations import hero_people as people


class _Client:
    def __init__(self, rows):
        self.rows = rows

    def query(self, document, variables):
        assert "company" in document and "partners" in document
        return {"data": {"company": {"partners": self.rows}}}


class TestHeroPeople(unittest.TestCase):
    def test_login_email_prefers_the_hero_user_and_is_lower_case(self):
        self.assertEqual(people.person_email({"email": "x@y.de", "user": {"email": " Andrej.L@Reba.de "}}), "andrej.l@reba.de")
        self.assertEqual(people.person_email({"email": "x@y.de", "user": None}), "x@y.de")
        self.assertIsNone(people.person_email({"email": "", "user": {}}))

    def test_only_missing_active_people_are_created_without_mail_or_customer_role(self):
        rows = [{"id": 1, "first_name": "Tim", "last_name": "K", "status": "active", "user": {"id": 11, "email": "tim@reba.de"}},
                {"id": 2, "first_name": "Old", "status": "inactive", "user": {"email": "old@reba.de"}},
                {"id": 3, "first_name": "Ana", "status": "active", "user": {"id": 33, "email": "ana@reba.de"}}]
        inserted, linked = [], []

        class User(dict):
            flags = SimpleNamespace()

            def get(self, key, default=None):
                return [SimpleNamespace(role="Customer")] if key == "roles" else dict.get(self, key, default)

            def insert(self, **kwargs):
                inserted.append(self)

            def remove_roles(self, *roles):
                self["removed"] = roles

        def sql(query, values=None):
            if "custom_hero_partner_id" in query:
                return []
            if "LOWER(email)=%s" in query:
                return [("ana@reba.de",)] if values[0] == "ana@reba.de" else []
            return [("ana@reba.de",), ("gone@reba.de",)]

        fake = SimpleNamespace(get_doc=lambda values: User(values),
                               db=SimpleNamespace(sql=sql, savepoint=lambda name: None, rollback=lambda **k: None,
                                                 has_column=lambda doctype, column: True,
                                                 get_value=lambda *a, **k: {}, set_value=lambda *a, **k: linked.append(a)))
        with mock.patch.object(people, "frappe", fake), mock.patch.object(people, "_approved_people_request",
                return_value=(SimpleNamespace(document="query { company { partners { id } } }", variables={}), "company.partners")):
            result = people.sync_users(_Client(rows))
        self.assertEqual(result["created"], ["tim@reba.de"])
        self.assertEqual(result["existing"], ["ana@reba.de"])
        self.assertEqual(len(result["skipped"]), 1)  # the inactive partner
        self.assertEqual(result["not_in_hero"], ["gone@reba.de"])
        user = inserted[0]
        self.assertEqual((user["send_welcome_email"], user["user_type"], user["enabled"]), (0, "System User", 1))
        self.assertTrue(user.flags.no_welcome_mail)
        self.assertEqual(user["removed"], ("Customer",))
        self.assertEqual((user["custom_hero_partner_id"], user["custom_hero_user_id"]), ("1", "11"))
        # The existing user only gets its empty ID fields filled.
        self.assertEqual(linked, [("User", "ana@reba.de", {"custom_hero_partner_id": "3", "custom_hero_user_id": "33"})])
        self.assertEqual(result["ids_linked"], ["ana@reba.de"])


class TestPeopleMaterialization(unittest.TestCase):
    def test_partner_read_is_schema_valid_for_both_supported_roots(self):
        from graphql import build_schema, introspection_from_schema, parse, validate
        from hero_integration.services.graphql_graph import analyze_snapshot
        from hero_integration.services.mapping_contract_v1 import SnapshotAdapter, StoredSnapshot
        fields = ' '.join(name + ': String' for name in people.PEOPLE_PATHS if '.' not in name)
        for path, root in [('partners', 'partners:[Partner]'), ('company.partners', 'company:Company')]:
            schema = build_schema('type Query { ' + root + ' } type Company { partners:[Partner] } '
                'type Partner { ' + fields + ' user:User } type User { id:String email:String }')
            raw = introspection_from_schema(schema)
            snapshot = SnapshotAdapter().adapt(StoredSnapshot('people', raw, analyze_snapshot(raw).fingerprint, 'source'))
            request = people._people_request(snapshot, path)
            self.assertEqual(validate(schema, parse(request.document)), [])
            self.assertEqual(request.variables, {})

    def test_missing_collection_is_not_reported_as_an_empty_success(self):
        with mock.patch.object(people, '_approved_people_request', return_value=(
                SimpleNamespace(document='query', variables={}), 'partners')):
            with self.assertRaises(ValueError):
                people.partners(SimpleNamespace(query=lambda *args: {}))
