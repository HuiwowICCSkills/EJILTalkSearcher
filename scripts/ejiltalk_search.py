#!/usr/bin/env python3
"""Command-line entry point for the standalone EJILTalkSearcher skill.

Commands:
  search   search EJIL: Talk! posts (WordPress REST API, headless HTML page
           as the fallback when the site's WAF blocks the API)
  fetch    fetch one post body (REST slug lookup, direct HTTP page, headless
           page render, in that order)
  doctor   check deployment prerequisites and reachability

Every command prints a JSON report on stdout.  EJIL: Talk! is a public blog:
no institutional session or cookie is used, and nothing is written outside
the requested --out file.

Boundaries: the REST API returns the exact hit count and public rendered post
bodies; the HTML fallback returns search-entry metadata and teasers only.
A single HTTP 403 is a per-request WAF answer, never evidence that no post
exists.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ejiltalk_adapter as adapter  # noqa: E402 - path set up above


def _emit(payload: dict[str, Any], out: str | None, summary: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + '\n', encoding='utf-8')
        summary = {'output': str(path.resolve()), **summary}
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='ejiltalk_search.py',
        description='Standalone EJIL: Talk! search and post fetch (public site).',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    search = sub.add_parser('search', help='Search EJIL: Talk! posts')
    search.add_argument('query', help='Keyword or phrase to search for')
    search.add_argument('--limit', type=int, default=10, help='1..50 (default 10)')
    search.add_argument('--offset', type=int, default=0, help='>= 0 (default 0)')
    search.add_argument(
        '--mode',
        choices=('auto', 'api', 'browser'),
        default='auto',
        help='auto = REST API then headless fallback (default)',
    )
    search.add_argument('--timeout', type=int, default=60, help='1..1800 seconds')
    search.add_argument('--wait-ms', type=int, default=15000, help='0..120000')
    search.add_argument('--chrome-path', help='Chrome/Chromium/Edge executable')
    search.add_argument('--out', help='Write the JSON report to this file')

    fetch = sub.add_parser('fetch', help='Fetch one post body by URL')
    fetch.add_argument('url', help='EJIL: Talk! post URL returned by search')
    fetch.add_argument(
        '--mode',
        choices=('auto', 'api', 'browser'),
        default='auto',
        help='auto = REST slug, then direct HTTP page, then headless render',
    )
    fetch.add_argument('--timeout', type=int, default=60, help='1..1800 seconds')
    fetch.add_argument('--wait-ms', type=int, default=15000, help='0..120000')
    fetch.add_argument('--chrome-path', help='Chrome/Chromium/Edge executable')
    fetch.add_argument('--out', help='Write the JSON report to this file')
    fetch.add_argument('--text-out', help='Also write the plain post text here')

    doctor = sub.add_parser('doctor', help='Check deployment prerequisites')
    doctor.add_argument('--timeout', type=int, default=30, help='1..300 seconds')
    doctor.add_argument('--chrome-path', help='Chrome/Chromium/Edge executable')
    doctor.add_argument(
        '--no-probe',
        action='store_true',
        help='Skip the site reachability probes',
    )
    doctor.add_argument('--out', help='Write the JSON report to this file')

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == 'doctor':
        if not 1 <= args.timeout <= 300:
            parser.error('Bounds: timeout 1..300 seconds')
        payload = adapter.doctor(
            probe=not args.no_probe,
            timeout=args.timeout,
            chrome_path=args.chrome_path,
        )
        _emit(payload, args.out, {'ok': payload['ok'], 'status': payload['status']})
        return 0 if payload['ok'] else 1

    out = getattr(args, 'out', None)
    try:
        if args.command == 'search':
            if not 1 <= args.limit <= 50 or args.offset < 0:
                parser.error('Bounds: limit 1..50, offset >= 0')
            if not 1 <= args.timeout <= 1800 or not 0 <= args.wait_ms <= 120000:
                parser.error('Bounds: timeout 1..1800 seconds, wait-ms 0..120000')
            payload = adapter.search(
                args.query,
                args.limit,
                args.offset,
                mode=args.mode,
                timeout=args.timeout,
                chrome_path=args.chrome_path,
                wait_ms=args.wait_ms,
            )
            _emit(
                payload,
                out,
                {
                    'status': payload.get('status'),
                    'mode': payload.get('mode'),
                    'total': payload.get('total'),
                    'total_pages': payload.get('total_pages'),
                    'results': len(payload.get('results') or []),
                    'next_offset': payload.get('next_offset'),
                    'warnings': payload.get('warnings', []),
                },
            )
        else:
            if not 1 <= args.timeout <= 1800 or not 0 <= args.wait_ms <= 120000:
                parser.error('Bounds: timeout 1..1800 seconds, wait-ms 0..120000')
            payload = adapter.fetch(
                args.url,
                mode=args.mode,
                timeout=args.timeout,
                chrome_path=args.chrome_path,
                wait_ms=args.wait_ms,
            )
            if args.text_out:
                text_path = Path(args.text_out)
                text_path.parent.mkdir(parents=True, exist_ok=True)
                text_path.write_text(payload['text'], encoding='utf-8')
            _emit(
                payload,
                out,
                {
                    'status': payload.get('status'),
                    'mode': payload.get('mode'),
                    'title': payload.get('title'),
                    'text_characters': payload.get('text_characters'),
                    'text_out': str(Path(args.text_out).resolve())
                    if args.text_out
                    else None,
                    'warnings': payload.get('warnings', []),
                },
            )
    except adapter.RetrievalError as exc:
        payload = {
            'skill': adapter.SKILL_NAME,
            'schema_version': adapter.SCHEMA_VERSION,
            'command': args.command,
            'status': 'error',
            'retrieved_at': adapter.now_utc(),
            'error': str(exc),
        }
        _emit(payload, out, {'status': 'error', 'error': str(exc)})
        return 1
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
