"""Opt-in loopback UI. No arbitrary file serving, origins or repository writes."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
from urllib.parse import urlsplit
from threading import BoundedSemaphore
from .source import SourceRoot


class Server(ThreadingHTTPServer):
    def server_close(self):
        try:
            super().server_close()
        finally:
            if hasattr(self, 'artifacts'):
                self.artifacts.__exit__()
            if hasattr(self, 'engine'):
                self.engine.close()
            if hasattr(self, 'queries'):
                self.queries.close()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_):
        pass  # Query text is never logged.

    def respond(self, status, value, mime='application/json'):
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers(); self.wfile.write(body)

    def trusted(self):
        host = f'127.0.0.1:{self.server.server_port}'
        return self.headers.get('Host') == host and self.headers.get('Origin', 'http://' + host) == 'http://' + host

    def do_GET(self):
        if not self.trusted(): self.respond(403, {'error':'Untrusted origin'}); return
        name = urlsplit(self.path).path
        if name == '/api/status':
            try:
                from .search import index_status
                result = index_status(self.server.engine.output, owner=self.server.engine.owner,
                    backend_available=self.server.engine.embedder is not None)
            except (OSError, RuntimeError, sqlite3.Error):
                self.respond(409, {'error': 'Index owner unavailable; reopen the original output'}); return
            result['semantic'] = result['semantic_index']['query_available']
            result['rerankers'] = ['none'] + (['local'] if self.server.local_reranker else []) + (['jev'] if self.server.allow_jev else [])
            self.respond(200, result); return
        if name not in {'/', '/architecture.html', '/graph.html', '/graph.json', '/architecture.mmd', '/architecture.md'}:
            self.respond(404, {'error':'Not found'}); return
        file = self.server.engine.output / ('architecture.html' if name == '/' else name[1:])
        mime = 'text/html; charset=utf-8' if file.suffix == '.html' else 'application/json' if file.suffix == '.json' else 'text/plain; charset=utf-8'
        try:
            with self.server.artifacts.open(file.name) as stream:
                body = stream.read()
        except OSError:
            self.respond(404, {'error': 'Artifact unavailable'}); return
        self.respond(200, body, mime)

    def do_POST(self):
        if not self.trusted(): self.respond(403, {'error':'Untrusted origin'}); return
        if self.path not in ('/api/search', '/api/query'): self.respond(404, {'error':'Not found'}); return
        if self.headers.get('Content-Type') != 'application/json':
            self.respond(415, {'error':'Use application/json'}); return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192: raise ValueError('Request must be 1–8192 bytes')
            payload = json.loads(self.rfile.read(length))
            if self.path == '/api/query':
                if self.headers.get('X-Repo-Graph-Output', self.server.engine.owner) != self.server.engine.owner:
                    self.respond(409, {'error': 'Query server belongs to another output directory'}); return
                from .analysis_queries import encoded
                result = self.server.queries.run(payload)
                self.respond(200, encoded(result)); return
            query = payload['query']; mode = payload.get('mode', 'hybrid')
            if not isinstance(query, str) or not isinstance(payload.get('prefix', ''), str):
                raise ValueError('Query and prefix must be text')
            method = payload.get('rerank', 'none')
            reranker = None
            if method == 'local':
                reranker = self.server.local_reranker
                if not reranker: raise ValueError('Restart with --local-reranker to enable local reranking')
            elif method == 'jev':
                if not self.server.allow_jev: raise ValueError('Restart with --allow-jev to permit source export')
                from .rerank import JevReranker
                reranker = JevReranker(self.server.engine.output)
            elif method != 'none': raise ValueError('Unknown reranker')
            expensive = mode != 'keyword' or reranker is not None
            acquired = expensive and self.server.search_slot.acquire(blocking=False)
            if expensive and not acquired:
                self.respond(429, {'error':'A model search is running. Try again shortly or use keywords without reranking.'}); return
            try:
                result = self.server.engine.run(query, mode=mode, limit=10, prefix=payload.get('prefix', ''), reranker=reranker)
            finally:
                if acquired: self.server.search_slot.release()
            self.respond(200, result)
        except OSError:
            self.respond(409, {'error': 'Index owner unavailable; reopen the original output'})
        except (ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error) as error:
            self.respond(400, {'error':str(error)})


def create_server(engine, port=0, *, local_reranker=None, allow_jev=False):
    server = Server(('127.0.0.1', port), Handler)
    try:
        server.artifacts = SourceRoot(engine.output)
        if server.artifacts.identity != engine.owner:
            raise RuntimeError('Index output owner changed; reopen the original output directory')
    except (OSError, RuntimeError):
        server.server_close()
        raise
    server.engine = engine
    from .analysis_queries import Queries
    server.queries = Queries(engine.output, owner=engine.owner)
    server.local_reranker, server.allow_jev = local_reranker, allow_jev
    # ponytail: one expensive search at a time; status, artifacts and plain keywords remain responsive.
    server.search_slot = BoundedSemaphore(1)
    server.timeout = 1
    return server


def serve(engine, port=0, **options):
    with create_server(engine, port, **options) as server:
        print(f'http://127.0.0.1:{server.server_port}/architecture.html', flush=True)
        try: server.serve_forever()
        except KeyboardInterrupt: pass
