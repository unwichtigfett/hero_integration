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
        rows = [{"id": 1, "first_name": "Tim", "last_name": "K", "status": "active", "user": {"email": "tim@reba.de"}},
                {"id": 2, "first_name": "Old", "status": "inactive", "user": {"email": "old@reba.de"}},
                {"id": 3, "first_name": "Ana", "status": "active", "user": {"email": "ana@reba.de"}}]
        inserted = []

        class User(dict):
            flags = SimpleNamespace()

            def get(self, key, default=None):
                return [SimpleNamespace(role="Customer")] if key == "roles" else dict.get(self, key, default)

            def insert(self, **kwargs):
                inserted.append(self)

            def remove_roles(self, *roles):
                self["removed"] = roles

        def sql(query, values=None):
            if "LOWER(email)=%s" in query:
                return [("ana@reba.de",)] if values[0] == "ana@reba.de" else []
            return [("ana@reba.de",), ("gone@reba.de",)]

        fake = SimpleNamespace(get_doc=lambda values: User(values),
                               db=SimpleNamespace(sql=sql, savepoint=lambda name: None, rollback=lambda **k: None))
        with mock.patch.object(people, "frappe", fake):
            result = people.sync_users(_Client(rows))
        self.assertEqual(result["created"], ["tim@reba.de"])
        self.assertEqual(result["existing"], ["ana@reba.de"])
        self.assertEqual(len(result["skipped"]), 1)  # the inactive partner
        self.assertEqual(result["not_in_hero"], ["gone@reba.de"])
        user = inserted[0]
        self.assertEqual((user["send_welcome_email"], user["user_type"], user["enabled"]), (0, "System User", 1))
        self.assertTrue(user.flags.no_welcome_mail)
        self.assertEqual(user["removed"], ("Customer",))
