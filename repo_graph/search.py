"""Incremental SQLite keyword index and optional CPU semantic search."""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import heapq
import json
import os
from pathlib import Path
import re
import sqlite3
import shutil
import tempfile
from threading import Lock
import time
import uuid
from .source import SourceRoot

MODEL = "BAAI/bge-small-en-v1.5"
STOPWORDS = set("a an the and or of to in on for with by from is are was were be been which that this these those how where what when why will can as it its us our".split())
READ_LIMIT = 64 * 1024
TEXT_EXTENSIONS = {".md", ".markdown", ".mdx", ".rst", ".txt", ".go", ".py", ".js", ".jsx", ".ts", ".tsx",
                   ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".sh", ".tf", ".sql", ".vue", ".svelte"}
SECRET = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})")
SNAPSHOT_LOCK = Lock()


def _token(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _artifact_token(boundary):
    for name in ('search.db-journal', 'search.db-wal', 'search.db-shm'):
        try: boundary.info(name)
        except FileNotFoundError: continue
        raise RuntimeError('Legacy index sidecars are present; stop its writer and remap into a new output directory')
    try: return _token(boundary.info('search.db'))
    except FileNotFoundError: return None


@contextmanager
def _index_lock(boundary):
    import fcntl
    try:
        with boundary.open('.index.lock', create=True): pass
    except FileExistsError: pass
    with boundary.open('.index.lock') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield


def _fresh(boundary, output, expected):
    with SourceRoot(output) as current:
        if current.identity != boundary.identity:
            raise RuntimeError('Index output owner changed; reopen the original output directory')
    if _artifact_token(boundary) != expected:
        raise RuntimeError('Index snapshot is stale; remap or retry the index command')


class IndexConnection(sqlite3.Connection):
    """SQLite touches private stages; only complete artifacts enter the output directory."""
    boundary = None
    failed = False
    closed = False

    def __exit__(self, kind, value, traceback):
        try:
            result = super().__exit__(kind, value, traceback)
            if self.boundary is not None:
                if kind is not None: self.failed = True
                else: _fresh(self.boundary, self.output, self.base_token)
            return result
        except BaseException:
            self.failed = True
            raise

    def publish(self):
        if self.failed: return
        if not self.closed:
            raise RuntimeError('Only closed private index stages may be published')
        try:
            if _token((Path(self.temporary.name) / 'search.db').stat()) == self.initial_private_token:
                return
            with _index_lock(self.boundary):
                _fresh(self.boundary, self.output, self.base_token)
                with SourceRoot(Path(self.temporary.name)) as stage:
                    _artifact_token(stage)  # A committed stage has no persistent journal/WAL state.
                    with stage.open('search.db') as source:
                        before = _token(os.fstat(source.fileno()))
                        with self.boundary.atomic_writer('search.db', before_replace=lambda: _fresh(
                                self.boundary, self.output, self.base_token)) as target:
                            shutil.copyfileobj(source, target, 64 * 1024)
                            if _token(os.fstat(source.fileno())) != before:
                                raise RuntimeError('Private index changed during publication')
                self.base_token = _artifact_token(self.boundary)
        except BaseException:
            self.failed = True
            raise

    def close(self):
        if self.closed: return
        self.closed = True
        try:
            super().close()  # Rolls back unfinished transactions before any export.
            if self.boundary is not None: self.publish()
        finally:
            if self.boundary is not None:
                self.boundary.__exit__()
                self.boundary = None
            if hasattr(self, 'temporary') and getattr(self, 'cache', None) is None:
                self.temporary.cleanup()

    def __del__(self):
        try:
            self.failed = True  # Abandoned stages never publish.
            self.close()
        except Exception: pass


def _snapshot(boundary, output, owner, cache, readonly):
    if not boundary.secure:
        raise OSError('Secure index access is unavailable on this platform')
    if owner is not None and boundary.identity != owner:
        raise RuntimeError('Index output owner changed; reopen the original output directory')
    token = _artifact_token(boundary)
    if readonly and token is None: raise FileNotFoundError('Index is unavailable; map the repository first')
    if cache is not None and cache.get('token') == token and cache.get('owner') == boundary.identity:
        return cache['temporary'], token
    temporary = tempfile.TemporaryDirectory(prefix='repo-graph-index-')
    path = Path(temporary.name) / 'search.db'
    try:
        if token is not None:
            with boundary.open('search.db') as source, path.open('xb') as target:
                before = _token(os.fstat(source.fileno()))
                shutil.copyfileobj(source, target, 64 * 1024)
                if before != token or _token(os.fstat(source.fileno())) != before:
                    raise RuntimeError('Index changed during snapshot copy; retry')
        _fresh(boundary, output, token)
    except BaseException:
        temporary.cleanup()
        raise
    if cache is not None:
        if cache.get('temporary') is not None: cache['temporary'].cleanup()
        cache.update(temporary=temporary, token=token, owner=boundary.identity)
    return temporary, token


def words(text: str) -> str:
    return re.sub(r"[^\w]+", " ", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)).replace("_", " ")


def synopsis(path: str, source: str) -> str:
    """Index names and declarations/comments, not every implementation token."""
    if "PRIVATE KEY-----" in source:
        return ""
    title = words(path)
    lines = []
    prose = Path(path).suffix in {".md", ".markdown", ".mdx", ".rst", ".txt"}
    boilerplate = re.compile(r"copyright|spdx|licensed under|permission is hereby", re.I)
    declaration = re.compile(r"(?://|#|/\*|\*|\"\"\"|'''|func |def |class |pub |fn |export |interface |type |resource |data )")
    length = 0
    for number, line in enumerate(source.splitlines(), 1):
        line = line.strip()
        if not line or boilerplate.search(line):
            continue
        if prose or declaration.match(line):
            # Evidence retains original line references; identifier terms aid exact retrieval.
            lines.append(f"L{number}: {SECRET.sub('[redacted]', line[:180])}")
            length += len(lines[-1])
        if length >= 1400:
            break
    return title + "\n" + "\n".join(lines)


def connect(output: Path, *, readonly: bool = False, owner: str | None = None, cache=None) -> sqlite3.Connection:
    boundary = SourceRoot(output)
    db = None
    temporary = None
    try:
        # ponytail: one streamed copy per index generation; measure cold I/O before adding another backend.
        with SNAPSHOT_LOCK:
            temporary, token = _snapshot(boundary, output, owner, cache, readonly)
            path = Path(temporary.name) / 'search.db'
            db = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1' if readonly else str(path),
                                 uri=readonly, timeout=30, factory=IndexConnection)
            db.temporary = temporary
            db.cache = cache
            _fresh(boundary, output, token)
        if readonly:
            boundary.__exit__()
        else:
            db.boundary, db.output, db.base_token = boundary, output, token
            db.initial_private_token = _token(path.stat()) if token is not None else None
    except BaseException:
        if db is not None:
            db.failed = True
            db.close()
        elif temporary is not None and cache is None:
            temporary.cleanup()
        boundary.__exit__()
        raise
    try:
        db.row_factory = sqlite3.Row
        if not readonly:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL,
              stamp TEXT NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL, terms TEXT NOT NULL, vector BLOB);
            CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(terms, content='docs', content_rowid='id', tokenize='porter unicode61');
            CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
              INSERT INTO fts(rowid,terms) VALUES(new.id,new.terms); END;
            CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
              INSERT INTO fts(fts,rowid,terms) VALUES('delete',old.id,old.terms); END;
            CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE OF terms ON docs BEGIN
              INSERT INTO fts(fts,rowid,terms) VALUES('delete',old.id,old.terms);
              INSERT INTO fts(rowid,terms) VALUES(new.id,new.terms); END;
            """)
            if 'porter' not in db.execute("SELECT sql FROM sqlite_master WHERE name='fts'").fetchone()[0]:
                db.executescript("""BEGIN;
                  DROP TABLE fts;
                  CREATE VIRTUAL TABLE fts USING fts5(terms,content='docs',content_rowid='id',tokenize='porter unicode61');
                  INSERT INTO fts(fts) VALUES('rebuild');
                  COMMIT;""")
            if 'content_digest' not in {row['name'] for row in db.execute('PRAGMA table_info(docs)')}:
                db.execute("ALTER TABLE docs ADD COLUMN content_digest TEXT NOT NULL DEFAULT ''")
        return db
    except BaseException:
        db.failed = True
        db.close()
        raise


def catalog(root: Path, files: list[str], output: Path) -> dict:
    started = time.monotonic()
    scanned = reused = truncated = 0
    failures = []
    with SourceRoot(root) as source_root, closing(connect(output)) as db, db:
        identity = {'schema': '2', 'repository': source_root.identity, 'analyzer': 'synopsis-v2',
                    'config': hashlib.sha256(json.dumps([READ_LIMIT, sorted(TEXT_EXTENSIONS)]).encode()).hexdigest()}
        previous = dict(db.execute('SELECT key,value FROM meta'))
        if any(previous.get(key) != value for key, value in identity.items()):
            db.execute('DELETE FROM docs')
        db.execute("CREATE TEMP TABLE seen(path TEXT PRIMARY KEY)")
        for path in files:
            is_text = Path(path).suffix.lower() in TEXT_EXTENSIONS
            try:
                if is_text:
                    data, content_digest, stat = source_root.read(path, READ_LIMIT)
                else:
                    stat = source_root.info(path)
                    data = b''
                    content_digest = hashlib.sha256(f'{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}'.encode()).hexdigest()
            except OSError as error:
                failures.append({'path': path, 'reason': error.strerror})
                continue
            stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
            db.execute("INSERT OR IGNORE INTO seen VALUES(?)", (path,))
            old = db.execute("SELECT content_digest FROM docs WHERE path=?", (path,)).fetchone()
            truncated += is_text and stat.st_size > READ_LIMIT
            if old and old['content_digest'] == content_digest:
                db.execute('UPDATE docs SET stamp=? WHERE path=?', (stamp, path))
                reused += 1
                continue
            source = data.decode('utf-8', errors='replace')
            body = synopsis(path, source) or words(path)
            digest = hashlib.sha256(body.encode()).hexdigest()
            db.execute("""INSERT INTO docs(path,stamp,digest,body,terms,content_digest) VALUES(?,?,?,?,?,?)
              ON CONFLICT(path) DO UPDATE SET stamp=excluded.stamp,digest=excluded.digest,
                body=excluded.body,terms=excluded.terms,content_digest=excluded.content_digest,vector=NULL""",
                       (path, stamp, digest, body, words(body), content_digest))
            scanned += 1
        deleted = db.execute("DELETE FROM docs WHERE path NOT IN (SELECT path FROM seen)").rowcount
        generation = uuid.uuid4().hex
        db.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)', [*identity.items(), ('generation', generation)])
        count = db.execute("SELECT count(*) FROM docs").fetchone()[0]
    return dict(documents=count, scanned=scanned, reused=reused, deleted=deleted, truncated=truncated,
                seconds=round(time.monotonic() - started, 3), identity=identity, generation=generation,
                failed=len(failures), failures=failures[:50], secure_reads=source_root.secure)


class Embeddings:
    def __init__(self, model: str = MODEL, *, offline: bool = False):
        try:
            from fastembed import TextEmbedding
            import numpy as np
        except ImportError as error:
            raise RuntimeError("Semantic search needs the semantic extra: uv sync --extra semantic, or install repo-graph-agent[semantic].") from error
        self.np = np
        self.name = model
        self.model = TextEmbedding(model_name=model, threads=4, providers=['CPUExecutionProvider'],
                                   cache_dir=str(Path.home() / '.cache/repo-graph/models'), local_files_only=offline)

    def passages(self, texts):
        return self.model.passage_embed(texts, batch_size=32)

    def query(self, text):
        return next(self.model.query_embed(text))

    def packed(self, vector):
        vector = self.np.asarray(vector, dtype='<f4')
        norm = self.np.linalg.norm(vector)
        if vector.ndim != 1 or not self.np.isfinite(vector).all() or norm <= 0:
            raise ValueError('Embedding returned an invalid vector')
        return (vector / norm).astype('<f4').tobytes()


def embed_index(output: Path, embedder: Embeddings) -> dict:
    started = time.monotonic()
    with closing(connect(output)) as db, db:
        with db:
            db.execute('BEGIN IMMEDIATE')
            identity = dict(db.execute('SELECT key,value FROM meta'))
            if identity.get('schema') != '2' or any(not identity.get(k) for k in ('repository', 'generation', 'analyzer', 'config')):
                raise RuntimeError('Legacy or incomplete source identity; run repo-graph map REPO --output OUTPUT before embedding.')
            if db.execute("SELECT count(*) FROM docs WHERE digest='' OR content_digest=''").fetchone()[0]:
                raise RuntimeError('Source/evidence identity is incomplete; remap before embedding.')
            old = db.execute("SELECT value FROM meta WHERE key='model'").fetchone()
            if old and old[0] != embedder.name:
                raise ValueError(f"Index uses {old[0]}; rebuild in a different output directory to change model.")
            db.execute("INSERT OR REPLACE INTO meta VALUES('model',?)", (embedder.name,))
    with closing(connect(output)) as db, db:
        reused = db.execute("SELECT count(*) FROM docs WHERE vector IS NOT NULL").fetchone()[0]
        embedded = 0
        while True:
            # Capture rows and owner/generation together, then release the read snapshot before model work.
            with db:
                db.execute('BEGIN')
                captured = dict(db.execute('SELECT key,value FROM meta'))
                rows = db.execute("SELECT id,path,body,digest,content_digest FROM docs WHERE vector IS NULL ORDER BY id LIMIT 256").fetchall()
            if not rows: break
            vectors = list(embedder.passages([row['body'] for row in rows]))
            if len(vectors) != len(rows):
                raise ValueError('Embedding batch has missing results')
            with db:
                for row, vector in zip(rows, vectors):
                    changed = db.execute("""UPDATE docs SET vector=? WHERE id=? AND path=? AND digest=?
                        AND content_digest=? AND vector IS NULL
                        AND (SELECT value FROM meta WHERE key='repository')=?
                        AND (SELECT value FROM meta WHERE key='generation')=?
                        AND (SELECT value FROM meta WHERE key='model')=?""",
                        (embedder.packed(vector), row['id'], row['path'], row['digest'], row['content_digest'],
                         captured['repository'], captured['generation'], embedder.name)).rowcount
                    if not changed:
                        raise RuntimeError('Embedding source/model snapshot is stale; remap or retry the index command.')
            embedded += len(rows)
        return dict(embedded=embedded, reused=reused, model=embedder.name, seconds=round(time.monotonic() - started, 3))


class Search:
    def __init__(self, output: Path, embedder=None):
        self.output, self.embedder, self.snapshot = output, embedder, {}
        with SourceRoot(output) as boundary:
            self.owner = boundary.identity

    def connect(self):
        return connect(self.output, readonly=True, owner=self.owner, cache=self.snapshot)

    def close(self):
        with SNAPSHOT_LOCK:
            if self.snapshot.get('temporary') is not None:
                self.snapshot['temporary'].cleanup()
            self.snapshot.clear()

    def __del__(self):
        try: self.close()
        except Exception: pass

    def run(self, query: str, *, mode='hybrid', limit=10, prefix='', reranker=None) -> dict:
        if mode not in {'keyword', 'semantic', 'hybrid'}:
            raise ValueError('Unknown search mode')
        if not query.strip() or len(query) > 1000 or not 1 <= limit <= 50:
            raise ValueError('Query must have 1–1000 characters; limit must be 1–50')
        started = time.monotonic()
        ranks, cosine = [], {}
        with closing(self.connect()) as db:
            db.execute('BEGIN')  # All ranks and evidence come from one SQLite snapshot.
            prefix = prefix.strip('/')
            where = "(path=? OR (path>=? AND path<?))" if prefix else "1"
            params = (prefix, prefix + '/', prefix + '0') if prefix else ()
            count = db.execute(f"SELECT count(*) FROM docs WHERE {where}", params).fetchone()[0]
            candidate_count = max(50, limit * 5)
            if mode != 'semantic':
                terms = [term for term in re.findall(r'\w+', words(query)) if term.lower() not in STOPWORDS][:32]
                expression = ' OR '.join('"' + term + '"' for term in terms)
                hits = db.execute(f"""SELECT docs.id FROM fts JOIN docs ON docs.id=fts.rowid
                    WHERE fts MATCH ? AND {where} ORDER BY bm25(fts),docs.path LIMIT ?""",
                                  (expression or '""', *params, candidate_count)).fetchall()
                ranks.append([row['id'] for row in hits])
            if mode != 'keyword':
                if self.embedder is None:
                    raise RuntimeError('Build a semantic index with repo-graph index OUTPUT --semantic first, or select keyword mode.')
                model = db.execute("SELECT value FROM meta WHERE key='model'").fetchone()
                identity = dict(db.execute('SELECT key,value FROM meta'))
                if (not model or model[0] != self.embedder.name or identity.get('schema') != '2'
                        or any(not identity.get(k) for k in ('repository', 'generation', 'analyzer', 'config'))):
                    raise RuntimeError('Semantic index is missing, stale, or uses a different model. Run repo-graph index OUTPUT --semantic.')
                np = self.embedder.np
                q = np.frombuffer(self.embedder.packed(self.embedder.query(query)), dtype='<f4')
                best = []
                cursor = db.execute(f"SELECT id,vector,content_digest FROM docs WHERE {where} ORDER BY id", params)
                # ponytail: exact search uses bounded 512-vector blocks; add an ANN shard when measured latency exceeds the budget.
                while rows := cursor.fetchmany(512):
                    if any(row['vector'] is None or not row['content_digest'] for row in rows):
                        raise RuntimeError('Semantic index is missing, stale, or uses a different model. Run repo-graph index OUTPUT --semantic.')
                    matrix = np.frombuffer(b''.join(row['vector'] for row in rows), dtype='<f4').reshape(len(rows), -1)
                    scores = matrix @ q
                    indices = np.flatnonzero(scores >= best[0][0]) if len(best) == candidate_count else range(len(rows))
                    for index in indices:
                        item = (float(scores[index]), rows[index]['id'])
                        if len(best) < candidate_count: heapq.heappush(best, item)
                        elif item > best[0]: heapq.heapreplace(best, item)
                best.sort(reverse=True)
                ranks.append([id for score, id in best]); cosine = {id:score for score,id in best}
            fused = {}
            for rank in ranks:
                for pos, id in enumerate(rank, 1):
                    fused[id] = fused.get(id, 0) + 1 / (60 + pos)
            # Retrieval depth stays the same across comparisons; only the returned shortlist grows.
            selected = sorted(fused, key=lambda id: (-fused[id], id))[:max(limit,32) if reranker else limit]
            results = []
            for id in selected:
                row = db.execute('SELECT path,body FROM docs WHERE id=?', (id,)).fetchone()
                results.append(dict(path=row['path'], evidence=row['body'][:1400], score=round(fused[id], 6),
                                    similarity=round(cosine[id], 4) if id in cosine else None))
        receipt = {}
        if reranker:
            try:
                results, receipt = reranker.rank(query, results)
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                # A failed or malformed remote judgment must preserve the local ranking.
                receipt = dict(getattr(reranker,'last_receipt',{}), status='fallback', model=reranker.name,
                    reason='Reranker unavailable or invalid response; local order retained')
        response = dict(query=query, mode=mode, documents=count, results=results[:limit],
                        seconds=round(time.monotonic() - started, 4))
        if reranker: response['rerank'] = receipt
        return response
