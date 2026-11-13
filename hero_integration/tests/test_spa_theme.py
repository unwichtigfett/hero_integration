"""Navy reaches the standalone Helpdesk and CRM pages only, and never the Desk or portal."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from werkzeug.wrappers import Response

from hero_integration.services import branding, site_indicator, spa_theme


class TestSpaTheme(TestCase):
    def test_only_helpdesk_and_crm_paths_get_the_theme(self):
        for path in ("/helpdesk", "/helpdesk/tickets/1", "/crm", "/crm/deals"):
            html = spa_theme.markup(SimpleNamespace(path=path))
            for asset in ("navy_theme.css", "navy_spa.css", "navy_spa.js"):
                self.assertIn(asset, html)
        for request in (None, SimpleNamespace(path="/desk"), SimpleNamespace(path="/crmx"),
                        SimpleNamespace(path="/login"), SimpleNamespace(path="/helpdesk-docs")):
            self.assertEqual(spa_theme.markup(request), "")

    def test_full_pages_receive_it_once_next_to_the_site_indicator(self):
        fake = SimpleNamespace(conf={}, local=SimpleNamespace(site="dev.example.invalid"))
        with patch.object(site_indicator, "frappe", fake):
            response = Response('<html><body><div id="app"></div></body></html>', mimetype="text/html")
            request = SimpleNamespace(method="GET", path="/helpdesk")
            site_indicator.after_request(response, request)
            site_indicator.after_request(response, request)
            self.assertEqual(response.get_data(as_text=True).count(spa_theme.MARKER), 1)
            desk = Response("<html><body>Desk</body></html>", mimetype="text/html")
            site_indicator.after_request(desk, SimpleNamespace(method="GET", path="/desk"))
            self.assertNotIn(spa_theme.MARKER, desk.get_data(as_text=True))


class TestBranding(TestCase):
    def _boot(self, logos):
        boot = {"app_data": [{"app_name": "erpnext", "app_logo_url": "/assets/erpnext/logo.svg"}]}
        fake = SimpleNamespace(db=SimpleNamespace(get_single_value=lambda doctype, field: logos.get(doctype)))
        with patch.object(branding, "frappe", fake):
            branding.extend_bootinfo(boot)
        return boot

    def test_configured_site_logo_is_announced_and_app_marks_are_kept(self):
        boot = self._boot({"Navbar Settings": "/files/logo.svg", "Website Settings": "/files/web.svg"})
        self.assertEqual(boot["hero_site_logo"], "/files/logo.svg")
        self.assertEqual(boot["app_data"][0]["app_logo_url"], "/assets/erpnext/logo.svg")
        self.assertEqual(self._boot({"Website Settings": "/files/web.svg"})["hero_site_logo"], "/files/web.svg")

    def test_without_a_site_logo_nothing_changes(self):
        self.assertNotIn("hero_site_logo", self._boot({}))
