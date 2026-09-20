import unittest
from html.parser import HTMLParser

from dashboard import HTML


class PageStructure(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.nested_views = []
        self.element_views = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        parent_view = next((view for _, view in reversed(self.stack) if view), None)
        view = attrs.get('data-view')
        if view and parent_view:
            self.nested_views.append((view, parent_view))
        if attrs.get('id'):
            self.element_views[attrs['id']] = view or parent_view
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append((tag, view))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


class DashboardLayoutTests(unittest.TestCase):
    def setUp(self):
        self.layout = PageStructure()
        self.layout.feed(HTML)

    def test_main_pages_are_independent_so_switching_tabs_does_not_hide_children(self):
        self.assertEqual(self.layout.nested_views, [])

    def test_positions_settings_and_reviews_stay_in_their_intended_pages(self):
        for element, view in {
            'sports-open-bets': 'sports', 'sports-history': 'sports',
            'crypto-open-bets': 'crypto', 'crypto-history': 'crypto',
            'crypto-patient-activate': 'crypto',
            'SPORTS_EXECUTION_MODE': 'control',
            'CRYPTO_EXECUTION_MODE': 'crypto-control',
            'sports-audit-results': 'analytics', 'itf-shadow-strategies': 'analytics',
            'logs-sports-candidates': 'logs', 'logs-crypto-candidates': 'logs',
        }.items():
            with self.subTest(element=element):
                self.assertEqual(self.layout.element_views[element], view)
