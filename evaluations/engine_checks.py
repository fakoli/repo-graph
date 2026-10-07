"""Finite native worker checks; unavailable engine capabilities remain blocked.

The coordinator may call run_checks(evidence_directory=PRIVATE_DIRECTORY).
Workers receive only copied source bytes and metadata inventories. This module
never installs a backend, imports fixture code, or selects a structural owner.
"""
import ast
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import signal
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
                     'evaluations/tree_sitter_baseline.py', 'evaluations/analysis.py', 'repo_graph/source.py'):
            if source.info(path).st_size > LOG_BYTES:
                raise ValueError('Implementation identity exceeds bound')
            raw, sha, info = source.read(path, LOG_BYTES + 1, hash_full=True)
            if len(raw) != info.st_size or len(raw) > LOG_BYTES:
                raise ValueError('Implementation identity exceeds bound')
            implementation[path] = sha
            if path.endswith('tree_sitter_baseline.py'):
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
    checks.append({'id': 'incremental_equivalence', 'status': 'blocked',
        'reason': 'Only stateless scan/extract interfaces exist; no retained index/update API to compare with clean rebuild',
        'source_api': exports, 'scenarios': [{key: item[key] for key in ('id', 'language', 'kind')}
            | {'status': 'not_run', 'reason': 'Incremental update interface unavailable'} for item in fixture['updates']]})
    checks.append({'id': 'bounded_query_work', 'status': 'blocked',
        'reason': 'No bounded query adapter, snapshot pagination, work counters, or cancellation interface exists',
        'high_fan_out': 'not_run', 'pagination': 'not_run', 'generation_rejection': 'not_run'})
    return {'schema_version': 1, 'engine': 'tree-sitter', 'status': 'blocked',
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
            'Full rescans are not incremental updates. Query and incremental gates remain blocked.',
            'Lifecycle checks do not establish real-source call quality, scale, installation distribution, or engine selection.']}


if __name__ == '__main__':
    if sys.argv[1:] != ['--worker']:
        raise SystemExit('Coordinator calls run_checks with an explicit private evidence directory')
    raise SystemExit(_worker())
