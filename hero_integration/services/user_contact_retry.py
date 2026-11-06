"""Recover Frappe's User-contact job from transient database deadlocks."""
from hashlib import sha256
import sys

import frappe

CORE_JOB = 'frappe.core.doctype.user.user.create_contact'
RETRY_JOB = 'hero_integration.services.user_contact_retry.retry_contact'


def after_job(method, kwargs, result=None):
    """Schedule one deduplicated retry only for the known failed core job."""
    del result
    error = sys.exc_info()[1]
    if method != CORE_JOB or not isinstance(error, frappe.QueryDeadlockError):
        return
    user = (kwargs or {}).get('user')
    name = user.get('name') if hasattr(user, 'get') else getattr(user, 'name', None)
    if not isinstance(name, str) or not name or name in {'Administrator', 'Guest'}:
        return
    key = sha256((frappe.local.site + '\0' + name).encode()).hexdigest()
    try:
        frappe.enqueue(RETRY_JOB, queue='default', job_id='hero-user-contact-' + key,
                       deduplicate=True, user_name=name,
                       ignore_links=(kwargs or {}).get('ignore_links') is True,
                       ignore_mandatory=(kwargs or {}).get('ignore_mandatory') is True)
    except Exception:
        # Never mask the original core-job failure if the retry queue is down.
        frappe.logger('hero_integration').warning('Could not enqueue User contact retry')


def retry_contact(user_name, ignore_links=False, ignore_mandatory=False):
    """Use Frappe's own bounded retry handling with the current User values."""
    if not frappe.db.exists('User', user_name):
        return
    from frappe.core.doctype.user.user import create_contact
    try:
        create_contact(frappe.get_doc('User', user_name), ignore_links=ignore_links,
                       ignore_mandatory=ignore_mandatory)
    except frappe.QueryDeadlockError as error:
        raise frappe.RetryBackgroundJobError from error
