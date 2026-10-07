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
from repo_graph.source import (SourceRoot, DESCRIPTOR_OPENS, code_identity as source_code_identity,
                               source_hash, empty_source_accounting, SOURCE_ACCOUNTING_FIELDS)
from repo_graph import LOADED_CODE_SHA256

CONTROL_BYTES = 16 * 1024
CODE_BYTES = 2 * 1024 * 1024
NATIVE_TIMINGS = ('backend_setup_seconds', 'parse_seconds',
                  'traversal_lowering_seconds', 'collect_elapsed_seconds',
                  'handoff_serialize_seconds')
CONTROLLER_TIMINGS = ('mailbox_write_seconds', 'mailbox_read_seconds',
                      'receipt_decode_seconds', 'handoff_decode_seconds',
                      'admission_seconds', 'observer_seconds')
MAILBOXES = ('control.json', 'ready.json', 'request.json', 'source.bin',
             'payload.json', 'result.json')
IMPLEMENTATIONS = ('repo_graph/analysis_queue.py', 'repo_graph/analysis_native.py',
                   'repo_graph/source.py', 'repo_graph/__init__.py')
SOURCE_ACCOUNTING_PASSES = ('mailbox_source', 'worker_source_validation', 'native_source_validation')


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


GRACE_SECONDS = 0.25


def _group_exists(process):
    try:
        os.killpg(process.pid, 0)
        return True
    except ProcessLookupError:
        return False


def _stop_and_reap(process):
    """Signal only the new session/group created for this owned Popen child."""
    if hasattr(process, '_engine_checks_cleanup'):
        return process._engine_checks_cleanup
    sent = []
    for sig, wait in ((signal.SIGTERM, GRACE_SECONDS), (signal.SIGKILL, 2)):
        if _group_exists(process):
            try:
                os.killpg(process.pid, sig)
                sent.append(sig.name)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            continue
        if not _group_exists(process):
            break
    receipt = {'signals': sent, 'leader_reaped': process.returncode is not None,
               'group_absent': not _group_exists(process), 'returncode': process.returncode}
    if receipt['leader_reaped'] and receipt['group_absent']:
        # Forget the group after proving cleanup; never signal a reused PID later.
        process._engine_checks_cleanup = receipt
    return receipt



def _environment(job):
    # An explicit environment avoids inheriting provider/account configuration.
    directories = {key: job / name for key, name in (
        ('HOME', 'home'), ('XDG_CONFIG_HOME', 'config'), ('XDG_CACHE_HOME', 'cache'),
        ('XDG_DATA_HOME', 'data'), ('TMPDIR', 'tmp'))}
    for directory in directories.values():
        directory.mkdir()
    return {**{key: str(value) for key, value in directories.items()},
            'PATH': os.defpath, 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
            'TEMP': str(directories['TMPDIR']), 'TMP': str(directories['TMPDIR'])}



def _baseline():
    from repo_graph import analysis_native
    return analysis_native


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


def _read(guarded, path, cap, *, measurements=None):
    raw, digest, info = guarded.read(path, cap + 1, hash_full=False, measurements=measurements)
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
    """Bind identity-bearing helpers to the code actually loaded."""
    with SourceRoot(ROOT) as source:
        hashes = {path: _read(source, path, CODE_BYTES)[1]
                  for path in IMPLEMENTATIONS}
    if hashes['repo_graph/analysis_queue.py'] != _LOADED_CONTROLLER_SHA256:
        raise ValueError('Queued controller implementation changed since module import')
    if hashes['repo_graph/source.py'] != source_code_identity():
        raise ValueError('Source guard identity differs from loaded implementation')
    if hashes['repo_graph/__init__.py'] != LOADED_CODE_SHA256:
        raise ValueError('Package implementation changed since module import')
    baseline = _baseline()
    return {'implementations': hashes, 'collector': baseline.collector_identity(),
            'loaded_controller_sha256': _LOADED_CONTROLLER_SHA256,
            'rules': baseline.RULE_VERSION, 'pins': dict(baseline.PINS)}


def _check(cancel, deadline):
    if cancel is not None and cancel():
        raise PoolStopped('cancelled')
    if time.monotonic() >= deadline:
        raise PoolStopped('deadline_exceeded')


def _record(blob, budget, *, source_measurements=None):
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
              'bytes': len(raw), 'sha256': source_hash(raw, measurements=source_measurements)}
    if ('bytes' in blob and (type(blob['bytes']) is not int or blob['bytes'] != len(raw)) or
            'sha256' in blob and (type(blob['sha256']) is not str or blob['sha256'] != record['sha256'])):
        raise ValueError('Source metadata differs from bytes')
    return record


def _process_valid(value):
    if (type(value) is not dict or set(value) != {'pid', 'starttime_ticks', 'pgid', 'sid'} or
            any(type(number) is not int or not 0 < number <= 2**63 - 1 for number in value.values()) or
            any(value[key] > 2**31 - 1 for key in ('pid', 'pgid', 'sid'))):
        raise ValueError('Invalid owned process identity')


def _process_identity(pid):
    """Bounded Linux identity reads from one held proc directory; no host scan."""
    if type(pid) is not int or not 0 < pid <= 2**31 - 1:
        raise ValueError('Invalid owned process PID')
    directory = os.open('/proc/' + str(pid), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    def read():
        fd = os.open('stat', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
        if len(raw) > 4096:
            raise ValueError('Owned process identity byte ceiling exceeded')
        head, separator, tail = raw.rpartition(b') ')
        fields = tail.split()
        if not separator or not head.startswith(str(pid).encode() + b' (') or len(fields) < 20:
            raise ValueError('Malformed owned process stat')
        value = {'pid': pid, 'starttime_ticks': int(fields[19]),
                 'pgid': int(fields[2]), 'sid': int(fields[3])}
        _process_valid(value)
        return value
    try:
        before = read()
        if os.getpgid(pid) != before['pgid'] or os.getsid(pid) != before['sid'] or read() != before:
            raise ValueError('Owned process identity changed')
        return before
    finally:
        os.close(directory)


def _verify_worker(worker):
    expected = worker['process_identity']
    if (worker['process'].poll() is not None or
            _process_identity(worker['process'].pid) != expected or
            expected['pid'] != expected['pgid'] or expected['pid'] != expected['sid']):
        raise ValueError('Stale or foreign owned worker identity')


def _event_valid(event):
    counters = ('monotonic_ns', 'configured_concurrency', 'workers_started', 'live_workers',
                'pending_requests', 'inflight_reserved_bytes', 'mailbox_source_bytes',
                'mailbox_request_bytes', 'mailbox_result_bytes', 'admitted_bytes')
    if (type(event) is not dict or set(event) != set(counters) | {
            'schema_version', 'event', 'mode', 'role', 'controller', 'worker', 'index', 'cleanup'} or
            type(event['schema_version']) is not int or event['schema_version'] != 1 or
            event['event'] not in ('readiness', 'submit', 'receive', 'failure', 'cleanup') or
            event['mode'] not in ('serial', 'queued') or event['role'] not in ('controller', 'worker') or
            any(type(event[key]) is not int or not 0 <= event[key] <= 2**63 - 1 for key in counters) or
            not 1 <= event['configured_concurrency'] <= 4 or
            event['mode'] == 'serial' and event['configured_concurrency'] != 1 or
            not event['live_workers'] <= event['workers_started'] <= event['configured_concurrency'] or
            not 0 <= event['pending_requests'] <= event['configured_concurrency'] or
            (event['index'] is not None and (type(event['index']) is not int or not 0 <= event['index'] < 4096))):
        raise ValueError('Invalid bounded observer event')
    _process_valid(event['controller'])
    if event['worker'] is not None:
        _process_valid(event['worker'])
        if event['worker']['pid'] != event['worker']['pgid'] or event['worker']['pid'] != event['worker']['sid']:
            raise ValueError('Observer worker is not an owned session leader')
    if (event['role'] == 'worker') != (event['worker'] is not None):
        raise ValueError('Observer event role differs from ownership')
    if event['event'] == 'cleanup':
        cleanup = event['cleanup']
        if (type(cleanup) is not dict or set(cleanup) != {'leader_reaped', 'group_absent', 'mailboxes_removed'} or
                any(type(value) is not bool for value in cleanup.values())):
            raise ValueError('Typed observer cleanup proof required')
    elif event['cleanup'] is not None:
        raise ValueError('Unexpected observer cleanup proof')


def _timed(measurements, key, function, *args, **kwargs):
    if measurements is None:
        return function(*args, **kwargs)
    before = time.monotonic()
    try:
        return function(*args, **kwargs)
    finally:
        measurements[key] += time.monotonic() - before


def _ready(ready, worker, identity, token, limits):
    keys = {'schema_version', 'token', 'identity', 'pid', 'limits', 'isolation'}
    if worker.get('telemetry'):
        keys.add('process_identity')
    if (type(ready) is not dict or set(ready) !=
            keys or
            type(ready['schema_version']) is not int or ready['schema_version'] != 1 or
            ready['token'] != token or ready['identity'] != identity or
            type(ready['pid']) is not int or ready['pid'] != worker['process'].pid or
            _encoded(ready['limits'], CONTROL_BYTES) != _encoded(asdict(limits), CONTROL_BYTES) or
            _encoded(ready['isolation'], CONTROL_BYTES) != _encoded(
                {'python_isolated_mode': True, 'bytecode_writes_disabled': True,
                 'user_site_disabled': True, 'private_environment': True,
                 'own_session_and_group': True, 'controller_death_signal': True}, CONTROL_BYTES)):
        raise ValueError('Invalid owned worker readiness')
    if worker.get('telemetry'):
        _process_valid(ready['process_identity'])
        worker['process_identity'] = dict(ready['process_identity'])
        _verify_worker(worker)


def _source_accounting_row(value, cap, *, stream):
    if (type(value) is not dict or set(value) != set(SOURCE_ACCOUNTING_FIELDS) or
            any(type(number) is not int or not 0 <= number <= 2**63 - 1 for number in value.values()) or
            value['operations'] != value['successful_operations'] + value['failed_operations'] or
            value['operations'] > 1 or value['open_operations'] > value['operations'] or
            value['hash_passes'] > value['operations'] or value['hashed_bytes'] > cap or
            value['hashed_bytes'] and not value['hash_passes'] or
            not value['operations'] and any(value.values())):
        raise ValueError('Invalid fixed source accounting row')
    if stream:
        if (value['stream_bytes'] > cap or value['hashed_bytes'] > value['stream_bytes'] or
                value['returned_prefix_bytes'] > value['stream_bytes'] or
                value['stream_bytes'] and not value['open_operations'] or
                value['failed_operations'] and value['returned_prefix_bytes'] or
                value['successful_operations'] and (value['open_operations'] != 1 or
                    value['hashed_bytes'] != value['stream_bytes'])):
            raise ValueError('Invalid guarded-stream source accounting')
    elif value['open_operations'] or value['stream_bytes'] or value['returned_prefix_bytes']:
        raise ValueError('Buffer hash cannot report a stream read')
    if value['successful_operations'] and value['hash_passes'] != 1:
        raise ValueError('Completed digest must report one hash pass')


def _source_accounting_valid(value, record=None, budget=None, *, successful=False):
    if (type(value) is not dict or set(value) != {'schema_version', 'transport', 'source_sha256',
            'source_bytes', 'original_source_stream_bytes', 'accounting_complete', 'passes'} or
            type(value['schema_version']) is not int or value['schema_version'] != 1 or
            value['transport'] != 'immutable_mailbox_blob_v1' or value['accounting_complete'] is not True or
            type(value['source_bytes']) is not int or not 0 <= value['source_bytes'] <= 16 * 1024 * 1024 or
            type(value['source_sha256']) is not str or len(value['source_sha256']) != 64 or
            any(char not in '0123456789abcdef' for char in value['source_sha256']) or
            type(value['original_source_stream_bytes']) is not int or value['original_source_stream_bytes'] != 0 or
            type(value['passes']) is not dict or set(value['passes']) != set(SOURCE_ACCOUNTING_PASSES)):
        raise ValueError('Invalid bound source accounting receipt')
    if record is not None and (value['source_sha256'] != record['sha256'] or value['source_bytes'] != record['bytes']):
        raise ValueError('Source accounting differs from pending source identity')
    cap = budget.max_file_bytes if budget is not None else 16 * 1024 * 1024
    for name, row in value['passes'].items():
        _source_accounting_row(row, cap + 1 if name == 'mailbox_source' else cap,
                               stream=name == 'mailbox_source')
        if successful:
            expected = empty_source_accounting()
            expected.update(operations=1, successful_operations=1, hash_passes=1,
                            hashed_bytes=value['source_bytes'])
            if name == 'mailbox_source':
                expected.update(open_operations=1, stream_bytes=value['source_bytes'],
                                returned_prefix_bytes=value['source_bytes'])
            if row != expected:
                raise ValueError('Collected source has incomplete source accounting')


def _resources(resources, telemetry=False, record=None, budget=None):
    keys = {'elapsed_seconds', 'process_peak_rss_bytes', 'process_user_seconds', 'process_system_seconds'}
    if telemetry:
        keys |= {'file_user_seconds', 'file_system_seconds', 'timings', 'source_accounting'}
    if type(resources) is not dict or set(resources) != keys:
        raise ValueError('Invalid worker resource receipt')
    for key, value in resources.items():
        if key == 'source_accounting':
            _source_accounting_valid(value, record, budget)
            continue
        if key == 'timings':
            if type(value) is not dict or set(value) != set(NATIVE_TIMINGS):
                raise ValueError('Invalid native timing fields')
            if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in value.values()):
                raise ValueError('Invalid native timing number')
            continue
        if key == 'process_peak_rss_bytes':
            valid = type(value) is int and value >= 0
        else:
            valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
        if not valid:
            raise ValueError('Invalid worker resource number')
    if telemetry:
        if any(resources['file_' + key] > resources['process_' + key] for key in ('user_seconds', 'system_seconds')):
            raise ValueError('Per-file CPU delta exceeds lifetime counter')
        timings = resources['timings']
        if (sum(timings[key] for key in NATIVE_TIMINGS[:3]) > timings['collect_elapsed_seconds'] + 0.001 or
                timings['collect_elapsed_seconds'] + timings['handoff_serialize_seconds'] > resources['elapsed_seconds'] + 0.001):
            raise ValueError('Disjoint timings exceed inclusive interval')


def _receive(worker, identity, token, budget, limits, cancel, measurements=None):
    guarded, pending = worker['guarded'], worker['pending']
    try:
        raw, _ = _timed(measurements, 'mailbox_read_seconds', _read, guarded, 'result.json', CONTROL_BYTES)
    except FileNotFoundError:
        return None
    receipt = _timed(measurements, 'receipt_decode_seconds', _decode, raw)
    keys = {'schema_version', 'token', 'index', 'record', 'identity', 'status',
            'sha256', 'bytes', 'error_kind', 'reason', 'resources'}
    if (type(receipt) is not dict or set(receipt) != keys or
            type(receipt['schema_version']) is not int or receipt['schema_version'] != 1 or
            receipt['token'] != token or receipt['identity'] != identity or
            type(receipt['index']) is not int or receipt['index'] != pending['index'] or
            _encoded(receipt['record'], CONTROL_BYTES) != _encoded(pending['record'], CONTROL_BYTES)):
        raise ValueError('Stale or foreign owned producer receipt')
    _resources(receipt['resources'], worker.get('telemetry', False), pending['record'], budget)
    if receipt['status'] == 'collected':
        if worker.get('telemetry'):
            _source_accounting_valid(receipt['resources']['source_accounting'], pending['record'], budget, successful=True)
        if receipt['error_kind'] is not None or receipt['reason'] is not None or type(receipt['bytes']) is not int:
            raise ValueError('Invalid successful producer receipt')
        encoded, digest = _timed(measurements, 'mailbox_read_seconds', _read, guarded, 'payload.json', limits.max_result_bytes)
        if receipt['bytes'] != len(encoded) or receipt['sha256'] != digest:
            raise ValueError('Owned producer payload identity differs')
        result = _timed(measurements, 'handoff_decode_seconds', _baseline().CollectedFile.from_json, encoded, pending['record'],
                    receipt['sha256'], budget=budget, cancel=cancel)
        if result.collector_sha256 != identity['collector']:
            raise ValueError('Producer differs from admitted collector implementation')
    elif (receipt['status'] == 'failed' and receipt['sha256'] is None and
          type(receipt['bytes']) is int and receipt['bytes'] == 0 and
          receipt['error_kind'] in ('BackendUnavailable', 'StopScan', 'ValueError',
                                    'UnicodeError', 'MemoryError', 'RecursionError', 'OSError') and
          receipt['reason'] in ('backend_unavailable', 'collection_failed', 'collection_stopped')):
        result = None
    else:
        raise ValueError('Invalid producer outcome')
    _remove(guarded, ('result.json', 'payload.json', 'source.bin', 'request.json'))
    return result, receipt


def _start(guarded, identity, token, budget, limits, workers, cancel, deadline, telemetry=False):
    worker = {'guarded': guarded, 'pending': None, 'requests': 0, 'telemetry': telemetry}
    workers.append(worker)
    _write(guarded, 'control.json', _encoded({'schema_version': 1, 'token': token,
           'identity': identity, 'budget': asdict(budget), 'limits': asdict(limits),
           'telemetry': telemetry}, CONTROL_BYTES), CONTROL_BYTES)
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
                  limits=None, cancel=None, evidence_directory=None, telemetry=False,
                  observer=None, observer_max_events=10000):
    """Return collected files in input order plus finite failures/cleanup.

    Never calls resolve_collected, retries a failed worker, or changes modes.
    evidence_directory must already exist outside this source checkout. A
    private run directory retains metadata logs/receipt; default runs are
    temporary. No successful outcome here constitutes engine qualification.

    Optional telemetry is outside collected facts/cache identity. A synchronous
    observer enables telemetry and must return exact True. Events are bounded,
    source/path-free and not retained here. Refusal/error/overflow stops work;
    cleanup remains owned even when observer delivery fails. The callback must
    not block: this loop cannot interrupt a callback or a filesystem operation.
    """
    baseline = _baseline()
    budget, limits = budget or baseline.Budget(), limits or QueueLimits()
    if (type(telemetry) is not bool or observer is not None and not callable(observer) or
            type(observer_max_events) is not int or not 1 <= observer_max_events <= 10000):
        raise ValueError('Explicit telemetry and bounded observer required')
    telemetry = telemetry or observer is not None
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
    controller_timings = {key: 0.0 for key in CONTROLLER_TIMINGS} if telemetry else None
    controller_source_accounting = empty_source_accounting() if telemetry else None
    controller_identity = None
    observer_count, observer_failure = 0, None
    def emit(event, worker=None, index=None, cleanup=None):
        nonlocal observer_count, observer_failure
        if observer is None or observer_failure is not None:
            return
        observer_started = time.monotonic()
        try:
            if observer_count >= observer_max_events:
                raise PoolStopped('observer_event_budget_exceeded')
            mailbox = {name: 0 for name in ('source', 'request', 'result')}
            for owned in workers:
                if owned['guarded'].fd is None:
                    continue
                for name, category in (('source.bin', 'source'), ('request.json', 'request'),
                                       ('payload.json', 'result'), ('result.json', 'result')):
                    try:
                        mailbox[category] += owned['guarded'].info(name).st_size
                    except FileNotFoundError:
                        pass
            actual = sum('process' in owned for owned in workers)
            pending = sum(owned['pending'] is not None for owned in workers)
            value = {'schema_version': 1, 'event': event, 'monotonic_ns': time.monotonic_ns(),
                'mode': mode, 'configured_concurrency': concurrency, 'workers_started': actual,
                'live_workers': sum('process' in owned and owned['process'].poll() is None for owned in workers),
                'pending_requests': pending, 'inflight_reserved_bytes': pending * reservation,
                'mailbox_source_bytes': mailbox['source'], 'mailbox_request_bytes': mailbox['request'],
                'mailbox_result_bytes': mailbox['result'], 'admitted_bytes': admitted_bytes,
                'role': 'worker' if worker is not None else 'controller',
                'controller': dict(controller_identity),
                'worker': dict(worker['process_identity']) if worker is not None else None,
                'index': index, 'cleanup': dict(cleanup) if cleanup is not None else None}
            _event_valid(value)
            _encoded(value, CONTROL_BYTES)
            observer_count += 1
            if observer(value) is not True:
                raise PoolStopped('observer_refused')
        except BaseException as error:
            observer_failure = (str(error) if type(error) is PoolStopped and str(error) in
                ('observer_event_budget_exceeded', 'observer_refused') else 'observer_error')
            raise PoolStopped(observer_failure) from None
        finally:
            controller_timings['observer_seconds'] += time.monotonic() - observer_started
        if event in ('readiness', 'submit', 'receive'):
            _check(cancel, deadline)
    try:
        if telemetry:
            controller_identity = _process_identity(os.getpid())
        emit('readiness')
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
                if telemetry:
                    _verify_worker(worker)
                received = _receive(worker, identity, token, per_file, limits, cancel, controller_timings)
                if received is None:
                    if worker['process'].poll() is not None:
                        raise PoolStopped('worker_exited')
                    continue
                file, receipt = received
                worker['pending'] = None
                worker.setdefault('resources', []).append(receipt['resources'])
                admission_started = time.monotonic() if telemetry else None
                try:
                    if file is None:
                        result.failures.append({'index': pending['index'], 'record': pending['record'],
                                                'kind': receipt['error_kind'], 'reason': receipt['reason']})
                    else:
                        admitted_bytes += receipt['bytes']
                        nodes += file.counts['nodes']
                        facts += file.counts['definitions'] + len(file.imports)
                        if (admitted_bytes > min(limits.max_admitted_bytes, budget.max_collected_bytes) or
                                nodes > budget.max_nodes or facts > budget.max_facts):
                            result.failures.append({'index': pending['index'], 'record': pending['record'],
                                                    'kind': 'aggregate_budget_exceeded'})
                            raise PoolStopped('aggregate_budget_exceeded')
                        completed[pending['index']] = file
                        if file.partial or file.errors:
                            result.failures.append({'index': pending['index'], 'record': pending['record'],
                                                    'kind': 'partial_file', 'error_count': len(file.errors)})
                finally:
                    if telemetry:
                        controller_timings['admission_seconds'] += time.monotonic() - admission_started
                if file is None or file.partial or file.errors:
                    emit('failure', worker, pending['index'])
                if file is None:
                    if receipt['error_kind'] == 'BackendUnavailable':
                        raise PoolStopped('backend_unavailable')
                emit('receive', worker, pending['index'])
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
                source_measurements = {} if telemetry else None
                try:
                    record = _timed(controller_timings, 'admission_seconds', _record, blob, budget,
                                    source_measurements=source_measurements)
                finally:
                    if telemetry:
                        for key, value in source_measurements.items():
                            controller_source_accounting[key] += value
                admission_started = time.monotonic() if telemetry else None
                try:
                    if count >= budget.max_files or source_bytes + record['bytes'] > budget.max_total_bytes:
                        raise PoolStopped('source_admission_budget_exceeded')
                    if record['path'] in seen:
                        raise ValueError('Duplicate source path')
                    seen.add(record['path'])
                    source_bytes += record['bytes']
                finally:
                    if telemetry:
                        controller_timings['admission_seconds'] += time.monotonic() - admission_started
                worker = next((w for w in workers if w['pending'] is None), None)
                if worker is None:
                    worker_name = 'worker-' + str(len(workers))
                    worker = _start(_new_directory(run, worker_name), identity, token, per_file,
                                    limits, workers, cancel, deadline, telemetry)
                    emit('readiness', worker)
                if telemetry:
                    _verify_worker(worker)
                request = _encoded({'schema_version': 1, 'token': token, 'index': count,
                                     'identity': identity, 'record': record}, limits.max_request_bytes)
                _timed(controller_timings, 'mailbox_write_seconds', _write,
                       worker['guarded'], 'source.bin', blob['content'], budget.max_file_bytes)
                _timed(controller_timings, 'mailbox_write_seconds', _write,
                       worker['guarded'], 'request.json', request, limits.max_request_bytes)
                worker['pending'] = {'index': count, 'record': record,
                    'deadline': min(deadline, time.monotonic() + limits.worker_wall_seconds)}
                worker['requests'] += 1
                count += 1
                inflight_peak = max(inflight_peak, sum(w['pending'] is not None for w in workers) * reservation)
                emit('submit', worker, count - 1)
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
        result.status = 'failed' if observer_failure is not None else ('partial' if completed else 'failed')
        for worker in workers:
            if worker['pending'] is not None:
                result.failures.append({'index': worker['pending']['index'],
                    'record': worker['pending']['record'], 'kind': type(error).__name__,
                    'reason': result.stop_reason})
        if not result.failures:
            result.failures.append({'index': count, 'kind': type(error).__name__, 'reason': result.stop_reason})
        try:
            emit('failure', index=count if count < 4096 else None)
        except PoolStopped as observer_error:
            result.status, result.stop_reason = 'failed', str(observer_error)
            result.failures.append({'index': count, 'kind': 'observer_failed', 'reason': result.stop_reason})
    finally:
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
            worker['pending'] = None
            if telemetry and 'process_identity' in worker:
                try:
                    emit('cleanup', worker, cleanup={key: cleanup[key] for key in
                        ('leader_reaped', 'group_absent', 'mailboxes_removed')})
                except PoolStopped as error:
                    result.status, result.stop_reason = 'failed', str(error)
                    result.failures.append({'index': count, 'kind': 'observer_failed', 'reason': result.stop_reason})
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
        if telemetry:
            result.resources['telemetry'] = {'schema_version': 1, 'controller_timings': controller_timings,
                'controller_identity': controller_identity, 'observer_events_delivered': observer_count,
                'observer_failed': observer_failure is not None, 'observer_failure_reason': observer_failure,
                'actual_workers_started': sum('process' in worker for worker in workers),
                'worker_process_identities': [dict(worker['process_identity']) for worker in workers if 'process_identity' in worker],
                'source_accounting': {'schema_version': 1,
                    'controller_source_validation': controller_source_accounting,
                    'worker_requests_with_accounting': sum(len(worker.get('resources', [])) for worker in workers),
                    'worker_requests_unknown': count - sum(len(worker.get('resources', [])) for worker in workers),
                    'worker_accounting_complete': count == sum(len(worker.get('resources', [])) for worker in workers),
                    'scope': 'source-only guarded mailbox reads and explicit buffer hashes; missing worker receipts are unknown'}}
        summary = {'status': result.status, 'stop_reason': result.stop_reason,
            'failures': [{key: value for key, value in row.items() if key != 'record'}
                         for row in result.failures],
            'resources': result.resources, 'cleanup': result.cleanup,
            'runtime_qualified': False, 'selected_engine': None}
        summary_cap = max(1024 * 1024, budget.max_files * 512 + CONTROL_BYTES)
        try:
            _write(run, 'receipt.json', _encoded(summary, summary_cap), summary_cap)
        except (RuntimeError, ValueError, OSError) as error:
            # A directory fsync can fail after publication. Preserve the result
            # and cleanup, and remove that receipt so it cannot imply success.
            result.status, result.stop_reason = 'failed', 'input_or_protocol_rejected'
            result.failures.append({'index': count, 'kind': type(error).__name__,
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
        if (type(control) is not dict or set(control) != {'schema_version', 'token', 'identity', 'budget', 'limits', 'telemetry'} or
                type(control['schema_version']) is not int or control['schema_version'] != 1 or
                type(control['telemetry']) is not bool):
            return 2
        telemetry = control['telemetry']
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
        def identity_current():
            try: return _identity() == identity
            except (OSError, ValueError, RuntimeError): return False
        isolated = {'python_isolated_mode': bool(sys.flags.isolated),
                    'bytecode_writes_disabled': bool(sys.dont_write_bytecode),
                    'user_site_disabled': bool(sys.flags.no_user_site),
                    'private_environment': all(Path(os.environ.get(key, '')).resolve() == job.root / name
                        for key, name in (('HOME', 'home'), ('XDG_CONFIG_HOME', 'config'),
                            ('XDG_CACHE_HOME', 'cache'), ('XDG_DATA_HOME', 'data'), ('TMPDIR', 'tmp'))),
                    'own_session_and_group': os.getpid() == os.getsid(0) == os.getpgrp(),
                    'controller_death_signal': True}
        ready = {'schema_version': 1, 'token': token,
            'identity': identity, 'pid': os.getpid(), 'limits': asdict(limits),
            'isolation': isolated}
        if telemetry:
            ready['process_identity'] = _process_identity(os.getpid())
        _write(job, 'ready.json', _encoded(ready, CONTROL_BYTES), CONTROL_BYTES)
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
            record = request['record']
            try:
                baseline._record_valid(record, budget)
            except ValueError:
                return 2
            started = time.monotonic()
            before_cpu = resource.getrusage(resource.RUSAGE_SELF) if telemetry else None
            timings = {} if telemetry else None
            source_rows = {name: {} for name in SOURCE_ACCOUNTING_PASSES} if telemetry else None
            raw = supplied = None
            payload, error_kind, reason = None, None, None
            try:
                raw = _read(job, 'source.bin', budget.max_file_bytes,
                            measurements=source_rows['mailbox_source'] if telemetry else None)[0]
                supplied = {**record, 'content': raw}
                if _record(supplied, budget, source_measurements=
                        source_rows['worker_source_validation'] if telemetry else None) != record:
                    raise ValueError('Owned source differs from admitted identity')
                if not identity_current():
                    return 2
                collected = baseline.collect_file(supplied, budget=budget, measurements=timings,
                    source_measurements=source_rows['native_source_validation'] if telemetry else None)
                if telemetry:
                    timings['handoff_serialize_seconds'] = 0.0
                before = time.monotonic() if telemetry else None
                try:
                    payload = collected.to_json(budget=budget)
                finally:
                    if telemetry:
                        timings['handoff_serialize_seconds'] += time.monotonic() - before
                del collected
            except baseline.BackendUnavailable:
                error_kind, reason = 'BackendUnavailable', 'backend_unavailable'
            except baseline.StopScan:
                error_kind, reason = 'StopScan', 'collection_stopped'
            except (ValueError, UnicodeError, MemoryError, RecursionError) as error:
                error_kind = 'UnicodeError' if isinstance(error, UnicodeError) else type(error).__name__
                reason = 'collection_failed'
            except OSError:
                error_kind, reason = 'OSError', 'collection_failed'
            if error_kind is not None and not identity_current():
                return 2  # Source failures cannot rescue a stale loaded implementation.
            usage = resource.getrusage(resource.RUSAGE_SELF)
            resources = {'elapsed_seconds': time.monotonic() - started,
                'process_peak_rss_bytes': usage.ru_maxrss * (1 if sys.platform == 'darwin' else 1024),
                'process_user_seconds': usage.ru_utime, 'process_system_seconds': usage.ru_stime}
            if telemetry:
                for key in NATIVE_TIMINGS: timings.setdefault(key, 0.0)
                resources.update(file_user_seconds=usage.ru_utime - before_cpu.ru_utime,
                    file_system_seconds=usage.ru_stime - before_cpu.ru_stime, timings=timings,
                    source_accounting={'schema_version': 1, 'transport': 'immutable_mailbox_blob_v1',
                        'source_sha256': record['sha256'], 'source_bytes': record['bytes'],
                        'original_source_stream_bytes': 0, 'accounting_complete': True,
                        'passes': {name: row or empty_source_accounting() for name, row in source_rows.items()}})
                _resources(resources, True, record, budget)
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
