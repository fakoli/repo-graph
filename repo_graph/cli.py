"""Small, bounded agent interface."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

from . import builder
from .search import Embeddings, Search, connect, embed_index, MODEL


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
        if not (output / 'search.db').is_file():
            raise ValueError('No search index here. Run repo-graph map REPO first and use its output directory.')
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
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f'repo-graph: {error}', file=sys.stderr); return 1
