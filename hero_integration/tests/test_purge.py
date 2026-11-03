"""The HERO purge deletes only bound targets, dependents first, and keeps referenced ones."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hero_integration.services import purge


class _Throw(Exception):
    pass


class _Frappe:
    def __init__(self, links, active=(), allow_deletion=True):
        self.links = {link.name: link for link in links}
        self.docs = {(link.target_doctype, link.target_name) for link in links}
        self.active, self.allow_deletion = list(active), allow_deletion
        self.deleted, self.commits = [], 0
        self.db = SimpleNamespace(
            exists=lambda doctype, name: (doctype, name) in self.docs,
            savepoint=lambda name: None, rollback=lambda **kwargs: None,
            commit=self._commit, get_single_value=lambda *args: self.allow_deletion,
            count=lambda doctype: 0, delete=lambda doctype: None,
        )

    def _commit(self):
        self.commits += 1

    def only_for(self, role):
        pass

    def throw(self, message, exc=None):
        raise _Throw(message)

    def get_all(self, doctype, filters=None, fields=None, pluck=None, limit_page_length=None):
        if doctype == "HERO Integration Run":
            return self.active
        return list(self.links.values())

    def delete_doc(self, doctype, name, **kwargs):
        if doctype == "HERO Integration Link":
            self.links.pop(name)
        else:
            self.docs.discard((doctype, name))
        self.deleted.append((doctype, name))


def _link(name, doctype, target):
    return SimpleNamespace(name=name, target_doctype=doctype, target_name=target)


LINKS = [_link("L1", "Customer", "C-1"), _link("L2", "Project", "P-1"), _link("L3", "Task", "T-1"),
         _link("L4", "Event", "E-1"), _link("L5", "Address", "A-1"), _link("L6", "Customer", "C-2")]


class TestPurge(unittest.TestCase):
    def _run(self, fake, references=lambda doctype, name, ignore=(): None, **kwargs):
        import hero_integration.services.guards as guards
        from hero_integration.services import migration
        with patch.object(purge, "frappe", fake), patch.object(guards, "frappe", fake), \
                patch.object(migration, "_references_besides_hero", side_effect=references):
            return purge.purge_hero_data(**kwargs)

    def test_employee_and_existing_user_are_preserved_with_their_source_links(self):
        personnel = [_link("LE", "Employee", "employee-1"), _link("LU", "User", "user@example.invalid")]
        for dry_run in (1, 0):
            fake = _Frappe(personnel)
            report = self._run(fake, dry_run=dry_run, confirm=purge.CONFIRMATION)
            self.assertEqual(report["kept"], {"Employee": 1, "User": 1})
            self.assertEqual(fake.deleted, [])
            self.assertEqual(len(fake.links), 2)
            if dry_run:
                self.assertEqual(report["would_delete"], {})

    def test_dry_run_only_counts(self):
        fake = _Frappe(LINKS)
        report = self._run(fake)
        self.assertEqual(report["would_delete"], {"Task": 1, "Event": 1, "Project": 1, "Address": 1, "Customer": 2})
        self.assertEqual(fake.deleted, [])

    def test_real_run_needs_phrase_setting_and_no_active_run(self):
        for fake, kwargs in ((_Frappe(LINKS), {"dry_run": 0}),
                             (_Frappe(LINKS, allow_deletion=False), {"dry_run": 0, "confirm": purge.CONFIRMATION}),
                             (_Frappe(LINKS, active=["HERO-RUN-1"]), {"dry_run": 0, "confirm": purge.CONFIRMATION})):
            with self.assertRaises(Exception):
                self._run(fake, **kwargs)
            self.assertEqual(fake.deleted, [])

    def test_dependents_first_and_referenced_targets_are_kept(self):
        fake = _Frappe(LINKS)
        references = lambda doctype, name, ignore=(): "Sales Order SO-1" if name == "C-2" else None
        report = self._run(fake, references, dry_run=0, confirm=purge.CONFIRMATION)
        targets = [item for item in fake.deleted if item[0] != "HERO Integration Link"]
        self.assertEqual(targets, [("Task", "T-1"), ("Event", "E-1"), ("Project", "P-1"), ("Address", "A-1"),
                                   ("Customer", "C-1")])
        self.assertEqual(report["kept"], {"Customer": 1})
        self.assertIn("L6", fake.links)  # the kept Customer keeps its binding
        self.assertEqual(report["links_removed"], 5)

    def test_customer_ignores_its_own_contacts_and_addresses(self):
        seen = {}
        fake = _Frappe([_link("L1", "Customer", "C-1")])
        self._run(fake, lambda doctype, name, ignore=(): seen.setdefault(doctype, set(ignore)) and None,
                  dry_run=0, confirm=purge.CONFIRMATION)
        self.assertEqual(seen["Customer"], {"HERO Integration Link", "Contact", "Address"})


if __name__ == "__main__":
    unittest.main()
