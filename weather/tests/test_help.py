"""The in-app user guide (weather.help) and changelog."""
import re

from django.test import TestCase
from django.urls import reverse

from WS4Free import __version__
from weather import help as help_docs

from .helpers import make_station


class GuideTests(TestCase):
    def test_every_page_renders_and_every_link_resolves(self):
        anchors, links = {}, []
        for slug, _ in help_docs.PAGES:
            url = reverse('weather:help') if slug == 'index' else reverse('weather:help-page', args=[slug])
            with self.subTest(page=slug):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                body = response.content.decode()
                self.assertNotIn('.md"', body)                    # every guide link was rewritten
                anchors[url] = set(re.findall(r'id="([^"]+)"', body))
                links += [(slug, href) for href in re.findall(r'href="(/help/[^"]*)"', body)]
        for page, href in links:
            path, _, anchor = href.partition('#')
            with self.subTest(page=page, link=href):
                self.assertIn(path, anchors, f'{page} links to a missing page')
                if anchor:
                    self.assertIn(anchor, anchors[path], f'{page} links to a missing section')

    def test_no_orphan_guide_files(self):
        files = {p.stem for p in help_docs.GUIDE_DIR.glob('*.md')}
        self.assertEqual(files, {slug for slug, _ in help_docs.PAGES} - {'changelog'})

    def test_readme_and_changelog_links_map_into_the_app(self):
        html = self.client.get(reverse('weather:help')).content.decode()
        self.assertIn(f'href="{reverse("weather:help-page", args=["changelog"])}"', html)
        self.assertIn(f'href="{reverse("weather:help")}"', html)

    def test_unknown_page_and_index_alias(self):
        self.assertEqual(self.client.get('/help/nope/').status_code, 404)
        self.assertRedirects(self.client.get('/help/index/'), '/help/', status_code=301)

    def test_changelog_has_the_running_version_or_unreleased(self):
        text = help_docs.CHANGELOG.read_text()
        release = __version__.split('.dev')[0]
        self.assertTrue('## Unreleased' in text if '.dev' in __version__ else f'## {release} ' in text)

    def test_version_in_footer_and_help_on_station_tabs(self):
        station = make_station(is_public=True)
        response = self.client.get(reverse('weather:station-charts', args=[station.slug]))
        self.assertContains(response, f'v{__version__.replace(".dev0", "-dev")}')
        self.assertContains(response, f'href="{reverse("weather:help-page", args=["charts"])}"')
