"""Offline tests for the standalone EJILTalkSearcher skill.

Run from the skill directory (or its scripts/ folder):
    python -m unittest discover -p 'test_*.py' -v

No test performs a network request; every HTTP and headless call is faked.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ejiltalk_adapter as adapter  # noqa: E402
import ejiltalk_search as cli  # noqa: E402


SEARCH_HTML = (
    '<html><body><section class="search-page-articles">'
    '<div class="search-result-sentence"><p>Search results for '
    '<span>war crime</span>.</p></div><p>Page 1 of 142</p>'
    '<div class="post-tease-ejil"><div class="post-tease-image-ejil">'
    '<a href="/image-only-link/" class="article-link"><img src="/x.jpg"></a>'
    '</div><div class="post-tease-content-ejil">'
    '<a class="article-link" href="/first-post/"><h3 title="First Post">'
    'First Post</h3></a>'
    '<div class="featured-date-and-author"><p>October 12, 2026</p>'
    '<a class="blog-info-author"><p>Author One</p></a></div>'
    '<p>Teaser text.</p></div></div>'
    '</section></body></html>'
)

POST_HTML = (
    '<html><head><title>Post &ndash; EJIL: Talk!</title></head><body>'
    '<article class="article-ejil post-type-post" id="post-1">'
    '<div class="blog-info"><time class="blog-info-date pf-date" '
    'datetime="October 15, 2026">October 15, 2026</time>'
    '<h1 class="blog-info-title">The US Sanctions Against the ICC</h1>'
    '<address class="author">Written by '
    '<a href="/author/mweller/" class="author url fn">Marc Weller</a>'
    '</address></div><div class="blog-content">'
    '<p>First paragraph.</p><p>Second paragraph.</p>'
    '</div></article></body></html>'
)


def response(
    body: str | bytes,
    status: int = 200,
    headers: dict[str, str] | None = None,
    url: str = '',
) -> adapter.Response:
    if isinstance(body, str):
        body = body.encode('utf-8')
    return adapter.Response(status, headers or {}, body, url)


def rest_payload() -> str:
    return json.dumps(
        [
            {
                'id': 27019,
                'link': 'https://www.ejiltalk.org/the-us-sanctions-against-the-icc/',
                'date_gmt': '2026-10-15T09:00:00',
                'modified_gmt': '2026-10-15T10:00:00',
                'title': {'rendered': 'The US Sanctions Against the ICC'},
                'excerpt': {'rendered': '<p>Short summary.</p>'},
                'content': {
                    'rendered': '<p>First paragraph.</p><p>Second paragraph.</p>'
                },
                '_embedded': {'author': [{'name': 'Marc Weller'}]},
            }
        ]
    )


def rest_payload_of(count: int) -> str:
    posts = json.loads(rest_payload()) * count
    for index, post in enumerate(posts):
        post['id'] = 27019 + index
    return json.dumps(posts)


class FakeHttp:
    """Route by substring; an Exception value means the call failed."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        for marker, value in self.routes.items():
            if marker in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise adapter.RetrievalError(f'unexpected URL: {url}')


class HtmlToTextTests(unittest.TestCase):
    def test_html_to_text_keeps_paragraphs(self):
        text = adapter.html_to_text('<p>First paragraph.</p><p>Second paragraph.</p>')
        self.assertEqual(text, 'First paragraph.\n\nSecond paragraph.')

    def test_html_to_text_unescapes_entities(self):
        self.assertEqual(adapter.html_to_text('<p>State &amp; immunity</p>'), 'State & immunity')


class SearchApiTests(unittest.TestCase):
    def test_reads_totals_authors_body_and_encodes_spaces_with_percent20(self):
        http = FakeHttp(
            {
                '/wp-json/wp/v2/posts?': response(
                    rest_payload(),
                    headers={'X-WP-Total': '1419', 'X-WP-TotalPages': '142'},
                )
            }
        )
        results, meta = adapter.search_api('war crime', 10, 0, http=http)
        self.assertIn('search=war%20crime', http.calls[0])
        self.assertNotIn('search=war+crime', http.calls[0])
        self.assertEqual(meta['total'], 1419)
        self.assertEqual(meta['total_pages'], 142)
        self.assertEqual(meta['mode'], 'api')
        self.assertEqual(results[0]['title'], 'The US Sanctions Against the ICC')
        self.assertEqual(results[0]['authors'], ['Marc Weller'])
        self.assertIn('First paragraph.', results[0]['text'])
        self.assertEqual(results[0]['result_type'], 'retrieved_text')

    def test_next_offset_only_when_a_full_page_was_returned(self):
        short = json.dumps([])
        http = FakeHttp({'/wp-json/wp/v2/posts?': response(short)})
        results, meta = adapter.search_api('nothing', 10, 0, http=http)
        self.assertEqual(results, [])
        self.assertIsNone(meta['next_offset'])
        self.assertIsNone(meta['total'])

    def test_next_offset_stops_at_a_final_page_that_lands_on_the_total(self):
        # total == offset + page size: the last page must not advertise an
        # offset past the end of the result set.
        http = FakeHttp(
            {
                '/wp-json/wp/v2/posts?': response(
                    rest_payload_of(10),
                    headers={'X-WP-Total': '20', 'X-WP-TotalPages': '2'},
                )
            }
        )
        results, meta = adapter.search_api('ai', 10, 10, http=http)
        self.assertEqual(len(results), 10)
        self.assertEqual(meta['total'], 20)
        self.assertEqual(meta['total_pages'], 2)
        self.assertEqual(meta['page'], 2)
        self.assertIsNone(meta['next_offset'])

    def test_next_offset_advances_while_results_remain(self):
        http = FakeHttp(
            {
                '/wp-json/wp/v2/posts?': response(
                    rest_payload_of(10),
                    headers={'X-WP-Total': '26', 'X-WP-TotalPages': '3'},
                )
            }
        )
        results, meta = adapter.search_api('ai', 10, 10, http=http)
        self.assertEqual(len(results), 10)
        self.assertEqual(meta['total'], 26)
        self.assertEqual(meta['next_offset'], 20)

    def test_next_offset_present_when_the_total_header_is_missing(self):
        http = FakeHttp({'/wp-json/wp/v2/posts?': response(rest_payload_of(10))})
        results, meta = adapter.search_api('ai', 10, 0, http=http)
        self.assertEqual(len(results), 10)
        self.assertIsNone(meta['total'])
        self.assertEqual(meta['next_offset'], 10)

    def test_schema_change_raises(self):
        http = FakeHttp({'/wp-json/wp/v2/posts?': response('{"code":"rest_no_route"}')})
        with self.assertRaisesRegex(adapter.RetrievalError, 'schema changed'):
            adapter.search_api('x', 10, 0, http=http)

    def test_http_403_stays_an_error_not_an_empty_result(self):
        http = FakeHttp(
            {
                '/wp-json/wp/v2/posts?': adapter.RetrievalError(
                    'HTTP 403: https://www.ejiltalk.org/wp-json/wp/v2/posts'
                )
            }
        )
        with self.assertRaisesRegex(adapter.RetrievalError, 'HTTP 403'):
            adapter.search_api('war crime', 10, 0, http=http)

    def test_unresolved_coauthors_are_reported(self):
        payload = json.dumps(
            [
                {
                    'id': 1,
                    'link': 'https://www.ejiltalk.org/post/',
                    'title': {'rendered': 'Co-authored post'},
                    'content': {'rendered': '<p>Body.</p>'},
                    'coauthors': [1660, 3655],
                    '_embedded': {'author': [{'name': 'Abhijeet Shrivastava'}]},
                }
            ]
        )
        http = FakeHttp({'/wp-json/wp/v2/posts?': response(payload)})
        results, meta = adapter.search_api('unclos', 1, 0, http=http)
        self.assertIn('2 credited', results[0]['authors_note'])
        self.assertEqual(len(meta['warnings']), 1)


class SearchHtmlTests(unittest.TestCase):
    def test_parse_cards_uses_content_anchor_not_image_anchor(self):
        rows, info = adapter.parse_search_html(
            SEARCH_HTML, 'https://www.ejiltalk.org/?s=war%20crime', 10, 0
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], 'First Post')
        self.assertEqual(rows[0]['url'], 'https://www.ejiltalk.org/first-post/')
        self.assertEqual(rows[0]['authors'], ['Author One'])
        self.assertEqual(rows[0]['snippet'], 'Teaser text.')
        self.assertEqual(rows[0]['published_display'], 'October 12, 2026')
        self.assertEqual(rows[0]['result_type'], 'retrieved_metadata')
        self.assertEqual(rows[0]['text'], '')
        self.assertEqual(info['total_pages'], 142)
        self.assertEqual(info['page'], 1)
        self.assertIsNone(info['total'])
        self.assertEqual(info['next_offset'], 1)

    def test_challenge_page_is_reported(self):
        with self.assertRaisesRegex(adapter.RetrievalError, 'access challenge'):
            adapter.parse_search_html(
                '<html><body><h1>403 Forbidden</h1></body></html>',
                'https://www.ejiltalk.org/?s=x',
                10,
                0,
            )

    def test_schema_change_is_reported(self):
        with self.assertRaisesRegex(adapter.RetrievalError, 'schema changed'):
            adapter.parse_search_html(
                '<html><body><p>Nothing familiar here.</p></body></html>',
                'https://www.ejiltalk.org/?s=x',
                10,
                0,
            )


class SearchFlowTests(unittest.TestCase):
    def test_auto_falls_back_to_browser_when_rest_is_blocked(self):
        http = FakeHttp(
            {
                '/wp-json/wp/v2/posts?': adapter.RetrievalError(
                    'HTTP 403: https://www.ejiltalk.org/wp-json/wp/v2/posts'
                )
            }
        )
        calls = []

        def fake_headless(url, **kwargs):
            calls.append((url, kwargs))
            return SEARCH_HTML

        payload = adapter.search('war crime', 10, 0, http=http, headless=fake_headless)
        self.assertEqual(payload['status'], 'ok')
        self.assertEqual(payload['mode'], 'browser')
        self.assertEqual(payload['results'][0]['title'], 'First Post')
        self.assertTrue(payload['warnings'][0].startswith('WordPress REST API unavailable'))
        self.assertIn('section.search-page-articles', calls[0][1]['result_selector'])

    def test_api_mode_keeps_the_403_visible(self):
        http = FakeHttp(
            {
                '/wp-json/wp/v2/posts?': adapter.RetrievalError(
                    'HTTP 403: https://www.ejiltalk.org/wp-json/wp/v2/posts'
                )
            }
        )
        with self.assertRaisesRegex(adapter.RetrievalError, 'HTTP 403'):
            adapter.search('war crime', 10, 0, mode='api', http=http)

    def test_browser_page_url_advances_with_offset(self):
        urls = []

        def fake_headless(url, **kwargs):
            urls.append(url)
            return SEARCH_HTML

        payload = adapter.search(
            'war crime', 10, 25, mode='browser', headless=fake_headless
        )
        self.assertEqual(urls[0], 'https://www.ejiltalk.org/page/3/?s=war%20crime')
        self.assertTrue(payload['warnings'][0].startswith('Browser-only mode'))

    def test_both_paths_failing_reports_both_errors(self):
        http = FakeHttp({'/wp-json/wp/v2/posts?': adapter.RetrievalError('HTTP 403')})

        def dead_headless(url, **kwargs):
            raise adapter.RetrievalError('Node.js is required for the headless-browser fallback')

        with self.assertRaisesRegex(
            adapter.RetrievalError, 'REST API failed.*fallback failed'
        ):
            adapter.search('war crime', 10, 0, http=http, headless=dead_headless)

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            adapter.search('x', 10, 0, mode='rss')


class FetchTests(unittest.TestCase):
    POST_URL = 'https://www.ejiltalk.org/the-us-sanctions-against-the-icc/'

    def test_fetch_uses_rest_slug_lookup(self):
        http = FakeHttp({'slug=': response(rest_payload())})
        payload = adapter.fetch(self.POST_URL, http=http)
        self.assertEqual(payload['status'], 'ok')
        self.assertEqual(payload['mode'], 'api')
        self.assertEqual(payload['title'], 'The US Sanctions Against the ICC')
        self.assertEqual(payload['authors'], ['Marc Weller'])
        self.assertIn('Second paragraph.', payload['text'])
        self.assertIn('slug=the-us-sanctions-against-the-icc', http.calls[0])

    def test_fetch_falls_back_to_direct_page(self):
        http = FakeHttp(
            {
                'slug=': adapter.RetrievalError('HTTP 403'),
                'ejiltalk.org/the-us-sanctions-against-the-icc/': response(POST_HTML),
            }
        )
        payload = adapter.fetch(self.POST_URL, http=http)
        self.assertEqual(payload['mode'], 'page')
        self.assertEqual(payload['title'], 'The US Sanctions Against the ICC')
        self.assertEqual(payload['authors'], ['Marc Weller'])
        self.assertEqual(payload['published'], 'October 15, 2026')
        self.assertIn('Second paragraph.', payload['text'])
        self.assertTrue(payload['warnings'][0].startswith('REST API slug lookup'))

    def test_fetch_falls_back_to_headless_render(self):
        http = FakeHttp(
            {
                'slug=': adapter.RetrievalError('HTTP 403'),
                'ejiltalk.org/': adapter.RetrievalError('HTTP 403'),
            }
        )

        def fake_headless(url, **kwargs):
            return POST_HTML

        payload = adapter.fetch(self.POST_URL, http=http, headless=fake_headless)
        self.assertEqual(payload['mode'], 'headless')
        self.assertIn('First paragraph.', payload['text'])

    def test_fetch_rejects_foreign_hosts(self):
        with self.assertRaisesRegex(adapter.RetrievalError, 'Not an EJIL: Talk! URL'):
            adapter.fetch('https://example.org/post/')

    def test_post_schema_change_is_reported(self):
        with self.assertRaisesRegex(adapter.RetrievalError, 'schema changed'):
            adapter.parse_post_html('<html><body>plain</body></html>', self.POST_URL)


class CliTests(unittest.TestCase):
    def test_search_writes_report_and_prints_summary(self):
        payload = {
            'skill': adapter.SKILL_NAME,
            'command': 'search',
            'status': 'ok',
            'mode': 'api',
            'total': 1419,
            'total_pages': 142,
            'next_offset': 10,
            'warnings': [],
            'results': [{'title': 'Post'}],
        }
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / 'search.json'
            captured = io.StringIO()
            with mock.patch.object(cli.adapter, 'search', return_value=payload):
                with contextlib.redirect_stdout(captured):
                    code = cli.main(
                        ['search', 'war crime', '--limit', '10', '--out', str(out)]
                    )
            self.assertEqual(code, 0)
            self.assertTrue(out.is_file())
            written = json.loads(out.read_text(encoding='utf-8'))
            self.assertEqual(written['total'], 1419)
            summary = json.loads(captured.getvalue().strip())
            self.assertEqual(summary['results'], 1)
            self.assertEqual(summary['mode'], 'api')

    def test_error_report_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / 'error.json'
            captured = io.StringIO()
            with mock.patch.object(
                cli.adapter,
                'fetch',
                side_effect=adapter.RetrievalError('HTTP 403: https://www.ejiltalk.org/x/'),
            ):
                with contextlib.redirect_stdout(captured):
                    code = cli.main(
                        ['fetch', 'https://www.ejiltalk.org/x/', '--out', str(out)]
                    )
            self.assertEqual(code, 1)
            written = json.loads(out.read_text(encoding='utf-8'))
            self.assertEqual(written['status'], 'error')
            self.assertIn('HTTP 403', written['error'])

    def test_plain_output_is_json(self):
        payload = {
            'skill': adapter.SKILL_NAME,
            'command': 'search',
            'status': 'ok',
            'mode': 'api',
            'total': None,
            'total_pages': None,
            'next_offset': None,
            'warnings': [],
            'results': [],
        }
        captured = io.StringIO()
        with mock.patch.object(cli.adapter, 'search', return_value=payload):
            with contextlib.redirect_stdout(captured):
                code = cli.main(['search', 'x'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(captured.getvalue())['command'], 'search')


if __name__ == '__main__':
    unittest.main()
