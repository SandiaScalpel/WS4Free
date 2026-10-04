"""The user guide, rendered inside the app from the same Markdown files that
GitHub shows: docs/guide/*.md and CHANGELOG.md.

Links between the files are written as relative .md links (so they work on
GitHub); here they become /help/ URLs. Pages are rendered once and cached until
the file changes.
"""
import re
from functools import lru_cache
from pathlib import Path

import markdown
from django.conf import settings
from django.urls import reverse

GUIDE_DIR = Path(settings.BASE_DIR) / 'docs' / 'guide'
CHANGELOG = Path(settings.BASE_DIR) / 'CHANGELOG.md'

# Order of the guide's navigation. (slug, short title for the sidebar)
PAGES = [
    ('index', 'Welcome'),
    ('getting-started', 'Getting started'),
    ('connecting', 'Connecting a station'),
    ('dashboard', 'Dashboard'),
    ('charts', 'Charts'),
    ('almanac', 'Almanac'),
    ('growing', 'Growing'),
    ('reports', 'Reports'),
    ('extra-sensors', 'Extra sensors'),
    ('data-quality', 'Data quality and calibration'),
    ('settings', 'Settings and sharing'),
    ('how-it-works', 'How the numbers work'),
    ('changelog', 'What’s new'),
]
TITLES = dict(PAGES)

# Which guide page explains each station tab (for the "?" links on those pages).
TAB_PAGES = {
    'overview': 'dashboard', 'forecast': 'dashboard', 'charts': 'charts', 'almanac': 'almanac', 'growing': 'growing',
    'reports': 'reports', 'settings': 'settings', 'console': 'connecting', 'quality': 'data-quality', 'log': 'settings',
}


def path_for(slug):
    if slug == 'changelog':
        return CHANGELOG
    if slug in TITLES:
        return GUIDE_DIR / f'{slug}.md'
    return None


_MD_LINK = re.compile(r'href="(?!https?:|mailto:|#)([^"#]*?)([^/"#]+)\.md(#[^"]*)?"')


def _link(match):
    name, anchor = match.group(2), match.group(3) or ''
    if name.upper() == 'CHANGELOG':
        slug = 'changelog'
    elif name == 'index' or name.upper() == 'README':
        slug = 'index'
    else:
        slug = name
    url = reverse('weather:help') if slug == 'index' else reverse('weather:help-page', args=[slug])
    return f'href="{url}{anchor}"'


@lru_cache(maxsize=64)
def _render(path, mtime):
    md = markdown.Markdown(extensions=['tables', 'fenced_code', 'sane_lists', 'attr_list', 'toc'],
                           extension_configs={'toc': {'toc_depth': '2-2'}})
    html = md.convert(path.read_text(encoding='utf-8'))
    html = _MD_LINK.sub(_link, html)
    # Wide tables scroll inside their own box on phones instead of widening the page.
    html = html.replace('<table>', '<div class="guide-table"><table>').replace('</table>', '</table></div>')
    title = next((t['name'] for t in md.toc_tokens if t['level'] == 1), None)
    sections = [{'id': t['id'], 'name': t['name']} for top in md.toc_tokens
                for t in ([top] if top['level'] == 2 else top.get('children', [])) if t['level'] == 2]
    return title, html, sections


def page(slug):
    """(title, html, sections) for a guide page, or None if there is no such page."""
    path = path_for(slug)
    if path is None or not path.exists():
        return None
    title, html, sections = _render(path, path.stat().st_mtime)
    return title or TITLES[slug], html, sections
