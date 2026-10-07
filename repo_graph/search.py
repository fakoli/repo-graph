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
from .source import PublicationError, SourceRoot

MODEL = "BAAI/bge-small-en-v1.5"
STOPWORDS = set("a an the and or of to in on for with by from is are was were be been which that this these those how where what when why will can as it its us our".split())
READ_LIMIT = 64 * 1024
TEXT_EXTENSIONS = {".md", ".markdown", ".mdx", ".rst", ".txt", ".go", ".py", ".js", ".jsx", ".ts", ".tsx",
                   ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".sh", ".tf", ".sql", ".vue", ".svelte"}
SECRET = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})")
SNAPSHOT_LOCK = Lock()
_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def code_identity():
    observed = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if observed != _LOADED_SOURCE_SHA256:
        raise RuntimeError('Index writer implementation changed since module import')
    return observed


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
def _index_lock(boundary, *, check=None, create=True):
    import fcntl
    if create:
        try:
            with boundary.open('.index.lock', create=True): pass
        except FileExistsError: pass
    elif not _lock_exists(boundary):
        if check is not None: check()
        yield
        return
    with boundary.open('.index.lock') as stream:
        if check is None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        else:
            while True:
                check()
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(0.005)
        yield


def _lock_exists(boundary):
    try: boundary.info('.index.lock')
    except FileNotFoundError: return False
    return True


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


def _snapshot(boundary, output, owner, cache, readonly, check=None):
    if check is not None:
        check()
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
                while True:
                    if check is not None:
                        check()
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                if check is not None:
                    check()
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


def connect(output: Path, *, readonly: bool = False, owner: str | None = None, cache=None,
            check=None) -> sqlite3.Connection:
    if check is not None and not callable(check):
        raise ValueError('Callable index storage check required')
    if check is not None:
        check()
    boundary = SourceRoot(output)
    db = None
    temporary = None
    storage_stop = None
    def progress():
        nonlocal storage_stop
        try:
            check()
            return 0
        except BaseException as error:
            storage_stop = error
            return 1
    try:
        # ponytail: one streamed copy per index generation; measure cold I/O before adding another backend.
        if check is None:
            SNAPSHOT_LOCK.acquire()
        else:
            while True:
                check()
                if SNAPSHOT_LOCK.acquire(timeout=0.01):
                    break
        try:
            temporary, token = _snapshot(boundary, output, owner, cache, readonly, check)
            path = Path(temporary.name) / 'search.db'
            db = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1' if readonly else str(path),
                                 uri=readonly, timeout=30, factory=IndexConnection,
                                 check_same_thread=not readonly)
            db.temporary = temporary
            db.cache = cache
            if readonly and check is not None:
                db.set_progress_handler(progress, 64)
            _fresh(boundary, output, token)
            if check is not None:
                check()
        finally:
            SNAPSHOT_LOCK.release()
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
        identities = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('repository','structural_repository')"))
        if len(identities) == 2 and identities['repository'] != identities['structural_repository']:
            raise RuntimeError('Shared index contains conflicting repository identities; use a new output directory')
        if readonly and check is not None:
            check()
            db.set_progress_handler(None, 0)
        return db
    except BaseException:
        db.failed = True
        db.close()
        if storage_stop is not None:
            raise storage_stop
        raise


ATTEMPT_BYTES = 32 * 1024
ATTEMPT_STATES = {'updating', 'ready', 'interrupted', 'failed', 'publication_uncertain'}


def _attempt(boundary, component):
    try:
        with boundary.open(component + '-attempt.json') as stream:
            data = stream.read(ATTEMPT_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(data) > ATTEMPT_BYTES:
        raise ValueError('Index attempt record exceeds its byte budget')
    record = json.loads(data)
    if (type(record) is not dict or type(record.get('attempt_id')) is not str or
            not 1 <= len(record['attempt_id']) <= 128 or type(record.get('status')) is not str or
            record['status'] not in ATTEMPT_STATES):
        raise ValueError('Invalid persisted index attempt')
    return record


def record_attempt(output, owner, component, record, expected=None):
    """Record a bounded producer receipt independently of the last published facts."""
    if (component not in {'structural', 'semantic'} or type(record) is not dict or
            type(record.get('attempt_id')) is not str or not 1 <= len(record['attempt_id']) <= 128 or
            type(record.get('status')) is not str or record['status'] not in ATTEMPT_STATES or
            expected is not None and (type(expected) is not str or not 1 <= len(expected) <= 128)):
        raise ValueError('Component, attempt ID and lifecycle status are required')
    data = json.dumps(record, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode()
    if len(data) > ATTEMPT_BYTES:
        raise ValueError('Index attempt record exceeds its byte budget')
    with SourceRoot(Path(output)) as boundary:
        if boundary.identity != owner:
            raise RuntimeError('Index output owner changed; reopen the original output directory')
        with _index_lock(boundary):
            previous = _attempt(boundary, component)
            if expected is not None and (previous or {}).get('attempt_id') != expected:
                raise RuntimeError('Index attempt changed; retry with the current attempt ID')
            if previous is not None and previous['attempt_id'] == record['attempt_id']:
                if previous['status'] != 'updating':
                    if previous == record: return record
                    raise RuntimeError('Terminal index attempt is immutable')
            elif record['status'] != 'updating' or (previous is not None and
                    previous['status'] == 'updating' and expected is None):
                raise RuntimeError('Another index attempt owns the lifecycle record')
            def check_owner():
                with SourceRoot(Path(output)) as current:
                    if current.identity != owner:
                        raise RuntimeError('Index output owner changed; reopen the original output directory')
            check_owner()
            with boundary.atomic_writer(component + '-attempt.json', before_replace=check_owner) as target:
                target.write(data)
    return record


def begin_attempt(output, owner, component, record):
    """CAS-supersede the observed attempt without letting its later terminal win."""
    if component not in {'structural', 'semantic'}:
        raise ValueError('Unknown index lifecycle component')
    with SourceRoot(Path(output)) as boundary:
        if boundary.identity != owner:
            raise RuntimeError('Index output owner changed; reopen the original output directory')
        previous = _attempt(boundary, component)
    return record_attempt(output, owner, component, record,
                          expected=previous['attempt_id'] if previous else None)


def _status_component(attempt=None):
    return {'state': 'not_scanned', 'artifact_ready': False, 'query_available': False,
            'freshness': 'unknown', 'freshness_basis': 'unobserved_live_source',
            'identities': {}, 'receipt': None, 'last_attempt': attempt}


def index_status(output, *, owner=None, expected_source=None, backend_available=None):
    """Read one bounded captured index; never inspect live source, Git or a backend."""
    if expected_source is not None and (type(expected_source) is not str or
            not re.fullmatch('[0-9a-f]{64}', expected_source)):
        raise ValueError('Expected source must be a SHA256 identity')
    if backend_available is not None and type(backend_available) is not bool:
        raise ValueError('Backend availability must be an observed boolean or unknown')
    started = time.monotonic()
    result = {'status': 'ok', 'output_owner': owner, 'structural': _status_component(),
              'semantic_index': _status_component(), 'storage': {'deadline_seconds': 0.5}}
    result['semantic_index'].update(model=None, backend_available=backend_available,
        compatibility='unknown_legacy', generation_basis='keyword-docs-v2', structural_generation_affinity='unknown',
        catalog_receipt=None)
    def check():
        if time.monotonic() - started >= 0.5:
            raise TimeoutError('Index status storage deadline exceeded')
    stop = None
    def progress():
        nonlocal stop
        try: check(); return 0
        except TimeoutError as error: stop = error; return 1
    try:
        check()
        with SourceRoot(Path(output)) as boundary:
            if owner is not None and boundary.identity != owner:
                raise RuntimeError('Index output owner changed; reopen the original output directory')
            result['output_owner'] = boundary.identity
            with _index_lock(boundary, check=check, create=False):
                for name, key in [('structural', 'structural'), ('semantic', 'semantic_index')]:
                    result[key]['last_attempt'] = _attempt(boundary, name)
                    check()
                if _artifact_token(boundary) is not None:
                    with closing(connect(Path(output), readonly=True, owner=boundary.identity, check=check)) as db:
                        db.set_progress_handler(progress, 64)
                        keys = ('structural_schema', 'structural_repository', 'structural_source',
                                'structural_generation', 'structural_analyzer', 'structural_config',
                                'structural_receipt', 'schema', 'repository', 'generation',
                                'analyzer', 'config', 'model', 'semantic_receipt', 'catalog_receipt')
                        meta = dict(db.execute('SELECT key,substr(value,1,?) FROM meta WHERE key IN (' +
                            ','.join('?' for _ in keys) + ')', (ATTEMPT_BYTES + 1, *keys)))
                        check()
                        if any(type(value) is not str or len(value.encode()) > ATTEMPT_BYTES for value in meta.values()):
                            raise ValueError('Persisted index status receipt exceeds its byte budget')
                        structural = result['structural']
                        structural['identities'] = {key + '_identity' if key != 'generation' else key:
                            meta.get('structural_' + key) for key in ('repository', 'source', 'analyzer', 'config', 'generation')}
                        if meta.get('structural_receipt'):
                            structural['receipt'] = json.loads(meta['structural_receipt'])
                            if type(structural['receipt']) is not dict:
                                raise ValueError('Invalid structural status receipt')
                        coherent = all((structural['receipt'] or {}).get(key + '_identity' if key != 'generation' else key) ==
                            meta.get('structural_' + key) for key in ('repository', 'source', 'analyzer', 'config', 'generation'))
                        structural['artifact_ready'] = (meta.get('structural_schema') == 'structural-v2' and
                            all(meta.get('structural_' + key) for key in ('repository', 'source', 'analyzer', 'config', 'generation')) and
                            coherent and
                            (structural['receipt'] or {}).get('status') in ('ready', 'publication_uncertain'))
                        structural['query_available'] = structural['artifact_ready']
                        structural['state'] = 'ready' if structural['artifact_ready'] else (
                            'unknown_legacy' if meta.get('structural_generation') else 'not_scanned')
                        if expected_source is not None and meta.get('structural_source'):
                            structural['freshness_basis'] = 'caller_expected_source'
                            structural['freshness'] = 'current' if expected_source == meta['structural_source'] else 'stale'
                            if structural['freshness'] == 'stale': structural['state'] = 'stale'
                        semantic = result['semantic_index']
                        semantic['identities'] = {key + '_identity' if key != 'generation' else key: meta.get(key)
                            for key in ('repository', 'analyzer', 'config', 'generation')}
                        semantic['model'] = meta.get('model')
                        semantic['backend_available'] = backend_available
                        semantic['generation_basis'] = 'keyword-docs-v2'
                        semantic['structural_generation_affinity'] = 'unknown'
                        semantic['compatibility'] = 'unknown_legacy'
                        if meta.get('catalog_receipt'):
                            semantic['catalog_receipt'] = json.loads(meta['catalog_receipt'])
                            if type(semantic['catalog_receipt']) is not dict:
                                raise ValueError('Invalid captured catalog receipt')
                        if meta.get('semantic_receipt'):
                            receipt = semantic['receipt'] = json.loads(meta['semantic_receipt'])
                            if (type(receipt) is not dict or type(receipt.get('status')) is not str or
                                    receipt['status'] not in {'ready', 'stale', 'not_indexed'}):
                                raise ValueError('Invalid semantic status receipt')
                            coherent = (receipt.get('generation_basis') == 'keyword-docs-v2' and
                                all(meta.get(key) for key in ('repository', 'generation', 'analyzer', 'config')) and
                                all(receipt.get(key) == meta.get(key) for key in ('repository', 'generation', 'analyzer', 'config', 'model')) and
                                all(type(receipt.get(key)) is int and receipt[key] >= 0
                                    for key in ('documents', 'vectors', 'missing_vectors')) and
                                receipt['documents'] == receipt['vectors'] + receipt['missing_vectors'] and
                                meta.get('schema') == '2')
                            semantic['compatibility'] = 'captured' if coherent else 'stale'
                            semantic['artifact_ready'] = bool(coherent and receipt.get('status') == 'ready' and
                                meta.get('model') and receipt.get('missing_vectors') == 0)
                            semantic['state'] = 'ready' if semantic['artifact_ready'] else (receipt.get('status', 'unknown_legacy') if coherent else 'stale')
                        elif meta.get('generation'):
                            semantic['state'] = 'unknown_legacy'
                        semantic['query_available'] = semantic['artifact_ready'] and backend_available is True
                        db.set_progress_handler(None, 0)
            check()
            with SourceRoot(Path(output)) as current:
                if current.identity != boundary.identity:
                    raise RuntimeError('Index output owner changed; reopen the original output directory')
    except FileNotFoundError:
        result['status'] = 'unavailable'
    except (TimeoutError, sqlite3.Error, OSError, ValueError, RecursionError) as error:
        result['status'] = 'bounded_stop' if stop is not None or isinstance(error, TimeoutError) else 'unavailable'
        result['reason'] = str(stop or error)
        # No partial metadata observation is advertised as ready after interrupted storage work.
        for key in ('structural', 'semantic_index'):
            result[key].update(state='unknown', artifact_ready=False, query_available=False)
    for key in ('structural', 'semantic_index'):
        component = result[key]
        attempt = component['last_attempt']
        if attempt and attempt['status'] != 'ready' and result['status'] == 'ok':
            component['state'] = attempt['status']
            if attempt.get('reason') in ('source_changed_before_publication', 'repository_replaced_before_publication'):
                component['freshness'] = 'stale'
                component['freshness_basis'] = 'observed_attempt_failure'
    result['semantic_index'].setdefault('backend_available', backend_available)
    result['storage']['elapsed_seconds'] = time.monotonic() - started
    return result


def _run_semantic_writer(output, operation, run):
    with SourceRoot(Path(output)) as destination:
        owner = destination.identity
    attempt = {'attempt_id': uuid.uuid4().hex, 'status': 'updating', 'operation': operation,
               'started_at': time.time(), 'generation_basis': 'keyword-docs-v2'}
    try:
        result = run(owner, attempt)
    except BaseException as error:
        if attempt.get('recorded'):
            terminal = dict(attempt, status='publication_uncertain' if isinstance(error, PublicationError) else (
                'interrupted' if isinstance(error, (InterruptedError, KeyboardInterrupt)) else 'failed'),
                error_kind=type(error).__name__, reason=str(error)[:1024], finished_at=time.time(),
                published=isinstance(error, PublicationError))
            terminal.pop('recorded', None)
            try: record_attempt(output, owner, 'semantic', terminal, expected=attempt['attempt_id'])
            except Exception: pass  # A rejected/failed receipt remains honestly unsettled; preserve the original failure.
        raise
    terminal = dict(attempt, status='ready', finished_at=time.time(), published=True, receipt=result)
    terminal.pop('recorded', None)
    record_attempt(output, owner, 'semantic', terminal, expected=attempt['attempt_id'])
    return result


def _begin_semantic(output, owner, attempt, identity):
    attempt.update(repository_identity=identity.get('repository'), previous_generation=identity.get('generation'))
    begin_attempt(output, owner, 'semantic', attempt)
    attempt['recorded'] = True


def _semantic_receipt(db, identity, *, previous=None, embedded=False):
    documents, vectors = db.execute('SELECT count(*),coalesce(sum(vector IS NOT NULL),0) FROM docs').fetchone()
    model = db.execute("SELECT value FROM meta WHERE key='model'").fetchone()
    model = model[0] if model else None
    compatible = embedded or (previous is not None and previous.get('status') == 'ready' and
        previous.get('generation_basis') == 'keyword-docs-v2' and previous.get('schema') == '2' and
        previous.get('model') == model and all(previous.get(key) == identity.get(key)
            for key in ('repository', 'analyzer', 'config')))
    receipt = {**identity, 'model': model, 'generation_basis': 'keyword-docs-v2',
        'documents': documents, 'vectors': vectors, 'missing_vectors': documents - vectors,
        'status': 'ready' if model and compatible and documents == vectors else (
            'stale' if model else 'not_indexed')}
    db.execute("INSERT OR REPLACE INTO meta VALUES('semantic_receipt',?)",
               (json.dumps(receipt, ensure_ascii=True, separators=(',', ':')),))
    return receipt


def catalog(root: Path, files: list[str], output: Path) -> dict:
    return _run_semantic_writer(output, 'catalog', lambda owner, attempt: _catalog(root, files, output, owner, attempt))


def _catalog(root, files, output, owner, attempt):
    started = time.monotonic()
    scanned = reused = truncated = failed = 0
    failures = []
    with SourceRoot(root) as source_root, closing(connect(output, owner=owner)) as db, db:
        identity = {'schema': '2', 'repository': source_root.identity, 'analyzer': 'synopsis-v2',
                    'config': hashlib.sha256(json.dumps([READ_LIMIT, sorted(TEXT_EXTENSIONS)]).encode()).hexdigest()}
        previous = dict(db.execute('SELECT key,value FROM meta'))
        if previous.get('structural_repository') not in (None, source_root.identity):
            raise RuntimeError('Shared index belongs to another structural repository; use a new output directory')
        _begin_semantic(output, owner, attempt, previous)
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
                failed += 1
                if len(failures) < 20:
                    failure = {}
                    for key, value in [('path', path), ('reason', error.strerror or '')]:
                        bounded = value[:256]
                        while len(json.dumps(bounded, ensure_ascii=True).encode()) > 256:
                            bounded = bounded[:len(bounded) // 2]
                        failure[key] = bounded
                        if bounded != value: failure[key + '_truncated'] = True
                    failures.append(failure)
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
        semantic = _semantic_receipt(db, {**identity, 'generation': generation},
            previous=json.loads(previous['semantic_receipt']) if previous.get('semantic_receipt') else None)
        receipt = dict(documents=count, scanned=scanned, reused=reused, deleted=deleted, truncated=truncated,
                seconds=round(time.monotonic() - started, 3), identity=identity, generation=generation,
                failed=failed, failures=failures, secure_reads=source_root.secure,
                failures_truncated=failed > len(failures),
                semantic_index=semantic)
        attempt.update(generation=generation, repository_identity=source_root.identity)
        db.execute("INSERT OR REPLACE INTO meta VALUES('catalog_receipt',?)",
                   (json.dumps(receipt, ensure_ascii=True, separators=(',', ':')),))
    return receipt


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
    return _run_semantic_writer(output, 'embed', lambda owner, attempt: _embed_index(output, embedder, owner, attempt))


def _embed_index(output, embedder, owner, attempt):
    started = time.monotonic()
    with closing(connect(output, owner=owner)) as db, db:
        with db:
            db.execute('BEGIN IMMEDIATE')
            identity = dict(db.execute('SELECT key,value FROM meta'))
            _begin_semantic(output, owner, attempt, identity)
            if identity.get('schema') != '2' or any(not identity.get(k) for k in ('repository', 'generation', 'analyzer', 'config')):
                raise RuntimeError('Legacy or incomplete source identity; run repo-graph map REPO --output OUTPUT before embedding.')
            if db.execute("SELECT count(*) FROM docs WHERE digest='' OR content_digest=''").fetchone()[0]:
                raise RuntimeError('Source/evidence identity is incomplete; remap before embedding.')
            old = db.execute("SELECT value FROM meta WHERE key='model'").fetchone()
            if old and old[0] != embedder.name:
                raise ValueError(f"Index uses {old[0]}; rebuild in a different output directory to change model.")
            db.execute("INSERT OR REPLACE INTO meta VALUES('model',?)", (embedder.name,))
    with closing(connect(output, owner=owner)) as db, db:
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
        semantic = _semantic_receipt(db, {key: captured[key] for key in
            ('schema', 'repository', 'generation', 'analyzer', 'config')}, embedded=True)
        attempt.update(generation=captured['generation'])
        return dict(embedded=embedded, reused=reused, model=embedder.name,
                    seconds=round(time.monotonic() - started, 3), semantic_index=semantic)


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
