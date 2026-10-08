"""Bounded, preselection incremental experiment using one collector/resolver.

Only unchanged source collection is cached. Every update re-resolves all admitted
files, including negative lookups, because dependency closure is not qualified.
Digest validation reads are measured separately from actual changed-file parses.
This is not a persistent product index or acceptance of a structural owner.
"""
import hashlib
from importlib import metadata
from pathlib import Path, PurePosixPath
import time

from repo_graph.source import SourceRoot
from evaluations.bounded_queries import Snapshot, encoded
from evaluations.tree_sitter_baseline import (
    BackendUnavailable, Budget, CollectedFile, PINS, StopScan,
    collector_identity, resolve_collected,
)


class Candidate:
    def __init__(self, root, *, budget=None):
        self.root, self.budget = Path(root), budget or Budget()
        with SourceRoot(self.root) as source:
            self.owner = source.identity
        self._cache, self._snapshot = {}, None
        self.status, self.last_attempt = 'uninitialized', None

    @property
    def snapshot(self):
        return self._snapshot

    def refresh(self, paths, *, mode='serial', concurrency=1, cancel=None, evidence_directory=None):
        """Publish only after a complete staged scan and digest revalidation.

        ponytail: re-resolve the admitted graph on every update; replace this
        measured ceiling only after resolution-dependency closure is qualified.
        Cancellation/failure keeps the previous snapshot and cache unchanged.
        """
        from evaluations.queued_collector import QueueLimits, collect_files
        started = time.monotonic()
        budget = self.budget
        suffixes = {'.py': 'python', '.go': 'go', '.js': 'javascript', '.jsx': 'javascript', '.ts': 'typescript', '.tsx': 'typescript'}
        specifications = []
        for index, supplied in enumerate(paths):
            if index >= budget.max_files:
                raise ValueError('Incremental inventory file budget exceeded')
            if type(supplied) is str:
                supplied = {'path': supplied}
            if type(supplied) is not dict or 'path' not in supplied or not set(supplied) <= {'path', 'language', 'kind', 'sha256', 'bytes'}:
                raise ValueError('Inventory must contain source metadata only')
            path = supplied['path']
            if type(path) is not str or len(path.encode('utf-8')) > 4096 or str(PurePosixPath(path)) != path or '\\' in path:
                raise ValueError('Noncanonical incremental source path')
            SourceRoot.parts(path)
            record = dict(supplied)
            record.setdefault('language', suffixes.get(PurePosixPath(path).suffix, 'unknown'))
            record.setdefault('kind', 'configuration' if PurePosixPath(path).name == 'go.mod' else 'source')
            if record['kind'] not in ('source', 'configuration') or type(record['language']) is not str:
                raise ValueError('Invalid source metadata')
            specifications.append(record)
        if len({item['path'] for item in specifications}) != len(specifications):
            raise ValueError('Duplicate source inventory path')
        specifications.sort(key=lambda item: item['path'])
        self.status = 'updating'
        collector = None
        receipts, records, configurations, reused, staged, collected = [], [], {}, [], {}, []
        validation_failures = []
        queued, resolved = None, None
        read_bytes, validations, source_bytes = 0, 0, 0

        def check_deadline():
            if cancel is not None and cancel():
                raise StopScan('cancelled')
            if time.monotonic() - started >= budget.timeout_seconds:
                raise StopScan('deadline_exceeded')
            return False

        def check():
            check_deadline()
            if collector_identity() != collector:
                raise StopScan('collector_changed_during_update')

        def read_one(source, item):
            nonlocal read_bytes, validations, source_bytes
            path = item['path']
            info = source.info(path)
            if info.st_size > budget.max_file_bytes or source_bytes + info.st_size > budget.max_total_bytes:
                raise StopScan('source_byte_budget_exceeded')
            raw, sha, info = source.read(path, budget.max_file_bytes + 1, hash_full=False)
            read_bytes += len(raw); validations += 1
            if len(raw) != info.st_size or len(raw) > budget.max_file_bytes:
                raise StopScan('source_byte_budget_exceeded')
            if 'sha256' in item and item['sha256'] != sha or 'bytes' in item and (type(item['bytes']) is not int or item['bytes'] != len(raw)):
                raise ValueError('Source identity differs from inventory')
            source_bytes += len(raw)
            record = {key: item[key] for key in ('path', 'language', 'kind')}
            record.update(sha256=sha, bytes=len(raw)); records.append(record)
            if item['kind'] == 'configuration':
                raw.decode('utf-8'); configurations[path] = raw
                receipts.append(dict(record, status='configuration'))
            elif item['language'] not in PINS_LANGUAGES:
                receipts.append(dict(record, status='unsupported_language'))
            else:
                previous = self._cache.get(path)
                if previous is not None and previous[0] == record and previous[3] == collector:
                    file = CollectedFile.from_json(previous[1], record, previous[2], budget, check_deadline)
                    collected.append(file); staged[path] = previous; reused.append(path)
                    receipts.append(dict(record, status='partial_parse' if file.partial else 'parsed'))
                else:
                    return dict(record, content=raw)

        def blobs(source):
            for item in specifications:
                try:
                    check()
                    supplied = read_one(source, item)
                    if supplied is not None:
                        yield supplied
                except (OSError, ValueError, StopScan, MemoryError, RecursionError) as error:
                    receipts.append({key: item[key] for key in ('path', 'language', 'kind')} |
                        {'status': str(error) if isinstance(error, StopScan) else 'source_error', 'error_kind': type(error).__name__})
                    raise

        try:
            collector = collector_identity()
            if {name: metadata.version(name) for name in PINS} != PINS:
                raise BackendUnavailable('Optional backend versions differ from qualified pins')
            with SourceRoot(self.root) as source:
                if source.identity != self.owner:
                    raise ValueError('Repository ownership changed; cache reuse refused')
                check()
                remaining = budget.timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise StopScan('deadline_exceeded')
                queued = collect_files(blobs(source), mode=mode, concurrency=concurrency,
                    budget=budget, cancel=check_deadline, evidence_directory=evidence_directory,
                    limits=QueueLimits(total_wall_seconds=min(60.0, remaining),
                                       worker_wall_seconds=min(30.0, remaining)))
                for file in queued.collected:
                    receipts.append(dict(file.record, status='partial_parse' if file.partial else 'parsed'))
                if queued.status != 'complete' or queued.failures:
                    raise StopScan(queued.stop_reason or 'collector_failed')
                if any(receipt['status'] == 'unsupported_language' for receipt in receipts):
                    raise StopScan('unsupported_language_in_admitted_scope')
                for file in queued.collected:
                    check()
                    payload = file.to_json(budget, check_deadline)
                    staged[file.path] = (dict(file.record), payload, hashlib.sha256(payload).hexdigest(), collector)
                    collected.append(file)
                if sum(len(entry[1]) for entry in staged.values()) > budget.max_collected_bytes:
                    raise StopScan('collected_byte_budget_exceeded')
                collected.sort(key=lambda file: file.path)
                resolved = resolve_collected(collected, configurations, budget, check_deadline)
                if resolved['status'] != 'complete':
                    raise StopScan(resolved.get('stop_reason') or 'resolution_partial')
                if any(file.partial for file in collected):
                    raise StopScan('partial_parse')
                identity = hashlib.sha256(encoded({'owner': self.owner, 'records': records, 'collector': collector, 'versions': PINS})).hexdigest()
                snapshot = Snapshot(resolved['facts'], identity, collector)
                check()
            result = {'status': 'complete', 'generation': snapshot.generation, 'source_identity': identity,
                'semantic_facts_sha256': hashlib.sha256(encoded(resolved['facts'])).hexdigest(),
                'counts': {key: len(value) for key, value in resolved['facts'].items()},
                'inventory': sorted(receipts, key=lambda item: item['path']),
                'resources': {'source_bytes': source_bytes, 'digest_read_bytes': read_bytes,
                    'digest_read_operations': validations, 'changed_files_collected': len(queued.collected),
                    'unchanged_source_collections_reused': len(reused), 'all_admitted_bindings_reresolved': True,
                    'queued': queued.resources, 'resolve': resolved['resources'], 'elapsed_seconds': time.monotonic() - started},
                'cleanup': queued.cleanup, 'mode': mode, 'concurrency': concurrency,
                'scope': 'Finite incremental candidate; no persistent product or scale qualification'}
            check()
            # One publication point, after receipt construction and all checks.
            with SourceRoot(self.root) as current:
                if current.identity != self.owner:
                    raise StopScan('repository_replaced_before_publication')
                # Revalidate after Snapshot and receipt staging, at the publication boundary.
                for record in records:
                    try:
                        check()
                        raw, sha, info = current.read(record['path'], budget.max_file_bytes + 1, hash_full=False)
                        read_bytes += len(raw); validations += 1
                        if sha != record['sha256'] or len(raw) != record['bytes'] or len(raw) != info.st_size:
                            raise StopScan('source_changed_before_publication')
                    except (OSError, ValueError, RuntimeError, StopScan, MemoryError, RecursionError) as error:
                        validation_failures.append({'path': record['path'], 'error_kind': type(error).__name__,
                            'status': str(error) if isinstance(error, StopScan) else 'source_validation_error'})
                        raise
                result['resources'].update(digest_read_bytes=read_bytes,
                    digest_read_operations=validations, elapsed_seconds=time.monotonic() - started)
                check()
                self._cache, self._snapshot, self.status = staged, snapshot, 'ready'
        except (BackendUnavailable, metadata.PackageNotFoundError, OSError, ValueError, RuntimeError, StopScan, MemoryError, RecursionError) as error:
            self.status = 'interrupted' if isinstance(error, StopScan) and str(error) in ('cancelled', 'deadline_exceeded') else 'failed'
            result = {'status': self.status, 'error_kind': type(error).__name__,
                'stop_reason': str(error) if isinstance(error, StopScan) else None,
                'previous_generation': self._snapshot.generation if self._snapshot else None,
                'inventory': sorted(receipts, key=lambda item: item['path']),
                'validation_failures': validation_failures,
                'remaining_inventory': [item['path'] for item in specifications if item['path'] not in {r['path'] for r in receipts}],
                'resources': {'digest_read_bytes': read_bytes, 'digest_read_operations': validations,
                              'elapsed_seconds': time.monotonic() - started},
                'mode': mode, 'concurrency': concurrency, 'cache_or_ready_snapshot_published': False}
            if queued is not None:
                result.update(collector_failures=queued.failures, cleanup=queued.cleanup)
                result['resources']['queued'] = queued.resources
            if resolved is not None:
                result['resolution_errors'] = resolved['errors']
                result['resources']['resolve'] = resolved['resources']
        self.last_attempt = result
        return result


PINS_LANGUAGES = {'python', 'go', 'javascript', 'typescript'}
