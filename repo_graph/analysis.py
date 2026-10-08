"""One persistent structural index; optional native parsing stays in owned workers."""
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
from importlib import metadata
import json
import math
import os
from pathlib import Path, PurePosixPath
import select
import signal
import sqlite3
import subprocess
import time
import uuid

from . import analysis_native as native
from .analysis_queue import QueueLimits, collect_files, _identity as queue_identity
from .search import (connect, code_identity as writer_code_identity, begin_attempt,
                     record_attempt, project_function_evidence)
from .source import SourceRoot, PublicationError

SCHEMA = 'structural-v2'
IMPACT_SCHEMA = 'captured-impact-v1'
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
                                   'queue': queue_identity(), 'writer': writer_code_identity(), 'schema': SCHEMA})).hexdigest()


def _git_capture(root, args, maximum, check):
    """Bounded fixed-argument Git observation through the held source directory."""
    env = {'PATH': os.environ.get('PATH', ''), 'GIT_CONFIG_GLOBAL': os.devnull,
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0',
           'GIT_NO_LAZY_FETCH': '1'}
    if not isinstance(root, SourceRoot) or root.fd is None:
        raise OSError('Held secure source directory required for Git capture')
    command = ['git', '--no-optional-locks', '--no-replace-objects', '-c', 'core.fsmonitor=false',
               '-c', 'core.hooksPath=' + os.devnull, '-c', 'core.quotePath=true',
               '-C', '/proc/self/fd/' + str(root.fd)]
    deadline, data = time.monotonic() + 2, bytearray()
    with subprocess.Popen(command + args, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
            pass_fds=(root.fd,)) as process:
        try:
            while True:
                check()
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise TimeoutError('Git capture deadline')
                if not select.select([process.stdout], [], [], min(remaining, 0.01))[0]: continue
                chunk = os.read(process.stdout.fileno(), min(65536, maximum + 1 - len(data)))
                if not chunk:
                    return bytes(data) if process.wait(timeout=remaining) == 0 else None
                data.extend(chunk)
                if len(data) > maximum: raise ValueError('Git capture output ceiling exhausted')
        finally:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait()


def _git_observation(root, check):
    """Capture HEAD, never a project-configured worktree-cleanliness assertion."""
    try:
        revision = _git_capture(root, ['rev-parse', '--verify', 'HEAD'], 128, check)
        if revision is None: return {'revision': None, 'dirty': None, 'knowledge': 'unknown', 'reason': 'git_revision_unavailable'}
        revision = revision.decode('ascii').strip()
        if len(revision) not in (40, 64) or any(c not in '0123456789abcdef' for c in revision):
            raise ValueError('Invalid Git revision')
        # The source digest captures dirty content. Git status may execute project filters;
        # a configured clean/dirty boolean is deliberately unobserved by source-only analysis.
        return {'revision': revision, 'dirty': None, 'knowledge': 'captured_revision',
                'reason': 'git_dirty_not_observed_without_project_commands',
                'dirty_basis': 'unobserved_repository_configured_status'}
    except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
        return {'revision': None, 'dirty': None, 'knowledge': 'unknown', 'reason': 'git_capture_unavailable_or_bounded_stop'}


def _git_changes(root, base, revision, check):
    """Capture bounded commit paths; their working-tree byte affinity is unknown."""
    result = {'status': 'not_requested', 'base_revision': base, 'current_revision': revision,
              'source_byte_affinity': 'unobserved_worktree', 'dirty': None,
              'basis': 'committed_changed_paths_current_captured_definitions'}
    if base is None:
        return result, []
    try:
        if revision is None:
            raise ValueError('Current captured commit unavailable')
        prefix = _git_capture(root, ['rev-parse', '--show-prefix'], 4096, check)
        if prefix is None or prefix.strip():
            return dict(result, status='unavailable', reason='git_source_root_not_repository_root'), []
        resolved = _git_capture(root, ['rev-parse', '--verify', base + '^{commit}'], 128, check)
        if resolved is None or resolved.decode('ascii').strip() != base:
            raise ValueError('Exact base commit unavailable')
        raw = _git_capture(root, ['diff-tree', '--no-commit-id', '--name-status', '-r', '-z',
            '--no-renames', '--no-ext-diff', '--no-textconv', base, revision, '--'], 131072, check)
        if raw is None or raw and not raw.endswith(b'\0'):
            raise ValueError('Bounded changed-path receipt unavailable')
        values = raw.split(b'\0')[:-1] if raw else []
        if len(values) % 2 or len(values) > 512:
            raise ValueError('Changed-path ceiling exhausted')
        changes = []
        for status, path in zip(values[::2], values[1::2]):
            check()
            status, path = status.decode('ascii'), path.decode('utf-8')
            if status not in ('A', 'D', 'M', 'T') or len(path.encode()) > 4096 or '\\' in path or str(PurePosixPath(path)) != path:
                raise ValueError('Unsupported changed-path receipt')
            SourceRoot.parts(path)
            changes.append({'path': path, 'status': status})
        changes.sort(key=lambda item: item['path'])
        if len({item['path'] for item in changes}) != len(changes):
            raise ValueError('Duplicate changed path')
        return dict(result, status='ready', count=len(changes),
                    changes_sha256=hashlib.sha256(encoded(changes)).hexdigest()), changes
    except (OSError, UnicodeError, ValueError, TimeoutError, subprocess.TimeoutExpired):
        return dict(result, status='unavailable', reason='git_change_capture_unavailable_or_bounded_stop'), []


def _coverage(db, count, check):
    statuses = dict(db.execute('SELECT status,count(*) FROM structural_files GROUP BY status'))
    languages, overflow = {}, {'languages': 0, 'files_total': 0, 'file_status': {}}
    for language, status, total in db.execute('SELECT language,status,count(*) FROM structural_files GROUP BY language,status ORDER BY language,status'):
        check()
        if language not in languages and len(languages) >= 32:
            if language != overflow.get('last_language'): overflow['languages'] += 1
            overflow['last_language'] = language
            group = overflow
        else:
            group = languages.setdefault(language, {'files_total': 0, 'file_status': {}})
        group['files_total'] += total
        group['file_status'][status] = total + group['file_status'].get(status, 0)
    overflow.pop('last_language', None)
    sites, errors, samples, sample_bytes = {}, 0, [], 2
    for role, data in db.execute('SELECT role,data FROM structural_sites ORDER BY path,ordinal'):
        check()
        certainty = json.loads(data)['certainty']
        group = sites.setdefault(role, {})
        group[certainty] = group.get(certainty, 0) + 1
    for path, ir in db.execute('SELECT path,ir FROM structural_files WHERE ir IS NOT NULL ORDER BY path'):
        check()
        for error in json.loads(ir)['errors']:
            errors += 1
            sample = {'path': path, 'kind': error['kind'], 'range': error['range']}
            size = len(encoded(sample)) + bool(samples)
            if len(samples) < 16 and sample_bytes + size <= 4096:
                samples.append(sample); sample_bytes += size
    return {'files_total': count, 'files_supported': statuses.get('parsed', 0) + statuses.get('partial_parse', 0),
            'files_unsupported': statuses.get('unsupported_language', 0), 'status_counts': statuses,
            'file_status': statuses, 'by_language': languages, 'language_overflow': overflow,
            'sites_by_role_certainty': sites, 'parser_error_count': errors,
            'parser_error_samples': samples, 'parser_error_samples_truncated': errors > len(samples),
            'inventory_scope': 'caller_admitted_inventory', 'discovery_skipped_files': None,
            'discovery_skip_knowledge': 'outside_admitted_inventory_not_measured'}


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
        self.consumer = None
        self.fingerprint_cache = OrderedDict()
        self.scope_fingerprints = {}

    def fingerprint(self, kind, key):
        """Bounded lookup receipts include absent paths and whole package membership."""
        self.check()
        cache_key = kind, key
        cache = self.scope_fingerprints if kind in ('inventory', 'configurations') else self.fingerprint_cache
        if cache_key in cache:
            if cache is self.fingerprint_cache:
                cache.move_to_end(cache_key)
            return cache[cache_key]
        if kind == 'file':
            rows = self.db.execute('SELECT path,record,status FROM structural_files WHERE path=?', (key,))
        elif kind == 'directory':
            rows = self.db.execute("SELECT path,record,status FROM structural_files WHERE directory=? AND language='go' AND kind='source' ORDER BY path", (key,))
        elif kind == 'configurations':
            rows = self.db.execute("SELECT path,record,status FROM structural_files WHERE kind='configuration' ORDER BY path")
        elif kind == 'inventory':
            rows = self.db.execute('SELECT path,record,status FROM structural_files ORDER BY path')
        else:
            raise RuntimeError('Unknown structural dependency kind')
        digest, present = hashlib.sha256(kind.encode()), False
        for row in rows:
            self.check()
            digest.update(encoded([row['path'], json.loads(row['record']), row['status']]))
            present = True
        result = digest.hexdigest(), present
        cache[cache_key] = result
        if len(self.fingerprint_cache) > 8:
            self.fingerprint_cache.popitem(last=False)
        return result

    def observe(self, kind, key):
        if self.consumer is not None:
            digest, present = self.fingerprint(kind, key)
            self.db.execute('INSERT OR REPLACE INTO structural_dependencies VALUES(?,?,?,?,?)',
                            (self.consumer, kind, key, digest, int(present)))

    def __iter__(self):
        return (r[0] for r in self.db.execute('SELECT path FROM structural_files WHERE ir IS NOT NULL ORDER BY path'))

    def __len__(self):
        return self.db.execute('SELECT count(*) FROM structural_files WHERE ir IS NOT NULL').fetchone()[0]

    def __contains__(self, path):
        self.observe('file', path)
        return self.db.execute('SELECT 1 FROM structural_files WHERE path=? AND ir IS NOT NULL', (path,)).fetchone() is not None

    def inventoried(self, path):
        self.observe('file', path)
        return self.db.execute('SELECT 1 FROM structural_files WHERE path=?', (path,)).fetchone() is not None

    def in_directory(self, directory):
        directory = str(PurePosixPath(directory))
        self.observe('directory', directory)
        return (r[0] for r in self.db.execute("SELECT path FROM structural_files WHERE directory=? AND language='go' AND ir IS NOT NULL ORDER BY path", (directory,)))

    def go_package(self, directory):
        directory = str(PurePosixPath(directory))
        self.observe('directory', directory)
        row = self.db.execute('SELECT name,reason FROM structural_go_packages WHERE directory=?', (directory,)).fetchone()
        return {'name': row[0] if row else None, 'qualified': row is not None and not row[1],
                'reason': row[1] if row else 'Go source package is unavailable'}

    def go_binding(self, directory, name):
        directory = str(PurePosixPath(directory))
        self.observe('directory', directory)
        row = self.db.execute('SELECT count,path FROM structural_go_bindings WHERE directory=? AND name=?', (directory, name)).fetchone()
        return tuple(row) if row else (0, None)

    def index_go_packages(self):
        self.db.execute('DELETE FROM structural_go_packages')
        self.db.execute('DELETE FROM structural_go_bindings')
        for row in self.db.execute("SELECT path,directory,ir FROM structural_files WHERE language='go' AND kind='source' ORDER BY path"):
            self.check()
            name, reason = None, 'Go package contains an unparsed or excluded source file'
            if row['ir'] is not None:
                file = self[row['path']]
                name, reason = native.go_file_scope(file)
                for symbol, entries in file.module.bindings.items():
                    count = sum(b.kind != 'import' for b in entries)
                    if count:
                        self.db.execute('''INSERT INTO structural_go_bindings VALUES(?,?,?,?)
                          ON CONFLICT(directory,name) DO UPDATE SET count=count+excluded.count''',
                          (row['directory'], symbol, count, row['path']))
            prior = self.db.execute('SELECT name,reason FROM structural_go_packages WHERE directory=?', (row['directory'],)).fetchone()
            if prior:
                reason = prior[1] or reason or ('Go package names differ in inventoried source' if prior[0] != name else '')
                name = prior[0]
            self.db.execute('INSERT OR REPLACE INTO structural_go_packages VALUES(?,?,?)', (row['directory'], name, reason))

    def __getitem__(self, path):
        self.check()
        self.observe('file', path)
        if path in self.cache:
            self.cache.move_to_end(path)
            return self.cache[path]
        row = self.db.execute('SELECT record,ir,ir_sha FROM structural_files WHERE path=? AND ir IS NOT NULL', (path,)).fetchone()
        if row is None:
            raise KeyError(path)
        payload = json.loads(row['ir'])
        payload['definitions'] = [json.loads(r[0]) for r in self.db.execute('SELECT data FROM structural_symbols WHERE path=? ORDER BY ordinal', (path,))]
        payload['scopes'] = [json.loads(r[0]) for r in self.db.execute('SELECT data FROM structural_scopes WHERE path=? ORDER BY ordinal', (path,))]
        payload['imports'] = [json.loads(r[0]) for r in self.db.execute('SELECT data FROM structural_imports WHERE path=? ORDER BY ordinal', (path,))]
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
    CREATE TABLE IF NOT EXISTS structural_imports(path TEXT NOT NULL, ordinal INTEGER NOT NULL,
      data TEXT NOT NULL, PRIMARY KEY(path,ordinal));
    CREATE TABLE IF NOT EXISTS structural_summaries(path TEXT PRIMARY KEY,
      declaration TEXT NOT NULL, export TEXT NOT NULL, body TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS structural_dependencies(path TEXT NOT NULL,
      kind TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL, present INTEGER NOT NULL,
      PRIMARY KEY(path,kind,key));
    CREATE TABLE IF NOT EXISTS structural_go_packages(directory TEXT PRIMARY KEY, name TEXT, reason TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS structural_go_bindings(directory TEXT NOT NULL, name TEXT NOT NULL,
      count INTEGER NOT NULL, path TEXT NOT NULL, PRIMARY KEY(directory,name));
    CREATE TABLE IF NOT EXISTS structural_sites(id TEXT PRIMARY KEY, path TEXT NOT NULL,
      ordinal INTEGER NOT NULL, role TEXT NOT NULL, data TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS structural_site_file ON structural_sites(path,ordinal);
    CREATE TABLE IF NOT EXISTS structural_evidence(id TEXT PRIMARY KEY, path TEXT NOT NULL,
      ordinal INTEGER NOT NULL, data TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS structural_import_relationships(id TEXT NOT NULL, path TEXT NOT NULL,
      ordinal INTEGER NOT NULL, start_byte INTEGER NOT NULL, end_byte INTEGER NOT NULL,
      target_path TEXT NOT NULL, certainty TEXT NOT NULL, data TEXT NOT NULL,
      PRIMARY KEY(id,target_path));
    CREATE INDEX IF NOT EXISTS structural_reverse_import ON structural_import_relationships(target_path,path,start_byte,end_byte,id);
    CREATE TABLE IF NOT EXISTS structural_git_changes(path TEXT PRIMARY KEY,status TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS structural_relationships(site_id TEXT NOT NULL, target_id TEXT NOT NULL,
      path TEXT NOT NULL, role TEXT NOT NULL, certainty TEXT NOT NULL,
      PRIMARY KEY(site_id,target_id));
    CREATE INDEX IF NOT EXISTS structural_target ON structural_relationships(target_id,site_id);
    ''')
    # Internal occurrence columns are query projections of these same facts.
    for table, columns in (
        ('structural_symbols', {'start_byte': 'INTEGER NOT NULL DEFAULT 0', 'end_byte': 'INTEGER NOT NULL DEFAULT 0'}),
        ('structural_relationships', {'caller_id': "TEXT NOT NULL DEFAULT ''", 'site_start': 'INTEGER NOT NULL DEFAULT 0',
         'site_end': 'INTEGER NOT NULL DEFAULT 0', 'target_path': "TEXT NOT NULL DEFAULT ''",
         'target_start': 'INTEGER NOT NULL DEFAULT -1', 'target_end': 'INTEGER NOT NULL DEFAULT -1'})):
        present = {row['name'] for row in db.execute('PRAGMA table_info(' + table + ')')}
        for name, definition in columns.items():
            if name not in present:
                db.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + name + ' ' + definition)
    db.executescript('''
    CREATE INDEX IF NOT EXISTS structural_symbol_order ON structural_symbols(path,start_byte,end_byte,id);
    CREATE INDEX IF NOT EXISTS structural_outgoing ON structural_relationships(caller_id,path,site_start,site_end,role,target_path,target_start,target_end,site_id,target_id);
    CREATE INDEX IF NOT EXISTS structural_incoming ON structural_relationships(target_id,path,site_start,site_end,role,target_path,target_start,target_end,site_id);
    CREATE INDEX IF NOT EXISTS structural_occurrence_order ON structural_relationships(path,site_start,site_end,role,target_path,target_start,target_end,site_id,target_id);
    CREATE INDEX IF NOT EXISTS structural_unassigned_occurrence ON structural_relationships(path,target_id,site_start,site_end,role,target_path,target_start,target_end,site_id);
    ''')


class StructuralIndex:
    def __init__(self, root, output, *, budget=None, limits=None, framework_context=None):
        self.root, self.output = Path(root), Path(output)
        self.budget, self.limits = budget or native.Budget(), limits or IndexLimits()
        if type(self.budget) is not native.Budget or type(self.limits) is not IndexLimits:
            raise ValueError('Typed structural index limits required')
        self.framework_context = native.django_registration_context(
            framework_context, native.Work(self.budget, None))
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
        ordinary = db.execute("SELECT value FROM meta WHERE key='repository'").fetchone()
        if ordinary and ordinary[0] != self.owner:
            raise RuntimeError('Shared index belongs to another repository')
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
                  'scopes': 'structural_scopes', 'imports': 'structural_imports', 'relationships': 'structural_relationships',
                  'evidence': 'structural_evidence'}
        if kind not in tables:
            raise ValueError('Unknown structural fact kind')
        with closing(connect(self.output, readonly=True, owner=self.output_owner)) as db:
            self._metadata(db)
            if kind == 'relationships':
                for row in db.execute("SELECT site_id,target_id,path,role,certainty FROM structural_relationships WHERE target_id<>'' ORDER BY site_id,target_id"):
                    yield dict(row)
            else:
                for row in db.execute('SELECT path,data FROM ' + tables[kind] + ' ORDER BY path,ordinal'):
                    data = json.loads(row['data'])
                    if kind == 'scopes':
                        data['path'] = row['path']
                    yield data

    def refresh(self, paths, *, mode='serial', concurrency=1, cancel=None, evidence_directory=None, git_base=None):
        if (mode not in ('serial', 'queued') or type(concurrency) is not int or
                not 1 <= concurrency <= 4 or mode == 'serial' and concurrency != 1 or
                cancel is not None and not callable(cancel) or isinstance(paths, (str, bytes))):
            raise ValueError('Explicit serial/queued mode, bounded workers and inventory iterable required')
        if git_base is not None and (type(git_base) is not str or len(git_base) not in (40, 64) or
                any(c not in '0123456789abcdef' for c in git_base)):
            raise ValueError('Git base must be an exact lowercase commit identity')
        started, limits, budget = time.monotonic(), self.limits, self.budget
        resources = {'batches': 0, 'changed_files_collected': 0,
                     'unchanged_source_collections_reused': 0, 'source_bytes': 0,
                     'workers_started': 0, 'peak_batch_files': 0, 'peak_batch_source_bytes': 0,
                     'peak_batch_handoff_bytes': 0, 'owned_workers_reaped': 0,
                     'bindings_files_resolved': 0, 'bindings_files_reused': 0,
                     'unknown_closure_files_rebuilt': 0, 'dependency_lookups_checked': 0}
        previous, current_path, collection_failures = None, None, []
        attempt, receipt = None, None

        def check():
            if cancel is not None and cancel():
                raise native.StopScan('cancelled')
            if time.monotonic() - started >= limits.total_wall_seconds:
                raise native.StopScan('deadline_exceeded')
            return False

        try:
            analyzer = analyzer_identity()
            collection_config = hashlib.sha256(encoded({'budget': asdict(budget), 'limits': asdict(limits),
                                             'versions': native.PINS, 'schema': SCHEMA,
                                             'impact_schema': IMPACT_SCHEMA})).hexdigest()
            config = hashlib.sha256(encoded({'collection': collection_config,
                                             'framework_context': self.framework_context})).hexdigest()
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
                previous = old.get('structural_generation')
                attempt = {'attempt_id': uuid.uuid4().hex, 'status': 'updating',
                           'started_at': time.time(), 'repository_identity': self.owner,
                           'previous_generation': previous}
                begin_attempt(self.output, self.output_owner, 'structural', attempt)
                if {name: metadata.version(name) for name in native.PINS} != native.PINS:
                    raise native.BackendUnavailable('Optional backend versions differ from qualified pins')
                git_before = _git_observation(source, check)
                git_change, changes = _git_changes(source, git_base, git_before['revision'], check)
                _schema(db)
                db.execute('DELETE FROM structural_git_changes')
                db.executemany('INSERT INTO structural_git_changes VALUES(?,?)',
                               ((item['path'], item['status']) for item in changes))
                page_size = db.execute('PRAGMA page_size').fetchone()[0]
                pages = db.execute('PRAGMA max_page_count=' + str(max(1, limits.max_index_bytes // page_size))).fetchone()[0]
                if pages * page_size > limits.max_index_bytes:
                    raise native.StopScan('index_byte_budget_exceeded')
                db.set_progress_handler(lambda: int(check()), 1000)
                if old.get('structural_analyzer') != analyzer or old.get('structural_collection_config') != collection_config:
                    for table in ('structural_files', 'structural_symbols', 'structural_scopes', 'structural_imports',
                                  'structural_summaries', 'structural_dependencies', 'structural_sites', 'structural_relationships',
                                  'structural_import_relationships', 'structural_evidence'):
                        db.execute('DELETE FROM ' + table)
                    resources['invalidation_reason'] = 'analyzer_or_limits_changed_or_initial_index'
                else:
                    resources['invalidation_reason'] = 'source_and_positive_negative_lookup_dependencies'
                db.execute('CREATE TEMP TABLE structural_seen(path TEXT PRIMARY KEY)')
                db.execute('CREATE TEMP TABLE structural_dirty(path TEXT PRIMARY KEY)')
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
                    collection_failures.extend(row for row in collected.failures if row.get('kind') != 'partial_file')
                    if collected.stop_reason or any(row.get('kind') != 'partial_file' for row in collected.failures):
                        raise native.StopScan(collected.stop_reason or 'collector_failed')
                    if len(collected.collected) != len(batch):
                        raise native.StopScan('collector_failed')
                    for file in collected.collected:
                        payload = file.payload()
                        digest = hashlib.sha256(encoded(payload)).hexdigest()
                        definitions, scopes, imports = payload.pop('definitions'), payload.pop('scopes'), payload.pop('imports')
                        declaration = [{k: d[k] for k in ('id', 'name', 'kind', 'callable', 'range')} for d in definitions]
                        exported = [d for d, source_definition in zip(declaration, definitions) if file.language not in ('javascript', 'typescript', 'go') or
                            (file.language == 'go' and d['name'][:1].isupper()) or
                            (file.language in ('javascript', 'typescript') and source_definition['text'].startswith('export '))]
                        db.execute('INSERT OR REPLACE INTO structural_summaries VALUES(?,?,?,?)',
                            (file.path, hashlib.sha256(encoded([declaration, scopes, imports])).hexdigest(),
                             hashlib.sha256(encoded(exported)).hexdigest(),
                             file.record['sha256']))
                        db.execute('DELETE FROM structural_symbols WHERE path=?', (file.path,))
                        db.execute('DELETE FROM structural_scopes WHERE path=?', (file.path,))
                        db.execute('DELETE FROM structural_imports WHERE path=?', (file.path,))
                        db.execute('DELETE FROM structural_evidence WHERE path=?', (file.path,))
                        db.executemany('INSERT INTO structural_symbols VALUES(?,?,?,?,?,?)',
                            ((d['id'], file.path, i, encoded(d), d['range']['start_byte'], d['range']['end_byte']) for i, d in enumerate(definitions)))
                        db.executemany('INSERT INTO structural_scopes VALUES(?,?,?)',
                            ((file.path, i, encoded(s)) for i, s in enumerate(scopes)))
                        db.executemany('INSERT INTO structural_imports VALUES(?,?,?)',
                            ((file.path, i, encoded(s)) for i, s in enumerate(imports)))
                        for ordinal, row in enumerate(file.syntax_metadata['python_assignments']):
                            span = row['range']
                            identifier = f'{file.path}:{span["start_byte"]}:{span["end_byte"]}:assignment'
                            data = dict(id=identifier, path=file.path, language=file.language,
                                text=row['text'], range=span, provenance=dict(source_sha256=file.record['sha256'],
                                rule_version=native.RULE_VERSION, syntax_kind=row['kind'], evidence_kind='static_syntax'))
                            db.execute('INSERT INTO structural_evidence VALUES(?,?,?,?)',
                                (identifier, file.path, ordinal, encoded(data)))
                        db.execute('UPDATE structural_files SET ir=?,ir_sha=?,status=? WHERE path=?',
                            (encoded(payload), digest, 'partial_parse' if file.partial else 'parsed', file.path))
                        resources['changed_files_collected'] += 1
                    batch, batch_bytes = [], 0

                for supplied in paths:
                    check()
                    count += 1
                    resources['inventory_entries_consumed'] = count
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
                    if type(language) is not str or not 1 <= len(language.encode()) <= 128 or kind not in ('source', 'configuration'):
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
                    db.execute('INSERT OR IGNORE INTO structural_dirty VALUES(?)', (path,))
                    db.execute('INSERT OR REPLACE INTO structural_files VALUES(?,?,?,?,?,?,?,?,?)',
                        (path, str(PurePosixPath(path).parent), language, kind, encoded(record), status,
                         None, None, raw if status == 'configuration' else None))
                    if status != 'pending':
                        db.execute('DELETE FROM structural_symbols WHERE path=?', (path,))
                        db.execute('DELETE FROM structural_scopes WHERE path=?', (path,))
                        db.execute('DELETE FROM structural_imports WHERE path=?', (path,))
                        db.execute('DELETE FROM structural_evidence WHERE path=?', (path,))
                        db.execute('DELETE FROM structural_summaries WHERE path=?', (path,))
                        continue
                    if batch and (len(batch) >= min(limits.batch_files, budget.max_files) or batch_bytes + len(raw) > budget.max_total_bytes):
                        flush()
                    if len(raw) > budget.max_total_bytes:
                        raise native.StopScan('source_byte_budget_exceeded')
                    batch.append(dict(record, content=raw))
                    batch_bytes += len(raw)
                flush()
                if old.get('structural_config') != config:
                    # Enrollment affects binding identity, never target-free syntax.
                    db.execute('INSERT OR IGNORE INTO structural_dirty SELECT path FROM structural_files')
                for table in ('structural_files', 'structural_symbols', 'structural_scopes', 'structural_imports',
                              'structural_summaries', 'structural_dependencies', 'structural_sites', 'structural_relationships',
                              'structural_import_relationships', 'structural_evidence'):
                    db.execute('DELETE FROM ' + table + ' WHERE path NOT IN (SELECT path FROM structural_seen)')
                files = _Files(db, budget, check)
                files.index_go_packages()
                for row in db.execute('SELECT * FROM structural_dependencies ORDER BY kind,key,path'):
                    check()
                    resources['dependency_lookups_checked'] += 1
                    if files.fingerprint(row['kind'], row['key'])[0] != row['fingerprint']:
                        db.execute('INSERT OR IGNORE INTO structural_dirty VALUES(?)', (row['path'],))
                for table in ('structural_sites', 'structural_relationships', 'structural_dependencies', 'structural_import_relationships'):
                    db.execute('DELETE FROM ' + table + ' WHERE path IN (SELECT path FROM structural_dirty) OR path IN (SELECT path FROM structural_files WHERE ir IS NULL)')
                configurations = _Configurations(db)
                for path in files:
                    if db.execute('SELECT 1 FROM structural_dirty WHERE path=?', (path,)).fetchone() is None:
                        resources['bindings_files_reused'] += 1
                        continue
                    files.consumer = path
                    files.observe('configurations', '*')
                    file = files[path]
                    # Per-file resolver caches cannot conceal a dependency of the next consumer.
                    resolve = native.resolver(files, configurations, definitions=_Definitions(db),
                                              framework_context=self.framework_context)
                    work = native.Work(budget, check)
                    work.facts = file.collected_fact_count
                    unknown_import = False
                    for ordinal, item in enumerate(file.imports):
                        check()
                        targets, _, reason = native.module_paths(file, item, files, configurations)
                        if file.partial:
                            targets, reason = [], 'partial import source; candidates unavailable'
                        targets = sorted(set(targets))
                        partial_target = any(files[target].partial for target in targets)
                        certainty = 'unresolved' if not targets else 'resolved' if len(targets) == 1 and not partial_target and file.language != 'go' else 'candidate'
                        unknown_import |= not targets
                        span = item['range']
                        identifier = 'import:' + hashlib.sha256(encoded([path, ordinal, span])).hexdigest()
                        data = {'id': identifier, 'path': path, 'range': span,
                            'source_sha256': item['provenance']['source_sha256'], 'language': file.language,
                            'certainty': certainty, 'targets_exhaustive': False,
                            'source_candidates_exhaustive': not file.partial and not partial_target and bool(targets),
                            'reason': reason or ('Go package source membership; build/runtime selection unqualified' if file.language == 'go' else
                                                'partial imported source; runtime selection unqualified' if partial_target else
                                                'multiple admitted source candidates; runtime selection unqualified' if len(targets) > 1 else
                                                'admitted import source; runtime selection unqualified'),
                            'provenance': item['provenance'], 'scope': 'admitted_source_candidates_runtime_unqualified'}
                        for target in targets or ['']:
                            work.fact()
                            db.execute('INSERT INTO structural_import_relationships VALUES(?,?,?,?,?,?,?,?)',
                                (identifier, path, ordinal, span['start_byte'], span['end_byte'], target, certainty, encoded(data)))
                    file.emit_sites(resolve, work)
                    if file.partial or unknown_import or any(site['certainty'] == 'unresolved' for site in file.sites):
                        # ponytail: unknown closure rebuilds this consumer against the complete
                        # admitted inventory on change; qualify finer dependency rules later.
                        files.observe('inventory', '*')
                        resources['unknown_closure_files_rebuilt'] += 1
                    resources['bindings_files_resolved'] += 1
                    for i, site in enumerate(file.sites):
                        db.execute('INSERT INTO structural_sites VALUES(?,?,?,?,?)', (site['id'], path, i, site['role'], encoded(site)))
                        for target in site['targets'] or ['']:
                            definition = _Definitions(db)[target] if target else None
                            span = definition['range'] if definition else {'start_byte': -1, 'end_byte': -1}
                            db.execute('INSERT INTO structural_relationships VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                                (site['id'], target, path, site['role'], site['certainty'], site['caller'] or '',
                                 site['range']['start_byte'], site['range']['end_byte'],
                                 definition['path'] if definition else '', span['start_byte'], span['end_byte']))
                files.consumer = None
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
                for table in ('structural_symbols', 'structural_sites', 'structural_scopes', 'structural_imports', 'structural_evidence'):
                    for row in db.execute('SELECT data FROM ' + table + ' ORDER BY path,ordinal'):
                        generation.update(row[0] if type(row[0]) is bytes else row[0].encode())
                generation.update(IMPACT_SCHEMA.encode())
                for row in db.execute('SELECT target_path,data FROM structural_import_relationships ORDER BY path,ordinal,target_path'):
                    check()
                    generation.update(encoded([row['target_path'], json.loads(row['data'])]))
                git_after = _git_observation(source, check)
                if git_before != git_after:
                    git_after = {'revision': None, 'dirty': None, 'knowledge': 'unknown', 'reason': 'git_changed_during_capture'}
                    git_change, changes = dict(git_change, status='unavailable', current_revision=None,
                                               reason='git_changed_during_capture'), []
                    db.execute('DELETE FROM structural_git_changes')
                receipt = {'status': 'ready', 'published': True, 'generation': generation.hexdigest(),
                    'source_identity': source_identity, 'repository_identity': self.owner,
                    'analyzer_identity': analyzer, 'config_identity': config, 'resources': resources,
                    'framework_enrollment': self.framework_context,
                    'coverage': _coverage(db, count, check),
                    'versions': {'schema': SCHEMA, 'rules': native.RULE_VERSION, 'grammars': dict(native.PINS)},
                    'revision_dirty': dict(git_after, content_identity=source_identity),
                    'scope': 'Persistent bounded structural foundation; scale and query defaults unqualified'}
                base_snapshot = None
                prior_receipt = json.loads(old.get('structural_receipt', '{}'))
                prior_impact = json.loads(old.get('structural_impact_receipt', '{}'))
                if (git_base is not None and prior_impact.get('git_change', {}).get('base_revision') == git_base and
                        prior_impact.get('repository_identity') == self.owner):
                    base_snapshot = prior_impact.get('base_snapshot')
                if git_base is not None and prior_receipt.get('revision_dirty', {}).get('revision') == git_base:
                    base_snapshot = {key: old.get('structural_' + name) for key, name in
                        (('repository_identity', 'repository'), ('source_identity', 'source'), ('analyzer_identity', 'analyzer'),
                         ('config_identity', 'config'), ('generation', 'generation'))}
                impact = {'schema': IMPACT_SCHEMA, 'repository_identity': self.owner,
                    'source_identity': source_identity, 'analyzer_identity': analyzer, 'config_identity': config,
                    'generation': receipt['generation'], 'revision_dirty': receipt['revision_dirty'],
                    'git_change': git_change, 'base_snapshot': base_snapshot,
                    'historical_call_closure': 'unavailable_current_index_only', 'contracts_available': False}
                impact['identity'] = hashlib.sha256(encoded(impact)).hexdigest()
                receipt['impact_identity'] = impact['identity']
                db.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)',
                    (('structural_impact_schema', IMPACT_SCHEMA), ('structural_impact_receipt', encoded(impact).decode())))
                project_function_evidence(db, {
                    'repository_identity': self.owner, 'source_identity': source_identity,
                    'structural_generation': receipt['generation'],
                    'analyzer_identity': analyzer, 'config_identity': config}, check=check)
                values = {'schema': SCHEMA, 'repository': self.owner, 'analyzer': analyzer, 'config': config,
                          'collection_config': collection_config,
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
            if isinstance(error, PublicationError) and error.owner == self.output_owner and error.artifact == 'search.db':
                receipt.update(status='publication_uncertain', published=True, durability='unconfirmed',
                               error_kind=type(error).__name__, reason=str(error))
            else:
                receipt = {'status': 'interrupted' if isinstance(error, (native.StopScan, InterruptedError)) and
                       str(error) in ('cancelled', 'deadline_exceeded', 'Source read cancelled') else 'failed',
                       'published': False, 'error_kind': type(error).__name__, 'reason': str(error),
                       'path': current_path, 'previous_generation': previous, 'resources': resources,
                       'collection_failures': collection_failures,
                       'remaining_inventory_status': 'not_evaluated_after_failure',
                       'published_coverage_generation': previous}
        except BaseException as error:
            receipt = {'status': 'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                       'published': False, 'error_kind': type(error).__name__, 'reason': str(error)[:1024],
                       'previous_generation': previous, 'published_coverage_generation': previous,
                       'remaining_inventory_status': 'not_evaluated_after_failure', 'resources': resources}
            raise
        finally:
            if receipt is not None:
                self.last_attempt = receipt
            if attempt is not None and receipt is not None:
                captured = dict(receipt)
                if 'collection_failures' in captured:
                    samples, size = [], 0
                    for failure in captured['collection_failures']:
                        length = len(encoded(failure))
                        if len(samples) < 20 and size + length <= 4096:
                            samples.append(failure); size += length
                    captured.update(collection_failures=samples,
                        collection_failures_count=len(receipt['collection_failures']),
                        collection_failures_truncated=len(samples) != len(receipt['collection_failures']))
                if captured.get('reason'): captured['reason'] = captured['reason'][:1024]
                terminal = dict(attempt, status=receipt['status'], finished_at=time.time(),
                                published=receipt['published'], receipt=captured)
                if receipt.get('reason'):
                    terminal['reason'] = receipt['reason'][:1024]
                try:
                    record_attempt(self.output, self.output_owner, 'structural', terminal,
                                   expected=attempt['attempt_id'])
                except Exception as error:
                    # Published facts remain authoritative; a failed terminal write is observable.
                    receipt['attempt_record_error'] = type(error).__name__
        self.last_attempt = receipt
        return receipt
