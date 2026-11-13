"""Offer Navy in the theme choice of the standalone Helpdesk and CRM pages, which load no Desk assets.
Their own themes are untouched; Navy applies only after the user chooses it there."""
from __future__ import annotations

from urllib.parse import quote

from hero_integration import __version__

SPA_PREFIXES = ("/helpdesk", "/crm")
MARKER = 'id="hero-spa-theme"'


def is_spa(path: str | None) -> bool:
    return bool(path) and any(path == prefix or path.startswith(prefix + "/") for prefix in SPA_PREFIXES)


def markup(request) -> str:
    if request is None or not is_spa(getattr(request, "path", None)):
        return ""
    version = quote(__version__, safe="")
    assets = "/assets/hero_integration/"
    return ('<link rel="stylesheet" href="' + assets + 'css/navy_theme.css?v=' + version + '">'
            '<link rel="stylesheet" href="' + assets + 'css/navy_spa.css?v=' + version + '">'
            '<script ' + MARKER + ' src="' + assets + 'js/navy_spa.js?v=' + version + '"></script>'
            + _desk_return(request.path, assets, version))


def _desk_return(path: str, assets: str, version: str) -> str:
    """Helpdesk only, and only while its setting skips the workspace stop."""
    from hero_integration.integrations.helpdesk.desk_return import opens_desk_directly

    if not (path == "/helpdesk" or path.startswith("/helpdesk/")) or not opens_desk_directly():
        return ""
    return '<script src="' + assets + 'js/helpdesk_desk_return.js?v=' + version + '"></script>'
