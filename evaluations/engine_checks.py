"""Finite native worker checks; unavailable engine capabilities remain blocked.

The coordinator may call run_checks(evidence_directory=PRIVATE_DIRECTORY).
Workers receive only copied source bytes and metadata inventories. This module
never installs a backend, imports fixture code, or selects a structural owner.
"""
import ast
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluations.analysis import frozen_inputs, read_json, write_result
from evaluations.tree_sitter_baseline import BackendUnavailable, Budget, scan
from repo_graph.source import SourceRoot

SCAN_BUDGET = Budget(max_files=10, max_file_bytes=2048, max_total_bytes=8192,
                     max_nodes=30_000, max_facts=2048, timeout_seconds=3)
WALL_SECONDS = 5
GRACE_SECONDS = 0.25
MEMORY_BYTES = 512 * 1024 * 1024
LOG_BYTES = 1024 * 1024


from repo_graph.analysis_queue import _group_exists, _stop_and_reap, _environment



def _start_worker(job, logs, inventory, mode='scan'):
    # Parent descriptor paths are not inherited by the isolated child. Resolve
    # the already-created private job before giving it cwd/HOME/config paths.
    job = job.resolve(strict=True)
    write_result(job, 'control.json', {'inventory': inventory, 'mode': mode,
                                     'budget': asdict(SCAN_BUDGET)}, LOG_BYTES)
    outputs = [logs / (job.name + '.' + stream + '.log') for stream in ('stdout', 'stderr')]
    handles = [path.open('xb') for path in outputs]
    try:
        process = subprocess.Popen([sys.executable, '-I', '-B', str(Path(__file__).resolve()), '--worker'],
            cwd=job, env=_environment(job), stdin=subprocess.DEVNULL,
            stdout=handles[0], stderr=handles[1], start_new_session=True)
    finally:
        for handle in handles:
            handle.close()
    return process


def _wait_ready(job, process, seconds=WALL_SECONDS):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with SourceRoot(job) as source:
            try:
                ready, _ = read_json(source, 'ready.json')
                return ready
            except FileNotFoundError:
                pass
        if process.poll() is not None:
            return None
        time.sleep(0.01)
    return None


def _summary(job):
    try:
        with SourceRoot(job) as source:
            result, _ = read_json(source, 'result.json')
        return result
    except (OSError, ValueError):
        return None


def _worker():
    """Internal worker entry; operating-system limits affect this child only."""
    job = Path.cwd()
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (5, 6))
        resource.setrlimit(resource.RLIMIT_FSIZE, (LOG_BYTES, LOG_BYTES))
        if hasattr(os, 'sched_getaffinity'):
            os.sched_setaffinity(0, sorted(os.sched_getaffinity(0))[:2])
        with SourceRoot(job) as source:
            control, _ = read_json(source, 'control.json')
        mode = control['mode']
        if mode not in ('scan', 'barrier', 'cancel', 'forced_stall'):
            raise ValueError('Unknown worker mode')
        if mode == 'barrier':
            write_result(job, 'ready.json', {'phase': 'worker_ready_before_scan'}, 1024)
            deadline = time.monotonic() + WALL_SECONDS
            release = None
            while time.monotonic() < deadline:
                with SourceRoot(job) as source:
                    try:
                        release, _ = read_json(source, 'release.json')
                        break
                    except FileNotFoundError:
                        pass
                time.sleep(0.005)
            if release is None:
                raise TimeoutError('Release barrier not supplied')
            while time.monotonic() < release['at_monotonic']:
                time.sleep(0.001)
        before = time.monotonic()
        result = scan(job / 'source', control['inventory'], Budget(**control['budget']),
                      cancel=(lambda: True) if mode == 'cancel' else None)
        after = time.monotonic()
        usage = resource.getrusage(resource.RUSAGE_SELF)
        confined = all(Path(os.environ[key]) == job / name for key, name in (
            ('HOME', 'home'), ('XDG_CONFIG_HOME', 'config'), ('XDG_CACHE_HOME', 'cache'),
            ('XDG_DATA_HOME', 'data'), ('TMPDIR', 'tmp')))
        summary = {'status': result['status'], 'inventory': result['inventory'],
            'source_identity': result['source_identity'], 'versions': result['versions'],
            'facts_sha256': hashlib.sha256(json.dumps(result['facts'], sort_keys=True,
                separators=(',', ':')).encode()).hexdigest(),
            'counts': {name: len(result['facts'][name]) for name in ('definitions', 'sites')},
            'resources': dict(result['resources'], process_peak_rss_bytes=usage.ru_maxrss * 1024,
                process_user_seconds=usage.ru_utime, process_system_seconds=usage.ru_stime),
            'scan_interval': {'start_monotonic': before, 'end_monotonic': after},
            'isolation': {'python_isolated_mode': bool(sys.flags.isolated),
                'bytecode_writes_disabled': bool(sys.dont_write_bytecode),
                'user_site_disabled': bool(sys.flags.no_user_site), 'home_config_cache_temp_confined': confined,
                'own_session_and_group': os.getsid(0) == os.getpid() == os.getpgrp()},
            'engine_limits': result['limits'], 'stop_reason': result['stop_reason'],
            'worker_limits': {'address_space_bytes': MEMORY_BYTES, 'cpu_seconds': 5,
                              'cpu_affinity_count': len(os.sched_getaffinity(0)), 'log_bytes': LOG_BYTES}}
        write_result(job, 'result.json', summary, LOG_BYTES)
        print(json.dumps({'status': summary['status'], 'counts': summary['counts']}), flush=True)
        if mode == 'forced_stall':
            # Deliberate controller fault probe after an actual finite scan. It
            # proves owned-group cleanup, not cancellation inside native parse.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            write_result(job, 'ready.json', {'phase': 'controlled_post_scan_stall'}, 1024)
            deadline = time.monotonic() + WALL_SECONDS
            while time.monotonic() < deadline:
                time.sleep(0.05)
            return 3
        return 0
    except (BackendUnavailable, OSError, ValueError, KeyError, TimeoutError, MemoryError) as error:
        write_result(job, 'result.json', {'status': 'blocked', 'error_kind': type(error).__name__}, LOG_BYTES)
        print(json.dumps({'status': 'blocked', 'error_kind': type(error).__name__}), flush=True)
        return 2


def run_checks(root=ROOT, evidence_directory=None):
    """Return portable checks and preserve logs in the specified private folder."""
    root = Path(root)
    if root.resolve() != ROOT.resolve():
        raise ValueError('Lifecycle implementation root must match the worker checkout')
    if os.name != 'posix' or not hasattr(os, 'sched_getaffinity'):
        return {'schema_version': 1, 'status': 'blocked', 'reason': 'Linux worker qualification required',
                'engine_selected': False, 'qualification_complete': False}
    if evidence_directory is None:
        raise ValueError('Explicit private evidence directory required')
    fixture, identity = frozen_inputs(root)
    metadata = [{key: item[key] for key in ('path', 'language', 'kind', 'sha256', 'bytes')}
                for item in fixture['files']]
    blobs, implementation, exports = {}, {}, []
    with SourceRoot(Path(root)) as source:
        for item in metadata:
            if source.info(item['path']).st_size > SCAN_BUDGET.max_file_bytes:
                raise ValueError('Frozen source exceeds finite lifecycle file limit')
            raw, sha, info = source.read(item['path'], SCAN_BUDGET.max_file_bytes + 1, hash_full=True)
            if sha != item['sha256'] or len(raw) != item['bytes'] or len(raw) != info.st_size:
                raise ValueError('Frozen complete source identity mismatch')
            blobs[item['path']] = raw
        if (len(blobs) >= SCAN_BUDGET.max_files or sum(map(len, blobs.values())) > SCAN_BUDGET.max_total_bytes or
                max(map(len, blobs.values())) > SCAN_BUDGET.max_file_bytes):
            raise ValueError('Frozen source exceeds finite lifecycle inventory')
        for path in ('evaluations/engine_checks.py', 'evaluations/code-understanding/test_engine_checks.py',
                     'repo_graph/analysis_native.py', 'evaluations/analysis.py', 'repo_graph/source.py'):
            if source.info(path).st_size > LOG_BYTES:
                raise ValueError('Implementation identity exceeds bound')
            raw, sha, info = source.read(path, LOG_BYTES + 1, hash_full=True)
            if len(raw) != info.st_size or len(raw) > LOG_BYTES:
                raise ValueError('Implementation identity exceeds bound')
            implementation[path] = sha
            if path.endswith('analysis_native.py'):
                exports = [item.name for item in ast.parse(raw).body if isinstance(item, ast.FunctionDef)]
    evidence_directory = Path(evidence_directory)
    logs = evidence_directory / ('lifecycle-' + uuid.uuid4().hex[:12])
    logs.mkdir(mode=0o700)
    checks, owned = [], []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='engine-checks-', dir=evidence_directory) as scratch:
        scratch = Path(scratch)
        canary = b'def external_canary():\n    raise RuntimeError("scratch only")\n'
        (scratch / 'canary.py').write_bytes(canary)
        before_canary = (scratch / 'canary.py').stat()

        def job(name):
            directory = scratch / name
            (directory / 'source').mkdir(parents=True)
            for path, raw in blobs.items():
                target = directory / 'source' / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
            return directory

        def start(directory, inventory=metadata, mode='scan'):
            process = _start_worker(directory, logs, inventory, mode)
            owned.append(process)
            return process

        def finish(process, directory):
            timed_out = False
            try:
                process.wait(timeout=WALL_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
            cleanup = _stop_and_reap(process)
            return {'worker': _summary(directory), 'timed_out': timed_out, 'cleanup': cleanup}

        try:
            finite = job('finite')
            first = finish(start(finite), finite)
            passed = (first['worker'] is not None and first['worker']['status'] == 'complete' and
                      all(first['worker']['isolation'].values()) and
                      first['cleanup']['leader_reaped'] and first['cleanup']['group_absent'] and not first['timed_out'])
            checks.append({'id': 'finite_native_scan', 'status': 'passed' if passed else 'failed', **first})

            pairs = [job('coexist-' + str(i)) for i in range(2)]
            processes = [start(directory, mode='barrier') for directory in pairs]
            ready = [_wait_ready(directory, process) for directory, process in zip(pairs, processes)]
            live = all(item is not None for item in ready) and all(process.poll() is None for process in processes)
            release = time.monotonic() + 0.1
            for directory in pairs:
                write_result(directory, 'release.json', {'at_monotonic': release}, 1024)
            concurrent = [finish(process, directory) for directory, process in zip(pairs, processes)]
            summaries = [item['worker'] for item in concurrent]
            overlap = (all(item and 'scan_interval' in item for item in summaries) and
                max(item['scan_interval']['start_monotonic'] for item in summaries) <
                min(item['scan_interval']['end_monotonic'] for item in summaries))
            equal = all(item and item.get('facts_sha256') == first['worker'].get('facts_sha256') for item in summaries) if first['worker'] else False
            passed = live and overlap and equal and all(item['worker']['status'] == 'complete' and
                all(item['worker']['isolation'].values()) and
                item['cleanup']['leader_reaped'] and item['cleanup']['group_absent'] and not item['timed_out'] for item in concurrent)
            checks.append({'id': 'two_coexisting_scans', 'status': 'passed' if passed else 'failed',
                'both_workers_live_before_release': live, 'scan_intervals_overlap': bool(overlap),
                'same_source_facts': equal, 'workers': concurrent})

            cancelled = job('cancelled')
            cancel = finish(start(cancelled, mode='cancel'), cancelled)
            summary = cancel['worker']
            passed = bool(summary and summary.get('inventory') and
                all(item['status'] == 'cancelled' for item in summary['inventory']) and
                summary['resources']['source_bytes'] == 0 and cancel['cleanup']['group_absent'] and
                cancel['cleanup']['leader_reaped'] and not cancel['timed_out'])
            checks.append({'id': 'cooperative_cancel_before_source_read', 'status': 'passed' if passed else 'failed',
                           'scope': 'Cancellation callback before source read; native parse interruption remains unavailable', **cancel})

            safety = job('safety')
            (safety / 'source' / 'outward.py').symlink_to(scratch / 'canary.py')
            outward = dict(path='outward.py', language='python', kind='source', bytes=len(canary),
                           sha256=hashlib.sha256(canary).hexdigest())
            safe = finish(start(safety, metadata + [outward]), safety)
            summary = safe['worker']
            denied = summary and [item for item in summary.get('inventory', []) if item['path'] == 'outward.py']
            passed = bool(denied and len(denied) == 1 and denied[0]['status'] == 'source_error' and
                          safe['cleanup']['leader_reaped'] and safe['cleanup']['group_absent'] and not safe['timed_out'])
            checks.append({'id': 'outside_root_symlink_rejected', 'status': 'passed' if passed else 'failed', **safe})

            timeout = job('timeout')
            process = start(timeout, mode='forced_stall')
            ready = _wait_ready(timeout, process)
            timed_out = False
            try:
                process.wait(timeout=0.15)
            except subprocess.TimeoutExpired:
                timed_out = True
            cleanup = _stop_and_reap(process)
            worker = _summary(timeout)
            passed = bool(ready and ready['phase'] == 'controlled_post_scan_stall' and timed_out and
                          worker and worker.get('status') == 'complete' and cleanup['leader_reaped'] and
                          cleanup['group_absent'] and cleanup['signals'] == ['SIGTERM', 'SIGKILL'])
            checks.append({'id': 'forced_timeout_kill_and_reap', 'status': 'passed' if passed else 'failed',
                'scope': 'Controlled post-scan stall ignores TERM to exercise owned-group KILL and reap',
                'timeout_seconds_after_ready': 0.15, 'timed_out': timed_out, 'cleanup': cleanup, 'worker': worker})
        finally:
            for process in owned:
                _stop_and_reap(process)
        with SourceRoot(scratch) as source:
            _, after_sha, after_canary = source.read('canary.py', len(canary) + 1, hash_full=True)
        unchanged = (after_sha == hashlib.sha256(canary).hexdigest() and
            (before_canary.st_ino, before_canary.st_size, before_canary.st_mtime_ns) ==
            (after_canary.st_ino, after_canary.st_size, after_canary.st_mtime_ns))
        checks.append({'id': 'external_scratch_canary_unchanged', 'status': 'passed' if unchanged else 'failed',
                       'sha256': after_sha, 'scope': 'Owned canary outside all worker source/home/cache/config roots'})
    checks.append({'id': 'owned_runtime_scratch_removed', 'status': 'passed' if not scratch.exists() else 'failed'})
    log_receipts = []
    with SourceRoot(logs) as source:
        for path in sorted(logs.iterdir()):
            _, sha, info = source.read(path.name, LOG_BYTES + 1, hash_full=True)
            log_receipts.append({'path': logs.name + '/' + path.name, 'sha256': sha, 'bytes': info.st_size})
    with SourceRoot(Path(root)) as source:
        stable = all(source.read(path, LOG_BYTES + 1, hash_full=True)[1] == expected
                     for path, expected in implementation.items())
    checks.append({'id': 'implementation_identity_stable', 'status': 'passed' if stable else 'failed'})
    return {'schema_version': 1, 'engine': 'tree-sitter',
        'status': 'passed' if all(c['status'] == 'passed' for c in checks) else 'failed',
        'engine_selected': False, 'qualification_complete': False, 'source_identity': identity,
        'requirement_refs': ['code-understanding:T007', 'docs/adr/0006-analysis-qualification.md#required-gates'],
        'implementation_sha256': implementation, 'check_results': checks, 'logs': log_receipts,
        'resources': {'elapsed_seconds': time.monotonic() - started, 'worker_processes_started': len(owned),
            'frozen_source_files': len(metadata), 'frozen_source_bytes': sum(map(len, blobs.values()))},
        'limits': {'scan': asdict(SCAN_BUDGET), 'worker_wall_seconds': WALL_SECONDS,
            'termination_grace_seconds': GRACE_SECONDS, 'worker_address_space_bytes': MEMORY_BYTES,
            'worker_cpu_seconds': 5, 'worker_cpu_affinity_max': 2, 'worker_log_bytes': LOG_BYTES},
        'limitations': ['This finite evaluation controller is not a product engine lifecycle API.',
            'No test interrupts an in-flight native parse; cancellation before read and forced process cleanup are distinct.',
            'Coexistence is measured between two isolated owned workers; unrelated live sessions were not inspected or changed.',
            'Lifecycle checks do not establish real-source call quality, scale, installation distribution, or engine selection.']}




ADAPTER_HELPERS = ('evaluations/incremental_candidate.py', 'repo_graph/analysis_queue.py',
    'repo_graph/analysis_native.py', 'evaluations/bounded_queries.py',
    'repo_graph/source.py', 'evaluations/engine_checks.py',
    'evaluations/analysis.py', 'evaluations/acceptance.py')
ADAPTER_REPORT_BYTES = 8 * 1024 * 1024


def _adapter_root(root):
    root = Path(root).resolve(strict=True)
    if os.name != 'posix' or not Path('/proc/self/fd').is_dir():
        raise OSError('Adapter directory ownership requires Linux descriptor paths')
    if not __debug__:
        raise ValueError('Adapter grader assertions require unoptimized Python')
    if root != ROOT.resolve(strict=True) or Path(__file__).resolve() != root / 'evaluations/engine_checks.py':
        raise ValueError('Adapter root must match its loaded implementation checkout')
    return root


def _adapter_head(root):
    result = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
        'rev-parse', '--verify', 'HEAD'], cwd=root, capture_output=True, text=True, timeout=20, check=True)
    value = result.stdout.strip()
    if len(value) != 40 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('Full current commit identity required')
    return value


def _adapter_bytes(root, path, expected=None, cap=1024 * 1024):
    with SourceRoot(root) as source:
        raw, digest, info = source.read(path, cap + 1, hash_full=False)
    if len(raw) != info.st_size or len(raw) > cap or expected is not None and digest != expected:
        raise ValueError('Adapter input identity or byte bound mismatch')
    return raw, digest


def _adapter_json(root, path, bound):
    from evaluations.supplement_preparation import decode
    expected = bound['input_binding'].get(path)
    if expected is None:
        raise ValueError('Input absent from committed adapter binding')
    return decode(_adapter_bytes(root, path, expected)[0])


def _adapter_capture(root):
    from evaluations.supplement_preparation import prepare_check, LOCK, decode
    from evaluations.acceptance import committed
    from evaluations.tree_sitter_baseline import collector_identity
    head = _adapter_head(root)
    implementation = {path: _adapter_bytes(root, path, cap=2 * 1024 * 1024)[1] for path in ADAPTER_HELPERS}
    if collector_identity() != implementation['repo_graph/analysis_native.py']:
        raise ValueError('Loaded collector differs from current adapter manifest')
    if not committed(root, implementation) or _adapter_head(root) != head:
        raise ValueError('Stable committed adapter implementation required')
    prepared = prepare_check(root)
    if prepared['checks_passed'] != 2322 or prepared['physical_ranges'] != 535:
        raise ValueError('Approved finite preparation receipt required')
    lock_raw, lock_sha = _adapter_bytes(root, LOCK, prepared['supplement_lock_sha256'])
    lock = decode(lock_raw)
    binding = dict(lock['sha256'])
    binding[LOCK] = lock_sha
    if len(binding) != 31 or not committed(root, binding):
        raise ValueError('Complete committed adapter input binding required')
    for path, expected in binding.items():
        _adapter_bytes(root, path, expected)
    if _adapter_head(root) != head:
        raise ValueError('Commit changed during adapter admission')
    with SourceRoot(root) as source:
        owner_identity = source.identity
    return {'measured_commit': head, 'implementation': implementation, 'input_binding': binding,
        'preparation': prepared, 'root_identity': owner_identity}


def _adapter_recheck(root, bound):
    from evaluations.acceptance import committed
    if _adapter_head(root) != bound['measured_commit']:
        raise ValueError('Commit changed during adapter experiment')
    with SourceRoot(root) as source:
        if source.identity != bound['root_identity']:
            raise ValueError('Adapter checkout owner changed')
    for path, expected in bound['implementation'].items():
        _adapter_bytes(root, path, expected, 2 * 1024 * 1024)
    for path, expected in bound['input_binding'].items():
        _adapter_bytes(root, path, expected)
    if not committed(root, bound['implementation'] | bound['input_binding']) or _adapter_head(root) != bound['measured_commit']:
        raise ValueError('Adapter committed binding changed')
    return {'measured_commit': bound['measured_commit'], 'implementation': dict(bound['implementation']),
        'input_binding': dict(bound['input_binding']), 'root_identity': bound['root_identity']}


@contextmanager
def _adapter_child(parent, name):
    parts = SourceRoot.parts(name)
    if len(parts) != 1 or parts[0] != name or '\\' in name or ':' in name:
        raise ValueError('One canonical private directory component required')
    with SourceRoot(parent) as owner:
        expected, actual = os.stat(parent), os.fstat(owner.fd)
        if not owner.secure or (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            raise OSError('Linux descriptor-owned private directories required')
        os.mkdir(name, 0o700, dir_fd=owner.fd)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=owner.fd)
        try:
            yield Path('/proc/self/fd') / str(fd)
        finally:
            os.close(fd)


@contextmanager
def _adapter_run(root, evidence_directory, label):
    parent = Path(evidence_directory).absolute()
    resolved = parent.resolve(strict=True)
    bridge = parent.parent == Path('/proc/self/fd') and parent.name.isdecimal()
    if (resolved != parent and not bridge) or resolved == root or root in resolved.parents:
        raise ValueError('Explicit existing private evidence parent outside checkout required')
    with SourceRoot(parent) as owner:
        expected, actual = os.stat(parent), os.fstat(owner.fd)
        if not owner.secure or owner.root != resolved or (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            raise OSError('Private evidence ownership changed')
        name = label + '-' + uuid.uuid4().hex
        os.mkdir(name, 0o700, dir_fd=owner.fd)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=owner.fd)
        try:
            yield Path('/proc/self/fd') / str(fd), name
        finally:
            os.close(fd)


def _adapter_materialize(root, blobs, removed=()):
    if len(blobs) > 128 or sum(len(raw) for raw in blobs.values()) > 4 * 1024 * 1024:
        raise ValueError('Private source materialization exceeds finite input bound')
    with SourceRoot(root) as owner:
        expected, actual = os.stat(root), os.fstat(owner.fd)
        if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            raise ValueError('Private source owner changed before materialization')
        for path in removed:
            parts = SourceRoot.parts(path)
            parent = os.dup(owner.fd)
            try:
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    os.close(parent);parent = child
                os.unlink(parts[-1], dir_fd=parent)
            finally:
                os.close(parent)
        for path, raw in blobs.items():
            if type(raw) is not bytes or len(raw) > 512 * 1024:
                raise ValueError('Immutable finite source bytes required')
            parts = SourceRoot.parts(path)
            if str(Path(path)) != path or '\\' in path or ':' in path:
                raise ValueError('Canonical private source path required')
            parent = os.dup(owner.fd)
            try:
                for part in parts[:-1]:
                    try:
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    except FileNotFoundError:
                        os.mkdir(part, 0o700, dir_fd=parent)
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    os.close(parent);parent = child
                with SourceRoot(Path('/proc/self/fd') / str(parent)) as target:
                    expected, actual = os.fstat(parent), os.fstat(target.fd)
                    if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
                        raise ValueError('Private source directory owner changed')
                    with target.atomic_writer(parts[-1]) as stream:
                        stream.write(raw)
            finally:
                os.close(parent)


def _adapter_operations(base, metadata, update):
    """Apply the existing finite frozen source operations, without gold facts."""
    changed = dict(base)
    inventory = {path: dict(record) for path, record in metadata.items()}
    for operation in update['operations']:
        path = operation['path']
        if operation['op'] == 'add':
            assert path not in changed
            changed[path] = operation['content'].encode()
            inventory[path] = {'path': path, 'language': update['language'], 'kind': 'source'}
        else:
            assert path in changed
            if 'sha256_before' in operation:
                assert hashlib.sha256(changed[path]).hexdigest() == operation['sha256_before']
            if operation['op'] == 'delete':
                del changed[path]; del inventory[path]
            elif operation['op'] == 'rename':
                assert operation['to'] not in changed
                changed[operation['to']] = changed.pop(path)
                inventory[operation['to']] = dict(inventory.pop(path), path=operation['to'])
                path = operation['to']
            elif operation['op'] == 'replace':
                assert changed[path].count(operation['old'].encode()) == operation['occurrences']
                changed[path] = changed[path].replace(operation['old'].encode(), operation['new'].encode())
            else:
                raise ValueError('Unfrozen operation')
        if 'sha256_after' in operation:
            assert hashlib.sha256(changed[path]).hexdigest() == operation['sha256_after']
    for path, raw in changed.items():
        inventory[path].update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
    return changed, inventory


@contextmanager
def _adapter_source(parent):
    """Hold a unique source directory through collection and fd-relative cleanup."""
    if not shutil.rmtree.avoids_symlink_attacks:
        raise OSError('Descriptor-relative source cleanup required')
    with SourceRoot(parent) as owner:
        expected, actual = os.stat(parent), os.fstat(owner.fd)
        if not owner.secure or (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            raise ValueError('Private source parent owner changed')
        name = 'source-' + uuid.uuid4().hex
        os.mkdir(name, 0o700, dir_fd=owner.fd)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=owner.fd)
        try:
            captured = os.fstat(fd)
            try:
                yield Path('/proc/self/fd') / str(fd)
            finally:
                named = os.stat(name, dir_fd=owner.fd, follow_symlinks=False)
                held = os.fstat(fd)
                if (not stat.S_ISDIR(named.st_mode) or
                        (captured.st_dev, captured.st_ino) != (named.st_dev, named.st_ino) or
                        (captured.st_dev, captured.st_ino) != (held.st_dev, held.st_ino)):
                    raise ValueError('Private source cleanup name no longer identifies its captured owner')
                # This detects an observed name swap. The check-to-rmtree same-name
                # race is not atomic; private directories require one writer.
                shutil.rmtree(name, dir_fd=owner.fd)
        finally:
            os.close(fd)


def _adapter_error(error):
    import traceback
    text, trace = str(error), traceback.format_exc()
    return {'error_kind': type(error).__name__, 'error': text[:1024], 'traceback': trace[:16384],
        'diagnostic_truncated': len(text) > 1024 or len(trace) > 16384}


def _adapter_dump(parent, name, value):
    """Bound the same JSON format and write through the verified held owner."""
    parts = SourceRoot.parts(name)
    if len(parts) != 1 or parts[0] != name or '\\' in name or ':' in name:
        raise ValueError('Canonical private receipt component required')
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode() + b'\n'
    if len(raw) > ADAPTER_REPORT_BYTES:
        raise ValueError('Private adapter report byte bound exceeded')
    with SourceRoot(parent) as owner:
        expected, actual = os.stat(parent), os.fstat(owner.fd)
        if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            raise ValueError('Private receipt directory owner changed')
        with owner.atomic_writer(name) as stream:
            stream.write(raw)


def _adapter_archive(run, name, report, excluded_runtime_directories=()):
    allowed = {'venv', 'source', 'map', 'home', 'config', 'cache', 'data', 'tmp'}
    if (type(excluded_runtime_directories) not in (tuple, list) or
            any(type(value) is not str or value not in allowed or
                len(SourceRoot.parts(value)) != 1 or '\\' in value or ':' in value
                for value in excluded_runtime_directories) or
            len(excluded_runtime_directories) != len(set(excluded_runtime_directories))):
        raise ValueError('Only fixed canonical missing-backend runtime roots may be excluded')
    if excluded_runtime_directories and report.get('excluded_runtime_directories') != list(excluded_runtime_directories):
        raise ValueError('Runtime archive omissions must be explicit in the full private report')
    references, total = [], 0
    with SourceRoot(run) as source:
        expected, actual = os.stat(run), os.fstat(source.fd)
        if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            raise ValueError('Private archive directory owner changed')
        for parent, directories, files in os.walk(run, followlinks=False):
            if Path(parent) == run:
                directories[:] = [value for value in directories if value not in excluded_runtime_directories]
            directories.sort()
            for filename in sorted(files):
                path = (Path(parent) / filename).relative_to(run).as_posix()
                if len(references) >= 10000:
                    raise ValueError('Private archive inventory bound exceeded')
                raw, digest, info = source.read(path, 16 * 1024 * 1024 + 1, hash_full=False)
                if len(raw) != info.st_size or len(raw) > 16 * 1024 * 1024:
                    raise ValueError('Private archive file byte bound exceeded')
                total += len(raw)
                if total > 256 * 1024 * 1024:
                    raise ValueError('Private archive aggregate byte bound exceeded')
                references.append({'path': path, 'sha256': digest, 'bytes': len(raw)})
    return {'status': report['status'], 'full_private_report': report,
        'archive': {'directory': name, 'files': references, 'bytes': total},
        'engine_selected': False, 'qualification_complete': False}


def _adapter_execute(kind, root, evidence_directory):
    root = _adapter_root(root)
    with _adapter_run(root, evidence_directory, kind) as (run, name):
        bound = None
        report = {'status': 'running', 'engine_selected': False, 'qualification_complete': False}
        try:
            bound = _adapter_capture(root)
            report = (_run_updates if kind == 'updates' else _run_queries)(root=root, evidence_directory=run, bound=bound)
            report['binding_before'] = bound
            report['binding_after'] = _adapter_recheck(root, bound)
        except Exception as error:
            report.update(status='failed', binding_before=bound, driver_failure=_adapter_error(error),
                engine_selected=False, qualification_complete=False)
        report['implementation_load_scope'] = 'Same loaded checkout; collector load-time digest verified; remaining helper hashes are current disk/commit bindings, not general module-load attestation.'
        _adapter_dump(run, 'report.json', report)
        return _adapter_archive(run, name, report)


def run_updates(*, root=ROOT, evidence_directory):
    """Return full private receipts and bounded relative archive refs for36 updates."""
    return _adapter_execute('updates', root, evidence_directory)


def run_queries(*, root=ROOT, evidence_directory):
    """Return full private receipts and bounded relative archive refs for20 queries."""
    return _adapter_execute('queries', root, evidence_directory)


def _adapter_snapshot_artifact(parent, filename, snapshot, receipt):
    """Save only the complete receipt's corresponding source/analyzer snapshot."""
    if (type(receipt) is not dict or receipt.get('status') != 'complete' or snapshot is None or
            snapshot.generation != receipt.get('generation') or snapshot.source_identity != receipt.get('source_identity') or
            snapshot.analyzer_identity != receipt.get('resources', {}).get('queued', {}).get('identity', {}).get('collector')):
        raise ValueError('Complete receipt lacks its corresponding source/analyzer snapshot')
    for value in (snapshot.generation, snapshot.source_identity, snapshot.analyzer_identity):
        if type(value) is not str or len(value) != 64 or any(character not in '0123456789abcdef' for character in value):
            raise ValueError('Snapshot receipt identities must be SHA256 digests')
    _adapter_dump(parent, filename, {'generation': snapshot.generation, 'source_identity': snapshot.source_identity,
        'analyzer_identity': snapshot.analyzer_identity,
        'facts': {'definitions': list(snapshot._definitions.values()), 'sites': list(snapshot._sites.values())}})
    with SourceRoot(parent) as owner:
        expected, actual = os.stat(parent), os.fstat(owner.fd)
        if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            raise ValueError('Produced-facts directory owner changed')
        raw, digest, info = owner.read(filename, ADAPTER_REPORT_BYTES+1, hash_full=False)
    if len(raw) != info.st_size or len(raw) > ADAPTER_REPORT_BYTES:
        raise ValueError('Produced-facts receipt exceeds private bound')
    return {'path': filename, 'sha256': digest, 'bytes': len(raw)}


def _run_updates(*, root, evidence_directory, bound):
    """Frozen equivalent source updates; preserve full per-attempt private receipts."""
    from evaluations.supplement_preparation import prepare_check, SOURCE, LOCK
    from evaluations.acceptance import committed
    from evaluations.incremental_candidate import Candidate
    import resource
    import traceback
    ROOT, OUT = Path(root), Path(evidence_directory)
    measured_commit = bound['measured_commit']
    prepare = bound['preparation']
    manifest = _adapter_json(ROOT, SOURCE, bound)
    fixture = _adapter_json(ROOT, 'evaluations/code-understanding/fixtures.json', bound)
    base={r['path']:r['content_utf8'].encode() for r in manifest['files']}
    metadata={r['path']:dict({k:r[k] for k in ('path','language','kind')}, sha256=hashlib.sha256(base[r['path']]).hexdigest(), bytes=len(base[r['path']])) for r in manifest['files']}
    # Gold judgments stay outside Candidate; operations contain only immutable source edits.
    updates=[{k:u[k] for k in ('id','language','operations')} for u in fixture['updates']+manifest['updates']]
    assert len(updates)==36
    implementation = bound['implementation']
    results=[];started=time.monotonic()
    for update in updates:
     attempts = {}
     result = {'id': update['id'], 'language': update['language'], 'status': 'running', 'attempts': attempts}
     try:
      changed,inventory=_adapter_operations(base,metadata,update)
      with _adapter_child(OUT, update['id']) as logs, _adapter_source(OUT) as source:
       with SourceRoot(source) as owned:result['source_owner_identity']=owned.identity
       def materialize(blobs):
        _adapter_materialize(source, blobs, base.keys()-blobs.keys())
       def attempt(name, candidate, records, mode, concurrency):
        receipt = candidate.refresh(records, mode=mode, concurrency=concurrency, evidence_directory=logs)
        attempts[name] = receipt
        _adapter_dump(logs, name+'.json', receipt)
        _adapter_dump(OUT, update['id']+'.json', result)
        snapshot = getattr(candidate, 'snapshot', None)
        receipt['snapshot_state'] = {'generation': snapshot.generation if snapshot is not None else None,
            'matches_returned_generation': snapshot is not None and snapshot.generation == receipt.get('generation'),
            'fresh_facts_retained': False}
        _adapter_dump(logs, name+'.json', receipt)
        if receipt.get('status') == 'complete':
         receipt['facts_artifact'] = _adapter_snapshot_artifact(logs, name+'.facts.json', snapshot, receipt)
         receipt['facts_artifact']['path'] = update['id']+'/'+receipt['facts_artifact']['path']
         receipt['snapshot_state']['fresh_facts_retained'] = True
        _adapter_dump(logs, name+'.json', receipt)
        _adapter_dump(OUT, update['id']+'.json', result)
        return receipt
       materialize(base)
       serial=Candidate(source,budget=Budget(timeout_seconds=20));queued=Candidate(source,budget=Budget(timeout_seconds=20))
       before_s=attempt('base_serial', serial, list(metadata.values()), 'serial', 1)
       before_q=attempt('base_queued', queued, list(metadata.values()), 'queued', 2)
       materialize(changed)
       after_s=attempt('update_serial', serial, list(inventory.values()), 'serial', 1)
       after_q=attempt('update_queued', queued, list(inventory.values()), 'queued', 2)
       clean=attempt('clean_serial', Candidate(source,budget=Budget(timeout_seconds=20)), list(inventory.values()), 'serial', 1)
       passed=all(a['status']=='complete' for a in attempts.values())
       if passed:
        passed=(before_s['generation']==before_q['generation'] and after_s['generation']==after_q['generation']==clean['generation'] and
                before_s['generation']!=after_s['generation'] and
                after_s['semantic_facts_sha256']==after_q['semantic_facts_sha256']==clean['semantic_facts_sha256'])
       result={'id':update['id'],'language':update['language'],'status':'passed' if passed else 'failed','attempts':attempts,'source_owner_identity':result['source_owner_identity']}
     except Exception as error:
      result.update(status='failed', **_adapter_error(error))
      _adapter_dump(OUT, update['id']+'.error.json', result)
     results.append(result)
     _adapter_dump(OUT,update['id']+'.json',result)

    report={'status':'passed' if all(r['status']=='passed' for r in results) else 'failed','results':results,'implementation':implementation,'measured_commit':measured_commit,'preparation_checks':prepare['checks_passed'],'source_lock_sha256':prepare['supplement_lock_sha256'],'elapsed_seconds':time.monotonic()-started,'parent_lifetime_peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,'resource_scope':'Parent process lifetime only; worker measurements retained individually, not combined owned RSS','engine_selected':False,'qualification_complete':False,'scope':'Frozen36 source updates, serial/queued2 vs clean same-owner rebuilt facts; unsupported semantics retained, not a precision or scale gate'}
    _adapter_dump(OUT,'report.json',report)
    return report


def _run_queries(*, root, evidence_directory, bound):
    """Run frozen source-bound query assertions in both modes after production."""
    from evaluations.supplement_preparation import prepare_check, LOCK, SOURCE, ORACLE
    from evaluations.acceptance import committed
    from unittest.mock import patch
    import datetime
    import traceback
    ROOT, E = Path(root), Path(evidence_directory)
    EXPECTED_HEAD = bound['measured_commit']
    PINS = bound['implementation']
    def sha(raw):return hashlib.sha256(raw).hexdigest()
    def dump(path,value):_adapter_dump(path.parent,path.name,value)
    def head():return _adapter_head(ROOT)
    def guarded_bytes(path,cap=1024*1024):
        expected = (bound['implementation'] | bound['input_binding']).get(path)
        if expected is None:raise ValueError('Unbound adapter read')
        return _adapter_bytes(ROOT, path, expected, cap)
    report={'schema_version':1,'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scope':'Finite frozen synthetic source query experiment in both required collection modes; not engine/scale/human acceptance.','modes':[],'failures':[],'events':[],'engine_selected':False,'qualification_complete':False,'T007_met':False,'T008_met':False,'R043_met':False}
    started=time.monotonic()
    def event(label,**kw):report['events'].append({'event':label,'elapsed_seconds':time.monotonic()-started,**kw})
    try:
        assert head()==EXPECTED_HEAD
        report['measured_commit_before']=head()
        report['implementation_hashes_before']={p:guarded_bytes(p,2*1024*1024)[1] for p in PINS}
        assert report['implementation_hashes_before']==PINS
        prepared=bound['preparation']
        assert prepared['checks_passed']==2322 and prepared['physical_ranges']==535
        report['prepared']=prepared;event('preparation_passed_before_first_extractor',checks=prepared['checks_passed'])
        lock_raw,lock_sha=guarded_bytes(LOCK);lock=_adapter_json(ROOT,LOCK,bound)
        binding=dict(lock['sha256']);binding[LOCK]=lock_sha
        assert len(binding)==31 and committed(ROOT,binding)
        report['input_binding']={'status':'passed','map_entries':len(binding),'bound_to_measured_commit':EXPECTED_HEAD,'sha256':binding}
        event('committed_binding_passed_before_first_extractor',files=len(binding))
        manifest=_adapter_json(ROOT,SOURCE,bound)
        record=next(r for r in manifest['files'] if r['path'].endswith('/python/fanout.py'))
        source_path=record['path'];base=record['content_utf8'].encode('utf-8')
        assert len(base)==5319 and sha(base)==record['sha256']
        # Only exact source metadata is handed to Candidate. No expected edges/counts.
        metadata={k:record[k] for k in ('path','language','kind','sha256','bytes')}
        from evaluations.incremental_candidate import Candidate
        from evaluations.tree_sitter_baseline import Budget
        from evaluations.bounded_queries import Snapshot,Limits,encoded
        from evaluations import bounded_queries as queries
        assert queries.QUERY_RULE_VERSION=='physical-occurrence-v2'
        report['query_rule_version']=queries.QUERY_RULE_VERSION
        def limits(values):return Limits(**{k:v for k,v in values.items() if k!='max_excerpt_bytes'})
        def no_bodies(value):
            if isinstance(value,dict):
                assert not {'text','content','excerpt','body','content_utf8'}&set(value)
                for child in value.values():no_bodies(child)
            elif isinstance(value,list):
                for child in value:no_bodies(child)
        def public_fact_view(snapshot):
            return {'definitions':list(snapshot._definitions.values()),'sites':list(snapshot._sites.values())}
        def physical(item):
            r=item['range'];return(item['path'],r['start_byte'],r['end_byte'],r['start_line'],r['end_line'])
        with _adapter_source(E) as source_root:
            def write_source(raw):
                _adapter_materialize(source_root, {source_path:raw})
            write_source(base)
            report['source']={'path':source_path,'sha256':sha(base),'bytes':len(base),'copy_verified_with_SourceRoot':False}
            with SourceRoot(source_root) as owned:
                copied,digest,info=owned.read(source_path,len(base)+1,hash_full=False)
                assert copied==base and digest==sha(base) and info.st_size==len(base)
                report['source']['copy_verified_with_SourceRoot']=True
                report['source']['owner_identity']=owned.identity
            for mode,concurrency in [('serial',1),('queued',2)]:
                write_source(base)
                with _adapter_child(E, mode) as mode_dir:
                    with _adapter_child(mode_dir, 'worker-evidence') as worker_evidence:
                        candidate=Candidate(source_root,budget=Budget(max_files=4,max_file_bytes=65536,max_total_bytes=262144,timeout_seconds=20))
                        mode_report={'mode':mode,'configured_concurrency':concurrency,'queries':[],'failures':[]}
                        report['modes'].append(mode_report)
                        event('before_first_mode_extractor',mode=mode,source_only_metadata_keys=sorted(metadata))
                        refresh=candidate.refresh([metadata],mode=mode,concurrency=concurrency,evidence_directory=worker_evidence)
                        mode_report['base_refresh']=refresh;dump(mode_dir/'base-refresh.json',refresh)
                        assert refresh['status']=='complete',refresh
                        snapshot=candidate.snapshot;fact_view=public_fact_view(snapshot)
                        assert sha(encoded(fact_view))==refresh['semantic_facts_sha256']
                        refresh['facts_artifact']=_adapter_snapshot_artifact(mode_dir,'base.facts.json',snapshot,refresh)
                        refresh['facts_artifact']['path']=mode+'/'+refresh['facts_artifact']['path']
                        dump(mode_dir/'base-refresh.json',refresh)
                        event('facts_produced_before_source_oracle_grading',mode=mode,semantic_facts_sha256=refresh['semantic_facts_sha256'])
                        # Source/gold oracle admission occurs only in this grader after facts exist.
                        oracle=_adapter_json(ROOT,ORACLE,bound)['query']
                        assert oracle['source_path']==source_path and oracle['source_sha256']==sha(base)
                        definitions={physical(d):d for d in fact_view['definitions']}
                        expected_definitions={physical(d):d for d in oracle['declarations']}
                        assert set(definitions)==set(expected_definitions)
                        key_to_id={};grade_checks=0
                        for location,d in expected_definitions.items():
                            actual=definitions[location]
                            assert actual['name']==d['name'] and actual['text']==d['text'] and actual['provenance']['source_sha256']==d['source_sha256']
                            assert base[d['name_range']['start_byte']:d['name_range']['end_byte']].decode()==d['name']
                            key_to_id[d['key']]=actual['id'];grade_checks+=4
                        sites={physical(s):s for s in fact_view['sites']}
                        assert set(sites)=={physical(i['site']) for i in oracle['invocations']}
                        for invocation in oracle['invocations']:
                            actual=sites[physical(invocation['site'])]
                            assert actual['text']==invocation['site']['text'] and actual['provenance']['source_sha256']==sha(base)
                            assert actual['role']=='call' and actual['certainty']=='resolved' and actual['targets_exhaustive'] is True
                            assert actual['caller']==key_to_id[invocation['caller_key']]
                            assert actual['targets']==[key_to_id[invocation['target_key']]];grade_checks+=5
                        names={d['name']:d['id'] for d in fact_view['definitions']}
                        expected_hub=sorted([i for i in oracle['invocations'] if i['caller_key']=='PY.fanout.hub'],key=lambda i:physical(i['site']))
                        expected_hub_keys=[(physical(i['site']),key_to_id[i['caller_key']],key_to_id[i['target_key']]) for i in expected_hub]
                        def row_key(row):return(physical(row['site']),row['caller']['id'] if row['caller'] else None,row['target']['id'] if row['target'] else None)
                        def observe(entry,snap,seed,**options):
                            started_query=time.monotonic();page=snap.query(seed,**options)
                            max_bytes=options.get('limits',Limits()).max_response_bytes
                            full_bytes=len(encoded(page))
                            entry.setdefault('responses',[]).append({'response':page,'serialized_response_bytes':full_bytes,'real_elapsed_seconds_observed':time.monotonic()-started_query})
                            dump(mode_dir/(entry['id']+'.json'),entry)
                            assert full_bytes<=max_bytes
                            no_bodies(page)
                            return page
                        mode_report['source_fact_grade']={'status':'passed','definitions':len(definitions),'invocation_occurrences':len(sites),'source_range_checks':grade_checks,'all_targets_from_physical_source_oracle':True,'raw_source_oracle_not_extractor_input':True}
                        for assertion in oracle['assertions']:
                            entry={'id':assertion['id'],'status':'running','responses':[]};mode_report['queries'].append(entry)
                            try:
                                seed=names[assertion['seed']];qid=assertion['id']
                                # Each assertion has its own continuation namespace over the same produced facts.
                                snap=Snapshot(fact_view,snapshot.source_identity,snapshot.analyzer_identity)
                                assert snap.generation==snapshot.generation
                                if qid=='Q-PY-ALL-CALLEES':
                                    cursor=None;rows=[];work=0
                                    for _ in range(12):
                                        page=observe(entry,snap,seed,cursor=cursor,limits=limits(assertion['limits']))
                                        rows+=page['rows'];work+=page['examined_relationships'];cursor=page['cursor']
                                        if cursor is None:break
                                    assert cursor is None and len(rows)==assertion['expected_logical_completion_rows']
                                    assert work==assertion['expected_examined_relationships_across_continuations']
                                    assert page['total_count']==assertion['total_count_at_completion']
                                    assert [row_key(r) for r in rows]==expected_hub_keys
                                    entry.update(rows=len(rows),examined_total=work,pages=len(entry['responses']))
                                elif qid=='Q-PY-WORK-EXHAUSTION':
                                    page=observe(entry,snap,seed,limits=limits(assertion['limits']))
                                    assert len(page['rows'])==assertion['expected_returned_rows'] and page['examined_relationships']==assertion['expected_examined_relationships']
                                    assert page['stop_reason']==assertion['required_stop_reason'] and page['truncated'] is True and page['total_count']['kind']=='lower_bound'
                                    resumed=observe(entry,snap,seed,cursor=page['cursor'],limits=Limits(max_edges=1))
                                    assert row_key(resumed['rows'][0])==expected_hub_keys[assertion['next_unemitted_row_index']]
                                elif qid=='Q-PY-FILTERED-WORK':
                                    prefix=assertion['filter']['target_name_prefix']
                                    page=observe(entry,snap,seed,prefix=prefix,limits=limits(assertion['limits']))
                                    assert page['examined_relationships']==assertion['expected_examined_relationships'] and len(page['rows'])==assertion['expected_returned_rows']
                                    assert page['truncated'] and page['total_count']=={'value':0,'kind':'lower_bound'}
                                    complete=observe(entry,snap,seed,prefix=prefix,limits=Limits(max_edges=120,max_entities=120,max_examined_relationships=120))
                                    assert len(complete['rows'])==assertion['matching_rows_in_complete_source_oracle'] and complete['examined_relationships']==len(expected_hub_keys)
                                elif qid=='Q-PY-OUTPUT-PAGES':
                                    cursor=None;rows=[];work=0;sizes=[]
                                    for _ in range(12):
                                        page=observe(entry,snap,seed,cursor=cursor,limits=limits(assertion['limits_per_page']))
                                        rows+=page['rows'];work+=page['examined_relationships'];sizes.append(len(page['rows']));cursor=page['cursor']
                                        if cursor is None:break
                                    assert cursor is None and sizes==assertion['expected_page_sizes'] and len(rows)==assertion['expected_total_rows'] and work==assertion['expected_total_examined_relationships']
                                    assert [row_key(r) for r in rows]==expected_hub_keys
                                    entry.update(page_sizes=sizes,examined_total=work,rows=len(rows))
                                    first=entry['responses'][0]['response'];cursor=first['cursor'];rejects=[]
                                    for name,change in [('role',{'role':'all'}),('scope',{'scope':'tests/fixtures/code-understanding/python/'}),('prefix',{'prefix':'leaf_0'}),('operation',{'operation':'callers'}),('depth',{'depth':2})]:
                                        with patch.object(snap,'_row',side_effect=AssertionError('must reject before materialization')):
                                            try:snap.query(seed,cursor=cursor,**change)
                                            except ValueError:rejects.append({'binding':name,'status':'rejected_before_row_materialization'})
                                            else:raise AssertionError('changed cursor accepted:'+name)
                                    with patch.object(queries,'QUERY_RULE_VERSION','private-rule-drift'),patch.object(snap,'_row',side_effect=AssertionError('must reject rule before materialization')):
                                        try:snap.query(seed,cursor=cursor)
                                        except ValueError:rejects.append({'binding':'query_rule','status':'rejected_before_row_materialization'})
                                        else:raise AssertionError('rule drift accepted')
                                    now=[0];expiring=Snapshot(fact_view,snapshot.source_identity,snapshot.analyzer_identity,clock=lambda:now[0]);exp=expiring.query(seed,limits=Limits(max_edges=17));now[0]=61
                                    with patch.object(expiring,'_row',side_effect=AssertionError('expiry must reject before materialization')):
                                        try:expiring.query(seed,cursor=exp['cursor'])
                                        except ValueError:rejects.append({'binding':'expiry','status':'rejected_before_row_materialization'})
                                        else:raise AssertionError('expiry accepted')
                                    entry['cursor_binding_checks']=rejects
                                elif qid=='Q-PY-RESPONSE-BYTES':
                                    page=observe(entry,snap,seed,limits=limits(assertion['limits']))
                                    assert page['examined_relationships']<len(expected_hub_keys) and page['cursor'] is not None
                                    next_index=len(page['rows'])
                                    resumed=observe(entry,snap,seed,cursor=page['cursor'],limits=Limits(max_edges=1,max_response_bytes=32768))
                                    assert row_key(resumed['rows'][0])==expected_hub_keys[next_index]
                                    assert resumed['examined_relationships']==0
                                    entry.update(first_response_rows=len(page['rows']),first_response_work=page['examined_relationships'],resumed_cached_work=resumed['examined_relationships'])
                                elif qid=='Q-PY-CYCLE-REACHABILITY':
                                    page=observe(entry,snap,seed,operation=assertion['operation'],depth=assertion['depth'],limits=limits(assertion['limits']))
                                    assert page['examined_relationships']==assertion['expected_examined_relationships'] and len(page['rows'])==assertion['expected_occurrence_relations']
                                    assert page['returned_entities']==len(assertion['expected_nonseed_vertices']) and page['cursor'] is None
                                    assert {r['target']['name'] for r in page['rows'] if r['target']['id']!=seed}==set(assertion['expected_nonseed_vertices'])
                                    assert any(r['target']['id']==seed for r in page['rows'])
                                elif qid=='Q-PY-CANCEL-BEFORE':
                                    page=observe(entry,snap,seed,cancel=lambda:True)
                                    assert page['examined_relationships']==assertion['expected_examined_relationships'] and len(page['rows'])==assertion['expected_returned_rows'] and page['stop_reason']==assertion['required_stop_reason'] and page['cursor'] is None
                                elif qid=='Q-PY-CANCEL-DURING':
                                    calls=[0]
                                    def cancel():calls[0]+=1;return calls[0]>3
                                    page=observe(entry,snap,seed,cancel=cancel)
                                    assert page['examined_relationships']<=assertion['maximum_examined_relationships'] and len(page['rows'])<=assertion['maximum_returned_rows'] and page['stop_reason']==assertion['required_stop_reason'] and page['cursor'] is None
                                    usable=observe(entry,snap,seed,limits=Limits(max_edges=1))
                                    assert row_key(usable['rows'][0])==expected_hub_keys[0]
                                    entry['cancellation_callback_calls']=calls[0]
                                elif qid=='Q-PY-DEADLINE':
                                    ticks=[0]
                                    def clock():ticks[0]+=1;return ticks[0]
                                    timed=Snapshot(fact_view,snapshot.source_identity,snapshot.analyzer_identity,clock=clock)
                                    page=observe(entry,timed,seed,limits=Limits(timeout_seconds=3.5))
                                    assert page['examined_relationships']==3 and page['examined_relationships']<=assertion['maximum_examined_relationships'] and page['stop_reason']==assertion['required_stop_reason']
                                    entry['clock_scope']='Injected integer monotonic control-flow ticks; not a latency measurement.'
                                elif qid=='Q-PY-STALE-GENERATION':
                                    page=observe(entry,snap,seed,limits=Limits(max_edges=17))
                                    change=manifest['query_freshness_update'];changed=change['after_content_utf8'].encode('utf-8')
                                    assert change['before_content_utf8'].encode('utf-8')==base and sha(changed)==assertion['source_change']['sha256_after']
                                    write_source(changed)
                                    changed_meta=dict(metadata,sha256=sha(changed),bytes=len(changed))
                                    changed_refresh=candidate.refresh([changed_meta],mode=mode,concurrency=concurrency,evidence_directory=worker_evidence)
                                    entry['changed_refresh']=changed_refresh
                                    dump(mode_dir/'changed-refresh.json',changed_refresh)
                                    dump(mode_dir/(assertion['id']+'.json'),entry)
                                    assert changed_refresh['status']=='complete' and changed_refresh['source_identity']!=refresh['source_identity'] and changed_refresh['generation']!=refresh['generation']
                                    fresh=candidate.snapshot;fresh_facts=public_fact_view(fresh);fresh_names={d['name']:d['id'] for d in fresh_facts['definitions']}
                                    changed_refresh['facts_artifact']=_adapter_snapshot_artifact(mode_dir,'changed.facts.json',fresh,changed_refresh)
                                    changed_refresh['facts_artifact']['path']=mode+'/'+changed_refresh['facts_artifact']['path']
                                    dump(mode_dir/'changed-refresh.json',changed_refresh)
                                    dump(mode_dir/(assertion['id']+'.json'),entry)
                                    def topology(view):
                                        byid={d['id']:d['name'] for d in view['definitions']}
                                        return sorted((byid[s['caller']],tuple(byid[t] for t in s['targets']),s['role'],s['text']) for s in view['sites'])
                                    assert topology(fact_view)==topology(fresh_facts)
                                    with patch.object(fresh,'_row',side_effect=AssertionError('stale cursor cannot materialize')):
                                        try:fresh.query(fresh_names['hub'],cursor=page['cursor'])
                                        except ValueError:entry['old_cursor_rejected_against_new_generation']=True
                                        else:raise AssertionError('old cursor accepted by new generation')
                                        try:fresh.query(seed)
                                        except ValueError:entry['old_physical_seed_refused']=True
                                        else:raise AssertionError('old physical seed silently reinterpreted')
                                    retained=observe(entry,snap,seed,cursor=page['cursor'],limits=Limits(max_edges=1))
                                    assert row_key(retained['rows'][0])==expected_hub_keys[17]
                                    assert retained['generation']==refresh['generation'] and retained['rows'][0]['site']['source_sha256']==sha(base)
                                    entry.update(topology_unchanged=True,generation_changed=True,old_snapshot_coherent=True,old_generation=refresh['generation'],new_generation=changed_refresh['generation'])
                                else:raise AssertionError('unhandled frozen assertion:'+qid)
                                entry['status']='passed'
                            except Exception as error:
                                entry.update(status='failed', **_adapter_error(error))
                                mode_report['failures'].append({'id':assertion['id'],'error_kind':type(error).__name__})
                            finally:dump(mode_dir/(assertion['id']+'.json'),entry)
                        assert len(mode_report['queries'])==10
                        mode_report['status']='passed' if not mode_report['failures'] else 'failed'
                        mode_report['actual_base_workers_started']=refresh['resources']['queued']['workers_started']
                        mode_report['queue_parallelism_observation']='One admitted file; configured queued2 startsoneworker, no throughput claim.'
                        write_source(base)
            bases=[m['base_refresh'] for m in report['modes']]
            report['mode_equivalence']={'semantic_facts_sha256_equal':bases[0]['semantic_facts_sha256']==bases[1]['semantic_facts_sha256'],'generation_equal':bases[0]['generation']==bases[1]['generation'],'source_identity_equal':bases[0]['source_identity']==bases[1]['source_identity'],'same_source_owner':True}
            assert all(report['mode_equivalence'].values())
        report['implementation_hashes_after']={p:guarded_bytes(p,2*1024*1024)[1] for p in PINS}
        report['measured_commit_after']=head()
        assert report['implementation_hashes_after']==PINS and report['measured_commit_after']==EXPECTED_HEAD
        _adapter_recheck(ROOT, bound)
        report['committed_inputs_stable_after']=True
        report['status']='passed' if all(m['status']=='passed' for m in report['modes']) else 'failed'
    except Exception as error:
        report['status']='failed';report['failures'].append({'stage':'driver', **_adapter_error(error)})
    finally:
        report['driver_sha256']=sha(Path(__file__).read_bytes())
        report['elapsed_seconds_observed']=time.monotonic()-started
        report['ceilings']=['Only a finite5319byte/onefile Python fixture is measured; both modesconfigured, queued2usesoneactualworker.','Query cancellation/deadline are finite control-flow checks, not wallclockservice latency guarantees.','Source iteration/filesystem/callbackblocking and parentRSS/unmeasureddefaultcaps retain documented ceilings.','No source fixture was imported/executed; only native syntaxanalysis, source-only metadata, producedfacts and post-production grader used.','No corpus/scale/structural-owner/engine-selection/type/config/contract/receiver completeness or human acceptance.','These20frozenassertion observations do not completeT007/T008/R043 or wholebundle qualification.']
        dump(E/'report.json',report)
    return report


_MISSING_HELPERS = ('evaluations/engine_checks.py', 'evaluations/incremental_candidate.py', 'repo_graph/analysis_queue.py', 'repo_graph/analysis_native.py', 'evaluations/analysis.py', 'evaluations/acceptance.py', 'repo_graph/__init__.py', 'evaluations/bounded_queries.py', 'repo_graph/source.py', 'repo_graph/cli.py', 'repo_graph/builder.py', 'repo_graph/search.py', 'repo_graph/jev.py', 'repo_graph/rerank.py', 'scripts/repo_graph.py', 'repo_graph/assets/diagram.html', 'repo_graph/assets/views.js')
_MISSING_CHECKS = ('pre_bootstrap_stdlib_venv_no_distributions', 'five_optional_modules_and_distributions_absent', 'core_cli_map_and_keyword_returned_zero', 'core_supported_scan_and_keyword', 'serial_and_queued_candidate_explicit_missing_distribution_no_snapshot', 'serial_and_queued_actual_queue_backend_refusal', 'queue_owned_children_removed', 'clean_venv_not_base_prefix', 'python_isolated_private_owned_session')
_MISSING_SOURCE_RECORDS = [{'path': 'main.py', 'language': 'python', 'kind': 'source', 'sha256': 'c9c7f51a0cfb2c5f224000b545143446d87615065fbbf194161b416aa533ad1b', 'bytes': 58}, {'path': 'helper.py', 'language': 'python', 'kind': 'source', 'sha256': '8b902c1b8fe3af086ceb732a513a199df0ba839730a080dbc3ce2c407c36d997', 'bytes': 27}]

_MISSING_SETUP = r'''
import json,resource,sys,time,venv
from pathlib import Path
resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,512*1024*1024))
resource.setrlimit(resource.RLIMIT_CPU,(30,30))
resource.setrlimit(resource.RLIMIT_FSIZE,(1024*1024,1024*1024))
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
started=time.monotonic()
venv.EnvBuilder(with_pip=False,system_site_packages=False,symlinks=True).create(Path(sys.argv[1])/'venv')
usage=resource.getrusage(resource.RUSAGE_SELF)
print(json.dumps({'venv_created':True,'with_pip':False,'system_site_packages':False,'symlinks':True,
 'python_version':sys.version,'elapsed_seconds':time.monotonic()-started,
 'process_peak_rss_bytes':usage.ru_maxrss*1024,'process_user_seconds':usage.ru_utime,
 'process_system_seconds':usage.ru_stime}))
'''

_MISSING_PROBE = r'''
import hashlib,importlib.util,json,os,resource,subprocess,sys,time
from importlib import metadata
from pathlib import Path
resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,512*1024*1024))
resource.setrlimit(resource.RLIMIT_CPU,(30,30))
resource.setrlimit(resource.RLIMIT_FSIZE,(1024*1024,1024*1024))
resource.setrlimit(resource.RLIMIT_CORE,(0,0))

import math,stat,traceback
def strict(raw):
    def unique(pairs):
        result={}
        for k,v in pairs:
            if k in result:raise ValueError('Duplicate JSON key')
            result[k]=v
        return result
    def finite(value):raise ValueError('Non-finite JSON number')
    def number(value):
        n=float(value)
        return n if math.isfinite(n) else finite(value)
    return json.loads(raw,object_pairs_hook=unique,parse_constant=finite,parse_float=number)
def read_owned(fd,path):
    if not isinstance(path,str) or path!=Path(path).as_posix() or '\\' in path or ':' in path or Path(path).is_absolute() or not Path(path).parts or '..' in Path(path).parts:raise ValueError('Noncanonical producer file')
    parent=os.dup(fd)
    try:
        parts=Path(path).parts
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent);os.close(parent);parent=child
        with os.fdopen(os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent),'rb') as stream:
            before=os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):raise ValueError('Producer input must be regular')
            raw=stream.read(1024*1024+1);after=os.fstat(stream.fileno())
        if len(raw)!=after.st_size or len(raw)>1024*1024 or (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise ValueError('Producer input byte/identity bound')
        return raw,hashlib.sha256(raw).hexdigest()
    finally:os.close(parent)
def source_binding(root,control):
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        if {'device':info.st_dev,'inode':info.st_ino}!=control['root_owner']:raise ValueError('Producer source owner changed')
        if set(control['implementation_sha256'])!=set(('evaluations/engine_checks.py', 'evaluations/incremental_candidate.py', 'repo_graph/analysis_queue.py', 'repo_graph/analysis_native.py', 'evaluations/analysis.py', 'evaluations/acceptance.py', 'repo_graph/__init__.py', 'evaluations/bounded_queries.py', 'repo_graph/source.py', 'repo_graph/cli.py', 'repo_graph/builder.py', 'repo_graph/search.py', 'repo_graph/jev.py', 'repo_graph/rerank.py', 'scripts/repo_graph.py', 'repo_graph/assets/diagram.html', 'repo_graph/assets/views.js')):raise ValueError('Complete producer implementation binding required')
        for path,expected in control['implementation_sha256'].items():
            if read_owned(fd,path)[1]!=expected:raise ValueError('Producer implementation changed before import or after probe')
    finally:os.close(fd)
def _missing_probe():
    root,job=map(Path,sys.argv[1:3]);expected=sys.argv[3];started=time.monotonic()
    jobfd=os.dup(int(job.name))
    try:
        actual,expected_owner=os.fstat(jobfd),os.stat(job)
        if (actual.st_dev,actual.st_ino)!=(expected_owner.st_dev,expected_owner.st_ino):raise ValueError('Producer job owner mismatch')
        raw,control_sha=read_owned(jobfd,'control.json')
        if control_sha!=expected:raise ValueError('Producer control identity mismatch')
        control=strict(raw)
        if type(control.get('schema_version')) is not int or control['schema_version']!=1:raise ValueError('Typed producer control required')
        receipt={'schema_version':1,'status':'running','checks':{},'cli':[],'core':{},'components':[],
            'control_sha256':control_sha,'producer_token':control['producer_token'],'implementation_sha256':control['implementation_sha256'],
            'producer_pid':os.getpid(),'source_records':control['source_records'],
            'engine_selected':False,'qualification_complete':False}
        def save():
            encoded=json.dumps(receipt,separators=(',',':'),allow_nan=False).encode()
            if len(encoded)>1024*1024:raise ValueError('Probe receipt byte bound')
            name='.'+str(os.getpid())+'.probe.tmp'
            try:
                fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=jobfd)
                with os.fdopen(fd,'wb') as stream:stream.write(encoded);stream.flush();os.fsync(stream.fileno())
                os.replace(name,'probe.json',src_dir_fd=jobfd,dst_dir_fd=jobfd);os.fsync(jobfd)
            finally:
                try:os.unlink(name,dir_fd=jobfd)
                except FileNotFoundError:pass
        try:
            source_binding(root,control);save()
            # Observe the stdlib venv before adding checkout-owned metadata/imports.
            pre_bootstrap_distributions=sorted(d.metadata['Name'] for d in metadata.distributions())
            pre_bootstrap_optional={}
            for distribution in ('tree-sitter','tree-sitter-python','tree-sitter-go','tree-sitter-javascript','tree-sitter-typescript'):
                try:version=metadata.version(distribution)
                except metadata.PackageNotFoundError:version=None
                pre_bootstrap_optional[distribution]={'module_spec_absent':importlib.util.find_spec(distribution.replace('-','_')) is None,'distribution_version':version}
            if pre_bootstrap_distributions or any(not v['module_spec_absent'] or v['distribution_version'] is not None for v in pre_bootstrap_optional.values()):raise ValueError('Empty stdlib venv admission failed before source imports')
            sys.path.insert(0,str(root))
            from repo_graph.source import SourceRoot
            from evaluations.incremental_candidate import Candidate
            from evaluations.queued_collector import collect_files,QueueLimits
            from evaluations.tree_sitter_baseline import Budget,PINS
            observed={}
            for distribution in PINS:
                module=distribution.replace('-','_')
                try:version=metadata.version(distribution)
                except metadata.PackageNotFoundError:version=None
                observed[distribution]={'module':module,'module_spec_absent':importlib.util.find_spec(module) is None,
                                        'distribution_version':version}
            source=job/'source';source.mkdir()
            blobs=[{'path':'main.py','language':'python','kind':'source','content':b'from helper import finish\n\ndef run():\n    return finish()\n'},
                   {'path':'helper.py','language':'python','kind':'source','content':b'def finish():\n    return 1\n'}]
            with SourceRoot(source) as owned:
                for blob in blobs:
                    with owned.atomic_writer(blob['path']) as stream:stream.write(blob['content'])
                    blob.update(sha256=hashlib.sha256(blob['content']).hexdigest(),bytes=len(blob['content']))
            if [{k:b[k] for k in ('path','language','kind','sha256','bytes')} for b in blobs]!=control['source_records']:raise ValueError('Synthetic source record pin mismatch')
            cli=receipt['cli']
            def command(name,args):
                with SourceRoot(job) as owned,owned.open(name+'.stdout.log',create=True) as out,owned.open(name+'.stderr.log',create=True) as err:
                    process=subprocess.run([sys.executable,'-I','-B',str(root/'scripts/repo_graph.py'),*args],
                        cwd=job,stdout=out,stderr=err,stdin=subprocess.DEVNULL,timeout=8)
                cli.append({'command':name,'returncode':process.returncode});save()
                return process.returncode
            mapped=job/'map'
            map_code=command('core-map',['map',str(source),'--output',str(mapped)])
            search_code=command('core-keyword-search',['search',str(mapped),'finish','--mode','keyword','--limit','2']) if map_code==0 else None
            core={};receipt['core']=core
            if map_code==0:
                with SourceRoot(mapped) as guarded:
                    raw,_,info=guarded.read('graph.json',1024*1024+1,hash_full=False)
                    if len(raw)!=info.st_size or len(raw)>1024*1024:raise ValueError('Core graph output exceeds budget')
                graph=json.loads(raw);core.update({'file_count':graph['file_count'],'scan':graph['scan'],'search':graph['search'],'jev':graph['jev']});save()
            if search_code==0:
                with SourceRoot(job) as guarded:raw,_,_=guarded.read('core-keyword-search.stdout.log',1024*1024,hash_full=False)
                search=json.loads(raw);core['keyword']={'mode':search['mode'],'documents':search['documents'],'results':len(search['results'])}
            components=receipt['components']
            logs=job/'components';logs.mkdir()
            for mode,concurrency in [('serial',1),('queued',2)]:
                candidate=Candidate(source,budget=Budget(max_files=4,max_file_bytes=65536,max_total_bytes=262144,timeout_seconds=5))
                result=candidate.refresh([{k:b[k] for k in ('path','language','kind','sha256','bytes')} for b in blobs],
                   mode=mode,concurrency=concurrency,evidence_directory=logs)
                component={'mode':mode,'configured_concurrency':concurrency,'candidate':result,
                    'candidate_snapshot_is_none':candidate.snapshot is None,'candidate_cache_entries':len(candidate._cache)}
                components.append(component);save()
                queued=collect_files(iter(blobs),mode=mode,concurrency=concurrency,evidence_directory=logs,
                   budget=Budget(max_files=4,max_file_bytes=65536,max_total_bytes=262144,timeout_seconds=5),
                   limits=QueueLimits(max_result_bytes=1024*1024,cpu_seconds=5,worker_wall_seconds=5,total_wall_seconds=10))
                component.update({'queue':{'status':queued.status,'stop_reason':queued.stop_reason,'failures':queued.failures,
                             'collected':len(queued.collected),'resources':queued.resources,'cleanup':queued.cleanup}});save()
            checks={'pre_bootstrap_stdlib_venv_no_distributions':pre_bootstrap_distributions==[] and all(v['module_spec_absent'] and v['distribution_version'] is None for v in pre_bootstrap_optional.values()),
             'five_optional_modules_and_distributions_absent':len(observed)==5 and all(v['module_spec_absent'] and v['distribution_version'] is None for v in observed.values()),
             'core_cli_map_and_keyword_returned_zero':map_code==0 and search_code==0,
             'core_supported_scan_and_keyword':core.get('file_count')==2 and core.get('scan',{}).get('scanned')==2 and core.get('scan',{}).get('secure_reads') is True and core.get('keyword',{}).get('results',0)>0,
             'serial_and_queued_candidate_explicit_missing_distribution_no_snapshot':all(v['candidate']['status']=='failed' and v['candidate']['error_kind']=='PackageNotFoundError' and v['candidate_snapshot_is_none'] and v['candidate_cache_entries']==0 for v in components),
             'serial_and_queued_actual_queue_backend_refusal':all(v['queue']['status']=='failed' and v['queue']['stop_reason']=='backend_unavailable' and v['queue']['collected']==0 for v in components),
             'queue_owned_children_removed':all(v['queue']['cleanup'] and all(c['leader_reaped'] and c['group_absent'] and c['mailboxes_removed'] for c in v['queue']['cleanup']) for v in components),
             'clean_venv_not_base_prefix':sys.prefix!=sys.base_prefix and importlib.util.find_spec('pip') is None,
             'python_isolated_private_owned_session':bool(sys.flags.isolated) and sys.dont_write_bytecode and sys.flags.no_user_site and os.getpid()==os.getsid(0)==os.getpgrp() and all(Path(os.environ[key]).resolve()==(job/name).resolve() for key,name in [('HOME','home'),('XDG_CONFIG_HOME','config'),('XDG_CACHE_HOME','cache'),('XDG_DATA_HOME','data'),('TMPDIR','tmp')])}
            usage=resource.getrusage(resource.RUSAGE_SELF)
            children=resource.getrusage(resource.RUSAGE_CHILDREN)
            receipt.update({'schema_version':1,'status':'passed' if all(checks.values()) else 'failed','checks':checks,
             'pre_bootstrap_distributions':pre_bootstrap_distributions,'pre_bootstrap_absent_optional_backend':pre_bootstrap_optional,
             'absent_optional_backend':observed,'source_checkout_metadata_after_bootstrap':sorted(d.metadata['Name'] for d in metadata.distributions()),
             'python_version':sys.version,'cli':cli,'core':core,'components':components,
             'isolation':{'python_isolated_mode':bool(sys.flags.isolated),'bytecode_writes_disabled':bool(sys.dont_write_bytecode),
                          'user_site_disabled':bool(sys.flags.no_user_site),'pid':os.getpid(),'sid':os.getsid(0),'pgrp':os.getpgrp(),
                          'job_directory':str(job.resolve()),'private_environment':{key:str(Path(os.environ[key]).resolve()) for key in ('HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','XDG_DATA_HOME','TMPDIR')},
                          'sys_prefix':sys.prefix,'sys_base_prefix':sys.base_prefix,'pip_module_spec_absent':importlib.util.find_spec('pip') is None},
             'resources':{'elapsed_seconds':time.monotonic()-started,'process_peak_rss_bytes':usage.ru_maxrss*1024,
                          'process_user_seconds':usage.ru_utime,'process_system_seconds':usage.ru_stime,
                          'reaped_children_peak_rss_bytes':children.ru_maxrss*1024,
                          'reaped_children_user_seconds':children.ru_utime,'reaped_children_system_seconds':children.ru_stime},
             'engine_selected':False,'qualification_complete':False})
            source_binding(root,control)
        except Exception as error:
            receipt.update(status='failed',driver_failure={'error_kind':type(error).__name__,'error':str(error)[:1024],
                'traceback':traceback.format_exc()[:16384]})
        finally:
            receipt['elapsed_seconds_observed']=time.monotonic()-started;save()
        print(json.dumps({'status':receipt['status'],'checks':receipt['checks']}))
        return 0 if receipt['status']=='passed' else 1
    finally:os.close(jobfd)
raise SystemExit(_missing_probe())
'''

# Uses the shared adapter root/run/HEAD/byte/error/dump guards after integration.
_MISSING_LOADED_CONTROLLER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _missing_binding(root):
    from evaluations.tree_sitter_baseline import collector_identity
    from evaluations.queued_collector import _identity
    commit = _adapter_head(root)
    implementation = {name: _adapter_bytes(root, name, cap=LOG_BYTES)[1] for name in _MISSING_HELPERS}
    if implementation['evaluations/engine_checks.py'] != _MISSING_LOADED_CONTROLLER_SHA256:
        raise ValueError('Missing-backend controller changed since module load')
    if collector_identity() != implementation['repo_graph/analysis_native.py']:
        raise ValueError('Missing-backend loaded collector mismatch')
    queued = _identity()
    if queued['loaded_controller_sha256'] != implementation['repo_graph/analysis_queue.py']:
        raise ValueError('Missing-backend loaded queue controller mismatch')
    if _adapter_head(root) != commit:
        raise ValueError('Commit changed during missing-backend admission')
    with SourceRoot(root) as owner:
        info = os.fstat(owner.fd)
        root_owner = {'device': info.st_dev, 'inode': info.st_ino}
    return {'measured_commit': commit, 'implementation_sha256': implementation, 'root_owner': root_owner,
        'loaded_controller_sha256': _MISSING_LOADED_CONTROLLER_SHA256,
        'load_scope': 'Controller, collector and queue load snapshots verified; other helpers bound to current disk bytes, not general module-load attestation.',
        'commit_scope': 'Actual current HEAD identity; this finite component probe does not establish committed blob equivalence or qualification.'}


def _missing_recheck(root, binding):
    if _adapter_head(root) != binding['measured_commit']:
        raise ValueError('Commit changed during missing-backend probe')
    with SourceRoot(root) as owner:
        info = os.fstat(owner.fd)
        if binding['root_owner'] != {'device': info.st_dev, 'inode': info.st_ino}:
            raise ValueError('Missing-backend source checkout owner changed')
    for name, expected in binding['implementation_sha256'].items():
        _adapter_bytes(root, name, expected, LOG_BYTES)
    return {'measured_commit': binding['measured_commit'], 'implementation_stable': True,
        'implementation_sha256': dict(binding['implementation_sha256']), 'root_owner': dict(binding['root_owner'])}


def _missing_register_runtime(guarded, owners):
    import stat
    for name in ('venv', 'source', 'map', 'home', 'config', 'cache', 'data', 'tmp'):
        if name in owners:
            continue
        try:
            info = os.stat(name, dir_fd=guarded.fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('Missing-backend runtime root must be an owned directory')
        owners[name] = {'device': info.st_dev, 'inode': info.st_ino}


def _missing_remove_runtime(guarded, owners):
    import shutil
    import stat
    if not shutil.rmtree.avoids_symlink_attacks:
        raise OSError('Descriptor-relative runtime cleanup required')
    receipts = []
    for name, expected in owners.items():
        row = {'path': name, 'removed': False}
        try:
            info = os.stat(name, dir_fd=guarded.fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or expected != {'device': info.st_dev, 'inode': info.st_ino}:
                raise ValueError('Runtime name no longer identifies the registered owned directory')
            shutil.rmtree(name, dir_fd=guarded.fd)
            row['removed'] = True
        except Exception as error:
            row.update(_adapter_error(error))
        receipts.append(row)
    return receipts


def _missing_stage(guarded, job, name, executable, code, args, env):
    row = {'stage': name, 'status': 'running', 'returncode': None, 'stop_reason': None, 'cleanup': None}
    process = None
    started = time.monotonic()
    try:
        with guarded.open(name+'.stdout.log', create=True) as out, guarded.open(name+'.stderr.log', create=True) as err:
            try:
                process = subprocess.Popen([executable, '-I', '-B', '-c', code, *args], cwd=job, env=env,
                    stdout=out, stderr=err, stdin=subprocess.DEVNULL, start_new_session=True,
                    close_fds=True, pass_fds=(guarded.fd,))
                row['pid'] = process.pid
                row['worker_source_sha256'] = hashlib.sha256(code.encode()).hexdigest()
                while process.poll() is None:
                    if time.monotonic()-started >= 30:
                        row['stop_reason'] = 'wall_budget_exceeded'
                        break
                    if any(guarded.info(name+'.'+stream+'.log').st_size > LOG_BYTES for stream in ('stdout', 'stderr')):
                        row['stop_reason'] = 'log_budget_exceeded'
                        break
                    time.sleep(.005)
            finally:
                if process is not None:
                    row['cleanup'] = _stop_and_reap(process)
                    row['returncode'] = process.returncode
        row['status'] = 'passed' if row['returncode'] == 0 and not row['stop_reason'] and row['cleanup']['leader_reaped'] and row['cleanup']['group_absent'] else 'failed'
    except Exception as error:
        row.update(status='failed', **_adapter_error(error))
    row['elapsed_seconds'] = time.monotonic()-started
    return row


def _missing_valid_probe(probe, control_sha, control, producer_pid):
    if type(probe) is not dict or type(probe.get('schema_version')) is not int or probe['schema_version'] != 1:
        return False
    checks = probe.get('checks')
    components = probe.get('components')
    return (probe.get('status') == 'passed' and probe.get('control_sha256') == control_sha
        and probe.get('producer_token') == control['producer_token']
        and type(probe.get('producer_pid')) is int and probe['producer_pid'] == producer_pid
        and probe.get('source_records') == control['source_records']
        and probe.get('implementation_sha256') == control['implementation_sha256']
        and type(checks) is dict and set(checks) == set(_MISSING_CHECKS) and all(type(v) is bool and v for v in checks.values())
        and type(components) is list and len(components) == 2 and [v.get('mode') for v in components] == ['serial', 'queued'])


def run_missing_backend(root=ROOT, evidence_directory=None):
    """Finite component probe with retained stage failures and private cleanup receipts.

    Shared adapter guards are required. No installation or engine qualification
    is accepted. Descendant queue-group cleanup requires its separate review.
    """
    root = _adapter_root(root)
    with _adapter_run(root, evidence_directory, 'missing-backend') as (run, name), SourceRoot(run) as guarded:
        expected, actual = os.stat(run), os.fstat(guarded.fd)
        if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            raise ValueError('Missing-backend private job owner changed')
        job = Path('/proc/'+str(os.getpid())+'/fd/'+str(guarded.fd))
        owners = {}
        result = {'schema_version': 1, 'status': 'running', 'stages': [], 'probe': None, 'logs': [],
            'engine_selected': False, 'qualification_complete': False, 'archive_directory': name,
            'excluded_runtime_directories': ['venv', 'source', 'map', 'home', 'config', 'cache', 'data', 'tmp'],
            'limits': {'worker_wall_seconds': 30, 'address_space_bytes': MEMORY_BYTES, 'cpu_seconds': 30, 'per_file_bytes': LOG_BYTES},
            'limitations': ['Successful retained source-checkout observations qualify map/keyword and explicit absent-backend refusal; installed-harness checks remain separate.',
                'No installed-harness, provider, corpus, scale, engine or human qualification.',
                'Parent RSS and blocked filesystem operations are not bounded by byte caps.',
                'Runtime identities are registered after each setup step; named-root checks reject observed swaps, but the check-to-rmtree same-name race is not atomic.',
                'Wrapper process cleanup covers its two owned groups; queue descendant survival/reaping remains separately qualified.']}
        control = None
        control_sha = None
        try:
            _adapter_dump(run, 'result.json', result)
            binding = _missing_binding(root)
            result.update(binding)
            result['binding_before'] = dict(binding)
            control = dict(binding, schema_version=1, producer_token=uuid.uuid4().hex,
                setup_script_sha256=hashlib.sha256(_MISSING_SETUP.encode()).hexdigest(),
                probe_script_sha256=hashlib.sha256(_MISSING_PROBE.encode()).hexdigest(),
                source_records=_MISSING_SOURCE_RECORDS)
            _adapter_dump(run, 'control.json', control)
            _, control_sha, _ = guarded.read('control.json', LOG_BYTES+1, hash_full=False)
            result['control_sha256'] = control_sha
            try:
                env = _environment(job)
            finally:
                _missing_register_runtime(guarded, owners)
            for stage, executable, code, args in [('venv', sys.executable, _MISSING_SETUP, [str(job)]),
                    ('probe', str(job/'venv/bin/python'), _MISSING_PROBE, [str(root), str(job), control_sha])]:
                row = _missing_stage(guarded, job, stage, executable, code, args, env)
                result['stages'].append(row)
                _adapter_dump(run, stage+'-stage.json', row)
                _adapter_dump(run, 'result.json', result)
                _missing_register_runtime(guarded, owners)
                if row['status'] != 'passed':
                    break
            from evaluations.acceptance import read_json as strict_json
            try:
                result['probe'], _, = strict_json(guarded, 'probe.json')
            except FileNotFoundError:
                pass
            result['binding_after'] = _missing_recheck(root, binding)
            valid = control is not None and len(result['stages']) == 2 and _missing_valid_probe(result['probe'], control_sha, control, result['stages'][1].get('pid'))
            result['status'] = 'passed' if len(result['stages']) == 2 and all(row['status']=='passed' for row in result['stages']) and valid else 'failed'
            if not valid:
                result['probe_validation'] = 'Missing or invalid owned typed nine-check receipt'
        except Exception as error:
            result.update(status='failed', driver_failure=_adapter_error(error))
        finally:
            try:
                _missing_register_runtime(guarded, owners)
            except Exception as error:
                result.update(status='failed', runtime_registration_failure=_adapter_error(error))
            try:
                result['runtime_cleanup'] = _missing_remove_runtime(guarded, owners)
                result['owned_runtime_removed'] = 'runtime_registration_failure' not in result and all(row['removed'] for row in result['runtime_cleanup'])
            except Exception as error:
                result.update(status='failed', owned_runtime_removed=False, runtime_cleanup_failure=_adapter_error(error))
            if not result.get('owned_runtime_removed'):
                result['status'] = 'failed'
            try:
                from evaluations.acceptance import read_json as strict_json
                if result['probe'] is None:
                    try:
                        result['probe'], _, = strict_json(guarded, 'probe.json')
                    except FileNotFoundError:
                        pass
                for parent, dirs, files in os.walk(run, followlinks=False):
                    dirs[:] = sorted(d for d in dirs if parent != str(run) or d not in owners)
                    for filename in sorted(files):
                        relative = (Path(parent)/filename).relative_to(run).as_posix()
                        if relative == 'result.json':
                            continue
                        raw, digest, info = guarded.read(relative, LOG_BYTES+1, hash_full=False)
                        if len(raw) != info.st_size or len(raw) > LOG_BYTES or len(result['logs']) >= 2048:
                            raise ValueError('Missing-backend log inventory exceeds bounds')
                        result['logs'].append({'path': name+'/'+relative, 'sha256': digest, 'bytes': len(raw)})
            except Exception as error:
                result.update(status='failed', log_or_probe_failure=_adapter_error(error))
            try:
                _adapter_dump(run, 'result.json', result)
            except Exception as error:
                result.update(status='failed', publication_failure=_adapter_error(error))
                # A prior running or passed result must not survive a failed final publication.
                try:
                    os.unlink('result.json', dir_fd=guarded.fd)
                except FileNotFoundError:
                    pass
                except Exception as removal_error:
                    result['stale_receipt_removal_failure'] = _adapter_error(removal_error)
        try:
            return _adapter_archive(run, name, result, result['excluded_runtime_directories'])
        except Exception as error:
            result.update(status='failed', archive_failure=_adapter_error(error))
            try:
                _adapter_dump(run, 'result.json', result)
            except Exception as publication_error:
                result['publication_failure'] = _adapter_error(publication_error)
            return {'status': 'failed', 'full_private_report': result,
                'archive': {'directory': name, 'files': [], 'bytes': 0},
                'engine_selected': False, 'qualification_complete': False}


if __name__ == '__main__':
    if sys.argv[1:] != ['--worker']:
        raise SystemExit('Coordinator calls run_checks with an explicit private evidence directory')
    raise SystemExit(_worker())
