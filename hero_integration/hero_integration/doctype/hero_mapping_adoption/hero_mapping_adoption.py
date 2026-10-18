from __future__ import annotations

import hashlib

import frappe
from frappe.model.document import Document


class HEROMappingAdoption(Document):
    def validate(self) -> None:
        frappe.only_for("System Manager")
        if self.package_json:
            self.package_fingerprint = hashlib.sha256(self.package_json.encode()).hexdigest()
