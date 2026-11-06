"""Public Send entry points backed by configured mutation mappings."""

import frappe
from hero_integration.services.localized_error import LocalizedError
from hero_integration.services.project_send import SEND_ROLES


class CustomerSendError(LocalizedError):
    """The configured send could not be completed safely."""


def may_send() -> bool:
    return bool(set(SEND_ROLES) & set(frappe.get_roles()))



def preview(customer: str, client=None) -> dict:
    from hero_integration.services.mapped_send import preview as mapped_preview
    try:
        return mapped_preview('Customer', customer, client=client)
    except ValueError as error:
        raise CustomerSendError.from_error(error) from error



def send(customer: str, fingerprint: str = '', confirmed=0, client=None, mutation_client=None) -> dict:
    from hero_integration.services.mapped_send import send as mapped_send
    try:
        return mapped_send('Customer', customer, fingerprint=fingerprint, confirmed=confirmed,
                           client=client, mutation_client=mutation_client)
    except ValueError as error:
        raise CustomerSendError.from_error(error) from error
