"""Incremental SQLite keyword index and optional CPU semantic search."""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import heapq
import json
import math
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


class _LegacyReceipt(ValueError):
    pass


def _json_record(value):
    try:
        data = value if isinstance(value, (str, bytes)) else json.dumps(
            value, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
        if len(data.encode() if isinstance(data, str) else data) > ATTEMPT_BYTES:
            raise ValueError('Index receipt exceeds its byte budget')
        def pairs(items):
            result = {}
            for key, item in items:
                if key in result: raise ValueError('Duplicate index receipt field')
                result[key] = item
            return result
        def constant(_): raise ValueError('Nonfinite index receipt value')
        record = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
        if type(record) is not dict: raise ValueError('Index receipt object required')
        return record
    except (TypeError, RecursionError) as error:
        raise ValueError('Invalid bounded index receipt') from error


def _count(value):
    if type(value) is not int or value < 0: raise ValueError('Nonnegative typed index count required')
    return value


def _string(value, maximum=128):
    if type(value) is not str or not 1 <= len(value.encode()) <= maximum:
        raise ValueError('Bounded index identity/string required')
    return value


def _hash(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('SHA256 index identity required')
    return value


def _required(record, fields):
    if type(record) is not dict: raise ValueError('Index receipt object required')
    if not set(fields.split()).issubset(record): raise _LegacyReceipt('Incomplete legacy index receipt')


def _counts(record):
    if type(record) is not dict or len(record) > 16: raise ValueError('Bounded count map required')
    for key, value in record.items(): _string(key); _count(value)
    return record


def _number(value):
    try: valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError: valid = False
    if not valid:
        raise ValueError('Finite nonnegative typed index number required')


def _resources(record):
    if type(record) is not dict: raise ValueError('Index resources object required')
    for key, value in record.items():
        _string(key)
        if type(value) is str: _string(value, 1024)
        elif key.endswith('seconds'): _number(value)
        else: _count(value)


def _coverage_receipt(coverage):
    if type(coverage) is not dict: raise ValueError('Structural coverage object required')
    for key in ('files_total', 'files_supported', 'files_unsupported', 'parser_error_count'):
        if key in coverage: _count(coverage[key])
    for key in ('status_counts', 'file_status'):
        if key in coverage: _counts(coverage[key])
    _required(coverage, 'files_total files_supported files_unsupported status_counts file_status by_language language_overflow sites_by_role_certainty parser_error_count parser_error_samples parser_error_samples_truncated inventory_scope discovery_skipped_files discovery_skip_knowledge')
    statuses = coverage['status_counts']
    if (set(statuses) - {'parsed', 'partial_parse', 'unsupported_language', 'configuration', 'excluded_size'} or
            coverage['file_status'] != statuses or sum(statuses.values()) != coverage['files_total'] or
            coverage['files_supported'] != statuses.get('parsed', 0) + statuses.get('partial_parse', 0) or
            coverage['files_unsupported'] != statuses.get('unsupported_language', 0)):
        raise ValueError('Structural coverage counts do not reconcile')
    languages, overflow = coverage['by_language'], coverage['language_overflow']
    if type(languages) is not dict or len(languages) > 32 or type(overflow) is not dict:
        raise ValueError('Bounded language coverage required')
    _required(overflow, 'languages files_total file_status')
    _count(overflow['languages'])
    totals = {}
    for language, group in [*languages.items(), ('overflow', overflow)]:
        _string(language)
        if type(group) is not dict: raise ValueError('Language coverage object required')
        _required(group, 'files_total file_status')
        _count(group['files_total']); _counts(group['file_status'])
        if sum(group['file_status'].values()) != group['files_total']:
            raise ValueError('Language coverage counts do not reconcile')
        for key, value in group['file_status'].items(): totals[key] = totals.get(key, 0) + value
    if (totals != statuses or overflow['languages'] == 0 and overflow['files_total'] != 0 or
            overflow['languages'] > 0 and (len(languages) != 32 or overflow['files_total'] < overflow['languages'])):
        raise ValueError('Language overflow does not reconcile')
    sites = coverage['sites_by_role_certainty']
    if type(sites) is not dict or set(sites) - {'call', 'reference'}:
        raise ValueError('Typed site roles required')
    for values in sites.values():
        _counts(values)
        if set(values) - {'resolved', 'candidate', 'unresolved'}: raise ValueError('Typed site certainty required')
    samples = coverage['parser_error_samples']
    if type(samples) is not list or len(samples) > 16 or len(json.dumps(samples, ensure_ascii=True, separators=(',', ':')).encode()) > 4096:
        raise ValueError('Bounded parser error samples required')
    for sample in samples:
        if type(sample) is not dict: raise ValueError('Parser error object required')
        _required(sample, 'path kind range'); _string(sample['path'], 4096); SourceRoot.parts(sample['path'])
        _string(sample['kind']); span = sample['range']
        if type(span) is not dict: raise ValueError('Parser error range required')
        _required(span, 'start_byte end_byte start_line end_line')
        for value in span.values(): _count(value)
        if span['end_byte'] < span['start_byte'] or span['start_line'] < 1 or span['end_line'] < span['start_line']:
            raise ValueError('Invalid parser error range')
    if (coverage['parser_error_count'] < len(samples) or type(coverage['parser_error_samples_truncated']) is not bool or
            coverage['parser_error_samples_truncated'] != (coverage['parser_error_count'] > len(samples)) or
            coverage['inventory_scope'] != 'caller_admitted_inventory' or coverage['discovery_skipped_files'] is not None or
            coverage['discovery_skip_knowledge'] != 'outside_admitted_inventory_not_measured'):
        raise ValueError('Invalid parser/discovery coverage knowledge')


def _structural_receipt(record, meta=None):
    if 'resources' in record: _resources(record['resources'])
    if 'coverage' in record: _coverage_receipt(record['coverage'])
    _required(record, 'status published repository_identity source_identity analyzer_identity config_identity generation coverage versions revision_dirty')
    for key in ('repository_identity', 'source_identity', 'analyzer_identity', 'config_identity', 'generation'): _hash(record[key])
    if record['status'] not in ('ready', 'publication_uncertain') or record['published'] is not True:
        raise ValueError('Published structural receipt required')
    versions, revision = record['versions'], record['revision_dirty']
    if type(versions) is not dict or type(revision) is not dict: raise ValueError('Typed versions/revision knowledge required')
    _required(versions, 'schema rules grammars'); _string(versions['rules'])
    if versions['schema'] != 'structural-v2' or type(versions['grammars']) is not dict or not 1 <= len(versions['grammars']) <= 32:
        raise ValueError('Invalid structural version manifest')
    for key, value in versions['grammars'].items(): _string(key); _string(value)
    _required(revision, 'revision dirty knowledge content_identity')
    if revision['content_identity'] != record['source_identity']: raise ValueError('Foreign revision content identity')
    if revision['knowledge'] == 'captured_git':
        if (type(revision['revision']) is not str or re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', revision['revision']) is None or
                type(revision['dirty']) is not bool): raise ValueError('Invalid captured Git knowledge')
    elif revision['knowledge'] == 'captured_revision':
        if (type(revision['revision']) is not str or re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', revision['revision']) is None or
                revision['dirty'] is not None or revision.get('reason') != 'git_dirty_not_observed_without_project_commands' or
                revision.get('dirty_basis') != 'unobserved_repository_configured_status'):
            raise ValueError('Invalid captured revision knowledge')
    elif revision['knowledge'] == 'unknown':
        if revision['revision'] is not None or revision['dirty'] is not None: raise ValueError('Unknown Git knowledge must remain unknown')
    else: raise ValueError('Invalid revision knowledge')
    if revision.get('reason') is not None: _string(revision['reason'])
    if meta is not None and any(record[key + '_identity' if key != 'generation' else key] != meta.get('structural_' + key)
            for key in ('repository', 'source', 'analyzer', 'config', 'generation')):
        raise ValueError('Foreign structural receipt identity')


def _semantic_receipt_valid(record, meta=None):
    _required(record, 'schema repository generation analyzer config model generation_basis documents vectors missing_vectors status')
    _hash(record['repository']); _hash(record['config']); _string(record['generation']); _string(record['analyzer'])
    for key in ('documents', 'vectors', 'missing_vectors'): _count(record[key])
    if record['model'] is not None: _string(record['model'], 256)
    if (record['schema'] != '2' or record['generation_basis'] != 'keyword-docs-v2' or
            record['status'] not in ('ready', 'stale', 'not_indexed') or
            record['documents'] != record['vectors'] + record['missing_vectors'] or
            record['status'] == 'ready' and (not record['model'] or record['missing_vectors'] != 0)):
        raise ValueError('Invalid semantic receipt counts/identity')
    return meta is None or all(record[key] == meta.get(key) for key in
        ('schema', 'repository', 'generation', 'analyzer', 'config', 'model'))


def _catalog_receipt(record, meta=None):
    if 'seconds' in record: _number(record['seconds'])
    if 'secure_reads' in record and type(record['secure_reads']) is not bool: raise ValueError('Typed secure-read knowledge required')
    _required(record, 'documents scanned reused deleted truncated failed failures failures_truncated identity generation semantic_index')
    for key in ('documents', 'scanned', 'reused', 'deleted', 'truncated', 'failed'): _count(record[key])
    failures, identity = record['failures'], record['identity']
    if type(failures) is not list or len(failures) > 20 or record['failed'] < len(failures): raise ValueError('Bounded catalog failures required')
    for failure in failures:
        if type(failure) is not dict: raise ValueError('Catalog failure object required')
        _required(failure, 'path reason')
        for key in ('path', 'reason'):
            if type(failure[key]) is not str or len(json.dumps(failure[key], ensure_ascii=True).encode()) > 256:
                raise ValueError('Bounded catalog failure text required')
            if key + '_truncated' in failure and type(failure[key + '_truncated']) is not bool: raise ValueError('Typed sample truncation required')
    if type(record['failures_truncated']) is not bool or record['failures_truncated'] != (record['failed'] > len(failures)):
        raise ValueError('Catalog failure sample counts do not reconcile')
    if type(identity) is not dict: raise ValueError('Catalog identity required')
    _required(identity, 'schema repository analyzer config'); _hash(identity['repository']); _hash(identity['config']); _string(identity['analyzer']); _string(record['generation'])
    if set(identity) != {'schema', 'repository', 'analyzer', 'config'}: raise ValueError('Invalid catalog identity fields')
    if identity['schema'] != '2' or record['truncated'] > record['documents']: raise ValueError('Invalid catalog counts/schema')
    semantic = record['semantic_index']
    if type(semantic) is not dict: raise ValueError('Catalog semantic receipt required')
    _semantic_receipt_valid(semantic)
    if semantic['documents'] != record['documents'] or semantic['generation'] != record['generation'] or any(semantic[key] != identity[key] for key in identity):
        raise ValueError('Catalog and semantic identity differ')
    if meta is not None and (record['generation'] != meta.get('generation') or any(identity[key] != meta.get(key) for key in identity)):
        raise ValueError('Foreign catalog receipt identity')


def _collection_failures(receipt):
    samples = receipt['collection_failures']
    if (type(samples) is not list or len(samples) > 20 or
            sum(len(json.dumps(sample, ensure_ascii=True, separators=(',', ':')).encode()) for sample in samples) > 4096):
        raise ValueError('Bounded collection failure samples required')
    _required(receipt, 'collection_failures_count collection_failures_truncated')
    _count(receipt['collection_failures_count'])
    if (receipt['collection_failures_count'] < len(samples) or type(receipt['collection_failures_truncated']) is not bool or
            receipt['collection_failures_truncated'] != (receipt['collection_failures_count'] > len(samples))):
        raise ValueError('Collection failure sample counts do not reconcile')
    for sample in samples:
        _required(sample, 'kind'); _string(sample['kind'])
        for key in ('index', 'error_count'):
            if key in sample: _count(sample[key])
        for key in ('reason', 'stage'):
            if key in sample and (type(sample[key]) is not str or len(sample[key].encode()) > 4096):
                raise ValueError('Bounded typed collection failure text required')
        if sample.get('path') is not None: _string(sample['path'], 4096)
        if sample.get('returncode') is not None and type(sample['returncode']) is not int:
            raise ValueError('Typed collection worker return code required')
        if 'record' in sample:
            record = sample['record']
            _required(record, 'path language kind bytes sha256')
            _string(record['path'], 4096); SourceRoot.parts(record['path'])
            _string(record['language']); _count(record['bytes']); _hash(record['sha256'])
            if record['kind'] != 'source': raise ValueError('Collection source record required')


def _attempt_valid(record, component):
    _required(record, 'attempt_id status'); _string(record['attempt_id'])
    if type(record['status']) is not str or record['status'] not in ATTEMPT_STATES: raise ValueError('Invalid index attempt state')
    for key in ('started_at', 'finished_at'):
        if key in record: _number(record[key])
    if all(key in record for key in ('started_at', 'finished_at')) and record['finished_at'] < record['started_at']: raise ValueError('Attempt timestamps are reversed')
    if 'published' in record and type(record['published']) is not bool: raise ValueError('Typed publication state required')
    if record.get('repository_identity') is not None: _hash(record['repository_identity'])
    for key in ('previous_generation', 'generation'):
        if record.get(key) is not None: _string(record[key])
    for key in ('reason', 'error_kind', 'operation', 'generation_basis'):
        if key in record and (type(record[key]) is not str or len(record[key].encode()) > 1024):
            raise ValueError('Bounded typed attempt text required')
    receipt = record.get('receipt')
    if receipt is not None:
        if type(receipt) is not dict: raise ValueError('Attempt receipt object required')
        if component == 'structural':
            _required(receipt, 'status published resources')
            if receipt['status'] != record['status']: raise ValueError('Attempt and receipt lifecycle differ')
            for key in ('reason', 'error_kind'):
                if key in receipt and (type(receipt[key]) is not str or len(receipt[key].encode()) > 4096):
                    raise ValueError('Bounded typed failure text required')
            if receipt.get('path') is not None: _string(receipt['path'], 4096)
            for key in ('previous_generation', 'published_coverage_generation'):
                if receipt.get(key) is not None: _string(receipt[key])
            if ('remaining_inventory_status' in receipt and
                    receipt['remaining_inventory_status'] != 'not_evaluated_after_failure'):
                raise ValueError('Invalid remaining inventory knowledge')
            if 'collection_failures' in receipt: _collection_failures(receipt)
        if component == 'structural' and record['status'] in ('ready', 'publication_uncertain'):
            try: _structural_receipt(receipt)
            except _LegacyReceipt: pass
        elif component == 'semantic' and record.get('operation') == 'catalog': _catalog_receipt(receipt)
        elif component == 'semantic' and record.get('operation') == 'embed':
            _required(receipt, 'embedded reused model seconds semantic_index')
            _count(receipt['embedded']); _count(receipt['reused']); _string(receipt['model'], 256)
            _number(receipt['seconds'])
            _semantic_receipt_valid(receipt['semantic_index'])
            if (receipt['embedded'] + receipt['reused'] != receipt['semantic_index']['documents'] or
                    receipt['model'] != receipt['semantic_index']['model']): raise ValueError('Embedding receipt counts/model differ')
        if 'resources' in receipt: _resources(receipt['resources'])
        if 'published' in receipt and type(receipt['published']) is not bool: raise ValueError('Typed receipt publication required')
        if 'published' in record and 'published' in receipt and record['published'] != receipt['published']:
            raise ValueError('Attempt and receipt publication differ')
        if receipt.get('repository_identity') is not None and receipt['repository_identity'] != record.get('repository_identity'):
            raise ValueError('Attempt receipt belongs to another repository')
        if component == 'semantic':
            for key in ('identity', 'semantic_index'):
                if key in receipt and type(receipt[key]) is not dict: raise ValueError('Attempt semantic identity object required')
            repository = (receipt.get('identity') or {}).get('repository') or (receipt.get('semantic_index') or {}).get('repository')
            if repository is not None and repository != record.get('repository_identity'):
                raise ValueError('Attempt semantic receipt belongs to another repository')
        if record.get('generation') is not None and receipt.get('generation') is not None and record['generation'] != receipt['generation']:
            raise ValueError('Attempt receipt generation differs')


def _attempt(boundary, component):
    try:
        with boundary.open(component + '-attempt.json') as stream:
            data = stream.read(ATTEMPT_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(data) > ATTEMPT_BYTES:
        raise ValueError('Index attempt record exceeds its byte budget')
    record = _json_record(data)
    _attempt_valid(record, component)
    return record


def record_attempt(output, owner, component, record, expected=None):
    """Record a bounded producer receipt independently of the last published facts."""
    if (type(component) is not str or component not in {'structural', 'semantic'} or type(record) is not dict or
            type(record.get('attempt_id')) is not str or not 1 <= len(record['attempt_id']) <= 128 or
            type(record.get('status')) is not str or record['status'] not in ATTEMPT_STATES or
            expected is not None and (type(expected) is not str or not 1 <= len(expected) <= 128)):
        raise ValueError('Component, attempt ID and lifecycle status are required')
    record = _json_record(record)
    _attempt_valid(record, component)
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
    if type(component) is not str or component not in {'structural', 'semantic'}:
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
            'identities': {}, 'receipt': None, 'receipt_knowledge': 'missing',
            'last_attempt': attempt, 'attempt_attribution': None}


def _attempt_attribution(record, component, meta):
    if record is None: return None
    if not record.get('repository_identity') or 'started_at' not in record: return 'unknown_legacy'
    repository = meta.get('structural_repository') or meta.get('repository')
    if repository is None: return 'unpublished_repository'
    if record['repository_identity'] != repository: return 'foreign_repository'
    generation = meta.get('structural_generation' if component == 'structural' else 'generation')
    receipt = record.get('receipt') or {}
    if (generation is not None and record['status'] in ('ready', 'publication_uncertain') and
            (record.get('generation') or receipt.get('generation')) != generation): return 'unrelated_generation'
    if generation is not None and generation not in (record.get('previous_generation'), record.get('generation'),
            receipt.get('generation'), receipt.get('previous_generation')):
        return 'unrelated_generation'
    return 'captured_repository'


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
        catalog_receipt=None, catalog_receipt_knowledge='missing')
    meta = {}
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
                            receipt = _json_record(meta['structural_receipt'])
                            try:
                                _structural_receipt(receipt, meta)
                                structural['receipt'] = receipt
                                structural['receipt_knowledge'] = 'captured'
                            except _LegacyReceipt:
                                structural['receipt_knowledge'] = 'unknown_legacy'
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
                            receipt = _json_record(meta['catalog_receipt'])
                            try:
                                _catalog_receipt(receipt, meta)
                                semantic['catalog_receipt'] = receipt
                                semantic['catalog_receipt_knowledge'] = 'captured'
                            except _LegacyReceipt:
                                semantic['catalog_receipt_knowledge'] = 'unknown_legacy'
                        if meta.get('semantic_receipt'):
                            receipt = _json_record(meta['semantic_receipt'])
                            try:
                                coherent = _semantic_receipt_valid(receipt, meta)
                                semantic['receipt'] = receipt
                                semantic['receipt_knowledge'] = 'captured'
                            except _LegacyReceipt:
                                semantic['receipt_knowledge'] = 'unknown_legacy'
                                coherent = False
                            semantic['compatibility'] = 'captured' if coherent else 'stale'
                            semantic['artifact_ready'] = bool(coherent and receipt.get('status') == 'ready' and
                                meta.get('model') and receipt.get('missing_vectors') == 0 and
                                semantic['receipt_knowledge'] == 'captured' and semantic['catalog_receipt_knowledge'] != 'unknown_legacy')
                            semantic['state'] = 'ready' if semantic['artifact_ready'] else (receipt.get('status', 'unknown_legacy') if coherent else 'stale')
                            if 'unknown_legacy' in (semantic['receipt_knowledge'], semantic['catalog_receipt_knowledge']):
                                semantic['state'] = semantic['compatibility'] = 'unknown_legacy'
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
        attribution = component['attempt_attribution'] = _attempt_attribution(attempt, 'structural' if key == 'structural' else 'semantic', meta)
        if attempt and attempt['status'] != 'ready' and result['status'] == 'ok' and attribution in ('captured_repository', 'unpublished_repository'):
            component['state'] = attempt['status']
            current_generation = meta.get('structural_generation' if key == 'structural' else 'generation')
            if (attribution == 'captured_repository' and current_generation is not None and
                    attempt['status'] in ('failed', 'interrupted') and
                    attempt.get('reason') in ('source_changed_before_publication', 'repository_replaced_before_publication')):
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


def _semantic_receipt(db, identity, *, previous=None, previous_meta=None, embedded=False):
    if previous is not None:
        try:
            if not _semantic_receipt_valid(previous, previous_meta): previous = None
        except _LegacyReceipt: previous = None
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
    _semantic_receipt_valid(receipt)
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
        _begin_semantic(output, owner, attempt, {**previous, 'repository': source_root.identity})
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
            previous=_json_record(previous['semantic_receipt']) if previous.get('semantic_receipt') else None,
            previous_meta=previous)
        receipt = dict(documents=count, scanned=scanned, reused=reused, deleted=deleted, truncated=truncated,
                seconds=round(time.monotonic() - started, 3), identity=identity, generation=generation,
                failed=failed, failures=failures, secure_reads=source_root.secure,
                failures_truncated=failed > len(failures),
                semantic_index=semantic)
        attempt.update(generation=generation, repository_identity=source_root.identity)
        _catalog_receipt(receipt)
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
