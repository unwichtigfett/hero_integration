"""Display the running app version and public site label on full HTML pages."""
from html import escape
from pathlib import Path
import re
from urllib.parse import quote, urlsplit

import frappe

from hero_integration import __version__


MARKER = 'id="hero-site-indicator"'
REVISION_FILE = Path('/opt/stack/hero_integration.revision')


def _build_revision():
    """Read the immutable image revision, rather than a possibly newer checkout."""
    try:
        revision = REVISION_FILE.read_text(encoding='ascii').strip()
    except (OSError, UnicodeError):
        return ''
    return revision[:7] if re.fullmatch(r'[0-9a-f]{40}', revision) else ''


# Loaded with the application code and stable until this worker restarts.
BUILD_REVISION = _build_revision()


def label():
    configured = frappe.conf.get('host_name') or ''
    try:
        host = urlsplit(configured if '://' in configured else '//' + configured).hostname
    except ValueError:
        host = None
    site = host or str(frappe.local.site)
    label = f'v{__version__}_{site.split(".", 1)[0]}'
    return f'{label} · {BUILD_REVISION}' if BUILD_REVISION else label


def after_request(response, request=None):
    """Cover Desk, portal and standalone app templates without a browser RPC."""
    if (response.mimetype != 'text/html' or response.is_streamed or response.direct_passthrough
            or response.headers.get('Content-Encoding') not in (None, 'identity')
            or 'attachment' in response.headers.get('Content-Disposition', '').lower()
            or request is not None and request.method == 'HEAD'):
        return
    html = response.get_data(as_text=True)
    if MARKER in html:
        return
    closing = list(re.finditer(r'</body\s*>', html, re.IGNORECASE))
    if not closing:
        return
    indicator = ('<link rel="stylesheet" href="/assets/hero_integration/css/site_indicator.css?v='
                 + quote(__version__, safe='') + '">'
                 + '<div ' + MARKER + '>' + escape(label()) + '</div>')
    position = closing[-1].start()
    response.set_data(html[:position] + indicator + html[position:])
    response.headers.pop('ETag', None)
