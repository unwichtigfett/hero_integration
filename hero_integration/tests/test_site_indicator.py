"""Full-page decoration must not change APIs, downloads or expose configuration."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from werkzeug.wrappers import Response

from hero_integration import __version__
from hero_integration.services import site_indicator


class TestSiteIndicator(TestCase):
    def setUp(self):
        self.frappe = SimpleNamespace(conf={'host_name': 'https://test.example.invalid'},
                                      local=SimpleNamespace(site='frontend'))
        p = patch.object(site_indicator, 'frappe', self.frappe)
        p.start(); self.addCleanup(p.stop)

    def test_public_site_label_and_running_version_on_desk_portal_and_standalone_pages(self):
        for body in ('<main id="body">Desk</main>', '<main>Login</main>', '<div id="app">CRM</div>'):
            response = Response('<html><body>' + body + '</body></html>', mimetype='text/html')
            site_indicator.after_request(response)
            html = response.get_data(as_text=True)
            self.assertIn(f'v{__version__}_test</div></body>', html)
            self.assertIn(body, html)
            self.assertEqual(response.content_length, len(response.get_data()))
            site_indicator.after_request(response)
            self.assertEqual(response.get_data(as_text=True).count(site_indicator.MARKER), 1)

    def test_site_folder_fallback_and_html_escaping(self):
        self.frappe.conf = {}
        self.frappe.local.site = 'dev.example.invalid'
        self.assertEqual(site_indicator.label(), f'v{__version__}_dev')
        self.frappe.local.site = '<unsafe>.example.invalid'
        response = Response('<HTML><BODY>Page</BODY></HTML>', mimetype='text/html')
        site_indicator.after_request(response)
        self.assertIn('&lt;unsafe&gt;', response.get_data(as_text=True))
        self.assertNotIn('<unsafe>', response.get_data(as_text=True))

    def test_api_fragments_downloads_and_head_responses_are_unchanged(self):
        responses = [Response('{"ok":true}', mimetype='application/json'),
                     Response('<div>Fragment</div>', mimetype='text/html'),
                     Response('<html><body>Download</body></html>', mimetype='text/html',
                              headers={'Content-Disposition': 'attachment; filename="export.html"'}),
                     Response('<html><body>HEAD</body></html>', mimetype='text/html')]
        for index, response in enumerate(responses):
            before = response.get_data()
            site_indicator.after_request(response, SimpleNamespace(method='HEAD' if index == 3 else 'GET'))
            self.assertEqual(response.get_data(), before)
