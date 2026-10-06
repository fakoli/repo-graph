"""Opt-in loopback UI. No arbitrary file serving, origins or repository writes."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from contextlib import closing
from .search import connect
from urllib.parse import urlsplit


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
            with closing(connect(self.server.engine.output, readonly=True)) as db:
                total, ready = db.execute('SELECT count(*),sum(vector IS NOT NULL) FROM docs').fetchone()
            self.respond(200, {'semantic':self.server.engine.embedder is not None and total > 0 and total == ready}); return
        if name not in {'/', '/architecture.html', '/graph.html', '/graph.json', '/architecture.mmd', '/architecture.md'}:
            self.respond(404, {'error':'Not found'}); return
        file = self.server.engine.output / ('architecture.html' if name == '/' else name[1:])
        mime = 'text/html; charset=utf-8' if file.suffix == '.html' else 'application/json' if file.suffix == '.json' else 'text/plain; charset=utf-8'
        self.respond(200, file.read_bytes(), mime)

    def do_POST(self):
        if not self.trusted(): self.respond(403, {'error':'Untrusted origin'}); return
        if self.path != '/api/search': self.respond(404, {'error':'Not found'}); return
        if self.headers.get('Content-Type') != 'application/json':
            self.respond(415, {'error':'Use application/json'}); return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192: raise ValueError('Request must be 1–8192 bytes')
            payload = json.loads(self.rfile.read(length))
            query = payload['query']; mode = payload.get('mode', 'hybrid')
            if not isinstance(query, str) or not isinstance(payload.get('prefix', ''), str):
                raise ValueError('Query and prefix must be text')
            result = self.server.engine.run(query, mode=mode, limit=10, prefix=payload.get('prefix', ''))
            self.respond(200, result)
        except (ValueError, KeyError, TypeError, RuntimeError) as error:
            self.respond(400, {'error':str(error)})


def create_server(engine, port=0):
    server = HTTPServer(('127.0.0.1', port), Handler)
    server.engine = engine
    server.timeout = 1
    return server


def serve(engine, port=0):
    with create_server(engine, port) as server:
        print(f'http://127.0.0.1:{server.server_port}/architecture.html', flush=True)
        try: server.serve_forever()
        except KeyboardInterrupt: pass
