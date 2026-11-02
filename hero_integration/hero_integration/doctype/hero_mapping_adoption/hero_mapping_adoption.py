from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib

import frappe
from frappe.model.document import Document


_service_write = ContextVar("hero_mapping_adoption_service_write", default=False)


@contextmanager
def service_write_context():
    token = _service_write.set(True)
    try:
        yield
    finally:
        _service_write.reset(token)


class HEROMappingAdoption(Document):
    def validate(self) -> None:
        frappe.only_for("System Manager")
        if not _service_write.get():
            frappe.throw(frappe._("Mapping adoption records are managed by the adoption service."))
        if self.package_json:
            self.package_fingerprint = hashlib.sha256(self.package_json.encode()).hexdigest()
