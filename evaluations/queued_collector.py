"""Finite, experimental owned-process collection; no resolution or selection.

Both modes use the same persistent mailbox protocol: serial has one worker;
queued has an explicit one to four. Inputs are immutable source-only blobs.
The caller owns guarded source admission, one writer and global resolution.

Limits below are unmeasured defaults, not capacity claims. Linux /proc descriptor
paths, no-follow opens, new sessions and rlimits are required; other hosts fail
closed. The parent bounds wall time and JSON-equivalent retained/in-flight
bytes; a worker bounds address space, cumulative CPU and individual file size.
These are not a parent RSS bound. A caller's blocking iterator/cancellation
callback and a blocked filesystem operation cannot be interrupted here.
Source-bearing mailboxes are removed; retained receipts/logs contain metadata.
"""
from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import signal
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
_LOADED_CONTROLLER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
sys.path.insert(0, str(ROOT))
from repo_graph.source import SourceRoot, DESCRIPTOR_OPENS

CONTROL_BYTES = 16 * 1024
CODE_BYTES = 2 * 1024 * 1024
MAILBOXES = ('control.json', 'ready.json', 'request.json', 'source.bin',
             'payload.json', 'result.json')
IMPLEMENTATIONS = ('evaluations/queued_collector.py',
                   'evaluations/tree_sitter_baseline.py',
                   'evaluations/engine_checks.py', 'evaluations/analysis.py',
                   'evaluations/acceptance.py', 'repo_graph/source.py', 'repo_graph/__init__.py')


@dataclass(frozen=True)
class QueueLimits:
    max_request_bytes: int = 16 * 1024
    max_result_bytes: int = 8 * 1024 * 1024
    max_inflight_bytes: int = 40 * 1024 * 1024
    max_admitted_bytes: int = 32 * 1024 * 1024
    memory_bytes: int = 512 * 1024 * 1024
    cpu_seconds: int = 30
    worker_wall_seconds: float = 30.0
    total_wall_seconds: float = 60.0
    log_bytes: int = 1024 * 1024

    def __post_init__(self):
        integers = (self.max_request_bytes, self.max_result_bytes,
                    self.max_inflight_bytes, self.max_admitted_bytes,
                    self.memory_bytes, self.cpu_seconds, self.log_bytes)
        if (any(type(v) is not int or v <= 0 for v in integers) or
                self.max_request_bytes > CONTROL_BYTES or
                self.max_result_bytes > 64 * 1024 * 1024 or
                self.max_inflight_bytes > 256 * 1024 * 1024 or
                self.max_admitted_bytes > 256 * 1024 * 1024 or
                self.memory_bytes > 4 * 1024 * 1024 * 1024 or
                self.log_bytes > 16 * 1024 * 1024 or
                self.cpu_seconds > 660 or
                any(type(v) not in (int, float) or not math.isfinite(v) or
                    not 0 < v <= 660 for v in
                    (self.worker_wall_seconds, self.total_wall_seconds))):
            raise ValueError('Finite typed queue limits required')


@dataclass
class CollectionResult:
    collected: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    resources: dict = field(default_factory=dict)
    cleanup: list = field(default_factory=list)
    status: str = 'failed'
    stop_reason: object = None


class PoolStopped(RuntimeError):
    pass


def _baseline():
    from evaluations import tree_sitter_baseline
    return tree_sitter_baseline


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _encoded(value, cap):
    chunks, size = [], 0
    for part in json.JSONEncoder(ensure_ascii=True, separators=(',', ':'),
                                 allow_nan=False).iterencode(value):
        raw = part.encode('utf-8')
        size += len(raw)
        if size > cap:
            raise ValueError('Queue JSON byte budget exceeded')
        chunks.append(raw)
    return b''.join(chunks)


def _decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate queue JSON key')
            result[key] = value
        return result
    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('Nonfinite queue JSON number')
        return number
    return json.loads(raw, object_pairs_hook=pairs, parse_float=finite,
                      parse_constant=finite)


def _read(guarded, path, cap):
    raw, digest, info = guarded.read(path, cap + 1, hash_full=False)
    if len(raw) > cap or len(raw) != info.st_size:
        raise ValueError('Queue input byte budget exceeded')
    return raw, digest


def _write(guarded, path, raw, cap):
    if type(raw) is not bytes or len(raw) > cap:
        raise ValueError('Queue output byte budget exceeded')
    with guarded.atomic_writer(path) as stream:
        stream.write(raw)


def _remove(guarded, names):
    for name in names:
        try:
            os.unlink(name, dir_fd=guarded.fd)
        except FileNotFoundError:
            pass


def _directory_fd(fd):
    """Adopt an owned directory descriptor without reopening its pathname."""
    guarded = SourceRoot.__new__(SourceRoot)
    guarded.fd = os.dup(fd)
    try:
        guarded.root = Path('/proc/self/fd/' + str(guarded.fd)).resolve(strict=True)
        guarded.secure = DESCRIPTOR_OPENS
        info = os.fstat(guarded.fd)
        guarded.identity = _sha(os.fsencode(guarded.root) +
                               f':{info.st_dev}:{info.st_ino}'.encode())
    except BaseException:
        os.close(guarded.fd)
        raise
    return guarded


def _new_directory(parent, name):
    os.mkdir(name, mode=0o700, dir_fd=parent.fd)
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent.fd)
    try:
        return _directory_fd(fd)
    finally:
        os.close(fd)


def _identity():
    """Recheck controller import snapshot; other helper hashes capture disk bytes."""
    with SourceRoot(ROOT) as source:
        hashes = {path: _read(source, path, CODE_BYTES)[1]
                  for path in IMPLEMENTATIONS}
    if hashes['evaluations/queued_collector.py'] != _LOADED_CONTROLLER_SHA256:
        raise ValueError('Queued controller implementation changed since module import')
    baseline = _baseline()
    return {'implementations': hashes, 'collector': baseline.collector_identity(),
            'loaded_controller_sha256': _LOADED_CONTROLLER_SHA256,
            'rules': baseline.RULE_VERSION, 'pins': dict(baseline.PINS)}


def _check(cancel, deadline):
    if cancel is not None and cancel():
        raise PoolStopped('cancelled')
    if time.monotonic() >= deadline:
        raise PoolStopped('deadline_exceeded')


def _record(blob, budget):
    if (type(blob) is not dict or not {'path', 'language', 'content'} <= set(blob) or
            set(blob) - {'path', 'language', 'content', 'kind', 'sha256', 'bytes'} or
            type(blob['content']) is not bytes):
        raise ValueError('Immutable source-only blob required')
    raw = blob['content']
    path = blob['path']
    if (type(path) is not str or len(path) > 4096 or '\\' in path or ':' in path or '\0' in path or
            str(PurePosixPath(path)) != path or path in ('', '.')):
        raise ValueError('Canonical relative source path required')
    SourceRoot.parts(path)
    if (type(blob['language']) is not str or blob['language'] not in _baseline().LANGUAGES or
            blob.get('kind', 'source') != 'source' or len(raw) > budget.max_file_bytes):
        raise ValueError('Bounded source metadata required')
    raw.decode('utf-8')
    record = {'path': path, 'language': blob['language'], 'kind': 'source',
              'bytes': len(raw), 'sha256': _sha(raw)}
    if ('bytes' in blob and (type(blob['bytes']) is not int or blob['bytes'] != len(raw)) or
            'sha256' in blob and (type(blob['sha256']) is not str or blob['sha256'] != record['sha256'])):
        raise ValueError('Source metadata differs from bytes')
    return record


def _ready(ready, worker, identity, token, limits):
    if (type(ready) is not dict or set(ready) !=
            {'schema_version', 'token', 'identity', 'pid', 'limits', 'isolation'} or
            type(ready['schema_version']) is not int or ready['schema_version'] != 1 or
            ready['token'] != token or ready['identity'] != identity or
            type(ready['pid']) is not int or ready['pid'] != worker['process'].pid or
            _encoded(ready['limits'], CONTROL_BYTES) != _encoded(asdict(limits), CONTROL_BYTES) or
            _encoded(ready['isolation'], CONTROL_BYTES) != _encoded(
                {'python_isolated_mode': True, 'bytecode_writes_disabled': True,
                 'user_site_disabled': True, 'private_environment': True,
                 'own_session_and_group': True, 'controller_death_signal': True}, CONTROL_BYTES)):
        raise ValueError('Invalid owned worker readiness')


def _resources(resources):
    if type(resources) is not dict or set(resources) != {'elapsed_seconds', 'process_peak_rss_bytes', 'process_user_seconds', 'process_system_seconds'}:
        raise ValueError('Invalid worker resource receipt')
    for key, value in resources.items():
        if key == 'process_peak_rss_bytes':
            valid = type(value) is int and value >= 0
        else:
            valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
        if not valid:
            raise ValueError('Invalid worker resource number')


def _receive(worker, identity, token, budget, limits, cancel):
    guarded, pending = worker['guarded'], worker['pending']
    try:
        raw, _ = _read(guarded, 'result.json', CONTROL_BYTES)
    except FileNotFoundError:
        return None
    receipt = _decode(raw)
    keys = {'schema_version', 'token', 'index', 'record', 'identity', 'status',
            'sha256', 'bytes', 'error_kind', 'reason', 'resources'}
    if (type(receipt) is not dict or set(receipt) != keys or
            type(receipt['schema_version']) is not int or receipt['schema_version'] != 1 or
            receipt['token'] != token or receipt['identity'] != identity or
            type(receipt['index']) is not int or receipt['index'] != pending['index'] or
            _encoded(receipt['record'], CONTROL_BYTES) != _encoded(pending['record'], CONTROL_BYTES)):
        raise ValueError('Stale or foreign owned producer receipt')
    _resources(receipt['resources'])
    if receipt['status'] == 'collected':
        if receipt['error_kind'] is not None or receipt['reason'] is not None or type(receipt['bytes']) is not int:
            raise ValueError('Invalid successful producer receipt')
        encoded, digest = _read(guarded, 'payload.json', limits.max_result_bytes)
        if receipt['bytes'] != len(encoded) or receipt['sha256'] != digest:
            raise ValueError('Owned producer payload identity differs')
        result = _baseline().CollectedFile.from_json(encoded, pending['record'],
                    receipt['sha256'], budget=budget, cancel=cancel)
        if result.collector_sha256 != identity['collector']:
            raise ValueError('Producer differs from admitted collector implementation')
    elif (receipt['status'] == 'failed' and receipt['sha256'] is None and
          type(receipt['bytes']) is int and receipt['bytes'] == 0 and
          receipt['error_kind'] in ('BackendUnavailable', 'StopScan', 'ValueError',
                                    'UnicodeError', 'MemoryError', 'RecursionError') and
          receipt['reason'] in ('backend_unavailable', 'collection_failed', 'collection_stopped')):
        result = None
    else:
        raise ValueError('Invalid producer outcome')
    _remove(guarded, ('result.json', 'payload.json', 'source.bin', 'request.json'))
    return result, receipt


def _start(guarded, identity, token, budget, limits, workers, cancel, deadline):
    from evaluations.engine_checks import _environment
    worker = {'guarded': guarded, 'pending': None, 'requests': 0}
    workers.append(worker)
    _write(guarded, 'control.json', _encoded({'schema_version': 1, 'token': token,
           'identity': identity, 'budget': asdict(budget), 'limits': asdict(limits)}, CONTROL_BYTES), CONTROL_BYTES)
    handles = [guarded.open(name, create=True) for name in ('stdout.log', 'stderr.log')]
    # This bridge names the held directory, even after an ancestor rename/swap.
    job = Path('/proc/' + str(os.getpid()) + '/fd/' + str(guarded.fd))
    with handles[0] as stdout, handles[1] as stderr:
        worker['process'] = subprocess.Popen(
            [sys.executable, '-I', '-B', str(Path(__file__).resolve()), '--worker-fd',
             str(guarded.fd), str(os.getpid())],
            cwd=job, env=_environment(job), stdin=subprocess.DEVNULL,
            stdout=stdout, stderr=stderr, start_new_session=True, close_fds=True,
            pass_fds=(guarded.fd,))
    worker_deadline = min(deadline, time.monotonic() + limits.worker_wall_seconds)
    while True:
        _check(cancel, worker_deadline)
        try:
            raw, _ = _read(guarded, 'ready.json', CONTROL_BYTES)
            ready = _decode(raw)
            _ready(ready, worker, identity, token, limits)
            worker['isolation'] = ready['isolation']
            return worker
        except FileNotFoundError:
            pass
        if worker['process'].poll() is not None:
            raise PoolStopped('worker_start_failed')
        time.sleep(0.005)


def collect_files(blobs, *, mode='serial', concurrency=1, budget=None,
                  limits=None, cancel=None, evidence_directory=None):
    """Return collected files in input order plus finite failures/cleanup.

    Never calls resolve_collected, retries a failed worker, or changes modes.
    evidence_directory must already exist outside this source checkout. A
    private run directory retains metadata logs/receipt; default runs are
    temporary. No successful outcome here constitutes engine qualification.
    """
    baseline = _baseline()
    budget, limits = budget or baseline.Budget(), limits or QueueLimits()
    if (type(budget) is not baseline.Budget or type(limits) is not QueueLimits or
            mode not in ('serial', 'queued') or type(concurrency) is not int or
            not 1 <= concurrency <= 4 or mode == 'serial' and concurrency != 1):
        raise ValueError('Explicit serial/queued concurrency and typed limits required')
    if (budget.max_files > 4096 or budget.max_file_bytes > 16 * 1024 * 1024 or
            budget.max_total_bytes > 256 * 1024 * 1024):
        raise ValueError('Finite source admission caps exceeded')
    if sys.platform != 'linux' or not DESCRIPTOR_OPENS or not Path('/proc/self/fd').is_dir():
        raise OSError('Finite owned collectors require Linux descriptor bridges and rlimits')
    import resource  # unsupported platforms fail closed before creating a worker
    if not all(hasattr(resource, name) for name in ('RLIMIT_AS', 'RLIMIT_CPU', 'RLIMIT_FSIZE')):
        raise OSError('Required worker resource limits unavailable')
    reservation = budget.max_file_bytes + limits.max_request_bytes + limits.max_result_bytes
    if limits.max_inflight_bytes < reservation:
        raise ValueError('In-flight budget cannot admit one bounded request/result')
    per_file = replace(budget, max_handoff_bytes=min(budget.max_handoff_bytes, limits.max_result_bytes),
                       max_collected_bytes=min(budget.max_collected_bytes, limits.max_admitted_bytes))
    result, workers, completed = CollectionResult(), [], {}
    started = time.monotonic()
    deadline = started + limits.total_wall_seconds
    identity, token = _identity(), uuid.uuid4().hex
    parent = None
    temporary = None
    owner = run = None
    try:
        if evidence_directory is None:
            temporary = tempfile.TemporaryDirectory(prefix='queued-collector-')
            parent = Path(temporary.name)
        else:
            parent = Path(evidence_directory).resolve(strict=True)
            if parent == ROOT or ROOT in parent.parents:
                raise ValueError('Collector evidence must be private and outside source')
        owner = SourceRoot(parent)
        if owner.root == ROOT or ROOT in owner.root.parents:
            raise ValueError('Collector evidence must be private and outside source')
        name = 'run-' + uuid.uuid4().hex
        run = _new_directory(owner, name)
    except BaseException:
        if run is not None:
            run.__exit__()
        if owner is not None:
            owner.__exit__()
        if temporary is not None:
            temporary.cleanup()
        raise
    source_bytes = admitted_bytes = inflight_peak = 0
    nodes = facts = count = 0
    seen, exhausted = set(), False
    try:
        iterator = iter(blobs)
        while not exhausted or any(w['pending'] is not None for w in workers):
            _check(cancel, deadline)
            for worker in workers:
                for log in ('stdout.log', 'stderr.log'):
                    if worker['guarded'].info(log).st_size > limits.log_bytes:
                        raise PoolStopped('worker_log_budget_exceeded')
                pending = worker['pending']
                if pending is None:
                    if worker['process'].poll() is not None:
                        raise PoolStopped('worker_exited')
                    continue
                if time.monotonic() >= pending['deadline']:
                    raise PoolStopped('worker_deadline_exceeded')
                received = _receive(worker, identity, token, per_file, limits, cancel)
                if received is None:
                    if worker['process'].poll() is not None:
                        raise PoolStopped('worker_exited')
                    continue
                file, receipt = received
                worker['pending'] = None
                if file is None:
                    result.failures.append({'index': pending['index'], 'record': pending['record'],
                                            'kind': receipt['error_kind'], 'reason': receipt['reason']})
                    if receipt['error_kind'] == 'BackendUnavailable':
                        raise PoolStopped('backend_unavailable')
                else:
                    admitted_bytes += receipt['bytes']
                    nodes += file.counts['nodes']
                    facts += file.counts['definitions']
                    if (admitted_bytes > min(limits.max_admitted_bytes, budget.max_collected_bytes) or
                            nodes > budget.max_nodes or facts > budget.max_facts):
                        result.failures.append({'index': pending['index'], 'record': pending['record'],
                                                'kind': 'aggregate_budget_exceeded'})
                        raise PoolStopped('aggregate_budget_exceeded')
                    completed[pending['index']] = file
                    if file.partial or file.errors:
                        result.failures.append({'index': pending['index'], 'record': pending['record'],
                                                'kind': 'partial_file', 'error_count': len(file.errors)})
                worker.setdefault('resources', []).append(receipt['resources'])
            while not exhausted:
                active = sum(w['pending'] is not None for w in workers)
                if active >= concurrency or (active + 1) * reservation > limits.max_inflight_bytes:
                    break
                _check(cancel, deadline)
                try:
                    blob = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                _check(cancel, deadline)
                record = _record(blob, budget)
                if count >= budget.max_files or source_bytes + record['bytes'] > budget.max_total_bytes:
                    raise PoolStopped('source_admission_budget_exceeded')
                if record['path'] in seen:
                    raise ValueError('Duplicate source path')
                seen.add(record['path'])
                source_bytes += record['bytes']
                worker = next((w for w in workers if w['pending'] is None), None)
                if worker is None:
                    worker_name = 'worker-' + str(len(workers))
                    worker = _start(_new_directory(run, worker_name), identity, token, per_file,
                                    limits, workers, cancel, deadline)
                request = _encoded({'schema_version': 1, 'token': token, 'index': count,
                                     'identity': identity, 'record': record}, limits.max_request_bytes)
                _write(worker['guarded'], 'source.bin', blob['content'], budget.max_file_bytes)
                _write(worker['guarded'], 'request.json', request, limits.max_request_bytes)
                worker['pending'] = {'index': count, 'record': record,
                    'deadline': min(deadline, time.monotonic() + limits.worker_wall_seconds)}
                worker['requests'] += 1
                count += 1
                inflight_peak = max(inflight_peak, sum(w['pending'] is not None for w in workers) * reservation)
                del blob
            if any(w['pending'] is not None for w in workers):
                time.sleep(0.005)
        if _identity() != identity:
            raise PoolStopped('implementation_changed')
        result.status = ('partial' if completed else 'failed') if result.failures else 'complete'
    except (PoolStopped, baseline.StopScan, OSError, ValueError, TypeError,
            KeyError, UnicodeError, MemoryError, RecursionError, RuntimeError) as error:
        if type(error) is PoolStopped:
            result.stop_reason = str(error)
        elif isinstance(error, baseline.StopScan):
            reason = str(error)
            result.stop_reason = reason if reason in {
                'cancelled', 'deadline_exceeded', 'source_byte_budget_exceeded',
                'file_budget_exceeded', 'node_budget_exceeded', 'fact_budget_exceeded',
                'collected_byte_budget_exceeded', 'handoff_byte_budget_exceeded'} else 'collection_stopped'
        else:
            result.stop_reason = 'input_or_protocol_rejected'
        result.status = 'partial' if completed else 'failed'
        for worker in workers:
            if worker['pending'] is not None:
                result.failures.append({'index': worker['pending']['index'],
                    'record': worker['pending']['record'], 'kind': type(error).__name__,
                    'reason': result.stop_reason})
        if not result.failures:
            result.failures.append({'index': count, 'kind': type(error).__name__, 'reason': result.stop_reason})
    finally:
        from evaluations.engine_checks import _stop_and_reap
        for worker in workers:
            process = worker.get('process')
            if process is not None and process.poll() is not None and result.status == 'complete':
                result.status, result.stop_reason = ('partial' if completed else 'failed'), 'worker_exited'
                result.failures.append({'index': count, 'kind': 'worker_exited',
                                        'returncode': process.returncode})
            try:
                cleanup = _stop_and_reap(process) if process is not None else {
                    'signals': [], 'leader_reaped': True, 'group_absent': True, 'returncode': None}
            except OSError:
                cleanup = {'signals': [], 'leader_reaped': False, 'group_absent': False,
                           'returncode': process.returncode if process is not None else None}
            cleanup = {**cleanup, 'requests': worker['requests']}
            result.cleanup.append(cleanup)
            try:
                _remove(worker['guarded'], MAILBOXES)
            except OSError:
                cleanup['mailboxes_removed'] = False
            else:
                cleanup['mailboxes_removed'] = True
            finally:
                worker['guarded'].__exit__()
        if any(not row['leader_reaped'] or not row['group_absent'] or not row['mailboxes_removed'] for row in result.cleanup):
            result.status, result.stop_reason = 'failed', 'cleanup_failed'
        result.collected = [completed[index] for index in sorted(completed)]
        result.failures.sort(key=lambda item: item['index'])
        result.resources = {'mode': mode, 'configured_concurrency': concurrency,
            'workers_started': len(workers), 'files_admitted': count,
            'files_collected': len(completed), 'source_bytes': source_bytes,
            'collected_handoff_bytes': admitted_bytes, 'collected_nodes': nodes,
            'collected_definitions': facts, 'peak_inflight_reserved_bytes': inflight_peak,
            'elapsed_seconds': time.monotonic() - started, 'limits': asdict(limits),
            'identity': identity, 'worker_resources': [w.get('resources', []) for w in workers],
            'worker_isolation': [w.get('isolation') for w in workers],
            'worker_file_hard_limit_bytes': max(limits.max_result_bytes, limits.max_request_bytes,
                                               limits.log_bytes, CONTROL_BYTES),
            'limits_qualification': 'unmeasured_configuration_not_capacity_evidence'}
        summary = {'status': result.status, 'stop_reason': result.stop_reason,
            'failures': [{key: value for key, value in row.items() if key != 'record'}
                         for row in result.failures],
            'resources': result.resources, 'cleanup': result.cleanup,
            'runtime_qualified': False, 'selected_engine': None}
        summary_cap = max(1024 * 1024, budget.max_files * 512 + CONTROL_BYTES)
        try:
            _write(run, 'receipt.json', _encoded(summary, summary_cap), summary_cap)
        except RuntimeError:
            # A directory fsync can fail after publication. Preserve the result
            # and cleanup, and remove that receipt so it cannot imply success.
            result.status, result.stop_reason = 'failed', 'input_or_protocol_rejected'
            result.failures.append({'index': count, 'kind': 'RuntimeError',
                                    'reason': result.stop_reason, 'stage': 'receipt'})
            result.resources['receipt_write_failed'] = True
            try:
                _remove(run, ('receipt.json',))
            except OSError:
                result.resources['receipt_removal_failed'] = True
        finally:
            run.__exit__()
            owner.__exit__()
            if temporary is not None:
                temporary.cleanup()
    return result


def _guard_controller(creator_pid):
    """Linux kills this worker when its creating controller thread exits.

    Verify the parent around prctl: death before registration otherwise leaves
    an orphan. This stops compute; the surviving owner must reap/remove evidence.
    https://man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html
    """
    import ctypes
    if sys.platform != 'linux' or type(creator_pid) is not int or creator_pid <= 0 or os.getppid() != creator_pid:
        raise OSError('Owned controller unavailable before worker admission')
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    if prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'Controller death signal unavailable')
    if os.getppid() != creator_pid:
        raise OSError('Owned controller changed during worker admission')


def _worker(fd, creator_pid):
    _guard_controller(creator_pid)
    import resource
    with _directory_fd(fd) as job:
        os.close(fd)
        if job.root == ROOT or ROOT in job.root.parents:
            return 2
        control = _decode(_read(job, 'control.json', CONTROL_BYTES)[0])
        if type(control) is not dict or set(control) != {'schema_version', 'token', 'identity', 'budget', 'limits'} or type(control['schema_version']) is not int or control['schema_version'] != 1:
            return 2
        limits = QueueLimits(**control['limits'])
        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
        file_cap = max(limits.max_result_bytes, limits.max_request_bytes, limits.log_bytes, CONTROL_BYTES)
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_cap, file_cap))
        if hasattr(resource, 'RLIMIT_CORE'):
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        baseline = _baseline()
        budget = baseline.Budget(**control['budget'])
        token, identity = control['token'], control['identity']
        if type(token) is not str or len(token) != 32 or identity != _identity():
            return 2
        isolated = {'python_isolated_mode': bool(sys.flags.isolated),
                    'bytecode_writes_disabled': bool(sys.dont_write_bytecode),
                    'user_site_disabled': bool(sys.flags.no_user_site),
                    'private_environment': all(Path(os.environ.get(key, '')).resolve() == job.root / name
                        for key, name in (('HOME', 'home'), ('XDG_CONFIG_HOME', 'config'),
                            ('XDG_CACHE_HOME', 'cache'), ('XDG_DATA_HOME', 'data'), ('TMPDIR', 'tmp'))),
                    'own_session_and_group': os.getpid() == os.getsid(0) == os.getpgrp(),
                    'controller_death_signal': True}
        _write(job, 'ready.json', _encoded({'schema_version': 1, 'token': token,
            'identity': identity, 'pid': os.getpid(), 'limits': asdict(limits),
            'isolation': isolated}, CONTROL_BYTES), CONTROL_BYTES)
        previous = -1
        while True:
            try:
                request = _decode(_read(job, 'request.json', limits.max_request_bytes)[0])
            except FileNotFoundError:
                time.sleep(0.005)
                continue
            if (type(request) is not dict or set(request) != {'schema_version', 'token', 'index', 'identity', 'record'} or
                    type(request['schema_version']) is not int or request['schema_version'] != 1 or
                    request['token'] != token or request['identity'] != identity or
                    type(request['index']) is not int or request['index'] <= previous):
                return 2
            previous = request['index']
            _remove(job, ('request.json',))
            raw = _read(job, 'source.bin', budget.max_file_bytes)[0]
            record = request['record']
            supplied = {**record, 'content': raw}
            if _record(supplied, budget) != record or _identity() != identity:
                return 2
            started = time.monotonic()
            payload, error_kind, reason = None, None, None
            try:
                collected = baseline.collect_file(supplied, budget=budget)
                payload = collected.to_json(budget=budget)
                del collected
            except baseline.BackendUnavailable:
                error_kind, reason = 'BackendUnavailable', 'backend_unavailable'
            except baseline.StopScan:
                error_kind, reason = 'StopScan', 'collection_stopped'
            except (ValueError, UnicodeError, MemoryError, RecursionError) as error:
                error_kind = 'UnicodeError' if isinstance(error, UnicodeError) else type(error).__name__
                reason = 'collection_failed'
            usage = resource.getrusage(resource.RUSAGE_SELF)
            resources = {'elapsed_seconds': time.monotonic() - started,
                'process_peak_rss_bytes': usage.ru_maxrss * (1 if sys.platform == 'darwin' else 1024),
                'process_user_seconds': usage.ru_utime, 'process_system_seconds': usage.ru_stime}
            if payload is not None:
                _write(job, 'payload.json', payload, limits.max_result_bytes)
            receipt = {'schema_version': 1, 'token': token, 'index': previous,
                'record': record, 'identity': identity,
                'status': 'collected' if payload is not None else 'failed',
                'sha256': _sha(payload) if payload is not None else None,
                'bytes': len(payload) if payload is not None else 0,
                'error_kind': error_kind, 'reason': reason, 'resources': resources}
            _write(job, 'result.json', _encoded(receipt, CONTROL_BYTES), CONTROL_BYTES)
            del raw, supplied, payload


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != '--worker-fd':
        raise SystemExit('Internal owned collector worker; use collect_files()')
    try:
        raise SystemExit(_worker(int(sys.argv[2]), int(sys.argv[3])))
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, MemoryError, RecursionError):
        raise SystemExit(2)
