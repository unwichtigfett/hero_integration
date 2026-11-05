"""Display the running app version and public site label on full HTML pages."""
from html import escape
import re
from urllib.parse import quote, urlsplit

import frappe

from hero_integration import __version__


MARKER = 'id="hero-site-indicator"'


def label():
    configured = frappe.conf.get('host_name') or ''
    try:
        host = urlsplit(configured if '://' in configured else '//' + configured).hostname
    except ValueError:
        host = None
    site = host or str(frappe.local.site)
    return f'v{__version__}_{site.split(".", 1)[0]}'


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
