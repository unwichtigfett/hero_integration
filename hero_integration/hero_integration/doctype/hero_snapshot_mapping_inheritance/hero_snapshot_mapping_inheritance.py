from __future__ import annotations

from frappe.model.document import Document


class HEROSnapshotMappingInheritance(Document):
    """Server-written audit row; created only by services.snapshot_inheritance."""
