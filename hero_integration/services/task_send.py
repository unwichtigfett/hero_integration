"""Public Send entry points backed by configured mutation mappings."""

import html
import re
import frappe
from hero_integration.services.localized_error import LocalizedError
from hero_integration.services.project_send import SEND_ROLES


class TaskSendError(LocalizedError):
    """The configured send could not be completed safely."""


def may_send() -> bool:
    return bool(set(SEND_ROLES) & set(frappe.get_roles()))



def plain_text(value) -> str | None:
    """Text of a Text Editor value (inverse of the inbound <p>…<br>…</p>)."""
    if not isinstance(value, str):
        return None
    text = re.sub(r"(?i)<br\s*/?>", "\n", value)
    text = re.sub(r"(?i)</p>\s*<p[^>]*>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
    return text or None



def preview(task: str, client=None) -> dict:
    from hero_integration.services.mapped_send import preview as mapped_preview
    try:
        return mapped_preview('Task', task, client=client)
    except ValueError as error:
        raise TaskSendError.from_error(error) from error



def send(task: str, fingerprint: str = '', confirmed=0, client=None, mutation_client=None) -> dict:
    from hero_integration.services.mapped_send import send as mapped_send
    try:
        return mapped_send('Task', task, fingerprint=fingerprint, confirmed=confirmed,
                           client=client, mutation_client=mutation_client)
    except ValueError as error:
        raise TaskSendError.from_error(error) from error
