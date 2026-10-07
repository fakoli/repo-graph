"""Small, bounded agent interface."""
import argparse
from contextlib import closing
from http.client import HTTPConnection, HTTPException
import json
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit

from . import builder
from .search import Embeddings, Search, connect, embed_index, MODEL


def _remote_query(address, payload, owner):
    """The CLI can continue the loopback server's captured query session."""
    url = urlsplit(address)
    if (url.scheme != 'http' or url.hostname != '127.0.0.1' or
            url.username or url.password or url.path not in ('', '/') or url.query or url.fragment or
            url.port is None or not 1 <= url.port <= 65535):
        raise ValueError('Use the serving address http://127.0.0.1:PORT')
    from .analysis_queries import encoded
    body = encoded(payload)
    if len(body) > 8192:
        raise ValueError('Query request exceeds 8192 bytes')
    with closing(HTTPConnection('127.0.0.1', url.port, timeout=5)) as connection:
        connection.request('POST', '/api/query', body, {'Content-Type': 'application/json',
                                                     'X-Repo-Graph-Output': owner})
        response = connection.getresponse()
        raw = response.read(1048577)
        if len(raw) > 1048576:
            raise ValueError('Query response exceeds the product ceiling')
        result = json.loads(raw)
        if response.status != 200:
            raise ValueError(result.get('error', 'Structural query failed'))
        return result


def _query_command(parsed, output):
    from .analysis_queries import Queries, encoded
    payload = dict(operation=parsed.operation, seed=parsed.seed, depth=parsed.depth,
                   prefix=parsed.prefix, scope=parsed.scope, role=parsed.role)
    if parsed.limits is not None:
        payload['limits'] = json.loads(parsed.limits)
    if parsed.cursor is not None:
        if not parsed.server:
            raise ValueError('Cross-command cursors need --server; --stdio retains local sessions')
        payload['cursor'] = parsed.cursor
    def emit(result):
        print(encoded(result).decode(), flush=True)
    with Queries(output) as queries:
        if not parsed.stdio:
            result = _remote_query(parsed.server, payload, queries.owner) if parsed.server else queries.run(payload)
            if not parsed.server:
                result['cursor'] = None  # The one-page process closes its snapshot.
            emit(result)
            return 0
        # ponytail: JSON lines keep a finite session alive; use the server for cross-process pagination.
        failed = False
        while True:
            line = sys.stdin.buffer.readline(8194)
            if not line:
                break
            if len(line) > 8193 or not line.endswith(b'\n') and len(line) > 8192:
                raise ValueError('Query request exceeds 8192 bytes')
            try:
                request = json.loads(line)
                emit(_remote_query(parsed.server, request, queries.owner) if parsed.server else queries.run(request))
            except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error) as error:
                emit({'error': str(error)})
                failed = True
        return int(failed)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == 'map':
        try:
            return builder.main(args[1:])
        except (OSError, RuntimeError, ValueError) as error:
            print(f'repo-graph: {error}', file=sys.stderr); return 1
    parser = argparse.ArgumentParser(description='Local diagrams and incremental repository search')
    subs = parser.add_subparsers(dest='command', required=True)
    subs.add_parser('map', help='Map a local repository or public HTTPS URL (map --help for flags)')
    analyze = subs.add_parser('analyze', help='Incrementally capture structural facts (requires the analysis extra)')
    analyze.add_argument('repository', type=Path)
    analyze.add_argument('--output', required=True, type=Path)
    analyze.add_argument('--mode', choices=['serial', 'queued'], default='serial')
    analyze.add_argument('--workers', type=int, default=1)
    query = subs.add_parser('query', help='Bounded structural queries; --stdio or --server retains pagination')
    query.add_argument('output', type=Path)
    query.add_argument('--operation', choices=['symbol', 'reference', 'call', 'callees', 'callers', 'reachable', 'impact'], default='symbol')
    query.add_argument('--seed'); query.add_argument('--depth', type=int, default=2)
    query.add_argument('--prefix', default=''); query.add_argument('--scope', default='')
    query.add_argument('--role', choices=['call', 'reference', 'all'], default='call')
    query.add_argument('--limits', help='JSON object reducing or overriding finite query limits')
    query.add_argument('--stdio', action='store_true', help='Read JSON requests and write bounded JSON responses, one per line')
    query.add_argument('--server', help='Reuse a loopback server session at http://127.0.0.1:PORT')
    query.add_argument('--cursor', help='Continue a --server query with the same filters')
    status = subs.add_parser('status', help='Read captured coverage, freshness and lifecycle without rescanning source')
    status.add_argument('output', type=Path)
    status.add_argument('--expect-source', help='Compare the captured structural source SHA256 to an expected identity')
    init = subs.add_parser('init', help='Install this product through native harness managers')
    init.add_argument('--harness', choices=['all', 'pi', 'codex', 'claude'], default='all')
    init.add_argument('--scope', choices=['user', 'project'], default='user')
    init.add_argument('--source', type=Path, help='Use a local product directory instead of the pinned public release')
    init.add_argument('--ref', help='Public product Git tag, branch, or commit (default: installed product version)')
    init.add_argument('--dry-run', action='store_true', help='Print the native install plan after checking prerequisites')
    index = subs.add_parser('index', help='Embed the generated search corpus locally')
    index.add_argument('output', type=Path)
    index.add_argument('--semantic', action='store_true')
    index.add_argument('--reranker', action='store_true', help='Cache the optional local CPU reranker')
    index.add_argument('--offline', action='store_true', help='Use only cached embedding model files')
    for name in ('search', 'serve'):
        p = subs.add_parser(name, help='Query the index' if name == 'search' else 'Serve the diagram and search on loopback')
        p.add_argument('output', type=Path)
        if name == 'search':
            p.add_argument('query'); p.add_argument('--limit', type=int, default=10)
            p.add_argument('--prefix', default=''); p.add_argument('--mode', choices=['keyword','semantic','hybrid'], default='hybrid')
            p.add_argument('--rerank', choices=['none','local','jev'], default='none', help='Optional shortlist judgment; jev exports query and bounded source excerpts')
        else:
            p.add_argument('--port', type=int, default=0, help='Loopback port; 0 selects an available port')
            p.add_argument('--local-reranker', action='store_true', help='Load the cached CPU reranker for the search tab')
            p.add_argument('--allow-jev', action='store_true', help='Allow explicit Jev searches to export query and bounded source excerpts')
        p.add_argument('--offline', action='store_true', help='Use only cached embedding model files')
    parsed = parser.parse_args(args)
    try:
        if parsed.command == 'init':
            from .installer import initialize
            result = initialize(harness=parsed.harness, scope=parsed.scope, source=parsed.source,
                                ref=parsed.ref, dry_run=parsed.dry_run)
            print(json.dumps(result, ensure_ascii=False)); return 0
        output = parsed.output.expanduser().resolve()
        if parsed.command == 'status':
            from .search import index_status
            result = index_status(output, expected_source=parsed.expect_source)
            print(json.dumps(result, ensure_ascii=False)); return 0 if result['status'] == 'ok' else 1
        if parsed.command == 'analyze':
            from .analysis import StructuralIndex
            root = parsed.repository.expanduser().resolve()
            if output == root or root in output.parents:
                raise ValueError('Choose an output directory outside the analyzed source root')
            coverage = {}
            paths = builder.repo_files(root, coverage=coverage)
            if coverage.get('failed'):
                raise ValueError('Inventory contains unreadable or unsafe paths; no structural generation published')
            result = StructuralIndex(root, output).refresh(paths, mode=parsed.mode, concurrency=parsed.workers)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result['status'] == 'ready' else 1
        if not (output / 'search.db').is_file():
            raise ValueError('No search index here. Run repo-graph map REPO first and use its output directory.')
        if parsed.command == 'query':
            return _query_command(parsed, output)
        embedder = None
        if parsed.command == 'index':
            if not (parsed.semantic or parsed.reranker):
                raise ValueError('Choose --semantic, --reranker, or both')
            result = {}
            if parsed.semantic:
                embedder = Embeddings(offline=parsed.offline)
                result = embed_index(output, embedder)
            if parsed.reranker:
                from .rerank import LocalReranker
                reranker = LocalReranker(offline=parsed.offline)
                result['reranker'] = dict(model=reranker.name, status='ready')
        else:
            with closing(connect(output, readonly=True)) as db:
                model = db.execute("SELECT value FROM meta WHERE key='model'").fetchone()
            if model and (parsed.command == 'serve' or parsed.mode != 'keyword'):
                embedder = Embeddings(model[0], offline=True)
            engine = Search(output, embedder)
            if parsed.command == 'serve':
                from .server import serve
                from .rerank import LocalReranker
                local = LocalReranker() if parsed.local_reranker else None
                serve(engine, parsed.port, local_reranker=local, allow_jev=parsed.allow_jev); return 0
            from .rerank import LocalReranker, JevReranker
            reranker = LocalReranker() if parsed.rerank == 'local' else JevReranker(output) if parsed.rerank == 'jev' else None
            result = engine.run(parsed.query, mode=parsed.mode, limit=parsed.limit, prefix=parsed.prefix, reranker=reranker)
        print(json.dumps(result, ensure_ascii=False)); return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error, HTTPException) as error:
        print(f'repo-graph: {error}', file=sys.stderr); return 1
