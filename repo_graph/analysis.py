"""One persistent structural index; optional native parsing stays in owned workers."""
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
from importlib import metadata
import json
import math
from pathlib import Path, PurePosixPath
import sqlite3
import time

from . import analysis_native as native
from .analysis_queue import QueueLimits, collect_files, _identity as queue_identity
from .search import connect
from .source import SourceRoot

SCHEMA = 'structural-v1'
_LOADED_INDEX_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
LANGUAGES = {'.py': 'python', '.go': 'go', '.js': 'javascript', '.jsx': 'javascript',
             '.ts': 'typescript', '.tsx': 'typescript'}


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()


def analyzer_identity():
    observed = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if observed != _LOADED_INDEX_SHA256:
        raise RuntimeError('Index implementation changed since module import')
    return hashlib.sha256(encoded({'index': observed, 'collector': native.collector_identity(),
                                   'queue': queue_identity(), 'schema': SCHEMA})).hexdigest()


@dataclass(frozen=True)
class IndexLimits:
    # Admission limits are configuration, not measured capacity or qualified defaults.
    max_files: int = 100_000
    max_source_bytes: int = 1024 ** 3
    max_index_bytes: int = 2 * 1024 ** 3
    batch_files: int = 32
    total_wall_seconds: float = 300

    def __post_init__(self):
        if (any(type(v) is not int or v <= 0 for v in (self.max_files, self.max_source_bytes,
                self.max_index_bytes, self.batch_files)) or self.batch_files > 128 or
                type(self.total_wall_seconds) not in (int, float) or
                not math.isfinite(self.total_wall_seconds) or self.total_wall_seconds <= 0):
            raise ValueError('Positive finite index limits and at most 128 files per batch required')


class _Definitions:
    def __init__(self, db):
        self.db = db

    def __getitem__(self, key):
        row = self.db.execute('SELECT data FROM structural_symbols WHERE id=?', (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return json.loads(row[0])


class _Configurations(Mapping):
    def __init__(self, db):
        self.db = db

    def __iter__(self):
        return (r[0] for r in self.db.execute('SELECT path FROM structural_files WHERE configuration IS NOT NULL ORDER BY path'))

    def __len__(self):
        return self.db.execute('SELECT count(*) FROM structural_files WHERE configuration IS NOT NULL').fetchone()[0]

    def __getitem__(self, path):
        row = self.db.execute('SELECT configuration FROM structural_files WHERE path=? AND configuration IS NOT NULL', (path,)).fetchone()
        if row is None:
            raise KeyError(path)
        return row[0]


class _Files(Mapping):
    """Only two decoded compact files are cached; definitions use indexed lookup."""
    def __init__(self, db, budget, check):
        self.db, self.budget, self.check, self.cache = db, budget, check, OrderedDict()

    def __iter__(self):
        return (r[0] for r in self.db.execute('SELECT path FROM structural_files WHERE ir IS NOT NULL ORDER BY path'))

    def __len__(self):
        return self.db.execute('SELECT count(*) FROM structural_files WHERE ir IS NOT NULL').fetchone()[0]

    def __contains__(self, path):
        return self.db.execute('SELECT 1 FROM structural_files WHERE path=? AND ir IS NOT NULL', (path,)).fetchone() is not None

    def in_directory(self, directory):
        return (r[0] for r in self.db.execute("SELECT path FROM structural_files WHERE directory=? AND language='go' AND ir IS NOT NULL ORDER BY path", (directory,)))

    def __getitem__(self, path):
        self.check()
        if path in self.cache:
            self.cache.move_to_end(path)
            return self.cache[path]
        row = self.db.execute('SELECT record,ir,ir_sha FROM structural_files WHERE path=? AND ir IS NOT NULL', (path,)).fetchone()
        if row is None:
            raise KeyError(path)
        payload = json.loads(row['ir'])
        payload['definitions'] = [json.loads(r[0]) for r in self.db.execute('SELECT data FROM structural_symbols WHERE path=? ORDER BY ordinal', (path,))]
        payload['scopes'] = [json.loads(r[0]) for r in self.db.execute('SELECT data FROM structural_scopes WHERE path=? ORDER BY ordinal', (path,))]
        file = native.CollectedFile.from_json(encoded(payload), json.loads(row['record']), row['ir_sha'], self.budget, self.check)
        self.cache[path] = file
        if len(self.cache) > 2:
            self.cache.popitem(last=False)
        return file


def _schema(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS structural_files(path TEXT PRIMARY KEY, directory TEXT NOT NULL,
      language TEXT NOT NULL, kind TEXT NOT NULL, record TEXT NOT NULL, status TEXT NOT NULL,
      ir BLOB, ir_sha TEXT, configuration BLOB);
    CREATE INDEX IF NOT EXISTS structural_directory ON structural_files(directory,language);
    CREATE TABLE IF NOT EXISTS structural_symbols(id TEXT PRIMARY KEY, path TEXT NOT NULL,
      ordinal INTEGER NOT NULL, data TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS structural_symbol_file ON structural_symbols(path,ordinal);
    CREATE TABLE IF NOT EXISTS structural_scopes(path TEXT NOT NULL, ordinal INTEGER NOT NULL,
      data TEXT NOT NULL, PRIMARY KEY(path,ordinal));
    CREATE TABLE IF NOT EXISTS structural_sites(id TEXT PRIMARY KEY, path TEXT NOT NULL,
      ordinal INTEGER NOT NULL, role TEXT NOT NULL, data TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS structural_site_file ON structural_sites(path,ordinal);
    CREATE TABLE IF NOT EXISTS structural_relationships(site_id TEXT NOT NULL, target_id TEXT NOT NULL,
      path TEXT NOT NULL, role TEXT NOT NULL, certainty TEXT NOT NULL,
      PRIMARY KEY(site_id,target_id));
    CREATE INDEX IF NOT EXISTS structural_target ON structural_relationships(target_id,site_id);
    ''')


class StructuralIndex:
    def __init__(self, root, output, *, budget=None, limits=None):
        self.root, self.output = Path(root), Path(output)
        self.budget, self.limits = budget or native.Budget(), limits or IndexLimits()
        if type(self.budget) is not native.Budget or type(self.limits) is not IndexLimits:
            raise ValueError('Typed structural index limits required')
        with SourceRoot(self.root) as source:
            self.owner = source.identity
        self.output.mkdir(parents=True, exist_ok=True)
        with SourceRoot(self.output) as destination:
            self.output_owner = destination.identity
        self.last_attempt = None

    def _metadata(self, db):
        data = dict(db.execute("SELECT key,value FROM meta WHERE key LIKE 'structural_%'"))
        if data and data.get('structural_repository') != self.owner:
            raise RuntimeError('Structural index belongs to another repository')
        if data.get('structural_schema') not in (None, SCHEMA):
            raise RuntimeError('Incompatible structural index; use a new output directory')
        return data

    def metadata(self):
        with closing(connect(self.output, readonly=True, owner=self.output_owner)) as db:
            data = self._metadata(db)
            return {'generation': data.get('structural_generation'),
                    'repository_identity': data.get('structural_repository'),
                    'source_identity': data.get('structural_source'),
                    'analyzer_identity': data.get('structural_analyzer'),
                    'config_identity': data.get('structural_config')}

    def read_facts(self, kind):
        tables = {'definitions': 'structural_symbols', 'sites': 'structural_sites',
                  'scopes': 'structural_scopes', 'relationships': 'structural_relationships'}
        if kind not in tables:
            raise ValueError('Unknown structural fact kind')
        with closing(connect(self.output, readonly=True, owner=self.output_owner)) as db:
            self._metadata(db)
            if kind == 'relationships':
                for row in db.execute('SELECT * FROM structural_relationships ORDER BY site_id,target_id'):
                    yield dict(row)
            else:
                for row in db.execute('SELECT path,data FROM ' + tables[kind] + ' ORDER BY path,ordinal'):
                    data = json.loads(row['data'])
                    if kind == 'scopes':
                        data['path'] = row['path']
                    yield data

    def refresh(self, paths, *, mode='serial', concurrency=1, cancel=None, evidence_directory=None):
        if (mode not in ('serial', 'queued') or type(concurrency) is not int or
                not 1 <= concurrency <= 4 or mode == 'serial' and concurrency != 1 or
                cancel is not None and not callable(cancel) or isinstance(paths, (str, bytes))):
            raise ValueError('Explicit serial/queued mode, bounded workers and inventory iterable required')
        started, limits, budget = time.monotonic(), self.limits, self.budget
        resources = {'batches': 0, 'changed_files_collected': 0,
                     'unchanged_source_collections_reused': 0, 'source_bytes': 0,
                     'workers_started': 0, 'peak_batch_files': 0, 'peak_batch_source_bytes': 0,
                     'peak_batch_handoff_bytes': 0, 'owned_workers_reaped': 0}
        previous, current_path = None, None

        def check():
            if cancel is not None and cancel():
                raise native.StopScan('cancelled')
            if time.monotonic() - started >= limits.total_wall_seconds:
                raise native.StopScan('deadline_exceeded')
            return False

        try:
            analyzer = analyzer_identity()
            if {name: metadata.version(name) for name in native.PINS} != native.PINS:
                raise native.BackendUnavailable('Optional backend versions differ from qualified pins')
            config = hashlib.sha256(encoded({'budget': asdict(budget), 'limits': asdict(limits),
                                             'versions': native.PINS, 'schema': SCHEMA})).hexdigest()
            with SourceRoot(self.output) as output:
                if output.identity != self.output_owner:
                    raise RuntimeError('Index output ownership changed')
                try:
                    if output.info('search.db').st_size > limits.max_index_bytes:
                        raise native.StopScan('index_byte_budget_exceeded')
                except FileNotFoundError:
                    pass
            with SourceRoot(self.root) as source, closing(connect(self.output, owner=self.output_owner)) as db, db:
                if source.identity != self.owner:
                    raise RuntimeError('Repository ownership changed')
                old = self._metadata(db)
                ordinary_owner = db.execute("SELECT value FROM meta WHERE key='repository'").fetchone()
                if ordinary_owner and ordinary_owner[0] != self.owner:
                    raise RuntimeError('Index belongs to another repository')
                previous = old.get('structural_generation')
                _schema(db)
                page_size = db.execute('PRAGMA page_size').fetchone()[0]
                pages = db.execute('PRAGMA max_page_count=' + str(max(1, limits.max_index_bytes // page_size))).fetchone()[0]
                if pages * page_size > limits.max_index_bytes:
                    raise native.StopScan('index_byte_budget_exceeded')
                db.set_progress_handler(lambda: int(check()), 1000)
                if old.get('structural_analyzer') != analyzer or old.get('structural_config') != config:
                    for table in ('structural_files', 'structural_symbols', 'structural_scopes'):
                        db.execute('DELETE FROM ' + table)
                for table in ('structural_sites', 'structural_relationships'):
                    db.execute('DELETE FROM ' + table)
                db.execute('CREATE TEMP TABLE structural_seen(path TEXT PRIMARY KEY)')
                batch, batch_bytes, count = [], 0, 0

                def flush():
                    nonlocal batch, batch_bytes
                    if not batch:
                        return
                    check()
                    collected = collect_files(iter(batch), mode=mode, concurrency=concurrency, budget=budget,
                        cancel=check, evidence_directory=evidence_directory,
                        limits=QueueLimits(total_wall_seconds=min(60, limits.total_wall_seconds - (time.monotonic() - started))))
                    resources['batches'] += 1
                    resources['workers_started'] += collected.resources.get('workers_started', 0)
                    resources['peak_batch_files'] = max(resources['peak_batch_files'], len(batch))
                    resources['peak_batch_source_bytes'] = max(resources['peak_batch_source_bytes'], batch_bytes)
                    resources['peak_batch_handoff_bytes'] = max(resources['peak_batch_handoff_bytes'], collected.resources.get('collected_handoff_bytes', 0))
                    resources['owned_workers_reaped'] += sum(row['leader_reaped'] and row['group_absent'] and row['mailboxes_removed'] for row in collected.cleanup)
                    if collected.stop_reason or any(row.get('kind') != 'partial_file' for row in collected.failures):
                        raise native.StopScan(collected.stop_reason or 'collector_failed')
                    if len(collected.collected) != len(batch):
                        raise native.StopScan('collector_failed')
                    for file in collected.collected:
                        payload = file.payload()
                        digest = hashlib.sha256(encoded(payload)).hexdigest()
                        definitions, scopes = payload.pop('definitions'), payload.pop('scopes')
                        db.execute('DELETE FROM structural_symbols WHERE path=?', (file.path,))
                        db.execute('DELETE FROM structural_scopes WHERE path=?', (file.path,))
                        db.executemany('INSERT INTO structural_symbols VALUES(?,?,?,?)',
                            ((d['id'], file.path, i, encoded(d)) for i, d in enumerate(definitions)))
                        db.executemany('INSERT INTO structural_scopes VALUES(?,?,?)',
                            ((file.path, i, encoded(s)) for i, s in enumerate(scopes)))
                        db.execute('UPDATE structural_files SET ir=?,ir_sha=?,status=? WHERE path=?',
                            (encoded(payload), digest, 'partial_parse' if file.partial else 'parsed', file.path))
                        resources['changed_files_collected'] += 1
                    batch, batch_bytes = [], 0

                for supplied in paths:
                    check()
                    count += 1
                    if count > limits.max_files:
                        raise native.StopScan('file_budget_exceeded')
                    item = {'path': supplied} if type(supplied) is str else supplied
                    if (type(item) is not dict or 'path' not in item or
                            set(item) - {'path', 'language', 'kind', 'sha256', 'bytes'}):
                        raise ValueError('Inventory must contain source metadata only')
                    path = current_path = item['path']
                    if (type(path) is not str or len(path.encode()) > 4096 or '\\' in path or
                            str(PurePosixPath(path)) != path):
                        raise ValueError('Canonical relative inventory path required')
                    try:
                        SourceRoot.parts(path)
                    except OSError as error:
                        raise ValueError('Canonical relative inventory path required') from error
                    if db.execute('SELECT 1 FROM structural_seen WHERE path=?', (path,)).fetchone():
                        raise ValueError('Duplicate inventory path')
                    db.execute('INSERT INTO structural_seen VALUES(?)', (path,))
                    language = item.get('language', LANGUAGES.get(PurePosixPath(path).suffix, 'unknown'))
                    kind = item.get('kind', 'configuration' if PurePosixPath(path).name == 'go.mod' else 'source')
                    if type(language) is not str or kind not in ('source', 'configuration'):
                        raise ValueError('Invalid source metadata')
                    remaining = limits.max_source_bytes - resources['source_bytes']
                    if source.info(path).st_size > remaining:
                        raise native.StopScan('source_byte_budget_exceeded')
                    raw, digest, info = source.read(path, budget.max_file_bytes + 1, cancel=check, max_bytes=remaining)
                    resources['source_bytes'] += info.st_size
                    if ('sha256' in item and item['sha256'] != digest or 'bytes' in item and
                            (type(item['bytes']) is not int or item['bytes'] != info.st_size)):
                        raise ValueError('Source identity differs from inventory')
                    record = {'path': path, 'language': language, 'kind': kind, 'sha256': digest, 'bytes': info.st_size}
                    prior = db.execute('SELECT record,ir FROM structural_files WHERE path=?', (path,)).fetchone()
                    status = ('excluded_size' if info.st_size > budget.max_file_bytes else 'configuration' if kind == 'configuration' else
                              'unsupported_language' if language not in native.LANGUAGES else 'pending')
                    reusable = status == 'pending' and prior and json.loads(prior['record']) == record and prior['ir'] is not None
                    if reusable:
                        resources['unchanged_source_collections_reused'] += 1
                        continue
                    db.execute('INSERT OR REPLACE INTO structural_files VALUES(?,?,?,?,?,?,?,?,?)',
                        (path, str(PurePosixPath(path).parent), language, kind, encoded(record), status,
                         None, None, raw if status == 'configuration' else None))
                    if status != 'pending':
                        db.execute('DELETE FROM structural_symbols WHERE path=?', (path,))
                        db.execute('DELETE FROM structural_scopes WHERE path=?', (path,))
                        continue
                    if batch and (len(batch) >= min(limits.batch_files, budget.max_files) or batch_bytes + len(raw) > budget.max_total_bytes):
                        flush()
                    if len(raw) > budget.max_total_bytes:
                        raise native.StopScan('source_byte_budget_exceeded')
                    batch.append(dict(record, content=raw))
                    batch_bytes += len(raw)
                flush()
                for table in ('structural_files', 'structural_symbols', 'structural_scopes'):
                    db.execute('DELETE FROM ' + table + ' WHERE path NOT IN (SELECT path FROM structural_seen)')
                files = _Files(db, budget, check)
                # ponytail: re-resolve every binding until negative dependency closure is qualified in T011.
                configurations = _Configurations(db)
                resolve = native.resolver(files, configurations, definitions=_Definitions(db))
                for path in files:
                    file = files[path]
                    work = native.Work(budget, check)
                    work.facts = len(file.definitions)
                    file.emit_sites(resolve, work)
                    for i, site in enumerate(file.sites):
                        db.execute('INSERT INTO structural_sites VALUES(?,?,?,?,?)', (site['id'], path, i, site['role'], encoded(site)))
                        db.executemany('INSERT INTO structural_relationships VALUES(?,?,?,?,?)',
                            ((site['id'], target, path, site['role'], site['certainty']) for target in site['targets']))
                check()
                manifest = hashlib.sha256(encoded({'repository': self.owner, 'analyzer': analyzer, 'config': config}))
                for row in db.execute('SELECT path,record,status FROM structural_files ORDER BY path'):
                    manifest.update(encoded({'path': row['path'], 'record': json.loads(row['record']), 'status': row['status']}))
                    check()
                    record = json.loads(row['record'])
                    _, digest, info = source.read(row['path'], 0, cancel=check, max_bytes=record['bytes'])
                    if digest != record['sha256'] or info.st_size != record['bytes']:
                        raise native.StopScan('source_changed_before_publication')
                source_identity = manifest.hexdigest()
                generation = hashlib.sha256(source_identity.encode())
                for table in ('structural_symbols', 'structural_sites', 'structural_scopes'):
                    for row in db.execute('SELECT data FROM ' + table + ' ORDER BY path,ordinal'):
                        generation.update(row[0] if type(row[0]) is bytes else row[0].encode())
                coverage = dict(db.execute('SELECT status,count(*) FROM structural_files GROUP BY status'))
                receipt = {'status': 'ready', 'published': True, 'generation': generation.hexdigest(),
                    'source_identity': source_identity, 'repository_identity': self.owner,
                    'analyzer_identity': analyzer, 'config_identity': config, 'resources': resources,
                    'coverage': {'files_total': count, 'files_supported': coverage.get('parsed', 0) + coverage.get('partial_parse', 0),
                                 'files_unsupported': coverage.get('unsupported_language', 0), 'status_counts': coverage},
                    'scope': 'Persistent bounded structural foundation; scale and query defaults unqualified'}
                values = {'schema': SCHEMA, 'repository': self.owner, 'analyzer': analyzer, 'config': config,
                          'source': source_identity, 'generation': receipt['generation'], 'receipt': encoded(receipt).decode()}
                db.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)',
                               (('structural_' + key, value) for key, value in values.items()))
                with SourceRoot(self.root) as current:
                    if current.identity != self.owner:
                        raise native.StopScan('repository_replaced_before_publication')
                if analyzer_identity() != analyzer:
                    raise native.StopScan('analyzer_changed_before_publication')
                check()
                db.set_progress_handler(None, 0)
            receipt['resources']['elapsed_seconds'] = time.monotonic() - started
        except (native.BackendUnavailable, metadata.PackageNotFoundError, OSError,
                RuntimeError, sqlite3.Error, native.StopScan, MemoryError, RecursionError) as error:
            receipt = {'status': 'interrupted' if isinstance(error, (native.StopScan, InterruptedError)) and
                       str(error) in ('cancelled', 'deadline_exceeded', 'Source read cancelled') else 'failed',
                       'published': False, 'error_kind': type(error).__name__, 'reason': str(error),
                       'path': current_path, 'previous_generation': previous, 'resources': resources}
        self.last_attempt = receipt
        return receipt
