"""EJIL: Talk! retrieval core for the standalone EJILTalkSearcher skill.

Two public paths reach the same blog; neither needs the GDUFS library
session, because EJIL: Talk! is a public site:

1. The WordPress REST posts API (``/wp-json/wp/v2/posts``): native search,
   exact totals from the ``X-WP-Total`` / ``X-WP-TotalPages`` headers,
   author names via ``_embed=author`` and the rendered post body in
   ``content.rendered``.
2. The site's own HTML search page (``/?s=...``) rendered in headless
   Chrome: result entries only (title, date, authors, teaser) plus the page
   count; it never provides an exact hit count or the post body.

The site sits behind a WAF that answers individual requests with HTTP 403;
a single 403 must be reported as such for that attempt and never turned
into "no results".
"""

from __future__ import annotations

import hashlib
import html as html_module
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SKILL_NAME = 'EJILTalkSearcher'
SCHEMA_VERSION = 1

SITE_ROOT = 'https://www.ejiltalk.org'
REST_POSTS = SITE_ROOT + '/wp-json/wp/v2/posts'
BROWSER_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/155.0.0.0 Safari/537.36'
)

# The site renders ten result cards per HTML search page; the REST API takes
# any per_page value.  Offsets are converted to pages with this constant.
HTML_SEARCH_PAGE_SIZE = 10

SCRIPT_DIR = Path(__file__).resolve().parent
HEADLESS_SCRIPT = SCRIPT_DIR / 'headless_fetch.mjs'

TAG_RE = re.compile(r'<[^>]+>')
PAGE_COUNT_RE = re.compile(r'Page\s+(\d+)\s+of\s+(\d+)', re.I)
CHALLENGE_RE = re.compile(
    r'403|forbidden|just a moment|access denied|human verification', re.I
)


class RetrievalError(RuntimeError):
    """A single retrieval attempt failed; callers decide about fallbacks."""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def clean(value: Any) -> str:
    if value is None:
        return ''
    text = html_module.unescape(str(value))
    return re.sub(r'\s+', ' ', text).strip()


def html_to_text(value: Any) -> str:
    """Plain text with paragraph breaks kept, so quoted wording stays readable."""
    if not value:
        return ''
    text = re.sub(r'(?is)</(p|div|li|h[1-6]|blockquote|tr)>', '\n\n', str(value))
    text = re.sub(r'(?i)<br\s*/?>', '\n', text)
    text = TAG_RE.sub('', text)
    text = html_module.unescape(text)
    text = re.sub(r'[ \t\r\f\v]+', ' ', text)
    text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
    return text.strip()


def sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class Response:
    """Minimal HTTP response record shared by the live client and tests."""

    __slots__ = ('status', 'headers', 'body', 'url')

    def __init__(self, status: int, headers: dict[str, str], body: bytes, url: str):
        self.status = status
        self.headers = {str(k).lower(): str(v) for k, v in headers.items()}
        self.body = body
        self.url = url


class HttpClient:
    """One request per call, browser-like User-Agent, no retries."""

    def __init__(self, timeout: int = 60):
        self.timeout = timeout

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> Response:
        request = urllib.request.Request(
            url, headers={'User-Agent': BROWSER_UA, **(headers or {})}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as resp:
                return Response(resp.status, dict(resp.headers), resp.read(), url)
        except urllib.error.HTTPError as exc:
            raise RetrievalError(f'HTTP {exc.code}: {url}') from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RetrievalError(f'Connection failed: {url}: {exc}') from exc


def _rest_url(params: dict[str, Any]) -> str:
    # Spaces are encoded as %20 rather than '+' on purpose: the site's WAF
    # answered ``search=war+crime`` with HTTP 403 on 2026-10-16 while the
    # identical %20 request returned 200.  See references/ejiltalk-search.md.
    return REST_POSTS + '?' + urllib.parse.urlencode(
        params, quote_via=urllib.parse.quote
    )


def _soup(html: str):
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover - deployment problem
        raise RetrievalError(
            'beautifulsoup4 is required for HTML parsing; run: '
            'python -m pip install -r requirements.txt'
        ) from exc
    return BeautifulSoup(html, 'html.parser')


def _author_names(post: dict[str, Any]) -> list[str]:
    embedded = post.get('_embedded') or {}
    names = [
        clean(author.get('name'))
        for author in embedded.get('author') or []
        if isinstance(author, dict)
    ]
    return [name for name in names if name]


def _post_result(post: dict[str, Any], provider: str) -> dict[str, Any]:
    body_html = (post.get('content') or {}).get('rendered', '')
    text = html_to_text(body_html)
    authors = _author_names(post)
    result: dict[str, Any] = {
        'id': post.get('id'),
        'title': clean((post.get('title') or {}).get('rendered', '')),
        'url': post.get('link', ''),
        'published': post.get('date_gmt', ''),
        'modified': post.get('modified_gmt', ''),
        'authors': authors,
        'excerpt': clean((post.get('excerpt') or {}).get('rendered', '')),
        'text': text,
        'text_characters': len(text),
        'body_html': body_html,
        'result_type': 'retrieved_text' if text else 'retrieved_metadata',
        'content_scope': (
            'public rendered post body via the WordPress REST posts API; '
            'protected posts may omit content'
        ),
        'provider': provider,
    }
    coauthors = post.get('coauthors')
    if isinstance(coauthors, list) and len(coauthors) > len(authors):
        result['authors_note'] = (
            f"REST author list resolves {len(authors)} of {len(coauthors)} credited "
            'authors; Co-Authors Plus guest authors are not exposed as WP users, '
            'so the HTML page byline is authoritative for the full list.'
        )
    return result


def search_api(
    query: str,
    limit: int,
    offset: int,
    http: HttpClient | None = None,
    timeout: int = 60,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Native WordPress post search; the exact totals come from the headers."""
    client = http or HttpClient(timeout)
    url = _rest_url(
        {
            'search': query,
            'per_page': limit,
            'offset': offset,
            '_embed': 'author',
        }
    )
    response = client.get(url, timeout=timeout)
    try:
        posts = json.loads(response.body.decode('utf-8', 'replace'))
    except ValueError as exc:
        raise RetrievalError('WordPress schema changed: response is not JSON') from exc
    if not isinstance(posts, list):
        raise RetrievalError('WordPress schema changed: expected a post array')

    results = [_post_result(post, 'WordPress REST posts API') for post in posts]
    total = response.headers.get('x-wp-total')
    total_pages = response.headers.get('x-wp-totalpages')
    consumed = offset + len(results)
    warnings = sorted(
        {row['authors_note'] for row in results if row.get('authors_note')}
    )
    meta = {
        'status': 'ok',
        'mode': 'api',
        'total': int(total) if total and total.isdigit() else None,
        'total_pages': int(total_pages) if total_pages and total_pages.isdigit() else None,
        'page': offset // limit + 1 if limit else 1,
        'page_size': limit,
        'next_offset': consumed if len(results) == limit else None,
        'scope': (
            'native WordPress post search; total and total_pages are the exact '
            'X-WP-Total / X-WP-TotalPages values'
        ),
        'request_url': url,
        'evidence': {
            'request_url': url,
            'http_status': response.status,
            'content_type': response.headers.get('content-type', 'application/json'),
            'retrieved_at': now_utc(),
            'bytes': len(response.body),
            'sha256': sha256_bytes(response.body),
        },
        'warnings': warnings,
    }
    return results, meta


def parse_search_html(
    html: str, page_url: str, limit: int, offset: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse the result cards of the site's own HTML search page."""
    soup = _soup(html)
    text = soup.get_text(' ', strip=True)
    section = soup.select_one('section.search-page-articles')
    if section is None:
        if CHALLENGE_RE.search(text):
            raise RetrievalError('HTML search page returned an access challenge')
        raise RetrievalError('HTML search schema changed: result section missing')
    cards = section.select('div.post-tease-ejil')
    if not cards and 'Search results for' not in text:
        raise RetrievalError(
            'HTML search schema changed: no result cards or result sentence'
        )

    page_match = PAGE_COUNT_RE.search(text)
    page_number = int(page_match.group(1)) if page_match else None
    total_pages = int(page_match.group(2)) if page_match else None

    rows: list[dict[str, Any]] = []
    for card in cards:
        content = card.select_one('div.post-tease-content-ejil') or card
        heading = content.select_one('h3')
        anchor = content.select_one('a.article-link[href]')
        if not heading or not anchor:
            continue
        date_node = card.select_one('div.featured-date-and-author > p')
        snippet_node = card.select_one('div.post-tease-content-ejil > p')
        authors = [
            clean(node.get_text(' ', strip=True))
            for node in card.select('a.blog-info-author')
        ]
        rows.append(
            {
                'id': None,
                'title': clean(heading.get('title') or heading.get_text(' ', strip=True)),
                'url': urllib.parse.urljoin(page_url, anchor['href']),
                'published': '',
                'published_display': clean(date_node.get_text()) if date_node else '',
                'authors': [name for name in authors if name],
                'excerpt': '',
                'snippet': clean(snippet_node.get_text()) if snippet_node else '',
                'text': '',
                'text_characters': 0,
                'body_html': '',
                'result_type': 'retrieved_metadata',
                'content_scope': (
                    'search result entry: title, date, authors and teaser; '
                    'not the full post'
                ),
                'provider': 'EJIL: Talk! HTML search page (headless Chrome)',
            }
        )

    per_page = len(cards) or HTML_SEARCH_PAGE_SIZE
    within = offset % per_page
    selected = rows[within : within + limit]
    next_offset = None
    if selected:
        consumed = offset + len(selected)
        more_on_page = within + len(selected) < len(rows)
        more_pages = bool(total_pages and page_number and page_number < total_pages)
        next_offset = consumed if more_on_page or more_pages else None

    scope = 'EJIL: Talk! HTML search page rendered in headless Chrome'
    if total_pages:
        scope += (
            f'; the site reported {total_pages} pages of about {per_page} results, '
            'not an exact result count'
        )
    meta = {
        'status': 'ok',
        'mode': 'browser',
        'total': None,
        'total_pages': total_pages,
        'page': page_number,
        'page_size': per_page,
        'next_offset': next_offset,
        'scope': scope,
        'request_url': page_url,
        'evidence': {
            'request_url': page_url,
            'http_status': 200,
            'content_type': 'text/html',
            'retrieved_at': now_utc(),
            'browser': 'headless-chrome',
            'bytes': len(html.encode('utf-8')),
            'sha256': sha256_bytes(html.encode('utf-8')),
        },
        'warnings': [],
    }
    return selected, meta


def find_chrome(explicit: str | None = None) -> str:
    if explicit:
        return explicit if Path(explicit).is_file() else ''
    for name in ('CHROME_PATH', 'CHROME_BIN'):
        value = os.environ.get(name)
        if value and Path(value).is_file():
            return value
    candidates = [
        r'C:\Program Files\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
        r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    for name in (
        'chrome',
        'google-chrome',
        'google-chrome-stable',
        'chromium',
        'chromium-browser',
        'msedge',
        'microsoft-edge',
    ):
        found = shutil.which(name)
        if found:
            return found
    return ''


def headless_html(
    url: str,
    chrome_path: str | None = None,
    timeout: int = 150,
    wait_ms: int = 15000,
    result_selector: str = '',
    result_text: str = '',
) -> str:
    """Render a public page in headless Chrome through the bundled helper."""
    node = shutil.which('node')
    if not node:
        raise RetrievalError('Node.js is required for the headless-browser fallback')
    if not HEADLESS_SCRIPT.is_file():
        raise RetrievalError(f'Headless helper is missing: {HEADLESS_SCRIPT}')
    with tempfile.TemporaryDirectory(prefix='ejiltalk-headless-') as folder:
        rendered = Path(folder) / 'page.html'
        command = [
            node,
            str(HEADLESS_SCRIPT),
            '--url',
            url,
            '--out',
            str(rendered),
            '--wait-ms',
            str(wait_ms),
        ]
        if result_selector:
            command += ['--result-selector', result_selector]
        if result_text:
            command += ['--result-text', result_text]
        if chrome_path:
            command += ['--chrome', chrome_path]
        try:
            completed = subprocess.run(
                command,
                text=True,
                encoding='utf-8',
                errors='replace',
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RetrievalError(f'Headless browser timed out: {url}') from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or '').strip()
            raise RetrievalError(f'Headless browser failed: {detail[:800]}')
        if not rendered.is_file():
            raise RetrievalError('Headless browser returned no rendered page')
        return rendered.read_text(encoding='utf-8', errors='replace')


def search_html(
    query: str,
    limit: int,
    offset: int,
    chrome_path: str | None = None,
    timeout: int = 150,
    wait_ms: int = 15000,
    headless: Callable[..., str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    render = headless or headless_html
    page = offset // HTML_SEARCH_PAGE_SIZE + 1
    query_string = urllib.parse.urlencode({'s': query}, quote_via=urllib.parse.quote)
    page_url = (
        f'{SITE_ROOT}/?{query_string}'
        if page == 1
        else f'{SITE_ROOT}/page/{page}/?{query_string}'
    )
    html = render(
        page_url,
        chrome_path=chrome_path,
        timeout=timeout,
        wait_ms=wait_ms,
        result_selector='section.search-page-articles',
        result_text='Search results for',
    )
    return parse_search_html(html, page_url, limit, offset)


def _payload(command: str, query_fields: dict[str, Any], results, meta) -> dict[str, Any]:
    payload = {
        'skill': SKILL_NAME,
        'schema_version': SCHEMA_VERSION,
        'command': command,
        'retrieved_at': now_utc(),
    }
    payload.update(query_fields)
    payload.update(meta)
    payload['results'] = results
    return payload


def search(
    query: str,
    limit: int = 10,
    offset: int = 0,
    *,
    mode: str = 'auto',
    http: HttpClient | None = None,
    timeout: int = 60,
    chrome_path: str | None = None,
    wait_ms: int = 15000,
    headless: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Search posts; ``auto`` tries the REST API, then the browser fallback."""
    if mode not in ('auto', 'api', 'browser'):
        raise ValueError("mode must be one of 'auto', 'api', 'browser'")

    api_error: RetrievalError | None = None
    if mode in ('auto', 'api'):
        try:
            results, meta = search_api(query, limit, offset, http=http, timeout=timeout)
            payload = _payload('search', {'query': query}, results, meta)
            return payload
        except RetrievalError as exc:
            api_error = exc
            if mode == 'api':
                raise

    try:
        results, meta = search_html(
            query,
            limit,
            offset,
            chrome_path=chrome_path,
            timeout=max(timeout * 2, 150),
            wait_ms=wait_ms,
            headless=headless,
        )
    except RetrievalError as exc:
        raise RetrievalError(
            f'REST API failed ({api_error}); headless-browser fallback failed ({exc})'
        ) from exc

    if mode == 'browser':
        meta['warnings'] = [
            'Browser-only mode was requested explicitly; results come from the '
            'HTML search page and contain entries, not post bodies.'
        ]
    else:
        meta['warnings'] = [
            f'WordPress REST API unavailable ({api_error}); results came from the '
            'headless-browser HTML search page and contain entries, not post bodies.'
        ]
    payload = _payload('search', {'query': query}, results, meta)
    return payload


def slug_from_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.netloc.lower() not in ('www.ejiltalk.org', 'ejiltalk.org'):
        raise RetrievalError(f'Not an EJIL: Talk! URL: {url}')
    path = parsed.path.strip('/')
    if not path:
        raise RetrievalError(f'No post slug in URL: {url}')
    return path.split('/')[-1]


def parse_post_html(html: str, url: str) -> dict[str, Any]:
    """Extract title, date, authors and body text from a rendered post page."""
    soup = _soup(html)
    article = soup.select_one('article.article-ejil') or soup.select_one('article')
    if article is None:
        if CHALLENGE_RE.search(soup.get_text(' ', strip=True)[:2000]):
            raise RetrievalError('Post page returned an access challenge')
        raise RetrievalError('Post page schema changed: article element missing')
    title_node = article.select_one('h1.blog-info-title')
    if title_node is None:
        raise RetrievalError('Post page schema changed: title element missing')
    date_node = article.select_one('time.blog-info-date')
    body_node = article.select_one('div.blog-content')
    if body_node is None:
        raise RetrievalError('Post page schema changed: body element missing')
    body_html = ''.join(str(child) for child in body_node.contents)
    text = html_to_text(body_html)
    return {
        'title': clean(title_node.get_text(' ', strip=True)),
        'url': url,
        'published': clean(date_node.get('datetime') if date_node else ''),
        'published_display': clean(date_node.get_text()) if date_node else '',
        'authors': [
            clean(node.get_text(' ', strip=True))
            for node in article.select('address.author a')
        ],
        'text': text,
        'text_characters': len(text),
        'body_html': body_html,
    }


def fetch(
    url: str,
    *,
    mode: str = 'auto',
    http: HttpClient | None = None,
    timeout: int = 60,
    chrome_path: str | None = None,
    wait_ms: int = 15000,
    headless: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Fetch one post body: REST slug lookup, then direct HTTP, then headless."""
    if mode not in ('auto', 'api', 'browser'):
        raise ValueError("mode must be one of 'auto', 'api', 'browser'")
    slug = slug_from_url(url)
    client = http or HttpClient(timeout)
    errors: list[str] = []
    notes: list[str] = []
    result: dict[str, Any] | None = None
    method = ''
    evidence: dict[str, Any] = {}

    if mode in ('auto', 'api'):
        api_url = _rest_url({'slug': slug, '_embed': 'author'})
        try:
            response = client.get(api_url, timeout=timeout)
            posts = json.loads(response.body.decode('utf-8', 'replace'))
            if not isinstance(posts, list):
                raise RetrievalError('WordPress schema changed: expected a post array')
            if not posts:
                raise RetrievalError(f'REST API has no post with slug {slug!r}')
            row = _post_result(posts[0], 'WordPress REST posts API')
            if row.get('authors_note'):
                notes.append(row['authors_note'])
            result = {
                'title': row['title'],
                'url': row['url'],
                'published': row['published'],
                'published_display': '',
                'authors': row['authors'],
                'text': row['text'],
                'text_characters': row['text_characters'],
                'body_html': row['body_html'],
            }
            method = 'api'
            evidence = {
                'request_url': api_url,
                'http_status': response.status,
                'retrieved_at': now_utc(),
                'bytes': len(response.body),
                'sha256': sha256_bytes(response.body),
            }
        except RetrievalError as exc:
            errors.append(f'REST API slug lookup ({exc})')
            if mode == 'api':
                raise

    if result is None:
        render = headless or headless_html
        try:
            response = client.get(url, timeout=timeout)
            result = parse_post_html(response.body.decode('utf-8', 'replace'), url)
            method = 'page'
            evidence = {
                'request_url': url,
                'http_status': response.status,
                'retrieved_at': now_utc(),
                'bytes': len(response.body),
                'sha256': sha256_bytes(response.body),
            }
        except RetrievalError as exc:
            errors.append(f'direct HTTP page ({exc})')
            try:
                html = render(
                    url,
                    chrome_path=chrome_path,
                    timeout=max(timeout * 2, 150),
                    wait_ms=wait_ms,
                    result_selector='article.article-ejil',
                    result_text='Written by',
                )
                result = parse_post_html(html, url)
                method = 'headless'
                evidence = {
                    'request_url': url,
                    'http_status': 200,
                    'retrieved_at': now_utc(),
                    'browser': 'headless-chrome',
                    'bytes': len(html.encode('utf-8')),
                    'sha256': sha256_bytes(html.encode('utf-8')),
                }
            except RetrievalError as fallback_exc:
                errors.append(f'headless page render ({fallback_exc})')
                raise RetrievalError('; '.join(errors)) from fallback_exc

    payload = {
        'skill': SKILL_NAME,
        'schema_version': SCHEMA_VERSION,
        'command': 'fetch',
        'status': 'ok',
        'mode': method,
        'url': result['url'],
        'requested_url': url,
        'retrieved_at': now_utc(),
        'title': result['title'],
        'published': result['published'],
        'published_display': result['published_display'],
        'authors': result['authors'],
        'text': result['text'],
        'text_characters': result['text_characters'],
        'body_html_characters': len(result['body_html']),
        'body_html': result['body_html'],
        'evidence': evidence,
        'warnings': errors + notes,
    }
    return payload


def doctor(
    *,
    probe: bool = True,
    http: HttpClient | None = None,
    timeout: int = 30,
    chrome_path: str | None = None,
) -> dict[str, Any]:
    checks: dict[str, Any] = {}

    checks['python'] = {
        'ok': sys.version_info >= (3, 10),
        'version': platform.python_version(),
        'required': True,
    }
    try:
        checks['beautifulsoup4'] = {
            'ok': True,
            'version': importlib.metadata.version('beautifulsoup4'),
            'required': True,
        }
        import bs4  # noqa: F401 - import verifies the package actually loads
    except Exception as exc:  # noqa: BLE001 - report any import problem
        checks['beautifulsoup4'] = {'ok': False, 'error': str(exc), 'required': True}

    node = shutil.which('node')
    node_version = ''
    node_major = 0
    if node:
        try:
            completed = subprocess.run(
                [node, '--version'],
                text=True,
                encoding='utf-8',
                errors='replace',
                capture_output=True,
                timeout=20,
                check=False,
            )
            node_version = (completed.stdout or '').strip()
            match = re.match(r'v?(\d+)', node_version)
            node_major = int(match.group(1)) if match else 0
        except (OSError, subprocess.SubprocessError):
            node_version = ''
    checks['node'] = {
        'ok': bool(node) and node_major >= 22,
        'path': node or '',
        'version': node_version,
        'required': False,
        'note': 'Node.js 22+ is required only for the headless-browser fallback',
    }

    chrome = find_chrome(chrome_path)
    checks['chrome'] = {
        'ok': bool(chrome),
        'path': chrome,
        'required': False,
        'note': 'Chrome/Chromium/Edge is required only for the browser fallback',
    }

    root_status = 'skipped'
    rest_status = 'skipped'
    if probe:
        client = http or HttpClient(timeout)
        try:
            root = client.get(SITE_ROOT, timeout=timeout)
            root_status = f'HTTP {root.status}'
        except RetrievalError as exc:
            root_status = str(exc)
        try:
            rest = client.get(_rest_url({'per_page': 1}), timeout=timeout)
            rest_status = f'HTTP {rest.status}'
        except RetrievalError as exc:
            rest_status = str(exc)
    checks['site_root'] = {'ok': root_status.startswith('HTTP 2'), 'status': root_status}
    checks['rest_api'] = {'ok': rest_status.startswith('HTTP 2'), 'status': rest_status}

    retrieval_path = checks['rest_api']['ok'] or (
        checks['node']['ok'] and checks['chrome']['ok'] and checks['site_root']['ok']
    )
    ok = bool(
        checks['python']['ok'] and checks['beautifulsoup4']['ok'] and retrieval_path
    )

    next_steps: list[str] = []
    if not checks['python']['ok']:
        next_steps.append('Install Python 3.10 or newer.')
    if not checks['beautifulsoup4']['ok']:
        next_steps.append('Run: python -m pip install -r requirements.txt')
    if not checks['node']['ok']:
        next_steps.append('Install Node.js 22+ to enable the headless-browser fallback.')
    if not checks['chrome']['ok']:
        next_steps.append('Install Chrome/Chromium/Edge or set CHROME_PATH.')
    if probe and not checks['rest_api']['ok']:
        next_steps.append(
            'The REST API probe did not return HTTP 2xx; search will fall back to '
            'the headless browser when a single request is blocked (403 is '
            'per-request, not a permanent state).'
        )

    return {
        'skill': SKILL_NAME,
        'schema_version': SCHEMA_VERSION,
        'command': 'doctor',
        'retrieved_at': now_utc(),
        'status': 'ok' if ok else 'degraded',
        'ok': ok,
        'checks': checks,
        'next_steps': next_steps,
    }
