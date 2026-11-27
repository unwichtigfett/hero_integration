import json

import frappe
from frappe.model.document import Document

from hero_integration.services.address_correction import key


class HEROAddressCorrection(Document):
    def autoname(self):
        self.name = key(self.source_instance, self.customer_id, self.address_id)

    def validate(self):
        if self.name != key(self.source_instance, self.customer_id, self.address_id):
            frappe.throw('The original customer and address identity are immutable.')
        if self.is_new():
            evidence = json.loads(self.evidence_json)
            csv_proof = evidence.get('version') == 1 and (evidence.get('csv') or {}).get('sha256')
            manual = (evidence.get('version') == 2 and evidence.get('method') == 'operator_location'
                      and evidence.get('operator') == frappe.session.user and evidence.get('original'))
            if not (csv_proof or manual) or not evidence['native']['creation']:
                frappe.throw('A verified address correction requires its original source and native evidence.')
        else:
            previous = self.get_doc_before_save()
            if previous and any(self.has_value_changed(field.fieldname) for field in self.meta.fields if field.fieldname != 'active'):
                frappe.throw('A recorded address correction can only be enabled or revoked.')
