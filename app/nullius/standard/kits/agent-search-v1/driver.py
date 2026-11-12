"""Search a generated HTTP collection with the agent's own tools.

No MCP server or search implementation is injected. The fixture supplies a
searchable website, random facts, source documents and trusted request logs.
Passing proves bounded web search/retrieval, not a public search provider or
access to arbitrary sites. Reference answers alone cannot pass: the HTTP log
must show a relevant query followed by the supporting document retrieval.
"""
import atexit
import html
import json
import os
import secrets
import socketserver
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit, quote
from urllib.request import urlopen

from nullius.standard.container import CHECK_IMAGE
from nullius.standard.jsonl import serve

UNITS = ('find-source', 'disambiguate', 'follow-reference')
FIXTURES = {}


class UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class Fixture:
    def __init__(self, unit, directory):
        self.unit = unit
        self.target = 'Archive-' + secrets.token_hex(4)
        self.answer = secrets.token_hex(12)
        self.page = '/documents/' + secrets.token_hex(12)
        self.index = '/documents/' + secrets.token_hex(12)
        self.evidence = f'The current catalogue number of {self.target} is {self.answer}.'
        self.calls = []
        self.lock = threading.Lock()
        self.log = os.path.join(os.path.dirname(directory), 'verifier', 'search-requests.jsonl')
        os.makedirs(os.path.dirname(self.log), exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='ns-', dir='/tmp')
        self.socket = os.path.join(self.temp.name, 'web.sock')
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                status, body, event = fixture.response(self.path)
                if event:
                    with fixture.lock:
                        fixture.calls.append(event)
                        with open(fixture.log, 'a') as f:
                            f.write(json.dumps(event) + '\n')
                data = ('<!doctype html><html><body>' + body + '</body></html>').encode()
                self.send_response(status)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.tcp = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.unix = UnixServer(self.socket, Handler)
        self.local = f'http://127.0.0.1:{self.tcp.server_port}'
        self.base = 'http://web-search:9001' if os.environ.get(CHECK_IMAGE) else self.local
        for server in (self.tcp, self.unix):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.closed = False

    def response(self, path):
        parsed = urlsplit(path)
        if parsed.path == '/':
            return 200, '<h1>Research archive</h1><form action="/search"><input name="q"><button>Search</button></form>', None
        if parsed.path == '/search':
            q = parse_qs(parsed.query).get('q', [''])[0]
            if self.target.casefold() not in q.casefold():
                return 200, '<p>No matching documents.</p>', {'kind': 'search', 'relevant': False}
            link = self.index if self.unit == 'follow-reference' else self.page
            rows = [f'<li><a href="{link}">{self.target}: current official record</a></li>']
            if self.unit == 'disambiguate':
                rows.insert(0, '<li><a href="/obsolete">Archived record (superseded)</a></li>')
                rows.append('<li><a href="/unrelated">A similarly named archive</a></li>')
            return 200, '<ul>' + ''.join(rows) + '</ul>', {'kind': 'search', 'relevant': True}
        if parsed.path == self.index and self.unit == 'follow-reference':
            return 200, f'<p>The current record is maintained in the <a href="{self.page}">source register</a>.</p>', {'kind': 'index'}
        if parsed.path == self.page:
            return 200, '<h1>Current source register</h1><p>' + html.escape(self.evidence) + '</p>', {'kind': 'source'}
        if parsed.path in ('/obsolete', '/unrelated'):
            return 200, '<p>Obsolete or unrelated catalogue number: NOT-CURRENT. Do not use as the current record.</p>', {'kind': 'decoy'}
        return 404, '<p>Not found</p>', None

    def close(self):
        if not self.closed:
            self.closed = True
            for server in (self.tcp, self.unix):
                server.shutdown()
                server.server_close()
            self.temp.cleanup()


def fixture(unit, directory):
    return FIXTURES[(unit, os.path.abspath(directory))]


def units(dataset):
    return {'units': list(UNITS)}


def setup(unit, dir):
    if unit not in UNITS:
        raise ValueError('unknown search check')
    key = (unit, os.path.abspath(dir))
    if key in FIXTURES:
        FIXTURES[key].close()
    f = FIXTURES[key] = Fixture(unit, os.path.abspath(dir))
    return {'prompt': f'Search the research archive at {f.base}/ for the current catalogue number of {f.target}. '
            'Find and open the supporting source. Return only a JSON object with answer (the catalogue number), '
            'source (the full supporting document URL), and evidence (an exact sentence from that document). '
            'Use your available web tools; the website has a search form.',
            'web_services': [{'name': 'web-search', 'port': 9001, 'socket': f.socket}]}


def verdict(f, answer):
    try:
        result = json.loads(answer or '')
    except (ValueError, TypeError):
        result = {}
    if not isinstance(result, dict):
        result = {}
    wanted = ['search', 'index', 'source'] if f.unit == 'follow-reference' else ['search', 'source']
    position = 0
    for event in f.calls:
        if position < len(wanted) and event['kind'] == wanted[position] and event.get('relevant', True):
            position += 1
    checks = {'answer': result.get('answer') == f.answer,
              'source': result.get('source') == f.base + f.page,
              'evidence': result.get('evidence') == f.evidence,
              'retrieval': position == len(wanted)}
    return {'passed': all(checks.values()), 'reward': float(all(checks.values())), 'detail': checks}


def score(unit, dir, transcript, answer):
    f = fixture(unit, dir)
    result = verdict(f, answer)
    f.close()
    return result


def oracle(unit, dir):
    # The leak checker invokes the oracle on a copy of the workspace.
    f = FIXTURES.get((unit, os.path.abspath(dir)))
    if f is None:
        f = next(value for (name, _), value in reversed(list(FIXTURES.items())) if name == unit)
    for path in ['/search?q=' + quote(f.target)] + ([f.index] if unit == 'follow-reference' else []) + [f.page]:
        with urlopen(f.local + path, timeout=5) as response:
            response.read()
    return {'answer': json.dumps({'answer': f.answer, 'source': f.base + f.page, 'evidence': f.evidence})}


@atexit.register
def close():
    for f in FIXTURES.values():
        f.close()


if __name__ == '__main__':
    serve({'units': units, 'setup': setup, 'score': score, 'oracle': oracle})
