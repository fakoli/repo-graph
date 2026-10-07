"""Finite worker observations; no engine-selection or scale acceptance claims."""
import errno
import hashlib
import json
import os
from contextlib import redirect_stdout
import io
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evaluations import performance
from evaluations import real_calls
from evaluations.acceptance import PINS


class ObservedProfile(unittest.TestCase):
    def test_representative_watchdog_bounds_each_job_and_retains_unknown_descendants(self):
        """Immediate synthetic timeouts; no worker, query, parser or profile pair."""
        from evaluations import engine_checks as checks
        for pair_elapsed, maximum in ((0, 900), (2350, 50)):
            with self.subTest(pair_elapsed=pair_elapsed), tempfile.TemporaryDirectory(prefix='representative-watchdog-') as scratch:
                original, protocol, evidence = (Path(scratch) / name for name in ('original', 'protocol', 'evidence'))
                for directory in (original, protocol, evidence): directory.mkdir()
                with performance.SourceRoot(original) as source, performance.SourceRoot(protocol) as inputs:
                    loaded = dict(config=dict(ceilings=dict(performance.REPRESENTATIVE_CEILINGS)),
                        original_owner=source.identity, protocol_owner=inputs.identity, protocol_sha256='a' * 64)
                clock, waits, cleaned = [0], [], []
                class Child:
                    pid, returncode = 999999999, None
                    def __init__(self, command, **options):
                        self.command = command
                        for fd in options['pass_fds']: os.fstat(fd)
                        os.write(options['stderr'].fileno(), b'synthetic stalled controller\n')
                    def wait(self, timeout):
                        waits.append(timeout)
                        raise subprocess.TimeoutExpired(self.command, timeout)
                def materialize(*args): clock[0] = pair_elapsed
                def cleanup(process):
                    cleaned.append(process)
                    return dict(leader_reaped=True, group_absent=True)
                with patch.object(performance.time, 'monotonic', side_effect=lambda: clock[0]), \
                        patch.object(performance, '_dual_supervisor_limits', return_value={}), \
                        patch.object(performance, '_persistent_protocol', return_value=loaded), \
                        patch.object(performance, '_persistent_capture', return_value={}), \
                        patch.object(performance, '_persistent_recheck', return_value={}), \
                        patch.object(performance, '_persistent_materialize', side_effect=materialize), \
                        patch.object(performance.subprocess, 'Popen', side_effect=Child) as launched, \
                        patch.object(checks, '_stop_and_reap', side_effect=cleanup):
                    result = performance.profile_persistent_fixture(Path(performance.__file__).resolve().parents[1],
                        evidence, protocol=protocol, original_source=original)
                self.assertEqual(launched.call_count, 1)
                self.assertEqual(len(waits), 1); self.assertGreater(waits[0], 0)
                self.assertLessEqual(waits[0], maximum)
                self.assertEqual(result['status'], 'failed')
                report = result['full_private_report']
                self.assertEqual(len(report['cases']), 1)
                case = report['cases'][0]
                self.assertEqual(case['mode'], 'serial'); self.assertEqual(case['status'], 'failed')
                self.assertEqual(case['failure']['error_kind'], 'TimeoutExpired')
                self.assertEqual(len(cleaned), 1)
                self.assertTrue(case['cleanup']['group_absent'])
                self.assertEqual(case['descendant_cleanup']['status'], 'unknown')
                self.assertIsNone(case['descendant_cleanup']['collector_sessions_reaped'])
                compact = performance.compact_persistent_result(result)
                self.assertEqual(compact['cases'][0]['descendant_cleanup'], {
                    'status': 'unknown', 'collector_sessions_reaped': None,
                    'scope': 'separate collector sessions; controller-group disappearance is not reaping proof'})
                self.assertNotIn('knowledge', compact['cases'][0]['descendant_cleanup'])
                run = evidence / result['archive']['directory']
                self.assertEqual(json.loads((run / 'report.json').read_bytes()), report)
                self.assertEqual((run / 'serial-1/stderr.log').read_bytes(), b'synthetic stalled controller\n')

    @unittest.skipUnless(sys.platform == 'linux', 'Native death/reaping control requires Linux')
    def test_controller_group_cleanup_and_parent_death_do_not_claim_descendant_reaping(self):
        root = str(Path(performance.__file__).resolve().parents[1])
        child = '\n'.join((
            'import json,os,sys,time', 'sys.path.insert(0,' + repr(root) + ')',
            'from repo_graph.analysis_queue import _guard_controller',
            '_guard_controller(os.getppid())',
            'print(json.dumps(dict(pid=os.getpid(),pgid=os.getpgrp(),sid=os.getsid(0))),flush=True)',
            'time.sleep(60)',
        ))
        controller = '\n'.join((
            'import json,os,subprocess,sys', 'sys.path.insert(0,' + repr(root) + ')',
            'from repo_graph.analysis_queue import _guard_controller', '_guard_controller(os.getppid())',
            'child=subprocess.Popen([sys.executable,"-I","-B","-c",' + repr(child) + '],',
            '    stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,start_new_session=True)',
            'row=json.loads(child.stdout.readline())',
            'print(json.dumps(dict(controller=os.getpid(),child=row)),flush=True)',
            'sys.stdin.buffer.read(1)',
        ))
        supervisor = '\n'.join((
            'import ctypes,json,os,select,signal,subprocess,sys,time',
            'sys.path.insert(0,' + repr(root) + ')',
            'from evaluations.engine_checks import _stop_and_reap',
            'assert ctypes.CDLL(None).prctl(36,1,0,0,0)==0',
            'process=None; child_pid=None',
            'try:',
            '    process=subprocess.Popen([sys.executable,"-I","-B","-c",' + repr(controller) + '],',
            '        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)',
            '    assert select.select([process.stdout],[],[],3)[0],"owned readiness unavailable"',
            '    row=json.loads(process.stdout.readline()); child_pid=row["child"]["pid"]',
            '    assert row["child"]["pgid"]==row["child"]["sid"]==child_pid!=process.pid',
            '    cleanup=_stop_and_reap(process)',
            '    assert cleanup["leader_reaped"] and cleanup["group_absent"]',
            '    deadline=time.monotonic()+2; state=None',
            '    while time.monotonic()<deadline:',
            '        with open("/proc/"+str(child_pid)+"/stat","rb") as stream: raw=stream.read(4096)',
            '        state=raw[raw.rfind(b")")+2:].split()[0].decode()',
            '        if state=="Z": break',
            '        time.sleep(.005)',
            '    assert state=="Z","death protection is not a reaping receipt"',
            '    waited,status=os.waitpid(child_pid,0); assert waited==child_pid',
            '    assert os.WIFSIGNALED(status) and os.WTERMSIG(status)==signal.SIGKILL',
            '    child_pid=None',
            '    print(json.dumps(dict(controller_cleanup=cleanup,descendant_state_before_reap=state,',
            '        descendant_reaped_by_surviving_owner=True)),flush=True)',
            'finally:',
            '    if process is not None:',
            '        _stop_and_reap(process)',
            '        for stream in (process.stdin,process.stdout,process.stderr): stream.close()',
            '    if child_pid is not None:',
            '        try: os.kill(child_pid,signal.SIGKILL)',
            '        except ProcessLookupError: pass',
            '        os.waitpid(child_pid,0)',
        ))
        result = subprocess.run([sys.executable, '-I', '-B', '-c', supervisor],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        observed = json.loads(result.stdout)
        self.assertTrue(observed['controller_cleanup']['group_absent'])
        self.assertEqual(observed['descendant_state_before_reap'], 'Z')
        self.assertTrue(observed['descendant_reaped_by_surviving_owner'])

    def test_representative_flat_manifest_rejects_foreign_duplicate_and_changed_records(self):
        rows = [dict(path='src/' + str(number).zfill(4) + '.py', language='python',
                     kind='source', bytes=1, sha256=hashlib.sha256(b'x').hexdigest())
                for number in range(2976)]
        rows += [dict(path='package.json', language='javascript', kind='configuration', bytes=2,
                      sha256=hashlib.sha256(b'{}').hexdigest()),
                 dict(path='pyproject.toml', language='python', kind='configuration', bytes=3,
                      sha256=hashlib.sha256(b'# x').hexdigest())]
        rows.sort(key=lambda row: row['path'])
        canonical = json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()
        raw = b''.join(json.dumps(row, sort_keys=True, separators=(',', ':')).encode() + b'\n' for row in rows)
        with tempfile.TemporaryDirectory(prefix='representative-manifest-') as scratch:
            directory, other = Path(scratch) / 'protocol', Path(scratch) / 'other'
            directory.mkdir(); other.mkdir()
            (directory / 'records.jsonl').write_bytes(raw)
            with performance.SourceRoot(directory) as owner, performance.SourceRoot(other) as foreign:
                loaded = dict(protocol_owner=owner.identity, header=dict(manifest=dict(
                    sha256=hashlib.sha256(raw).hexdigest(), records_sha256=hashlib.sha256(canonical).hexdigest(),
                    files=len(rows), bytes=len(raw), actual_content_bytes=sum(row['bytes'] for row in rows),
                    language_counts=dict(python=2976))))
                self.assertEqual(list(performance._persistent_records(directory, loaded)), rows)
                with self.assertRaisesRegex(ValueError, 'owner changed'):
                    list(performance._persistent_records(directory, dict(loaded, protocol_owner=foreign.identity)))
                duplicate = raw.splitlines(keepends=True)[0] + raw
                changed = raw.replace(rows[-1]['sha256'].encode(), b'f' * 64, 1)
                for label, mutation in (('duplicate', duplicate), ('changed', changed)):
                    with self.subTest(mutation=label):
                        (directory / 'records.jsonl').write_bytes(mutation)
                        with self.assertRaises(ValueError):
                            list(performance._persistent_records(directory, loaded))
                (directory / 'records.jsonl').write_bytes(raw)
                self.assertEqual(list(performance._persistent_records(directory, loaded)), rows)

    def test_pinned_sqlite_snapshot_keeps_wal_facts_and_failed_backups_unsealed(self):
        import sqlite3
        from repo_graph.analysis import SCHEMA
        facts = dict(
            definitions=[dict(id='main.py:0:8', path='main.py', name='target', text='def f():',
                range=dict(start_byte=0, end_byte=8, start_line=1, end_line=1))],
            sites=[dict(id='main.py:9:12', path='main.py', text='f()', role='call',
                        target_ids=['main.py:0:8'], target_certainty='exact'),
                   dict(id='main.py:13:22', path='main.py', text='missing()', role='call',
                        target_ids=[], target_certainty='unknown')],
            scopes=[dict(id='scope-main', parent=None, kind='module', name='', owner=None, path='main.py')],
            imports=[dict(path='main.py', module='missing', name='missing', text='import missing')],
            relationships=[dict(site_id='main.py:9:12', target_id='main.py:0:8', path='main.py',
                                role='call', certainty='exact')])
        semantic = b''.join(json.dumps(dict(kind=kind, fact=row), sort_keys=True,
            separators=(',', ':')).encode() + b'\n' for kind, rows in facts.items() for row in rows)
        with tempfile.TemporaryDirectory(prefix='representative-snapshot-') as scratch:
            source, output, retained = (Path(scratch) / name for name in ('source', 'output', 'retained'))
            for directory in (source, output, retained): directory.mkdir()
            with performance.SourceRoot(source) as owner, performance.SourceRoot(output) as artifact:
                index = SimpleNamespace(owner=owner.identity, output=output, output_owner=artifact.identity)
            identities = dict(generation='b' * 64, repository_identity=index.owner,
                              source_identity='c' * 64, analyzer_identity='d' * 64, config_identity='e' * 64)
            writer = sqlite3.connect(output / 'search.db')
            self.addCleanup(writer.close)
            writer.execute('PRAGMA journal_mode=WAL'); writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
            meta = {'repository': index.owner, 'structural_schema': SCHEMA}
            meta.update({'structural_' + key: identities[value] for key, value in
                dict(generation='generation', repository='repository_identity', source='source_identity',
                     analyzer='analyzer_identity', config='config_identity').items()})
            writer.executemany('INSERT INTO meta VALUES(?,?)', meta.items())
            tables = dict(definitions='structural_symbols', sites='structural_sites',
                          scopes='structural_scopes', imports='structural_imports')
            for kind, table in tables.items():
                writer.execute('CREATE TABLE ' + table + '(path TEXT,ordinal INTEGER,data TEXT)')
                writer.executemany('INSERT INTO ' + table + ' VALUES(?,?,?)',
                    [('main.py', number, json.dumps(row)) for number, row in enumerate(facts[kind])])
            writer.execute('CREATE TABLE structural_relationships(site_id TEXT,target_id TEXT,path TEXT,role TEXT,certainty TEXT)')
            writer.executemany('INSERT INTO structural_relationships VALUES(?,?,?,?,?)',
                [tuple(row[key] for key in ('site_id', 'target_id', 'path', 'role', 'certainty'))
                 for row in facts['relationships']] + [('main.py:13:22', '', 'main.py', 'call', 'unknown')])
            # Force more than one bounded backup page group; this is output padding, not source facts.
            writer.execute('CREATE TABLE padding(body BLOB)')
            writer.execute('INSERT INTO padding VALUES(?)', (b'x' * (512 * 1024),))
            writer.commit()
            self.assertGreater((output / 'search.db-wal').stat().st_size, 0)
            proof = performance._persistent_snapshot(index, retained, 'success', check=lambda: None,
                                                     limits=dict(snapshot_bytes=1024 * 1024))
            self.assertEqual(proof['evidence_mode'], 'pinned_sqlite_backup_v1')
            self.assertEqual(proof['identities'], identities)
            self.assertEqual(proof['counts'], {kind: len(rows) for kind, rows in facts.items()})
            self.assertEqual(proof['semantic_facts_sha256'], hashlib.sha256(semantic).hexdigest())
            self.assertTrue(proof['snapshot']['sealed']); self.assertTrue(proof['snapshot']['metadata_verified'])
            self.assertGreater(proof['snapshot']['backup_progress_callbacks'], 1)
            self.assertEqual(proof['retention']['kind'], 'independent_sealed')
            self.assertEqual(proof['retention']['measured_artifact'], proof['artifact'])
            alias = performance._persistent_snapshot(index, retained, 'alias', check=lambda: None,
                limits=dict(snapshot_bytes=1024 * 1024), prior_proofs=(proof,))
            self.assertEqual(alias['retention']['kind'], 'canonical_alias_v1')
            self.assertEqual(alias['artifact'], proof['artifact'])
            self.assertEqual(alias['retention']['alias_source_artifact'], proof['artifact'])
            measured = alias['retention']['measured_artifact']
            self.assertEqual(measured['path'], 'alias.facts.sqlite')
            self.assertEqual(measured['bytes'], proof['artifact']['bytes'])
            self.assertFalse((retained / measured['path']).exists())
            self.assertTrue(alias['snapshot']['sealed']); self.assertTrue(alias['snapshot']['metadata_verified'])
            self.assertGreater(alias['snapshot']['backup_progress_callbacks'], 1)
            for key in ('identities', 'counts', 'semantic_facts_sha256'):
                self.assertEqual(alias[key], proof[key])
            with performance._persistent_snapshot_view(retained, alias, lambda: None) as view:
                self.assertEqual(list(view.read_facts('definitions')), facts['definitions'])
            # Independent observed differences must retain their own sealed proof.
            identity_keys = dict(generation='structural_generation', source_identity='structural_source',
                analyzer_identity='structural_analyzer', config_identity='structural_config',
                repository_identity='structural_repository')
            for change in ('count', 'digest', *identity_keys):
                with self.subTest(snapshot_change=change):
                    if change == 'count':
                        writer.execute('INSERT INTO structural_symbols VALUES(?,?,?)',
                            ('main.py', 1, json.dumps(dict(facts['definitions'][0], id='extra', name='extra'))))
                    elif change == 'digest':
                        writer.execute('UPDATE structural_symbols SET data=? WHERE ordinal=0',
                            (json.dumps(dict(facts['definitions'][0], text='def h():')),))
                    else:
                        writer.execute('UPDATE meta SET value=? WHERE key=?', ('0' * 64, identity_keys[change]))
                        if change == 'repository_identity':
                            writer.execute("UPDATE meta SET value=? WHERE key='repository'", ('0' * 64,))
                            index.owner = '0' * 64
                    writer.commit()
                    changed_proof = performance._persistent_snapshot(index, retained, 'changed-' + change.replace('_', '-'),
                        check=lambda: None, limits=dict(snapshot_bytes=1024 * 1024), prior_proofs=(proof,))
                    self.assertEqual(changed_proof['retention']['kind'], 'independent_sealed')
                    self.assertNotEqual(changed_proof['artifact']['path'], proof['artifact']['path'])
                    self.assertTrue((retained / changed_proof['artifact']['path']).exists())
                    key = 'counts' if change == 'count' else 'semantic_facts_sha256' if change == 'digest' else 'identities'
                    self.assertNotEqual(changed_proof[key], proof[key])
                    if change == 'count': writer.execute('DELETE FROM structural_symbols WHERE ordinal=1')
                    elif change == 'digest':
                        writer.execute('UPDATE structural_symbols SET data=? WHERE ordinal=0',
                            (json.dumps(facts['definitions'][0]),))
                    else:
                        writer.execute('UPDATE meta SET value=? WHERE key=?', (identities[change], identity_keys[change]))
                        if change == 'repository_identity':
                            writer.execute("UPDATE meta SET value=? WHERE key='repository'", (identities[change],))
                            index.owner = identities[change]
                    writer.commit()
            corrupt = json.loads(json.dumps(proof))
            corrupt['artifact']['path'] = 'corrupt-prior.facts.sqlite'
            original_proof_bytes = (retained / proof['artifact']['path']).read_bytes()
            (retained / corrupt['artifact']['path']).write_bytes(
                original_proof_bytes.replace(b'c' * 64, b'f' * 64, 1))
            with self.assertRaises(ValueError):
                performance._persistent_snapshot(index, retained, 'corrupt-match', check=lambda: None,
                    limits=dict(snapshot_bytes=1024 * 1024), prior_proofs=(corrupt,))
            self.assertEqual((retained / proof['artifact']['path']).read_bytes(), original_proof_bytes)
            # Refresh is stubbed over the existing hand-built WAL owner; no parser runs.
            from repo_graph.analysis_native import Budget
            receipt = dict(identities, status='ready', published=True)
            sampler = SimpleNamespace(error=None, set_phase=lambda label: None)
            protocol = dict(config=dict(ceilings=dict(
                performance.REPRESENTATIVE_CEILINGS, snapshot_bytes=1024 * 1024)))
            for label, returned in (('matching', receipt),
                    ('switched', dict(receipt, generation='a' * 64, source_identity='f' * 64))):
                refresh = SimpleNamespace(**vars(index), budget=Budget(), last_attempt=dict(receipt),
                    refresh=lambda *args, returned=returned, **kwargs: dict(returned))
                attempt = performance._persistent_attempt(refresh, [], label, 'serial', 1, retained, sampler,
                    protocol=protocol, check=lambda: None)
                self.assertEqual(attempt['receipt'], returned)
                self.assertEqual(attempt['streamed_facts']['identities'], identities)
                self.assertTrue(attempt['streamed_facts']['snapshot']['sealed'])
                self.assertEqual(attempt['status'], 'ready' if label == 'matching' else 'failed')
                self.assertEqual(json.loads((retained / (label + '.json')).read_bytes()), attempt)
                self.assertTrue((retained / attempt['streamed_facts']['artifact']['path']).exists())
                if label == 'matching': self.assertTrue(attempt['snapshot_receipt_identities_verified'])
                else: self.assertEqual(attempt['error']['error_kind'], 'ValueError')
            # Only complete-report shape/attribution is graded; query workloads are stubbed.
            bound = dict.fromkeys(('measured_commit', 'implementation', 'input_binding', 'root_identity',
                'backend', 'queue_identity', 'runtime', 'persistent_writer_identity', 'persistent_limits'), 'synthetic')
            edits = ('U-PY-BODY', 'U-PY-EXPORT')
            report = dict(schema_version=1, kind='persistent_fixture', status='complete', binding_before=bound,
                binding_after=bound, mode='serial', concurrency=1, qualification_complete=False,
                resource_budgets_frozen=False, engine_selected=False, all_owned_source_reads_measured=False,
                source_data_accounting_complete=True, source_owner_identity=index.owner,
                source_owner_identity_after=index.owner, phases=[dict(label=label, status='ready', receipt=receipt,
                    streamed_facts=proof, source_accounting=dict(accounting_complete=True, totals_kind='exact',
                        worker_requests_unknown=0, collector_original_source_stream_bytes=0))
                    for label in performance.PERSISTENT_PHASES],
                owned_rss=dict(error=None, sampler_stopped=True, remaining_registered_owned_child_owners=[],
                    complete_sample_count=1, peak_sampled_owned_rss_bytes=1,
                    all_measured_phase_children_registered=True, all_created_children_registered=False,
                    excluded_source_fence_intervals=7),
                equivalence=dict(unchanged_generation=True, unchanged_facts=True,
                    **{edit: dict(identities=True, semantic_facts=True, counts=True, generation_changed=True,
                                 source_identity_changed=True) for edit in edits}))
            report['source_impacts'] = {label: {edit: dict(passed=True, checked_impacts=1, artifact={},
                oracle_projection_sha256=performance.PERSISTENT_IMPACT_SHA[edit]) for edit in
                (edits if label == 'fresh-output' else
                 (edits[0],) if label.startswith(edits[0]) else (edits[1],))}
                for label in performance.PERSISTENT_PHASES if label != 'unchanged-repeat'}
            with patch.object(performance, '_persistent_validate_queries', return_value=None):
                self.assertIs(performance._persistent_validate(report, bound, 'serial', 1), report)
                for key in identities:
                    changed_report = json.loads(json.dumps(report))
                    changed_report['phases'][1]['streamed_facts']['identities'][key] = '0' * 64
                    with self.subTest(identity=key), self.assertRaisesRegex(ValueError, 'identities differ'):
                        performance._persistent_validate(changed_report, bound, 'serial', 1)
            sealed = retained / proof['artifact']['path']
            initial = sealed.read_bytes()
            self.assertEqual(proof['artifact']['sha256'], hashlib.sha256(initial).hexdigest())
            self.assertEqual(proof['artifact']['bytes'], len(initial))
            with sqlite3.connect(sealed) as db:
                self.assertEqual(dict(db.execute('SELECT key,value FROM meta')), meta)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM structural_relationships').fetchone()[0], 2)
                self.assertEqual(json.loads(db.execute('SELECT data FROM structural_sites WHERE ordinal=1').fetchone()[0]),
                                 facts['sites'][1])
            foreign = SimpleNamespace(**dict(vars(index), owner='f' * 64))
            with self.assertRaisesRegex(ValueError, 'metadata affinity'):
                performance._persistent_snapshot(foreign, retained, 'foreign', check=lambda: None,
                                                 limits=dict(snapshot_bytes=1024 * 1024))
            writer.execute("UPDATE meta SET value='foreign-schema' WHERE key='structural_schema'"); writer.commit()
            with self.assertRaisesRegex(ValueError, 'metadata affinity'):
                performance._persistent_snapshot(index, retained, 'schema', check=lambda: None,
                                                 limits=dict(snapshot_bytes=1024 * 1024))
            writer.execute("UPDATE meta SET value=? WHERE key='structural_schema'", (SCHEMA,)); writer.commit()
            with self.assertRaisesRegex(ValueError, 'size ceiling'):
                performance._persistent_snapshot(index, retained, 'capped', check=lambda: None,
                                                 limits=dict(snapshot_bytes=8192))
            interrupted, connect = [False], sqlite3.connect
            class InterruptedConnection(sqlite3.Connection):
                def backup(self, target, **kwargs):
                    progress = kwargs['progress']
                    def stop_after_page_group(*args):
                        interrupted[0] = True
                        return progress(*args)
                    return super().backup(target, **dict(kwargs, progress=stop_after_page_group))
            def cancel_backup():
                if interrupted[0]: raise InterruptedError('synthetic interrupted backup')
            with patch.object(performance.sqlite3, 'connect', side_effect=lambda *args, **kwargs:
                    connect(*args, **dict(kwargs, factory=InterruptedConnection))):
                with self.assertRaisesRegex(InterruptedError, 'synthetic interrupted backup'):
                    performance._persistent_snapshot(index, retained, 'interrupted', check=cancel_backup,
                                                     limits=dict(snapshot_bytes=1024 * 1024), prior_proofs=(proof,))
            for label in ('foreign', 'schema', 'capped', 'interrupted'):
                failure = json.loads((retained / (label + '-snapshot-failure.json')).read_bytes())
                self.assertFalse(failure['sealed'])
                self.assertFalse((retained / (label + '.facts.sqlite')).exists())
            self.assertEqual(json.loads((retained / 'interrupted-snapshot-failure.json').read_bytes())['stage'],
                             'snapshot_backup')
            self.assertTrue((retained / 'interrupted.facts-building.sqlite').exists())
            self.assertEqual(sealed.read_bytes(), initial)
            self.assertFalse(list(retained.glob('*.facts.jsonl')))
            self.assertEqual(initial.count(b'c' * 64), 1)
            changed = initial.replace(b'c' * 64, b'f' * 64, 1)
            sealed.write_bytes(changed)
            self.assertEqual(len(changed), len(initial))
            self.assertNotEqual(hashlib.sha256(changed).hexdigest(), proof['artifact']['sha256'])
            forged = json.loads(json.dumps(proof))
            forged['artifact']['sha256'] = hashlib.sha256(changed).hexdigest()
            for saved in (proof, forged):
                with self.subTest(forged_digest=saved is forged), self.assertRaises(ValueError):
                    with performance._persistent_snapshot_view(retained, saved, lambda: None) as view:
                        list(view.read_facts('definitions'))
            writer.close()
            sealed.write_bytes(initial)
            # Only validated evidence permits removing a registered direct-child live index.
            from evaluations import engine_checks as checks
            live = retained / 'obsolete-index'; live.mkdir()
            (live / 'search.db').write_bytes(initial)
            with performance.SourceRoot(live) as owner: live_owner = owner.identity
            obsolete = SimpleNamespace(owner=index.owner, output=live, output_owner=live_owner,
                last_attempt=dict(identities, status='ready', published=True))
            unrelated = retained / 'unrelated.log'; unrelated.write_bytes(b'keep')
            bad_proof = dict(proof, artifact=dict(proof['artifact'], sha256='0' * 64))
            with patch.object(checks, '_missing_remove_runtime') as remove:
                with self.assertRaises(ValueError):
                    performance._persistent_retire(obsolete, retained, bad_proof, 'bad-proof', lambda: None)
                remove.assert_not_called()
            self.assertEqual((live / 'search.db').read_bytes(), initial)
            foreign_live = retained / 'foreign-index'; foreign_live.mkdir()
            (foreign_live / 'canary').write_bytes(b'foreign')
            linked = retained / 'linked-index'; linked.symlink_to(live, target_is_directory=True)
            for label, path in (('foreign-output', foreign_live), ('linked-output', linked)):
                with self.subTest(retire=label), self.assertRaises((OSError, ValueError)):
                    performance._persistent_retire(SimpleNamespace(**dict(vars(obsolete), output=path)),
                        retained, proof, label, lambda: None)
                self.assertTrue(live.exists()); self.assertEqual((foreign_live / 'canary').read_bytes(), b'foreign')
                self.assertFalse(json.loads((retained / (label + '-live-index-cleanup.json')).read_bytes())['removed'])
            held = retained / 'held-index'; live.rename(held); live.mkdir()
            (live / 'canary').write_bytes(b'replaced')
            with self.assertRaises((OSError, ValueError)):
                performance._persistent_retire(obsolete, retained, proof, 'replaced-output', lambda: None)
            self.assertEqual((live / 'canary').read_bytes(), b'replaced')
            self.assertTrue((held / 'search.db').exists())
            replaced = retained / 'replaced-index'; live.rename(replaced); held.rename(live)
            with performance.SourceRoot(live) as owner: self.assertEqual(owner.identity, live_owner)
            events, validate, remove = [], performance._persistent_snapshot_view, checks._missing_remove_runtime
            def validated(*args): events.append('validate-proof'); return validate(*args)
            def removed(*args): events.append('remove-live'); return remove(*args)
            with patch.object(performance, '_persistent_snapshot_view', side_effect=validated), \
                    patch.object(checks, '_missing_remove_runtime', side_effect=removed):
                cleanup = performance._persistent_retire(obsolete, retained, proof, 'retired', lambda: None)
            self.assertEqual(events, ['validate-proof', 'remove-live'])
            self.assertTrue(cleanup['removed']); self.assertFalse(live.exists())
            self.assertEqual(cleanup['identities'], identities)
            self.assertEqual(cleanup['proof_artifact'], proof['artifact'])
            self.assertEqual(json.loads((retained / 'retired-live-index-cleanup.json').read_bytes()), cleanup)
            self.assertEqual(sealed.read_bytes(), initial); self.assertEqual(unrelated.read_bytes(), b'keep')
            self.assertEqual((replaced / 'canary').read_bytes(), b'replaced')

    @unittest.skipUnless(sys.platform == 'linux', 'Owned native process limits require Linux')
    def test_small_native_children_lower_inherited_cpu_and_file_envelopes(self):
        from tests.test_analysis import AVAILABLE
        if not AVAILABLE: self.skipTest('Optional analysis extra is not installed')
        child = '\n'.join((
            'import json,resource,sys',
            'from pathlib import Path',
            'assert sys.flags.isolated and sys.flags.no_user_site and sys.dont_write_bytecode',
            'resource.setrlimit(resource.RLIMIT_CPU,(600,600))',
            'resource.setrlimit(resource.RLIMIT_FSIZE,(2*1024**3,2*1024**3))',
            'sys.path.insert(0,' + repr(str(Path(performance.__file__).resolve().parents[1])) + ')',
            'from repo_graph.analysis_queue import collect_files,QueueLimits',
            'seen=[]',
            'def observe(event):',
            '    if event["event"]=="readiness" and event["worker"] is not None:',
            '        pid=event["worker"]["pid"]',
            '        lines=Path("/proc",str(pid),"limits").read_text().splitlines()',
            '        row={key:list(map(int,next(line for line in lines if line.startswith(label)).split()[-3:-1]))',
            '            for key,label in (("cpu","Max cpu time"),("file","Max file size"))}',
            '        seen.append(dict(pid=pid,**row))',
            '    return True',
            'items=[dict(path=name,language="python",content=raw) for name,raw in',
            '    (("a.py",b"def first(): pass\\n"),("b.py",b"def second(): return 2\\n"))]',
            'result=collect_files(items,mode="queued",concurrency=2,limits=QueueLimits(),telemetry=True,observer=observe)',
            'assert result.status=="complete",(result.status,result.failures)',
            'assert len(result.collected)==2 and len(seen)==2',
            'assert all(row["cpu"]==[30,30] and row["file"]==[8*1024**2,8*1024**2] for row in seen)',
            'assert all(row["leader_reaped"] and row["group_absent"] for row in result.cleanup)',
            'print(json.dumps(dict(status=result.status,workers=seen,files=len(result.collected),',
            '    controller_cpu=resource.getrlimit(resource.RLIMIT_CPU),controller_file=resource.getrlimit(resource.RLIMIT_FSIZE))))',
        ))
        result = subprocess.run([sys.executable, '-I', '-B', '-c', child],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        observed = json.loads(result.stdout)
        self.assertEqual(observed['status'], 'complete')
        self.assertEqual(observed['files'], 2)
        self.assertEqual(observed['controller_cpu'], [600, 600])
        self.assertEqual(observed['controller_file'], [2 * 1024 ** 3, 2 * 1024 ** 3])
        self.assertEqual(len({row['pid'] for row in observed['workers']}), 2)

    def test_streamed_private_archive_full_hashes_empty_prefixes_and_finite_caps(self):
        from evaluations import engine_checks as checks
        from repo_graph.source import SourceRoot
        with tempfile.TemporaryDirectory(prefix='streamed-archive-') as scratch:
            directory = Path(scratch)
            bodies = {'first.bin': b'a' * 65553, 'last.txt': b'tail'}
            for name, raw in bodies.items(): (directory / name).write_bytes(raw)
            limits = dict(max_file_bytes=65553, max_total_bytes=65557, max_files=2)
            observed, original = [], SourceRoot.read
            def read(source, path, prefix, **kwargs):
                result = original(source, path, prefix, **kwargs)
                observed.append((path, prefix, kwargs, result))
                return result
            report = dict(status='failed', reason='synthetic retained result')
            with patch.object(SourceRoot, 'read', read):
                result = checks._adapter_archive(directory, 'owned-run', report, limits=limits)
            archive = result['archive']
            self.assertEqual(result['full_private_report'], report)
            self.assertFalse(result['qualification_complete'])
            self.assertEqual(archive['files'], [dict(path=name, bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest()) for name, raw in sorted(bodies.items())])
            self.assertEqual(archive['bytes'], sum(map(len, bodies.values())))
            self.assertEqual(archive['hash_work']['successful_operations'], 2)
            self.assertEqual(archive['hash_work']['hashed_bytes'], 65557)
            self.assertGreaterEqual(archive['hash_work']['elapsed_seconds'], 0)
            for path, prefix, kwargs, (raw, digest, info) in observed:
                self.assertEqual(prefix, 0)
                self.assertTrue(kwargs['hash_full'])
                self.assertEqual(raw, b'')
                self.assertEqual(kwargs['measurements']['returned_prefix_bytes'], 0)
                self.assertEqual(kwargs['measurements']['stream_bytes'], len(bodies[path]))
                self.assertEqual(kwargs['measurements']['hashed_bytes'], len(bodies[path]))
            legacy = checks._adapter_archive(directory, 'owned-run', report)
            self.assertEqual(legacy['archive'], {key: value for key, value in archive.items() if key != 'hash_work'})
            invalid = [dict(limits, max_file_bytes=65552), dict(limits, max_total_bytes=65556),
                       dict(limits, max_files=1), dict(limits, max_files=True),
                       dict(limits, unexpected=1), {'max_files': 2}]
            for bounds in invalid:
                with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                    checks._adapter_archive(directory, 'owned-run', report, limits=bounds)
            with self.assertRaises(InterruptedError):
                checks._adapter_archive(directory, 'owned-run', report, limits=limits, check=lambda: True)
            self.assertEqual({name: (directory / name).read_bytes() for name in bodies}, bodies)

    def test_persistent_query_failure_retains_responses_samples_and_prior_progress(self):
        """Synthetic returned envelopes exercise retention, without a query workload."""
        from repo_graph import analysis_queries as queries
        definitions = [dict(id='main.py:0:10', path='main.py', name='first',
                            range=dict(start_byte=0, end_byte=10, start_line=1, end_line=1)),
                       dict(id='main.py:10:20', path='main.py', name='second',
                            range=dict(start_byte=10, end_byte=20, start_line=2, end_line=2))]
        meta = dict(generation='g' * 64, source_identity='s' * 64,
                    repository_identity='r' * 64, analyzer_identity='a' * 64,
                    config_identity='c' * 64)
        specs = tuple(dict(id='synthetic-' + item['name'], operation='symbol',
                           name=item['name'], path=item['path'],
                           span=tuple(item['range'][key] for key in
                               ('start_byte', 'end_byte', 'start_line', 'end_line')))
                      for item in definitions)
        for failure in ('deadline_exceeded', 'cancelled', 'bad_counter', 'raised_query'):
            observed, prior_artifacts = [], {}
            class Index:
                output, output_owner, owner = Path('.'), 'output', meta['repository_identity']
                def metadata(self): return dict(meta)
                def read_facts(self, kind):
                    if kind != 'definitions': raise AssertionError('Only selector facts required')
                    return iter(definitions)
            class Session:
                def __init__(self, *args, **kwargs): pass
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def run(self, payload):
                    item = next(row for row in definitions if row['id'] == payload['seed'])
                    failing = item is definitions[1]
                    if failing and failure == 'raised_query':
                        prior_artifacts.update((path.name, path.read_bytes())
                            for path in directory.glob('synthetic-first-*.json'))
                        raise RuntimeError('synthetic query run failure')
                    stop = failure if failing and failure != 'bad_counter' else None
                    response = dict(rows=[item], generation=meta['generation'],
                        source_identity=meta['source_identity'], repository_identity=meta['repository_identity'],
                        returned_symbol_handles=0 if failing and failure == 'bad_counter' else 1,
                        returned_entities=0, returned_edges=0, examined_relationships=0, examined_symbols=1,
                        excerpt_bytes=0, stop_reason=stop, truncated=bool(stop), cursor=None,
                        storage_progress_callbacks=0, storage_setup_seconds=0, snapshot_copy_seconds=0,
                        total_count=dict(value=1, knowledge='exact'))
                    observed.append(response)
                    return response
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(prefix='persistent-query-failure-') as scratch:
                directory, progress = Path(scratch), {}
                error_kind, message = ((RuntimeError, 'synthetic query run failure')
                    if failure == 'raised_query' else
                    (ValueError, 'Frozen query generation, counter, work or stop control failed'))
                with patch.object(queries, 'Queries', Session):
                    with self.assertRaisesRegex(error_kind, message):
                        performance._persistent_queries(Index(), directory, specs=specs, progress=progress)
                self.assertEqual(len(observed), 11 if failure == 'raised_query' else 12)
                self.assertFalse(progress['passed'])
                self.assertEqual(len(progress['workloads']), 2)
                self.assertTrue(progress['workloads'][0]['passed'])
                self.assertTrue(all(sample['passed'] for sample in progress['workloads'][0]['samples']))
                failed = progress['workloads'][1]
                self.assertFalse(failed['passed'])
                self.assertEqual(len(failed['samples']), 1)
                sample = failed['samples'][0]
                self.assertFalse(sample['passed'])
                self.assertGreaterEqual(sample['elapsed_seconds'], 0)
                if failure == 'raised_query':
                    self.assertEqual(set(sample), {'temperature', 'passed', 'elapsed_seconds'})
                    self.assertFalse((directory / 'synthetic-second-00.json').exists())
                    self.assertEqual(len(prior_artifacts), 11)
                    for earlier, response in zip(progress['workloads'][0]['samples'], observed):
                        artifact = earlier['artifact']
                        raw = (directory / artifact['path']).read_bytes()
                        self.assertEqual(raw, prior_artifacts[artifact['path']])
                        self.assertEqual(raw, queries.encoded(response))
                        self.assertEqual(artifact['sha256'], hashlib.sha256(raw).hexdigest())
                        self.assertEqual(artifact['bytes'], len(raw))
                else:
                    self.assertEqual(sample['stop_reason'], observed[-1]['stop_reason'])
                    self.assertEqual(sample['returned_symbol_handles'], observed[-1]['returned_symbol_handles'])
                    raw = (directory / sample['artifact']['path']).read_bytes()
                    self.assertEqual(raw, queries.encoded(observed[-1]))
                    self.assertEqual(sample['artifact']['sha256'], hashlib.sha256(raw).hexdigest())
                    self.assertEqual(sample['wire_bytes'], len(raw))
                    self.assertEqual(sample['artifact']['bytes'], len(raw))
                self.assertEqual(json.loads((directory / 'queries.json').read_bytes()), progress)
                self.assertEqual(progress['failure']['error_kind'], error_kind.__name__)

    def test_persistent_collection_retention_failure_preserves_validated_lower_bounds(self):
        from evaluations import engine_checks as checks
        from repo_graph import analysis as runtime
        from tests.test_analysis import AVAILABLE
        if not AVAILABLE: self.skipTest('Optional analysis extra is not installed')
        class Sampler:
            error = None
            def set_phase(self, phase): pass
            def register_process(self, *args): pass
            def process_finished(self, *args): pass
            def observe(self, event): return True
        with tempfile.TemporaryDirectory(prefix='persistent-collection-failure-') as scratch:
            source, logs = Path(scratch) / 'source', Path(scratch) / 'logs'
            source.mkdir(); logs.mkdir()
            raw = b'def function():\n    return 1\n'
            (source / 'main.py').write_bytes(raw)
            index = runtime.StructuralIndex(source, Path(scratch) / 'index')
            captured, original_dump = [], checks._adapter_dump
            def fail_batch(directory, name, value):
                if name.startswith('retention-collection-'):
                    captured.append(value)
                    raise OSError(errno.ENOSPC, 'synthetic batch receipt retention failure')
                return original_dump(directory, name, value)
            with patch.object(checks, '_adapter_dump', side_effect=fail_batch):
                retained = performance._persistent_attempt(index, ['main.py'], 'retention',
                    'serial', 1, logs, Sampler())
            self.assertEqual(len(captured), 1)
            self.assertEqual(retained['status'], 'failed', retained)
            self.assertFalse(retained['receipt']['published'])
            self.assertEqual(retained['collection_batches'], 1)
            self.assertEqual(retained['collection_artifacts'], [])
            accounting = retained['source_accounting']
            self.assertFalse(accounting['accounting_complete'])
            self.assertEqual(accounting['totals_kind'], 'lower_bound')
            self.assertEqual(accounting['failure']['stage'], 'batch_receipt_retention')
            terminal = captured[0]['resources']
            control = terminal['telemetry']['source_accounting']
            self.assertEqual(accounting['passes']['controller_source_validation'], control['controller_source_validation'])
            self.assertEqual(accounting['worker_requests_with_accounting'], control['worker_requests_with_accounting'])
            self.assertEqual(accounting['worker_requests_with_accounting'], 1)
            self.assertEqual(accounting['worker_requests_unknown'], 0)
            worker = terminal['worker_resources'][0][0]['source_accounting']
            for name, row in worker['passes'].items():
                self.assertEqual(accounting['passes'][name], row)
            self.assertEqual(accounting['passes']['mailbox_source']['stream_bytes'], len(raw))
            self.assertGreater(accounting['total_hashed_bytes'], retained['source_reads']['hashed_bytes'])
            self.assertEqual(json.loads((logs / 'retention.json').read_bytes()), retained)

            # Without a returned collection, numeric zeroes are observed lower bounds only.
            with patch.object(runtime, 'collect_files', side_effect=OSError(errno.EIO, 'synthetic unreturned collection')):
                unreturned = performance._persistent_attempt(index, ['main.py'], 'unreturned',
                    'serial', 1, logs, Sampler())
            self.assertEqual(unreturned['status'], 'failed', unreturned)
            self.assertFalse(unreturned['receipt']['published'])
            self.assertEqual(unreturned['collection_batches'], 0)
            self.assertFalse(unreturned['source_accounting']['accounting_complete'])
            self.assertEqual(unreturned['source_accounting']['totals_kind'], 'lower_bound')
            self.assertEqual(unreturned['source_accounting']['failure']['stage'], 'collection')
            self.assertEqual(unreturned['source_accounting']['worker_requests_with_accounting'], 0)
            self.assertEqual(json.loads((logs / 'unreturned.json').read_bytes()), unreturned)

    def test_persistent_compact_projects_private_failures_without_diagnostics(self):
        """Serialize synthetic failure receipts; these counters are not a profile."""
        from evaluations import engine_checks as checks
        private_path = '/synthetic-private/diagnostic-canary.txt'
        secret = 'SYNTHETIC_SECRET_CANARY'
        try:
            raise OSError(errno.ENOSPC, private_path + ' ' + secret)
        except OSError as error:
            diagnostic = checks._adapter_error(error)
        diagnostic.update(errno=errno.ENOSPC, message=secret, reason=private_path)
        accounting = performance._persistent_source_accounting()
        for name, row in accounting['passes'].items():
            row.update(operations=1, successful_operations=1, hashed_bytes=17, hash_passes=1)
            if name in ('controller_original_source', 'mailbox_source'):
                row.update(open_operations=1, stream_bytes=17, returned_prefix_bytes=17)
        accounting.update(accounting_complete=False, totals_kind='lower_bound',
            worker_requests_with_accounting=1, total_source_stream_bytes=34,
            total_hashed_bytes=85, total_hash_passes=5,
            failure=dict(diagnostic, stage='batch_receipt_retention'),
            accounting_failure=dict(diagnostic, stage='source_accounting'))
        sample = dict(temperature='cold', passed=False, elapsed_seconds=.0625,
            returned_symbol_handles=1, returned_entities=0, returned_edges=0,
            artifact=dict(path='synthetic-response.json', sha256='d' * 64, bytes=17))
        queries = dict(passed=False, workloads=[dict(id='synthetic-query', passed=False, samples=[sample])],
            cursor_control=None, failure=dict(diagnostic, stage='queries'),
            retention_failure=dict(diagnostic, stage='query_report_retention'))
        phase = dict(label='synthetic-failed', status='failed', wall_seconds=.125,
                     source_accounting=accounting)
        wrapper = dict(status='failed', cases=[dict(id='serial-1', mode='serial', concurrency=1,
            status='failed', returncode=1, report=dict(phases=[phase], queries=queries))])
        with tempfile.TemporaryDirectory(prefix='persistent-compact-private-') as scratch:
            directory = Path(scratch)
            checks._adapter_dump(directory, 'private.json', wrapper)
            before = (directory / 'private.json').read_bytes()
            portable = performance.compact_persistent_result(wrapper)
            encoded = json.dumps(portable, sort_keys=True)
            for canary in (private_path, secret, 'Traceback (most recent call last)'):
                self.assertIn(canary, before.decode())
                self.assertNotIn(canary, encoded)
            self.assertEqual((directory / 'private.json').read_bytes(), before)
            self.assertEqual(json.loads(before), wrapper)
        self.assertEqual(portable['status'], 'failed')
        case = portable['cases'][0]
        self.assertEqual(case['status'], 'failed')
        self.assertEqual(case['phases'][0]['wall_seconds'], phase['wall_seconds'])
        public_accounting = case['phases'][0]['source_accounting']
        for field in ('passes', 'total_source_stream_bytes', 'total_hashed_bytes', 'total_hash_passes',
                      'worker_requests_with_accounting', 'accounting_complete', 'totals_kind'):
            self.assertEqual(public_accounting[field], accounting[field])
        projected = case['queries']['workloads'][0]['samples'][0]
        self.assertEqual(projected, {key: value for key, value in sample.items() if key != 'artifact'})
        self.assertFalse(case['queries']['passed'])
        for error, stage in ((public_accounting['failure'], 'batch_receipt_retention'),
                             (public_accounting['accounting_failure'], 'source_accounting'),
                             (case['queries']['failure'], 'queries'),
                             (case['queries']['retention_failure'], 'query_report_retention')):
            self.assertEqual(error['error_kind'], 'OSError')
            self.assertEqual(error['stage'], stage)
            self.assertEqual(error['errno'], errno.ENOSPC)
            self.assertTrue(set(error).isdisjoint(('error', 'message', 'reason', 'traceback')))

    def test_persistent_early_query_failure_retains_empty_progress_and_prior_phase(self):
        from repo_graph import analysis_queries as queries
        specs = tuple(dict(id='synthetic-missing-' + str(number), operation='symbol', name='missing' + str(number),
            path='main.py', span=(0, 10, 1, 1)) for number in range(6))
        meta = dict(generation='g' * 64, source_identity='s' * 64, repository_identity='r' * 64,
                    analyzer_identity='a' * 64, config_identity='c' * 64)
        for failure in ('missing_selector', 'metadata'):
            class Index:
                def metadata(self):
                    if failure == 'metadata': raise RuntimeError('synthetic metadata unavailable')
                    return dict(meta)
                def read_facts(self, kind):
                    if kind != 'definitions': raise AssertionError('Only selector facts required')
                    return iter(dict(id=spec['id'] + ':' + str(end), path=spec['path'], name=spec['name'],
                        range=dict(start_byte=0, end_byte=end, start_line=1, end_line=1))
                        for spec in specs for end in (11, 12, 13))
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(prefix='persistent-early-query-') as scratch:
                directory, progress = Path(scratch), {}
                error_kind, message = ((RuntimeError, 'synthetic metadata unavailable') if failure == 'metadata'
                    else (ValueError, 'Missing or changed frozen query selector'))
                with patch.object(queries, 'Queries') as session:
                    with self.assertRaisesRegex(error_kind, message):
                        performance._persistent_queries(Index(), directory, specs=specs, progress=progress)
                session.assert_not_called()
                self.assertFalse(progress['passed'])
                self.assertEqual(progress['workloads'], [])
                self.assertIsNone(progress['cursor_control'])
                self.assertEqual(progress['failure']['error_kind'], error_kind.__name__)
                self.assertEqual(progress['failure']['stage'], 'metadata' if failure == 'metadata' else 'selectors')
                mismatches = progress.get('selector_mismatches', [])
                self.assertLessEqual(len(mismatches), 6)
                if failure == 'missing_selector':
                    self.assertEqual(mismatches, [dict(id=spec['id'], expected_span=[0, 10, 1, 1],
                        matching_name_ranges=[[0, 11, 1, 1], [0, 12, 1, 1]], match_count=0) for spec in specs])
                else: self.assertEqual(mismatches, [])
                self.assertEqual(json.loads((directory / 'queries.json').read_bytes()), progress)
                self.assertEqual(sorted(path.name for path in directory.iterdir()), ['queries.json'])
                phase = dict(label='already-observed', status='ready', wall_seconds=.25,
                             collection_totals=dict(actual_workers_started=1, files_collected=1))
                wrapper = dict(status='failed', cases=[dict(id='serial-1', mode='serial', concurrency=1,
                    status='failed', returncode=1, report=dict(phases=[phase]))])
                prior_phases = performance.compact_persistent_result(wrapper)['cases'][0]['phases']
                for partial in (progress, {}, None):
                    wrapper['cases'][0]['report']['queries'] = partial
                    compact = performance.compact_persistent_result(wrapper)
                    self.assertEqual(compact['status'], 'failed')
                    self.assertEqual(compact['cases'][0]['phases'], prior_phases)
                    public = compact['cases'][0].get('queries')
                    if public is not None:
                        self.assertFalse(public.get('passed', False))
                        self.assertEqual(public.get('workloads', []), [])
                        self.assertIsNone(public.get('cursor_control'))

    def test_persistent_attempt_observes_actual_alias_streams_and_failed_observer(self):
        from repo_graph import analysis as runtime
        from tests.test_analysis import AVAILABLE
        if not AVAILABLE: self.skipTest('Optional analysis extra is not installed')
        class Sampler:
            error, refuse = None, False
            def __init__(self): self.events, self.created, self.phases = [], [], []
            def set_phase(self, phase): self.phases.append(phase)
            def register_process(self, process, role, created_ns): self.created.append((process.pid, role))
            def process_finished(self, process): pass
            def observe(self, event):
                self.events.append(event)
                if self.refuse:
                    self.error = {'kind': 'RuntimeError', 'reason': 'synthetic observer refusal'}
                    return False
                return True
        with tempfile.TemporaryDirectory(prefix='persistent-alias-test-') as scratch:
            source = Path(scratch) / 'source'; source.mkdir()
            raw = b'def target(): return 1\n'
            (source / 'main.py').write_bytes(raw)
            original = runtime.collect_files
            calls, proofs = [], []
            def collected(*args, **kwargs):
                calls.append((kwargs['mode'], kwargs['concurrency']))
                self.assertTrue(kwargs['telemetry'])
                self.assertTrue(callable(kwargs['observer']))
                self.assertEqual(kwargs['observer_max_events'], 10000)
                return original(*args, **kwargs)
            for mode, concurrency in performance.PERSISTENT_MODES:
                output, logs = Path(scratch) / mode, Path(scratch) / (mode + '-logs')
                logs.mkdir()
                index = runtime.StructuralIndex(source, output)
                sampler = Sampler()
                with patch.object(runtime, 'collect_files', side_effect=collected):
                    attempt = performance._persistent_attempt(index, ['main.py'], 'fresh', mode,
                        concurrency, logs, sampler)
                self.assertEqual(attempt['status'], 'ready', attempt)
                self.assertTrue(sampler.events)
                self.assertEqual(attempt['collection_batches'], 1)
                self.assertGreaterEqual(attempt['source_reads']['stream_bytes'], 2 * len(raw))
                self.assertFalse(attempt['source_reads']['all_owned_source_reads_measured'])
                proof = attempt['streamed_facts']; proofs.append(proof)
                artifact = proof['artifact']; data = (logs / artifact['path']).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), artifact['sha256'])
                self.assertEqual(len(data), artifact['bytes'])
                self.assertEqual(len(data.splitlines()), sum(proof['counts'].values()))
                for kind, count in proof['counts'].items():
                    self.assertEqual(count, sum(1 for _ in index.read_facts(kind)))
                if mode == 'queued':
                    self.assertTrue(any(role == 'worker' for _, role in sampler.created))
                    before = (output / 'search.db').read_bytes()
                    (source / 'main.py').write_bytes(b'def target(): return 2\n')
                    sampler.refuse = True
                    with patch.object(runtime, 'collect_files', side_effect=collected):
                        failed = performance._persistent_attempt(index, ['main.py'], 'failed', mode,
                            concurrency, logs, sampler)
                    self.assertEqual(failed['status'], 'measurement_failed', failed)
                    self.assertFalse(failed['receipt']['published'])
                    self.assertEqual((output / 'search.db').read_bytes(), before)
                    self.assertEqual(index.metadata()['generation'], proof['identities']['generation'])
                    self.assertTrue((logs / 'failed.json').exists())
            self.assertEqual(calls[:2], [('serial', 1), ('queued', 2)])
            self.assertEqual(proofs[0]['semantic_facts_sha256'], proofs[1]['semantic_facts_sha256'])
            self.assertEqual(proofs[0]['counts'], proofs[1]['counts'])

    def test_persistent_read_meter_counts_returned_streams_and_failed_partial_hashes(self):
        with tempfile.TemporaryDirectory(prefix='persistent-read-count-') as scratch:
            root, other, logs = (Path(scratch) / name for name in ('source', 'other', 'logs'))
            for path in (root, other, logs): path.mkdir()
            raw = b'PRIVATE_TEST_SOURCE' + b'x' * (65540 - len(b'PRIVATE_TEST_SOURCE'))
            (root / 'source.py').write_bytes(raw)
            (other / 'source.py').write_bytes(b'unmeasured other owner')
            with performance.SourceRoot(root) as source, performance.SourceRoot(other) as foreign:
                with performance._PersistentReadMeter(source.identity, 'read-test', logs) as meter:
                    self.assertEqual(source.read('source.py', 0)[0], b'')
                    self.assertEqual(source.read('source.py', 3, hash_full=False)[0], raw[:3])
                    self.assertEqual(source.read('source.py', 5)[0], raw[:5])
                    with self.assertRaises(InterruptedError):
                        source.read('source.py', 5, cancel=lambda: True)
                    cancellation_checks = []
                    def cancel_second_chunk():
                        cancellation_checks.append(True)
                        return len(cancellation_checks) == 2
                    with self.assertRaises(InterruptedError):
                        source.read('source.py', 3, cancel=cancel_second_chunk)
                    foreign.read('source.py', 100)
                measured = meter.summary()
            self.assertEqual(measured['operations'], 5)
            self.assertEqual((measured['successful_operations'], measured['failed_operations']), (3, 2))
            self.assertEqual(measured['open_operations'], 5)
            self.assertEqual(measured['stream_bytes'], 3 * len(raw) + 3 + 65536)
            self.assertEqual(measured['hashed_bytes'], 2 * len(raw) + 3 + 65536)
            self.assertEqual(measured['returned_prefix_bytes'], 8)
            self.assertEqual(measured['by_pass']['full_hash_zero_prefix']['stream_bytes'], len(raw))
            self.assertFalse(measured['all_owned_source_reads_measured'])
            self.assertEqual(measured['collector_mailbox_read_bytes']['knowledge'], 'unmeasured')
            rows = []
            for artifact in measured['artifacts']:
                data = (logs / artifact['path']).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), artifact['sha256'])
                self.assertEqual(len(data), artifact['bytes'])
                self.assertNotIn(b'PRIVATE_TEST_SOURCE', data)
                rows.extend(json.loads(line) for line in data.splitlines())
            failed = [row for row in rows if row['status'] == 'failed']
            self.assertEqual([(row['stream_bytes'], row['hashed_bytes'], row['returned_prefix_bytes'])
                              for row in failed], [(65536, 0, 0), (len(raw), 65536, 0)])

    def test_persistent_log_sync_failure_closes_owned_descriptors(self):
        with tempfile.TemporaryDirectory(prefix='persistent-log-sync-') as scratch:
            before = len(list(Path('/proc/self/fd').iterdir()))
            log = performance._PersistentLog(Path(scratch), 'read-test')
            log.append({'kind': 'synthetic'})
            with patch.object(performance.os, 'fsync', side_effect=OSError(errno.ENOSPC, 'synthetic full')):
                with self.assertRaises(OSError):
                    log.close()
            self.assertIsNone(log.fd)
            self.assertIsNone(log.owner.fd)
            self.assertEqual(len(list(Path('/proc/self/fd').iterdir())), before)

    def test_persistent_driver_rejects_nonzero_results_and_resets_shared_source(self):
        """Stubbed worker admission only; no collector, process or measurement run."""
        from evaluations import engine_checks as checks
        raw = b'def f(): pass\n'
        selector_failure = dict(error_kind='ValueError', error='Missing or changed frozen query selector',
            stage='selectors', traceback='synthetic selector diagnostic', diagnostic_truncated=False)
        for code in (0, 7, -9, 1):
            observed, cleaned, source_roots = [], [], []
            class Child:
                pid, returncode = 999999999, code
                def __init__(self, command, **kwargs):
                    marker = command.index('--persistent-worker')
                    job = Path('/proc/self/fd') / command[marker + 1]
                    source = Path('/proc/self/fd') / command[marker + 2]
                    with performance.SourceRoot(source) as owner:
                        content, sha, _ = owner.read('a.py', 128)
                        observed.append((owner.identity, sha, content))
                        source_roots.append(owner.root)
                        produced = dict(status='failed' if code == 1 else 'complete', stub_only=True,
                            source_owner_identity=owner.identity, source_owner_identity_after=owner.identity,
                            phases=[dict(label=label, streamed_facts=dict(
                                semantic_facts_sha256='a' * 64, stub_only=True))
                                for label in performance.PERSISTENT_PHASES])
                        if code == 1:
                            produced.pop('source_owner_identity_after')
                            produced.update(failure=selector_failure, queries=dict(passed=False, workloads=[],
                                cursor_control=None, failure=selector_failure))
                        with owner.atomic_writer('a.py') as stream:
                            stream.write(b'def changed(): pass\n')
                    checks._adapter_dump(job, 'result.json', produced)
                    os.write(kwargs['stdout'].fileno(), b'synthetic stdout\n')
                    os.write(kwargs['stderr'].fileno(), b'synthetic stderr\n')
                def wait(self, **kwargs): return self.returncode
            def cleanup(process):
                cleaned.append(process)
                return dict(leader_reaped=True, group_absent=True, stub_only=True)
            with self.subTest(exit_code=code), tempfile.TemporaryDirectory(prefix='persistent-admission-control-') as scratch:
                destination = Path(scratch)
                loaded, options = None, {}
                if code == 1:
                    original, protocol, destination = (destination / name for name in ('original', 'protocol', 'evidence'))
                    for path in (original, protocol, destination): path.mkdir()
                    with performance.SourceRoot(original) as original_owner, performance.SourceRoot(protocol) as inputs:
                        loaded = dict(config=dict(ceilings=dict(performance.REPRESENTATIVE_CEILINGS)),
                            original_owner=original_owner.identity, protocol_owner=inputs.identity, protocol_sha256='a' * 64)
                    options = dict(protocol=protocol, original_source=original)
                with patch.object(performance, '_dual_supervisor_limits', return_value={}), \
                        patch.object(performance, '_persistent_protocol', return_value=loaded), \
                        patch.object(performance, '_persistent_materialize', side_effect=lambda source, *args:
                            checks._adapter_materialize(source, {'a.py': raw})), \
                        patch.object(performance, '_persistent_capture', return_value={}), \
                        patch.object(performance, '_persistent_recheck', return_value={}), \
                        patch.object(performance, '_dual_inputs', return_value=({'a.py': raw}, [], {})), \
                        patch.object(performance, '_persistent_validate', side_effect=lambda value, *args: value), \
                        patch.object(performance.subprocess, 'Popen', Child), \
                        patch.object(checks, '_stop_and_reap', side_effect=cleanup):
                    result = performance.profile_persistent_fixture(PROFILE_ROOT, destination, **options)
                report = result['full_private_report']
                self.assertEqual(len(report['cases']), 2 if code == 0 else 1)
                self.assertEqual(len(cleaned), len(report['cases']))
                self.assertTrue(all(content == raw for _, _, content in observed))
                self.assertTrue(all(not path.exists() for path in source_roots))
                self.assertTrue(report['source_cleanup']['completed'])
                self.assertFalse(result['qualification_complete'])
                self.assertFalse(result['engine_selected'])
                if code == 0:
                    self.assertEqual(report['status'], 'complete')
                    self.assertEqual(observed[0], observed[1])
                    self.assertTrue(report['same_source_owner_across_modes'])
                    self.assertTrue(report['source_cleanup']['completed'])
                elif code == 1:
                    self.assertEqual(report['status'], 'failed')
                    self.assertEqual(report['cases'][0]['failure'], selector_failure)
                    self.assertEqual(report['failure'], selector_failure)
                    self.assertEqual(report['cases'][0]['source_owner_observation'], dict(
                        before_matches=True, after_knowledge='missing', after_matches=None))
                    self.assertEqual(report['cases'][0]['descendant_cleanup']['status'], 'unknown')
                    self.assertIsNone(report['cases'][0]['descendant_cleanup']['collector_sessions_reaped'])
                else:
                    self.assertEqual(report['status'], 'failed')
                    self.assertEqual(report['cases'][0]['failure']['error_kind'], 'ChildProcessError')
                archive = destination / result['archive']['directory']
                for case in report['cases']:
                    self.assertEqual(case['returncode'], code)
                    self.assertEqual(case['status'], 'complete' if code == 0 else 'failed')
                    self.assertEqual(case['report']['status'], 'failed' if code == 1 else 'complete')
                    self.assertTrue(case['report']['stub_only'])
                    self.assertTrue(case['cleanup']['leader_reaped'] and case['cleanup']['group_absent'])
                    stored = archive / case['report_artifact']['path']
                    self.assertEqual(json.loads(stored.read_bytes()), case['report'])
                    self.assertEqual(hashlib.sha256(stored.read_bytes()).hexdigest(), case['report_artifact']['sha256'])
                    self.assertEqual(len(case['logs']), 2)
                    for log in case['logs']:
                        data = (archive / log['path']).read_bytes()
                        self.assertTrue(log['complete'])
                        self.assertEqual(len(data), log['bytes'])
                        self.assertEqual(hashlib.sha256(data).hexdigest(), log['sha256'])

    def test_native_scan_output_swap_cannot_overwrite_source(self):
        from evaluations import tree_sitter_baseline as native
        with tempfile.TemporaryDirectory(prefix='repo-graph-profile-swap-') as scratch:
            root = Path(scratch)
            source, output = root / 'source', root / 'output'
            source.mkdir()
            output.mkdir()
            canary = source / 'native-facts.json'
            canary.write_text('PRIVATE_TEST_SOURCE')
            def swapped_scan(*args, **kwargs):
                output.rename(root / 'original-output')
                output.symlink_to(source, target_is_directory=True)
                return {'facts': {'definitions': [], 'sites': []}}
            captured = io.StringIO()
            with patch.object(native, 'scan', swapped_scan), redirect_stdout(captured):
                code = performance.structural_worker(['tree-sitter', str(source), str(output)])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(captured.getvalue())['error_kind'], 'ValueError')
            self.assertEqual(canary.read_text(), 'PRIVATE_TEST_SOURCE')

    def test_worker_rejects_graph_symlink_and_retains_truncation(self):
        with tempfile.TemporaryDirectory(prefix='repo-graph-profile-read-') as scratch:
            root = Path(scratch)
            source = root / 'source'
            source.mkdir()
            canary = source / 'private.json'
            canary.write_text('{"PRIVATE_TEST_SOURCE":true}')
            (source / 'a.py').write_text('PRIVATE_TEST_SOURCE' + ' ' * performance.builder.READ_LIMIT)
            graph = {'file_count': 1, 'files': ['a.py'], 'tree': [], 'dependencies': [],
                     'scope_edges': [], 'system': {}, 'search': {'documents': 1},
                     'scan': {'code_files': 1, 'failed': 0, 'truncated': 1, 'scanned': 1, 'reused': 0}}
            for symlink in (True, False):
                output = root / str(symlink)
                def fake_map(argv):
                    output.mkdir(exist_ok=True)
                    with performance.SourceRoot(source) as owner:
                        owner.read('a.py', performance.builder.READ_LIMIT)
                    if symlink:
                        (output / 'graph.json').symlink_to(canary)
                    else:
                        (output / 'graph.json').write_text(json.dumps(graph))
                    (output / 'scan-cache.json').write_text(json.dumps({'files': {'a.py': {'digest': 'a' * 64}}}))
                captured = io.StringIO()
                with patch.object(performance.builder, 'main', fake_map), redirect_stdout(captured):
                    code = performance.structural_worker(['current-map', str(source), str(output)])
                report = json.loads(captured.getvalue())
                with self.subTest(symlink=symlink):
                    self.assertEqual(code, 1 if symlink else 0)
                    if symlink:
                        self.assertEqual(report['error_kind'], 'OSError')
                        self.assertNotIn('PRIVATE_TEST_SOURCE', captured.getvalue())
                    else:
                        self.assertTrue(all(r['status'] == 'partial' and r['coverage']['truncated'] == 1
                                            for r in report['records']))
                        for record in report['records']:
                            receipt = record['coverage']['files'][0]
                            self.assertEqual(receipt['path'], 'a.py')
                            self.assertEqual(receipt['status'], 'truncated')
                            self.assertTrue(receipt['reads']['inventory']['prefix_truncated'])
                        self.assertNotIn('PRIVATE_TEST_SOURCE', captured.getvalue())
                self.assertEqual(canary.read_text(), '{"PRIVATE_TEST_SOURCE":true}')

    def test_atomic_report_preserves_source_canary_and_failed_workers(self):
        with tempfile.TemporaryDirectory(prefix='repo-graph-profile-boundary-') as scratch:
            root = Path(scratch)
            canary = root / 'source-canary'
            canary.write_text('PRIVATE_TEST_SOURCE')
            report = root / 'reports/profile.json'
            corpora = []
            for name in ('django', 'odoo', 'aws', 'kubernetes'):
                source = root / name
                source.mkdir()
                corpora.append({'id': name, 'source': str(source), 'revision': PINS[name]})
            config = root / 'sources.json'
            corpora.append({'id': 'sdk', 'source': str(root / 'unused-sdk'), 'revision': 'b' * 40})
            config.write_text(json.dumps({'corpora': corpora}))
            class FailedWorker:
                pid = 99999999
                returncode = 2
                def __init__(self, *args, **kwargs):
                    self_check.assertEqual(args[0][1:3], ['-I', '-B'])
                    self_check.assertEqual(set(kwargs['env']), {'HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME',
                        'XDG_DATA_HOME', 'TMPDIR', 'PATH', 'LANG', 'LC_ALL', 'TEMP', 'TMP'})
                    self_check.assertTrue(Path(kwargs['env']['HOME']).is_dir())
                    if not report.exists():
                        report.symlink_to(canary)
                def communicate(self, timeout=None):
                    return '{"status":"failed","error_kind":"fixture failure"}', ''
            def revision(argv, cwd, **kwargs):
                return 'a' * 40 + '\n'
            def checkout(source, revision):
                return {'status': 'verified', 'actual_revision': revision, 'clean': True}
            self_check = self
            with patch.object(performance.subprocess, 'Popen', FailedWorker), patch.object(
                    performance.subprocess, 'check_output', side_effect=revision), patch.object(
                    real_calls, 'checkout_identity', side_effect=checkout):
                records = performance.profile_structural(config, report, root / 'worker-logs')
            self.assertEqual(canary.read_text(), 'PRIVATE_TEST_SOURCE')
            self.assertFalse(report.is_symlink())
            self.assertEqual(len(records), 24)
            self.assertTrue(all(r['exit_code'] == 2 for r in records))
            data = json.loads(report.read_text())
            self.assertTrue(data['implementation']['sha256'])
            self.assertTrue(data['implementation']['native_backend'])
            self.assertEqual(set(data['corpus_revisions']), set(performance.LARGE_CORPORA))
            self.assertTrue(all(r['identity_verified'] for r in records))
            self.assertNotIn(str(root), report.read_text())

    def test_map_and_worker_identity_fail_closed(self):
        for change in ('duplicate', 'wrong-type', 'wrong-revision', 'dirty-before', 'dirty-after', 'revision-after',
                       'root-after', 'implementation-after'):
            with self.subTest(change=change), tempfile.TemporaryDirectory(prefix='repo-graph-profile-identity-') as scratch:
                root, started = Path(scratch), []
                corpora = []
                for name in performance.LARGE_CORPORA:
                    source = root / name
                    source.mkdir()
                    corpora.append({'id': name, 'source': str(source), 'revision': PINS[name]})
                if change == 'duplicate':
                    corpora.append(corpora[0])
                elif change == 'wrong-type':
                    corpora[0]['source'] = 42
                elif change == 'wrong-revision':
                    corpora[0]['revision'] = 'f' * 40
                config, report = root / 'sources.json', root / 'reports/profile.json'
                config.write_text(json.dumps({'corpora': corpora}))
                checked = {}
                def checkout(source, revision):
                    checked[source] = checked.get(source, 0) + 1
                    status = 'dirty_checkout' if (change == 'dirty-before' and checked[source] > 1) or (
                        started and change == 'dirty-after') else (
                        'revision_mismatch' if started and change == 'revision-after' else 'verified')
                    return {'status': status, 'actual_revision': 'f' * 40 if status == 'revision_mismatch' else revision,
                            'clean': status == 'verified'}
                class Worker:
                    pid, returncode = 99999999, 0
                    def __init__(self, *args, **kwargs):
                        started.append(True)
                    def communicate(self, timeout=None):
                        if change == 'root-after':
                            source = root / 'django'
                            source.rename(root / 'old-django')
                            source.mkdir()
                        return '{"records":[]}', ''
                original_read = performance.SourceRoot.read
                def read(owner, path, *args, **kwargs):
                    data, sha, info = original_read(owner, path, *args, **kwargs)
                    if started and change == 'implementation-after' and path == 'repo_graph/builder.py':
                        sha = '0' * 64
                    return data, sha, info
                with patch.object(real_calls, 'checkout_identity', side_effect=checkout), patch.object(
                        performance.subprocess, 'Popen', Worker), patch.object(
                        performance.subprocess, 'check_output', return_value='a' * 40 + '\n'), patch.object(
                        performance.SourceRoot, 'read', read):
                    with self.assertRaises(ValueError):
                        performance.profile_structural(config, report, root / 'logs')
                if change.endswith('after'):
                    self.assertEqual(len(started), 1)
                    data = json.loads(report.read_text())
                    self.assertEqual(data['status'], 'invalid_identity')
                    self.assertFalse(data['records'][0]['identity_verified'])
                    self.assertTrue((root / 'logs/django-current-map-0.stdout.log').exists())
                else:
                    self.assertFalse(started)

    def test_partial_file_receipts_keep_metadata_without_source_text(self):
        from evaluations import tree_sitter_baseline as native
        with tempfile.TemporaryDirectory(prefix='repo-graph-profile-receipts-') as scratch:
            root = Path(scratch)
            source, output = root / 'source', root / 'output'
            source.mkdir()
            (source / 'partial.py').write_text('PRIVATE_TEST_SOURCE')
            (source / 'unreadable.py').write_text('PRIVATE_TEST_SOURCE')
            (source / 'README.md').write_text('PRIVATE_TEST_SOURCE')
            result = {'facts': {'definitions': [{'text': 'PRIVATE_TEST_SOURCE'}], 'sites': []},
                'inventory': [{'path': 'partial.py', 'status': 'partial_parse', 'bytes': 19, 'sha256': 'a' * 64,
                               'parse_errors': [{'kind': 'missing', 'range': {'start_byte': 2, 'end_byte': 2}}]},
                              {'path': 'unreadable.py', 'status': 'source_error', 'error_kind': 'OSError', 'errno': 13}],
                'resources': {}, 'status': 'partial', 'stop_reason': None,
                'errors': [{'path': 'unreadable.py', 'kind': 'OSError'}]}
            captured = io.StringIO()
            with patch.object(native, 'scan', return_value=result), redirect_stdout(captured):
                code = performance.structural_worker(['tree-sitter', str(source), str(output)])
            self.assertEqual(code, 0)
            data = json.loads(captured.getvalue())
            for record in data['records']:
                coverage = record['coverage']
                files = {item['path']: item for item in coverage['files']}
                self.assertEqual(files['partial.py']['status'], 'partial_parse')
                self.assertTrue(files['partial.py']['parse_errors'])
                self.assertEqual(files['unreadable.py']['errno'], 13)
                self.assertEqual(files['README.md']['status'], 'excluded_non_source')
                self.assertEqual(coverage['errors'], result['errors'])
            self.assertNotIn('PRIVATE_TEST_SOURCE', captured.getvalue())
            self.assertNotIn(str(root), captured.getvalue())
            captured = io.StringIO()
            with redirect_stdout(captured):
                code = performance.structural_worker(['current-map', str(source), str(root / 'map-output')])
            self.assertEqual(code, 0)
            for record in json.loads(captured.getvalue())['records']:
                files = {item['path']: item for item in record['coverage']['files']}
                self.assertEqual(record['status'], 'complete')
                self.assertEqual(files['go.mod']['status'], 'absent_optional_configuration')
                self.assertTrue(files['partial.py']['reads'])
            self.assertNotIn('PRIVATE_TEST_SOURCE', captured.getvalue())

    def test_worker_records_actual_repeat_and_refuses_source_output(self):
        root = Path(__file__).resolve().parents[2]
        source = root / 'tests/fixtures/code-understanding'
        script = root / 'evaluations/performance.py'
        with tempfile.TemporaryDirectory(prefix='repo-graph-profile-check-') as scratch:
            run = subprocess.run([sys.executable, str(script), '--structural-worker',
                'current-map', str(source), str(Path(scratch) / 'output')],
                capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads(run.stdout)
            self.assertTrue(report['deterministic_repeat'])
            self.assertEqual(len(report['records']), 2)
            cold, warm = report['records']
            frozen = json.loads((root / 'evaluations/code-understanding/supplement-source.json').read_bytes())['files']
            self.assertEqual(cold['counts']['inventoried_files'], len(frozen))
            self.assertEqual(warm['coverage']['scanned'], 0)
            self.assertEqual(warm['coverage']['reused'], sum(item['kind'] == 'source' for item in frozen))
            self.assertGreater(cold['source_reads']['hashed_bytes'], 0)
            self.assertGreater(cold['peak_rss_bytes'], 0)
            self.assertEqual(cold['input_inventory_sha256'], warm['input_inventory_sha256'])
            rejected = subprocess.run([sys.executable, str(script), '--structural-worker',
                'current-map', str(source), str(source / 'FORBIDDEN-PROFILE-OUTPUT')],
                capture_output=True, text=True, timeout=20)
            self.assertEqual(rejected.returncode, 1)
            self.assertEqual(json.loads(rejected.stdout)['error_kind'], 'ValueError')
            self.assertFalse((source / 'FORBIDDEN-PROFILE-OUTPUT').exists())


PROFILE_ROOT = Path(__file__).resolve().parents[2]


def _fixture_stat(pid=101, start=456, pgid=101, sid=101, comm=b'x (y) z', state=b'S'):
    fields = [state, b'1', str(pgid).encode(), str(sid).encode()] + [b'0'] * 15 + [str(start).encode()] + [b'0'] * 5
    return str(pid).encode() + b' (' + comm + b') ' + b' '.join(fields) + b'\n'
_FIXTURE_CONTROLLER = {'pid': 101, 'starttime_ticks': 456, 'pgid': 101, 'sid': 101}
_FIXTURE_WORKER = {'pid': 202, 'starttime_ticks': 789, 'pgid': 202, 'sid': 202}

class _FakeProcOwner:
    current = {101: 1024, 202: 2048}
    instances = []

    def __init__(self, identity, *, separate_session=False):
        if set(identity) != set(_FIXTURE_CONTROLLER) or any((type(v) is not int or v <= 0 for v in identity.values())) or (separate_session and (not identity['pid'] == identity['pgid'] == identity['sid'])):
            raise ValueError('bad identity')
        self.identity = dict(identity)
        self.closed = False
        self.instances.append(self)

    def recheck(self):
        if self.current.get(self.identity['pid']) == 'stale':
            raise ValueError('changed identity')

    def rss(self):
        self.recheck()
        value = self.current[self.identity['pid']]
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        self.closed = True

def _fixture_event(kind='readiness', worker=None, role=None, cleanup=None, index=None):
    return {'schema_version': 1, 'event': kind, 'monotonic_ns': 10, 'mode': 'queued', 'role': role or ('worker' if worker else 'controller'), 'configured_concurrency': 4, 'workers_started': 1 if worker else 0, 'live_workers': 1 if worker else 0, 'pending_requests': 0, 'inflight_reserved_bytes': 0, 'mailbox_source_bytes': 0, 'mailbox_request_bytes': 0, 'mailbox_result_bytes': 0, 'admitted_bytes': 0, 'controller': dict(_FIXTURE_CONTROLLER), 'worker': worker, 'index': index, 'cleanup': cleanup}

@unittest.skipUnless(sys.platform == 'linux', 'Owned RSS profiling requires Linux')
class OwnedDualProfile(unittest.TestCase):

    def setUp(self):
        _FakeProcOwner.instances = []
        _FakeProcOwner.current = {101: 1024, 202: 2048}

    def sampler(self):
        with patch.object(performance, '_dual_self_identity', return_value=dict(_FIXTURE_CONTROLLER)), patch.object(performance, '_DualProcOwner', _FakeProcOwner):
            return performance._DualSampler()

    def test_persistent_rotation_preserves_owned_exit_gaps_and_window_limit(self):
        class ProcOwner(_FakeProcOwner):
            def __init__(self, identity, *, separate_session=False, allow_exited=False):
                super().__init__(identity, separate_session=separate_session)
            def recheck(self, *, require_live=False):
                super().recheck()
                if require_live and isinstance(self.current[self.identity['pid']], OSError):
                    raise ValueError('Synthetic owner is not live')
        class Child:
            pid, returncode = 202, None
        with tempfile.TemporaryDirectory(prefix='persistent-owner-window-') as scratch, \
                patch.object(performance, '_dual_self_identity', return_value=dict(_FIXTURE_CONTROLLER)), \
                patch.object(performance, '_DualProcOwner', ProcOwner), \
                patch.object(performance, 'PERSISTENT_MAX_WINDOWS', 3), \
                patch.object(performance, 'DUAL_MAX_SAMPLES', 2):
            sampler = performance._PersistentSampler().attach_log(Path(scratch))
            child = Child()
            _FakeProcOwner.current[202] = ProcessLookupError(errno.ESRCH, 'synthetic owned child exited')
            raw = _fixture_stat(pid=202, start=789, pgid=202, sid=202, state=b'Z')
            with patch.object(performance.os, 'open', return_value=12345), \
                    patch.object(performance.os, 'read', return_value=raw), \
                    patch.object(performance.os, 'close'), patch.object(performance.os, 'getpid', return_value=999):
                with self.assertRaises(ValueError):
                    sampler.register_process(child, 'git_revision', 1)
            self.assertEqual(sampler.created_children, 0)
            with patch.object(performance.os, 'open', return_value=12345), \
                    patch.object(performance.os, 'read', return_value=raw), \
                    patch.object(performance.os, 'close'), patch.object(performance.os, 'getpid', return_value=1):
                sampler.register_process(child, 'git_revision', 1)
            sampler.sample()  # Reaching the sample bound rotates with the live child still held.
            self.assertTrue(any(row.get('continuation') and row['identity'] == _FIXTURE_WORKER
                                for row in sampler.lifecycles))
            child.returncode = 0
            with patch('repo_graph.analysis_queue._group_exists', return_value=False):
                sampler.process_finished(child)
            sampler.sample()
            with sampler.lock, self.assertRaises(ValueError):
                sampler._rotate()
            result = sampler.finish()
            self.assertEqual(result['sample_gap_count'], 2)
            self.assertEqual(result['complete_sample_count'], 1)
            self.assertEqual(result['peak_sampled_owned_rss_bytes'], 1024)
            self.assertEqual(result['created_child_count'], 1)
            self.assertEqual(result['remaining_registered_owned_child_owners'], [])
            self.assertIsNone(result['error'])
            self.assertFalse(result['unsampled_peak_bound'])
            self.assertEqual(len(result['windows']), 3)
            for window in result['windows']:
                self.assertLess(window['samples'], result['per_window_limits']['samples'])
                data = (Path(scratch) / window['path']).read_bytes()
                self.assertEqual(len(data), window['bytes'])
                self.assertEqual(hashlib.sha256(data).hexdigest(), window['sha256'])
            self.assertTrue(all(owner.closed for owner in _FakeProcOwner.instances))

    def observe(self, sampler, value):
        with patch.object(performance, '_DualProcOwner', _FakeProcOwner):
            return sampler.observe(value)

    def test_proc_stat_keeps_starttime_and_parentheses(self):
        self.assertEqual(performance._dual_proc_stat(_fixture_stat()), _FIXTURE_CONTROLLER)
        for raw in (_fixture_stat(state=b'Z'), _fixture_stat(state=b'bad'), _fixture_stat(start=0), _fixture_stat(pgid=-1), b'1 (x) S', _fixture_stat() + b'x', b'x' * 4097):
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                performance._dual_proc_stat(raw)

    def test_rss_is_current_kib_not_peak_pss(self):
        self.assertEqual(performance._dual_proc_rss(b'Rss: 7 kB\nPss: 2 kB\nPrivate_Clean: 1 kB\n'), 7168)
        for raw in (b'Rss: 1 kB\nRss: 2 kB\n', b'Pss: 7 kB\n', b'Rss: -1 kB\n', b'Rss: 1 MB\n', b'Rss: 1e999 kB\n', b'Rss: 99999999999999999999 kB\n'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                performance._dual_proc_rss(raw)

    def test_small_actual_own_pid_and_descriptor_close(self):
        before = len(list(Path('/proc/self/fd').iterdir()))
        identity = performance._dual_self_identity()
        self.assertEqual(identity['pid'], os.getpid())
        owner = performance._DualProcOwner(identity)
        try:
            self.assertGreater(owner.rss(), 0)
            with self.assertRaises(ValueError):
                owner.read('stat', 1)
            with patch.object(owner, 'read', return_value=_fixture_stat(pid=identity['pid'], start=identity['starttime_ticks'] + 1, pgid=identity['pgid'], sid=identity['sid'])):
                with self.assertRaises(ValueError):
                    owner.recheck()
        finally:
            owner.close()
        self.assertIsNone(owner.fd)
        self.assertEqual(len(list(Path('/proc/self/fd').iterdir())), before)

    def test_missing_read_is_gap_never_zero_sum(self):
        s = self.sampler()
        self.assertTrue(self.observe(s, _fixture_event(worker=dict(_FIXTURE_WORKER))))
        _FakeProcOwner.current[202] = OSError(errno.ENOENT, 'gone')
        s.sample()
        self.assertIsNone(s.samples[-1]['owned_rss_bytes'])
        self.assertFalse(s.samples[-1]['complete'])
        self.assertEqual(s.samples[-1]['gaps'][0]['errno'], errno.ENOENT)
        r = s.finish()
        self.assertIsNone(r['peak_sampled_owned_rss_bytes'])
        self.assertIsNotNone(r['error'])

    def test_identity_change_and_unowned_worker_refuse(self):
        s = self.sampler()
        self.assertFalse(self.observe(s, _fixture_event('submit', dict(_FIXTURE_WORKER), index=0)))
        self.assertIsNotNone(s.error)
        s.finish()
        s = self.sampler()
        self.assertTrue(self.observe(s, _fixture_event(worker=dict(_FIXTURE_WORKER))))
        _FakeProcOwner.current[202] = 'stale'
        self.assertFalse(self.observe(s, _fixture_event('receive', dict(_FIXTURE_WORKER), index=0)))
        self.assertIsNotNone(s.error)
        s.finish()

    def test_registration_log_is_durable_before_callback_returns(self):
        s = self.sampler()
        with tempfile.TemporaryDirectory(prefix='mock-log-') as temporary:
            s.attach_log(Path(temporary))
            self.assertTrue(self.observe(s, _fixture_event(worker=dict(_FIXTURE_WORKER))))
            rows = [json.loads(line) for line in (Path(temporary) / 'owned-telemetry.jsonl').read_bytes().splitlines()]
            self.assertEqual(rows[-1]['kind'], 'event')
            self.assertEqual(rows[-1]['value']['worker'], _FIXTURE_WORKER)
            self.assertEqual(rows[-2]['kind'], 'lifecycle')
            self.assertEqual(rows[-2]['value']['identity'], _FIXTURE_WORKER)
            s.sample()
            s.finish()
            self.assertIsNone(s.log_fd)
            self.assertIsNone(s.log_owner)

    def test_log_setup_and_write_failure_close_owned_handles(self):
        s = self.sampler()
        with tempfile.TemporaryDirectory(prefix='mock-log-failure-') as temporary:
            target = Path(temporary) / 'owned-telemetry.jsonl'
            target.write_bytes(b'canary')
            with self.assertRaises(FileExistsError):
                s.attach_log(Path(temporary))
            self.assertEqual(target.read_bytes(), b'canary')
            self.assertIsNone(s.log_fd)
            self.assertIsNone(s.log_owner)
        s.finish()
        s = self.sampler()
        with tempfile.TemporaryDirectory(prefix='mock-write-failure-') as temporary:
            s.attach_log(Path(temporary))
            with patch.object(performance.os, 'write', side_effect=OSError(errno.ENOSPC, 'mock full')):
                self.assertFalse(self.observe(s, _fixture_event(worker=dict(_FIXTURE_WORKER))))
            self.assertIsNotNone(s.error)
            s.finish()
            self.assertIsNone(s.log_fd)
            self.assertIsNone(s.log_owner)
            self.assertTrue(all((o.closed for o in _FakeProcOwner.instances)))

    def test_source_reset_and_independent_clean_candidates_with_failure_retention(self):
        from evaluations import incremental_candidate as incremental, engine_checks as checks
        raw = b'call(1)'
        path = 'a.py'
        blobs = {path: raw}
        metadata = [{'path': path, 'language': 'python', 'kind': 'source', 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}]
        edits = {}
        for name, new in (('U-PY-BODY', 'body(1)'), ('U-PY-EXPORT', 'export(1)')):
            edits[name] = {'id': name, 'language': 'python', 'operations': [{'op': 'replace', 'path': path, 'old': 'call(1)', 'new': new, 'occurrences': 1, 'sha256_before': hashlib.sha256(raw).hexdigest(), 'sha256_after': hashlib.sha256(new.encode()).hexdigest()}]}
        calls = []
        created = []

        class MockCandidate:
            failure_at = None

            def __init__(self, source, budget):
                self.root = source
                self.last_attempt = None
                self.snapshot = None
                with performance.SourceRoot(source) as owner:
                    self.owner = owner.identity
                self.number = len(created)
                created.append(self)

            def refresh(self, records, **kwargs):
                with performance.SourceRoot(self.root) as source:
                    data, _, _ = source.read(path, 100, hash_full=False)
                calls.append((self.number, data, kwargs['mode'], kwargs['concurrency']))
                receipt = {'status': 'complete', 'generation': hashlib.sha256(data).hexdigest(), 'source_identity': 'b' * 64, 'semantic_facts_sha256': hashlib.sha256(data).hexdigest()}
                if self.failure_at == len(calls):
                    receipt = {'status': 'failed', 'reason': 'mock late refresh failure'}
                self.last_attempt = receipt
                return receipt

        class MockSampler:

            def __init__(self, supervisor):
                self.error = None

            def attach_log(self, directory):
                self.directory = directory
                with performance.SourceRoot(directory) as owner:
                    with owner.atomic_writer('owned-telemetry.jsonl') as f:
                        f.write(b'{"kind":"mock_no_workers"}\n')
                return self

            def start(self):
                return self

            def set_phase(self, label):
                pass

            def observe(self, event):
                raise AssertionError('no actual queue')

            def finish(self):
                return {'error': None, 'remaining_registered_worker_owners': [], 'samples': [], 'queue_events': [], 'mock_only': True}
        bound = {'mock_binding': 'synthetic only'}

        def run(failure_at=None):
            MockCandidate.failure_at = failure_at
            calls.clear()
            created.clear()
            with tempfile.TemporaryDirectory(prefix='mock-phase-') as temporary:
                with patch.object(performance, '_dual_inputs', return_value=(blobs, metadata, edits)), patch.object(performance, '_dual_recheck', return_value=bound), patch.object(performance, '_DualSampler', MockSampler), patch.object(performance, '_dual_isolation', return_value={'mock_only': True}), patch.object(checks, '_adapter_snapshot_artifact', return_value={'path': 'mock.facts.json', 'sha256': 'a' * 64, 'bytes': 0}), patch.object(incremental, 'Candidate', MockCandidate):
                    report = performance._dual_run(PROFILE_ROOT, Path(temporary), bound, 'queued', 4, 0, None)
                disk = json.loads((Path(temporary) / 'result.json').read_bytes())
                self.assertEqual(disk, report)
                self.assertFalse(report['engine_selected'])
                return (report, list(calls))
        report, seen = run()
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(len(seen), 8)
        self.assertEqual([c[1] for c in seen], [raw, raw, raw, b'body(1)', b'body(1)', raw, b'export(1)', b'export(1)'])
        self.assertNotEqual(seen[3][0], seen[4][0])
        self.assertNotEqual(seen[6][0], seen[7][0])
        self.assertTrue(all((c[2:] == ('queued', 4) for c in seen)))
        failed, seen = run(6)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(len(failed['phases']), 6)
        self.assertEqual(failed['phases'][-1]['receipt']['reason'], 'mock late refresh failure')
        self.assertTrue(all((p['status'] == 'complete' for p in failed['phases'][:-1])))

    def test_worker_result_rejects_vacuous_and_typed_false_success(self):
        bound = {key: {} for key in ('implementation', 'input_binding', 'backend', 'queue_identity', 'runtime')}
        bound.update(measured_commit='a' * 40, root_identity='b' * 64)
        report = {'schema_version': 1, 'kind': 'native_dual_fixture', 'status': 'failed', 'mode': 'serial', 'concurrency': 1, 'repeat': 0, 'binding_before': bound, 'phases': [], 'engine_selected': False, 'qualification_complete': False}
        self.assertIs(performance._dual_validate_result(report, bound, 'serial', 1, 0), report)
        for bad in (dict(report, status='complete'), dict(report, concurrency=True), dict(report, phases=[None]), dict(report, engine_selected=True)):
            with self.assertRaises(ValueError):
                performance._dual_validate_result(bad, bound, 'serial', 1, 0)

    def test_private_environment_bridge_and_identity_failure_stop_admission(self):
        from evaluations import engine_checks as checks
        bound = {'mock_binding': 'no extraction'}
        seen = []
        checks_count = [0]

        def recheck(*args):
            checks_count[0] += 1
            if checks_count[0] == 2:
                raise ValueError('mock helper drift after admission failure')
            return bound

        def no_process(command, **kwargs):
            seen.append(command)
            bridge = Path(kwargs['cwd'])
            self.assertEqual(bridge.parent, Path('/proc') / str(os.getpid()) / 'fd')
            fd = kwargs['pass_fds'][0]
            self.assertEqual(bridge.name, str(fd))
            for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME', 'TMPDIR'):
                self.assertEqual(Path(kwargs['env'][key]).parent, bridge)
                self.assertEqual(Path(kwargs['env'][key]).resolve().parent, bridge.resolve())
            self.assertEqual(command[1:3], ['-I', '-B'])
            self.assertTrue(kwargs['start_new_session'])
            raise OSError(errno.EIO, 'mock Popen admission refusal; no process exists')
        with tempfile.TemporaryDirectory(prefix='mock-environment-') as temporary:
            with patch.object(performance, '_dual_supervisor_limits', return_value={'mock_only': True}), patch.object(checks, '_adapter_root', side_effect=lambda root: Path(root)), patch.object(performance, '_dual_capture', return_value=bound), patch.object(performance, '_dual_recheck', side_effect=recheck), patch.object(performance.subprocess, 'Popen', side_effect=no_process):
                result = performance.profile_native_dual(PROFILE_ROOT, Path(temporary))
            self.assertEqual(len(seen), 1)
            self.assertEqual(result['status'], 'invalid_identity')
            case = result['full_private_report']['cases'][0]
            self.assertEqual(case['status'], 'invalid_identity')
            self.assertFalse(case['identity_verified'])
            self.assertIsNone(case['cleanup'])
            self.assertEqual(case['failure']['error_kind'], 'OSError')
            self.assertTrue(case['logs'])

    def test_blocked_sampler_failure_does_not_wait_on_its_lock(self):
        s = self.sampler()

        class MockBlocked:
            ident = 1

            def join(self, timeout):
                self.timeout = timeout

            def is_alive(self):
                return True
        s.thread = MockBlocked()

        class ForbiddenLock:

            def __enter__(self):
                raise AssertionError('must not block on sampler lock')
        s.lock = ForbiddenLock()
        receipt = s.finish()
        self.assertFalse(receipt['sampler_stopped'])
        self.assertIsNotNone(receipt['error'])
        self.assertIsNone(receipt['peak_sampled_owned_rss_bytes'])
        self.assertEqual(s.thread.timeout, 2)
        for held in s.owners.values():
            held.close()

    def test_locked_manifest_source_edits_admitted_without_gold(self):
        from evaluations import engine_checks as checks
        from evaluations.supplement_preparation import SOURCE
        raw = (PROFILE_ROOT / SOURCE).read_bytes()
        bound = {'input_binding': {SOURCE: hashlib.sha256(raw).hexdigest()}}
        blobs, rows, edits = performance._dual_inputs(PROFILE_ROOT, bound)
        self.assertEqual(len(rows), 24)
        self.assertEqual(sum(map(len, blobs.values())), 11463)
        self.assertEqual(sum((row['kind'] == 'source' for row in rows)), 16)
        self.assertEqual(sum((row['kind'] == 'configuration' for row in rows)), 8)
        self.assertEqual(set(edits), {'U-PY-BODY', 'U-PY-EXPORT'})
        for update in edits.values():
            self.assertEqual(set(update), {'id', 'language', 'operations'})
            changed, after = performance._dual_mutation(blobs, rows, update)
            self.assertEqual(sum((blobs[path] != changed[path] for path in blobs)), 1)
            self.assertEqual(sum((a['sha256'] != b['sha256'] for a, b in zip(rows, after))), 1)
        self.assertNotIn('expected', json.dumps(rows))

    def test_supervisor_interactive_refusal_precedes_limit_changes(self):
        import resource
        for identity, isolated, user_site, bytecode, reason in (
                (dict(_FIXTURE_CONTROLLER, sid=999), 1, 1, True, 'Interactive caller refused'),
                (_FIXTURE_CONTROLLER, 0, 1, True, 'isolated dedicated profiler'),
                (_FIXTURE_CONTROLLER, 1, 0, True, 'isolated dedicated profiler'),
                (_FIXTURE_CONTROLLER, 1, 1, False, 'isolated dedicated profiler')):
            with self.subTest(identity=identity, isolated=isolated, user_site=user_site, bytecode=bytecode), \
                    patch.object(performance, '_dual_self_identity', return_value=identity), \
                    patch.object(performance.sys, 'flags', SimpleNamespace(isolated=isolated, no_user_site=user_site)), \
                    patch.object(performance.sys, 'dont_write_bytecode', bytecode), \
                    patch.object(resource, 'setrlimit', side_effect=AssertionError('refused caller limits untouched')), \
                    patch.object(performance.os, 'sched_setaffinity', side_effect=AssertionError('refused caller affinity untouched')), \
                    patch.object(performance.signal, 'signal', side_effect=AssertionError('refused caller signal untouched')):
                with self.assertRaisesRegex(ValueError, reason):
                    performance._dual_supervisor_limits()

    def test_supervisor_finite_soft_hard_and_affinity_are_owned_mock_only(self):
        import resource
        calls = []
        affinity = []
        with patch.object(performance.sys, 'flags', SimpleNamespace(isolated=1, no_user_site=1)), patch.object(performance.sys, 'dont_write_bytecode', True), patch.object(performance, '_dual_self_identity', return_value=_FIXTURE_CONTROLLER), patch.object(performance.os, 'sched_getaffinity', return_value=set(range(8))), patch.object(performance.os, 'sched_setaffinity', side_effect=lambda pid, cpus: affinity.append((pid, cpus))), patch.object(performance.signal, 'signal'), patch.object(performance.signal, 'getsignal', return_value=performance.signal.SIG_DFL), patch.object(resource, 'getrlimit', return_value=(resource.RLIM_INFINITY, resource.RLIM_INFINITY)), patch.object(resource, 'setrlimit', side_effect=lambda kind, value: calls.append((kind, value))):
            result = performance._dual_supervisor_limits()
        self.assertEqual(result['address_space_soft_bytes'], 256 * 1024 * 1024)
        self.assertEqual(result['address_space_hard_bytes'], 512 * 1024 * 1024)
        self.assertEqual(result['cpu_soft_seconds'], 10)
        self.assertEqual(result['cpu_hard_seconds'], 60)
        self.assertEqual(affinity, [(0, {0, 1, 2, 3})])
        self.assertEqual(result['affinity'], [0, 1, 2, 3])
        self.assertIn((resource.RLIMIT_CORE, (0, 0)), calls)
        with patch.object(performance.sys, 'flags', SimpleNamespace(isolated=1, no_user_site=1)), patch.object(performance.sys, 'dont_write_bytecode', True), patch.object(performance, '_dual_self_identity', return_value=_FIXTURE_CONTROLLER), patch.object(performance.os, 'sched_getaffinity', return_value={0}), patch.object(resource, 'setrlimit', side_effect=AssertionError('invalid affinity refused before mutation')):
            with self.assertRaisesRegex(ValueError, 'distinct allowed CPUs'):
                performance._dual_supervisor_limits([0, 0])

    def test_known_exiting_owner_is_gap_but_readiness_and_foreign_identity_refuse(self):
        owner = performance._DualProcOwner.__new__(performance._DualProcOwner)
        owner.identity = dict(_FIXTURE_WORKER)
        owner.fd = None
        for state in (b'Z', b'X', b'x'):
            with patch.object(owner, 'read', return_value=_fixture_stat(pid=202, start=789, pgid=202, sid=202, state=state)):
                with self.assertRaises(ProcessLookupError) as failure:
                    owner.rss()
                self.assertEqual(failure.exception.errno, errno.ESRCH)
                with self.assertRaises(ValueError):
                    owner.recheck(require_live=True)
            with patch.object(owner, 'read', return_value=_fixture_stat(pid=202, start=790, pgid=202, sid=202, state=state)):
                with self.assertRaises(ValueError):
                    owner.rss()
        s = self.sampler()
        self.assertTrue(self.observe(s, _fixture_event(worker=dict(_FIXTURE_WORKER))))
        _FakeProcOwner.current[202] = ProcessLookupError(errno.ESRCH, 'known exiting')
        s.sample()
        self.assertIsNone(s.samples[-1]['owned_rss_bytes'])
        self.assertIsNone(s.error)
        self.assertEqual(s.samples[-1]['gaps'][0]['errno'], errno.ESRCH)
        proof = {'leader_reaped': True, 'group_absent': True, 'mailboxes_removed': True}
        self.assertTrue(self.observe(s, _fixture_event('cleanup', dict(_FIXTURE_WORKER), cleanup=proof)))
        s.sample()
        r = s.finish()
        self.assertEqual(r['peak_sampled_owned_rss_bytes'], 1024)
        self.assertEqual(r['sample_gap_count'], 1)
        self.assertIsNone(r['error'])

    def test_registry_times_use_observation_instead_of_waiting_producer_clock(self):
        with patch.object(performance.time, 'monotonic_ns', return_value=100):
            sampler = self.sampler()
        # Emitted readiness can wait behind a sample which still has no worker.
        readiness = dict(_fixture_event(worker=dict(_FIXTURE_WORKER)), monotonic_ns=150)
        with patch.object(performance.time, 'monotonic_ns', return_value=200):
            sampler.sample()
        with patch.object(performance.time, 'monotonic_ns', return_value=300):
            self.assertTrue(self.observe(sampler, readiness))
        with patch.object(performance.time, 'monotonic_ns', return_value=400):
            sampler.sample()
        # Cleanup can likewise wait behind the last sample containing the owner.
        cleanup = dict(_fixture_event('cleanup', dict(_FIXTURE_WORKER), cleanup={
            'leader_reaped': True, 'group_absent': True, 'mailboxes_removed': True}), monotonic_ns=450)
        with patch.object(performance.time, 'monotonic_ns', return_value=500):
            sampler.sample()
        with patch.object(performance.time, 'monotonic_ns', return_value=600):
            self.assertTrue(self.observe(sampler, cleanup))
        with patch.object(performance.time, 'monotonic_ns', return_value=700):
            sampler.sample()
        with patch.object(performance.time, 'monotonic_ns', return_value=800):
            report = sampler.finish()
        life = next(row for row in report['lifecycles'] if row['role'] == 'worker')
        self.assertEqual((life['registered_ns'], life['removed_ns']), (300, 600))
        self.assertEqual([row['monotonic_ns'] for row in report['queue_events']], [150, 450])
        for sample in report['samples']:
            expected = life['registered_ns'] <= sample['started_ns'] < life['removed_ns']
            self.assertEqual(_FIXTURE_WORKER['pid'] in {row['pid'] for row in sample['owners']}, expected)


if __name__ == '__main__':
    unittest.main()
