"""Small, bounded agent interface."""
import argparse
from contextlib import closing
from http.client import HTTPConnection, HTTPException
import json
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import urlsplit

from . import builder
from .search import Embeddings, Search, connect, embed_index, MODEL


def _captured_commit(value):
    from .analysis_queries import _impact_options
    try: _impact_options({'kind': 'git_change', 'base_revision': value}, None, None)
    except ValueError as error: raise argparse.ArgumentTypeError(str(error)) from error
    return value


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
    if parsed.family is not None: payload['families'] = parsed.family
    if parsed.relation_kind is not None: payload['kinds'] = parsed.relation_kind
    if parsed.source_area is not None or parsed.git_base is not None or parsed.relation is not None or parsed.certainty is not None:
        if parsed.operation != 'impact': raise ValueError('Impact selectors and filters require --operation impact')
        if parsed.stdio: raise ValueError('Use selector/filter JSON for --stdio impact requests')
        if parsed.source_area is not None:
            payload['selector'] = {'kind': 'source_area', 'paths': parsed.source_area}
        elif parsed.git_base is not None:
            payload['selector'] = {'kind': 'git_change', 'base_revision': parsed.git_base}
        if parsed.relation is not None: payload['relations'] = parsed.relation
        if parsed.certainty is not None: payload['certainties'] = parsed.certainty
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
    analyze.add_argument('--framework-context', type=Path, help='Explicit trusted Django dependency/source-root enrollment JSON; no automatic project enrollment')
    analyze.add_argument('--git-base', type=_captured_commit, help='Capture changed paths against this exact Git commit during analysis')
    query = subs.add_parser('query', help='Bounded structural queries; --stdio or --server retains pagination')
    query.add_argument('output', type=Path)
    query.add_argument('--operation', choices=['symbol', 'reference', 'call', 'framework', 'callees', 'callers', 'reachable', 'impact'], default='symbol')
    query.add_argument('--family', action='append', choices=['framework', 'framework_boundary'])
    query.add_argument('--relation-kind', action='append', choices=['django_route', 'django_management_handle', 'django_orm_get_queryset', 'unknown_framework_candidate'])
    query.add_argument('--seed'); query.add_argument('--depth', type=int, default=2)
    query.add_argument('--prefix', default=''); query.add_argument('--scope', default='')
    query.add_argument('--role', choices=['call', 'reference', 'all'], default='call')
    selectors = query.add_mutually_exclusive_group()
    selectors.add_argument('--source-area', action='append', help='Impact of captured path/directory; repeat for multiple areas')
    selectors.add_argument('--git-base', type=_captured_commit, help='Impact from producer-captured changes against this exact commit')
    query.add_argument('--relation', action='append', choices=['call', 'import'], help='Impact relation filter; repeat to include both')
    query.add_argument('--certainty', action='append', choices=['resolved', 'candidate', 'unresolved'], help='Impact certainty filter; repeat for multiple levels')
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
    index.add_argument('--kind', choices=['files', 'functions'], default='files')
    index.add_argument('--semantic', action='store_true')
    index.add_argument('--reranker', action='store_true', help='Cache the optional local CPU reranker')
    index.add_argument('--offline', action='store_true', help='Use only cached embedding model files')
    for name in ('search', 'serve'):
        p = subs.add_parser(name, help='Query the index' if name == 'search' else 'Serve the diagram and search on loopback')
        p.add_argument('output', type=Path)
        if name == 'search':
            p.add_argument('query'); p.add_argument('--limit', type=int, default=10)
            p.add_argument('--kind', choices=['files', 'functions'], default='files')
            p.add_argument('--limits', help='JSON object reducing function-evidence query budgets')
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
            framework_context = None
            if parsed.framework_context is not None:
                from .source import SourceRoot
                path = parsed.framework_context.expanduser().absolute()
                with SourceRoot(path.parent) as owner:
                    raw, _, info = owner.read(path.name, 16385, hash_full=False)
                if info.st_size != len(raw) or len(raw) > 16384: raise ValueError('Framework enrollment exceeds its byte bound')
                from .search import _json_record
                framework_context = _json_record(raw)
            result = StructuralIndex(root, output, framework_context=framework_context).refresh(paths, mode=parsed.mode, concurrency=parsed.workers,
                git_base=parsed.git_base)
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
                result = embed_index(output, embedder, kind=parsed.kind)
            if parsed.reranker:
                from .rerank import LocalReranker
                reranker = LocalReranker(offline=parsed.offline)
                result['reranker'] = dict(model=reranker.name, status='ready')
        else:
            kind = getattr(parsed, 'kind', 'files')
            if parsed.command == 'search' and parsed.limits is not None and kind != 'functions':
                raise ValueError('--limits applies to --kind functions')
            function_limits = None
            if kind == 'functions':
                from .search import EvidenceLimits
                values = json.loads(parsed.limits) if parsed.limits is not None else {}
                if type(values) is not dict:
                    raise ValueError('Function limits must be a JSON object')
                try:
                    function_limits = EvidenceLimits(**values)
                except TypeError:
                    raise ValueError('Unknown function limit') from None
            if parsed.command == 'serve' or parsed.mode != 'keyword':
                storage_started = time.monotonic()
                def check_model_storage():
                    if function_limits is not None and time.monotonic() - storage_started >= function_limits.timeout_seconds:
                        raise TimeoutError('Function search model metadata deadline exceeded')
                with closing(connect(output, readonly=True, check=check_model_storage if function_limits is not None else None)) as db:
                    model = db.execute('SELECT value FROM meta WHERE key=?',
                                       ('function_model' if kind == 'functions' else 'model',)).fetchone()
                    if parsed.command == 'serve' and model is None:
                        model = db.execute("SELECT value FROM meta WHERE key='function_model'").fetchone()
                    check_model_storage()
                if function_limits is not None:
                    from dataclasses import replace
                    function_limits = replace(function_limits, timeout_seconds=function_limits.timeout_seconds -
                                              (time.monotonic() - storage_started))
                if model:
                    try:
                        embedder = Embeddings(model[0], offline=True)
                    except (OSError, RuntimeError, ValueError):
                        if parsed.command != 'serve':
                            raise
                        # A viewer with an unavailable optional backend still serves keyword evidence.
            engine = Search(output, embedder)
            if parsed.command == 'serve':
                from .server import serve
                from .rerank import LocalReranker
                local = LocalReranker() if parsed.local_reranker else None
                serve(engine, parsed.port, local_reranker=local, allow_jev=parsed.allow_jev); return 0
            from .rerank import LocalReranker, JevReranker
            reranker = LocalReranker() if parsed.rerank == 'local' else JevReranker(output) if parsed.rerank == 'jev' else None
            options = dict(mode=parsed.mode, limit=parsed.limit, prefix=parsed.prefix, reranker=reranker)
            if kind == 'functions':
                options.update(kind=kind, limits=function_limits)
            result = engine.run(parsed.query, **options)
            if kind == 'functions':
                from .analysis_queries import encoded
                print(encoded(result).decode()); return 0
        print(json.dumps(result, ensure_ascii=False)); return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error, HTTPException) as error:
        print(f'repo-graph: {error}', file=sys.stderr); return 1
