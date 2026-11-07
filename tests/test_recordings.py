"""velcro recordings: the ledger it reads, the order of steps, the retry/alert schedule, the hook contract.

  python3 -m unittest discover -s tests
"""
import importlib.machinery
import sys
sys.dont_write_bytecode = True  # no __pycache__ next to velcro-recordings
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def load_helper():
    loader = importlib.machinery.SourceFileLoader('velcro_recordings', str(ROOT / 'velcro-recordings'))
    spec = importlib.util.spec_from_loader('velcro_recordings', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


vr = load_helper()


def script(path, body):
    path.write_text('#!/bin/sh\n' + body)
    path.chmod(0o755)
    return str(path)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='velcro-rec-test-')).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir, self.out, self.state, self.bin = (self.tmp / n for n in ('rec', 'out', 'state', 'bin'))
        for d in (self.dir, self.bin):
            d.mkdir()
        self.engine = script(self.bin / 'engine', 'echo "heard: $(basename "$1")"\n')

    def add(self, name, data=b'RIFFaudio', sha=None, imported_at='2026-10-21T10:00:00+09:00', **extra):
        (self.dir / name).write_bytes(data)
        rec = {'file': name, 'size': len(data), 'sha256': sha or hashlib.sha256(data).hexdigest(),
               'imported_at': imported_at, 'duration_s': 60.0, **extra}
        with open(self.dir / '.velcro-import.jsonl', 'a') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        return rec

    def run_helper(self, *extra, command='run'):
        argv = [command, '--dir', str(self.dir), '--out', str(self.out), '--state', str(self.state),
                '--engine', self.engine, *extra]
        buf = io.StringIO()
        with mock.patch('sys.stdout', buf):
            rc = vr.main(argv)
        return rc, buf.getvalue()

    def state_doc(self):
        return json.loads(next(self.state.glob('recordings-*.json')).read_text())

    def share_mount(self, path):
        """Model a separate share mount without requiring mount privileges."""
        original = Path.stat

        def mounted_stat(p, *args, **kwargs):
            st = original(p, *args, **kwargs)
            if p == path or path in p.parents:
                fields = list(st)
                fields[2] += 1  # st_dev
                return os.stat_result(fields)
            return st

        return mock.patch.object(Path, 'stat', mounted_stat)


class Steps(Base):
    def test_transcribe_tidy_deliver_in_order_with_hook_variables(self):
        self.add('20261020181749.WAV')
        tidy = script(self.bin / 'tidy', 'echo "tidied $(basename "$VELCRO_TRANSCRIPT")"; cat "$VELCRO_TRANSCRIPT"\n'
                                         'echo "$VELCRO_RECORD" > "$VELCRO_OUT/record.seen"\n')
        deliver = script(self.bin / 'deliver', 'echo "delivered from $(basename "$VELCRO_TIDIED")"\n')
        rc, _ = self.run_helper('--tidy', tidy, '--deliver', deliver)
        self.assertEqual(rc, 0)
        self.assertEqual((self.out / '20261020181749.txt').read_text(), 'heard: 20261020181749.WAV\n')
        doc = json.loads((self.out / '20261020181749.json').read_text())
        self.assertEqual(doc['engine'], 'command')
        self.assertEqual(doc['file'], '20261020181749.WAV')
        self.assertIn('tidied 20261020181749.txt', (self.out / '20261020181749.tidy.txt').read_text())
        self.assertEqual((self.out / '20261020181749.deliver.txt').read_text(), 'delivered from 20261020181749.tidy.txt\n')
        self.assertEqual(json.loads((self.out / 'record.seen').read_text())['file'], '20261020181749.WAV')
        self.assertEqual(set(self.state_doc()['done']['20261020181749.WAV']), {'transcribe', 'tidy', 'deliver'})
        # Everything done: a second run does nothing and the recording folder is untouched.
        before = sorted(p.name for p in self.dir.iterdir())
        _, out = self.run_helper('--tidy', tidy, '--deliver', deliver)
        self.assertIn('nothing new', out)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), before)

    def test_placeholders_and_engine_json(self):
        self.add('a.WAV')
        self.engine = script(self.bin / 'engine2', 'echo "{\\"chunks\\": 3}" > "$4"; echo "text of $2"\n')
        self.engine += ' --file {file} --json {json}'
        self.run_helper()
        text = (self.out / 'a.txt').read_text()
        self.assertTrue(text.startswith('text of ') and text.endswith('/a.WAV\n'), text)
        self.assertNotIn(str(self.dir), text)  # the engine gets a private copy, never the shared file
        self.assertEqual(json.loads((self.out / 'a.json').read_text())['detail'], {'chunks': 3})
        self.assertEqual([p.name for p in self.out.glob('.*')], [])  # the engine's JSON is not left behind

    def test_claude_and_codex_engines_run_velcro_transcribe_json(self):
        for engine in ('claude', 'codex'):
            name = f'memo-{engine}.m4a'
            self.add(name)
            fake_velcro = script(self.bin / 'velcro', 'echo "$*" > "$(dirname "$0")/args"; '
                                                       f'echo \'{{"engine":"{engine}","file":"x","text":"안녕하세요"}}\'\n')
            argv = ['run', '--dir', str(self.dir), '--out', str(self.out), '--state', str(self.state),
                    '--engine', engine, '--velcro', fake_velcro, '--lang', 'ko']
            with mock.patch('sys.stdout', io.StringIO()):
                vr.main(argv)
            args = (self.bin / 'args').read_text().strip()
            self.assertTrue(args.startswith(f'transcribe --json --engine {engine} --lang ko {self.state}/'), args)
            self.assertTrue(args.endswith(f'/{name}'), args)
            stem = name[:-4]
            self.assertEqual((self.out / f'{stem}.txt').read_text(), '안녕하세요\n')
            self.assertEqual(json.loads((self.out / f'{stem}.json').read_text())['engine'], engine)

    def test_since_and_missing_source(self):
        self.add('old.WAV', imported_at='2026-10-20T09:00:00+09:00')
        self.add('new.WAV')
        self.add('gone.WAV')
        (self.dir / 'gone.WAV').unlink()
        _, out = self.run_helper('--since', '2026-10-21', command='status')
        self.assertNotIn('old.WAV', out)
        self.assertIn('new.WAV', out)
        self.assertIn('source gone', out)

    def test_decomposed_hangul_name_finds_the_composed_file(self):
        import unicodedata
        nfd = unicodedata.normalize('NFD', '자양동.m4a')
        rec = self.add('자양동.m4a')
        lines = (self.dir / '.velcro-import.jsonl').read_text().replace(json.dumps(rec['file'], ensure_ascii=False)[1:-1], nfd)
        (self.dir / '.velcro-import.jsonl').write_text(lines)
        self.run_helper()
        self.assertTrue((self.out / '자양동.txt').exists())


class Failures(Base):
    def test_sha_mismatch_is_a_failure_and_nothing_is_written(self):
        self.add('bad.WAV', sha='0' * 64)
        self.run_helper()
        self.assertFalse((self.out / 'bad.json').exists())
        f = self.state_doc()['failures']['bad.WAV']
        self.assertEqual((f['count'], f['step']), (1, 'transcribe'))
        self.assertIn("SHA-256 doesn't match", f['error'])

    def test_backoff_doubles_and_alert_from_the_third_failure_once_a_day(self):
        self.add('x.WAV')
        self.engine = script(self.bin / 'broken', 'echo boom >&2; exit 3\n')
        alerts = self.tmp / 'alerts'
        alert = script(self.bin / 'alert', f'echo "$1" >> {alerts}\n')
        t = 1_800_000_000.0
        nexts = []
        for i in range(4):
            with mock.patch.object(vr.time, 'time', return_value=t):
                self.run_helper('--alert', alert)
            f = self.state_doc()['failures']['x.WAV']
            self.assertEqual(f['count'], i + 1)
            nexts.append(f['next'] - t)
            t = f['next'] + 1  # just after the retry time
        self.assertEqual(nexts, [600, 1200, 2400, 4800])
        sent = alerts.read_text().splitlines()
        self.assertEqual(len(sent), 1)  # the 3rd failure alerts; the 4th is the same day, so no second alert
        self.assertIn('x.WAV transcribe failed 3 times', sent[0])

    def test_waiting_file_is_not_retried_early_and_backoff_caps_at_six_hours(self):
        self.add('x.WAV')
        self.engine = script(self.bin / 'broken', 'exit 1\n')
        self.run_helper()
        _, out = self.run_helper(command='status')
        self.assertIn('transcribe (try 2 after', out)
        r = vr.Run(mock.Mock(dir=str(self.dir), out=str(self.out), state=str(self.state), alert=''))
        r.state['failures'] = {'x.WAV': {'count': 20, 'step': 'transcribe'}}
        with mock.patch('sys.stdout', io.StringIO()):
            r.failed({'file': 'x.WAV'}, 'transcribe', 'again')
        f = r.state['failures']['x.WAV']
        self.assertEqual(f['next'] - f['at'], 6 * 3600)

    def test_hook_failure_keeps_the_transcript_and_retries_only_the_hook(self):
        self.add('h.WAV')
        tidy = script(self.bin / 'tidy', 'echo nope >&2; exit 1\n')
        self.run_helper('--tidy', tidy)
        self.assertTrue((self.out / 'h.json').exists())
        f = self.state_doc()['failures']['h.WAV']
        self.assertEqual((f['step'], f['count']), ('tidy', 1))
        self.assertIn('nope', f['error'])
        self.assertFalse((self.out / 'h.tidy.txt').exists())

    def test_one_file_failing_does_not_stop_the_next(self):
        self.add('bad.WAV', sha='0' * 64)
        self.add('good.WAV')
        self.run_helper()
        self.assertTrue((self.out / 'good.json').exists())

    def test_lock_one_run_at_a_time(self):
        self.add('l.WAV')
        r = vr.Run(mock.Mock(dir=str(self.dir), out=str(self.out), state=str(self.state)))
        self.state.mkdir()
        held = open(r.lock_file, 'w')
        fcntl.flock(held, fcntl.LOCK_EX)
        self.addCleanup(held.close)
        _, out = self.run_helper()
        self.assertIn('another run is still going', out)
        self.assertFalse((self.out / 'l.json').exists())

    def test_budget_stops_new_steps(self):
        self.add('b.WAV')
        _, out = self.run_helper('--budget', '-1')
        self.assertIn('run budget used', out)
        self.assertFalse((self.out / 'b.json').exists())


    def test_timeout_stops_the_engine_and_its_children(self):
        self.add('t.WAV')
        pidfile = self.tmp / 'child.pid'
        self.engine = script(self.bin / 'slow', f'sleep 60 & echo $! > {pidfile}; wait\n')
        t0 = time.time()
        self.run_helper('--timeout', '1')
        self.assertLess(time.time() - t0, 20)
        self.assertIn('took over', self.state_doc()['failures']['t.WAV']['error'])
        child = int(pidfile.read_text())
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail('the engine\'s child kept running')


class Done(Base):
    """recordings.done: once per import, after every recording that import brought in is done."""

    def done_hook(self, body=''):
        """A done command that logs the import id per call and keeps what else it got in seen/<id>.*."""
        self.calls, seen = self.tmp / 'done.calls', self.tmp / 'seen'
        seen.mkdir(exist_ok=True)
        return script(self.bin / 'done', body + f'echo "$VELCRO_IMPORT" >> {self.calls}\n'
                      f'printf "%s" "$VELCRO_RECORDINGS" > {seen}/"$VELCRO_IMPORT".recordings\n'
                      f'printf "%s" "$VELCRO_TRANSCRIPTS" > {seen}/"$VELCRO_IMPORT".transcripts\n'
                      f'printf "%s" "$VELCRO_TRANSCRIPTS_JSON" > {seen}/"$VELCRO_IMPORT".json\n'
                      f'printf "%s" "$VELCRO_RECORDS" > {seen}/"$VELCRO_IMPORT".records\n'
                      f'printf "%s\\n" "$VELCRO_TRANSCRIPTS" | while IFS= read -r f; do cat "$f"; done'
                      f' > {seen}/"$VELCRO_IMPORT".texts\n')

    def calls_made(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def seen(self, imp, what):
        return (self.tmp / 'seen' / f'{imp}.{what}').read_text()

    def after_retry(self, key):
        """time.time() just past the retry time of key's failure."""
        return mock.patch.object(vr.time, 'time', return_value=self.state_doc()['failures'][key]['next'] + 1)

    def test_a_transcript_link_planted_before_the_hook_is_not_handed_on(self):
        self.add('a.WAV', **{'import': 'imp-1'})
        self.run_helper()  # transcribed; no done hook yet
        secret = self.tmp / 'secret.txt'
        secret.write_text('not for the hook')
        (self.out / 'a.txt').unlink()
        (self.out / 'a.txt').symlink_to(secret)
        done = self.done_hook()
        self.run_helper('--done', done)
        self.assertEqual(self.calls_made(), [])
        self.assertIn('import imp-1', self.state_doc()['failures'])
        self.assertEqual(secret.read_text(), 'not for the hook')

    def test_a_transcript_left_from_an_older_recording_is_not_handed_on(self):
        self.add('memo.WAV', **{'import': 'imp-1'})
        self.run_helper()
        # Same name, but the transcript's metadata says it is of other audio (an older memo.WAV).
        doc = json.loads((self.out / 'memo.json').read_text())
        doc['sha256'] = '0' * 64
        (self.out / 'memo.json').write_text(json.dumps(doc))
        done = self.done_hook()
        self.run_helper('--done', done)
        self.assertEqual(self.calls_made(), [])
        self.assertIn('another recording', self.state_doc()['failures']['import imp-1']['error'])

    def test_a_big_import_goes_in_files_when_it_does_not_fit_the_environment(self):
        self.add('a.WAV', **{'import': 'imp-1'})
        self.add('b.WAV', **{'import': 'imp-1'})
        seen = self.tmp / 'seen-files'
        done = script(self.bin / 'done-files', f'echo "$VELCRO_IMPORT" >> {self.tmp / "done.calls"}\n'
                      f'printf "%s" "${{VELCRO_RECORDS-unset}}" > {seen}.inline\n'
                      f'cat "$VELCRO_RECORDS_FILE" > {seen}.records\n'
                      f'cat "$VELCRO_TRANSCRIPTS_FILE" | while IFS= read -r f; do cat "$f"; done > {seen}.texts\n')
        self.calls = self.tmp / 'done.calls'
        with mock.patch.object(vr, 'INLINE_MAX', 10):
            self.run_helper('--done', done)
        self.assertEqual(self.calls_made(), ['imp-1'])
        self.assertEqual(Path(f'{seen}.inline').read_text(), 'unset')
        self.assertEqual([r['file'] for r in json.loads(Path(f'{seen}.records').read_text())], ['a.WAV', 'b.WAV'])
        self.assertEqual(Path(f'{seen}.texts').read_text(),
                         (self.out / 'a.txt').read_text() + (self.out / 'b.txt').read_text())

    def test_runs_once_when_every_recording_of_the_import_is_done(self):
        self.add('a.WAV', **{'import': 'imp-1'})
        self.add('b.WAV', **{'import': 'imp-1'})
        self.add('c.WAV', **{'import': 'imp-2'})
        flag = self.tmp / 'b-ok'
        self.engine = script(self.bin / 'engine-b', f'case "$1" in *b.WAV) [ -e {flag} ] || exit 1;; esac; echo "heard $1"\n')
        done = self.done_hook()
        self.run_helper('--done', done)
        # b failed to transcribe: imp-1 is not done, imp-2 is.
        self.assertTrue((self.out / 'a.json').exists())
        self.assertEqual(self.calls_made(), ['imp-2'])
        flag.touch()
        with self.after_retry('b.WAV'):
            self.run_helper('--done', done)
        self.assertEqual(self.calls_made(), ['imp-2', 'imp-1'])
        # Private copies like the other hooks get (never paths on the share), in import order.
        for what, names in (('recordings', ['a.WAV', 'b.WAV']), ('transcripts', ['a.txt', 'b.txt']),
                            ('json', ['a.json', 'b.json'])):
            got = self.seen('imp-1', what).split('\n')
            self.assertEqual([Path(g).name for g in got], names)
            for g in got:
                self.assertFalse(g.startswith(str(self.dir)) or g.startswith(str(self.out)), g)
        self.assertEqual(self.seen('imp-1', 'texts'),
                         (self.out / 'a.txt').read_text() + (self.out / 'b.txt').read_text())
        self.assertEqual([r['file'] for r in json.loads(self.seen('imp-1', 'records'))], ['a.WAV', 'b.WAV'])
        self.assertEqual(set(self.state_doc()['imports']), {'imp-1', 'imp-2'})
        # Later runs call it for neither import again.
        self.run_helper('--done', done)
        self.run_helper('--done', done)
        self.assertEqual(self.calls_made(), ['imp-2', 'imp-1'])

    def test_waits_for_tidy_and_deliver_of_every_recording(self):
        self.add('a.WAV', **{'import': 'imp-1'})
        self.add('b.WAV', **{'import': 'imp-1'})
        done = self.done_hook()
        tidy = script(self.bin / 'tidy', 'echo tidy\n')
        deliver = script(self.bin / 'deliver', 'case "$VELCRO_RECORDING" in *b.WAV) exit 1;; esac; echo ok\n')
        self.run_helper('--tidy', tidy, '--deliver', deliver, '--done', done)
        self.assertEqual(self.calls_made(), [])
        deliver = script(self.bin / 'deliver', 'echo ok\n')
        with self.after_retry('b.WAV'):
            self.run_helper('--tidy', tidy, '--deliver', deliver, '--done', done)
        self.assertEqual(self.calls_made(), ['imp-1'])

    def test_failure_is_retried_later_and_not_before(self):
        self.add('a.WAV', **{'import': 'imp-1'})
        flag = self.tmp / 'ok'
        done = self.done_hook(f'[ -e {flag} ] || {{ echo not yet >&2; exit 1; }}\n')
        self.run_helper('--done', done)
        f = self.state_doc()['failures']['import imp-1']
        self.assertEqual((f['step'], f['count']), ('done', 1))
        self.assertIn('not yet', f['error'])
        flag.touch()
        self.run_helper('--done', done)  # its retry time hasn't come
        self.assertEqual(self.calls_made(), [])
        _, out = self.run_helper('--done', done, command='status')
        self.assertIn('done (try 2 after', out)
        with self.after_retry('import imp-1'):
            self.run_helper('--done', done)
        self.assertEqual(self.calls_made(), ['imp-1'])
        doc = self.state_doc()
        self.assertNotIn('import imp-1', doc.get('failures', {}))
        self.assertIn('imp-1', doc['imports'])
        self.run_helper('--done', done)
        self.assertEqual(self.calls_made(), ['imp-1'])

    def test_lines_without_an_import_id_never_run_it(self):
        self.add('old.WAV')
        done = self.done_hook()
        self.run_helper('--done', done)
        self.assertTrue((self.out / 'old.json').exists())
        self.assertEqual(self.calls_made(), [])

    def test_late_queued_member_runs_done_again_for_the_changed_group(self):
        self.add('a.WAV', **{'import': 'imp-1'})
        revisions = self.tmp / 'revisions'
        done = self.done_hook(f'echo "$VELCRO_IMPORT_REVISION" >> {revisions}\n')
        self.run_helper('--done', done)
        self.add('b.WAV', **{'import': 'imp-1'})  # a later queue flush, same original import
        self.run_helper('--done', done)
        self.assertEqual(self.calls_made(), ['imp-1', 'imp-1'])
        self.assertEqual([r['file'] for r in json.loads(self.seen('imp-1', 'records'))], ['a.WAV', 'b.WAV'])
        self.assertEqual(len(set(revisions.read_text().splitlines())), 2)
        self.run_helper('--done', done)
        self.assertEqual(self.calls_made(), ['imp-1', 'imp-1'])

    def test_legacy_timestamp_completion_is_reconciled_once(self):
        self.add('a.WAV', **{'import': 'imp-1'})
        done = self.done_hook()
        self.run_helper('--done', done)
        state = self.state_doc()
        state['imports']['imp-1'] = '2026-11-02T10:00:00+09:00'
        next(self.state.glob('recordings-*.json')).write_text(json.dumps(state))
        self.run_helper('--done', done)
        self.run_helper('--done', done)
        self.assertEqual(self.calls_made(), ['imp-1', 'imp-1'])


class Check(Base):
    def check(self, *args):
        rc, out = self.run_helper(*args, command='check')
        return rc, json.loads(out)

    def test_missing_manifest_or_unregistered_audio_is_not_healthy(self):
        (self.dir / 'orphan.WAV').write_bytes(b'audio')
        rc, report = self.check()
        self.assertEqual(rc, 1)
        self.assertFalse(report['ok'])
        self.assertEqual(report['counts']['unregistered'], 1)
        self.assertIn({'stage': 'manifest missing'}, report['issues'])
        self.assertFalse(self.out.exists())
        self.assertFalse(self.state.exists())

    def test_malformed_manifest_is_reported_even_with_completed_recordings(self):
        self.add('a.WAV')
        self.run_helper()
        with (self.dir / '.velcro-import.jsonl').open('a') as f:
            f.write('{broken\n[]\n')
        rc, report = self.check()
        self.assertEqual(rc, 1)
        self.assertEqual(report['counts']['complete'], 1)
        self.assertEqual(len(report['issues']), 2)

    def test_counts_alone_do_not_hide_missing_or_failed_handoffs(self):
        self.add('a.WAV')
        self.add('b.WAV')
        deliver = script(self.bin / 'deliver', 'exit 1\n')
        self.run_helper('--deliver', deliver)
        rc, report = self.check('--deliver', deliver)
        self.assertEqual(rc, 1)
        self.assertEqual(report['counts']['pending'], 2)
        self.assertTrue(all('deliver' in i['stage'] for i in report['issues']))
        deliver = script(self.bin / 'deliver', 'echo acknowledged\n')
        with mock.patch.object(vr.time, 'time', return_value=time.time() + 601):
            self.run_helper('--deliver', deliver)
        rc, report = self.check('--deliver', deliver)
        self.assertEqual(rc, 0)
        self.assertEqual(report['counts']['complete'], 2)

    def test_required_hooks_and_missing_engine_cannot_look_complete(self):
        self.add('a.WAV')
        self.run_helper()
        rc, report = self.check('--engine', '', '--require', 'tidy', '--require', 'deliver', '--require', 'done')
        self.assertEqual(rc, 1)
        self.assertEqual(len(report['issues']), 5)  # engine, 3 hooks, old line with no import id

    def test_checks_transcript_receipt_and_missing_source(self):
        self.add('a.WAV')
        self.run_helper()
        (self.out / 'a.txt').unlink()
        rc, report = self.check()
        self.assertEqual(rc, 1)
        self.assertEqual(report['issues'][0]['stage'], 'transcript missing or mismatched')
        (self.dir / 'a.WAV').unlink()
        rc, report = self.check()
        self.assertEqual(rc, 1)
        self.assertEqual(report['issues'][0]['stage'], 'source gone')

    def test_done_receipt_is_required_even_after_transcription(self):
        self.add('a.WAV', **{'import': 'imp-1'})
        done = script(self.bin / 'done', 'echo acknowledged\n')
        self.run_helper()
        rc, report = self.check('--done', done)
        self.assertEqual(rc, 1)
        self.assertEqual(report['issues'], [{'import': 'imp-1', 'stage': 'done hook pending'}])
        self.run_helper('--done', done)
        self.assertEqual(self.check('--done', done)[0], 0)
        self.add('b.WAV', **{'import': 'imp-1'})
        self.assertEqual(self.check('--done', done)[0], 1)

    def test_since_does_not_misidentify_registered_older_audio_as_orphan(self):
        self.add('old.WAV', imported_at='2026-10-20T09:00:00+09:00')
        rc, report = self.check('--since', '2026-10-21')
        self.assertEqual(rc, 0)
        self.assertEqual(report['counts']['recordings'], 0)
        self.assertEqual(report['counts']['unregistered'], 0)


class Command(Base):
    """velcro recordings hands the settings to the helper."""

    def test_settings_reach_the_helper(self):
        home = self.tmp / 'home'
        (home / '.config/velcro').mkdir(parents=True)
        env = {k: v for k, v in os.environ.items() if k not in ('XDG_CONFIG_HOME', 'XDG_STATE_HOME')}
        env['HOME'] = str(home)
        velcro = ['zsh', str(ROOT / 'velcro')]
        self.add('s.WAV', imported_at='2026-10-22T08:00:00+09:00')
        for kv in (['recordings.dir', str(self.dir)], ['recordings.out', str(self.out)],
                   ['recordings.engine', self.engine], ['recordings.since', '2026-10-22']):
            subprocess.run(velcro + ['set', *kv], env=env, check=True)
        bad = subprocess.run(velcro + ['set', 'recordings.since', 'yesterday'], env=env, capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)
        r = subprocess.run(velcro + ['recordings', 'run'], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.out / 's.txt').read_text(), 'heard: s.WAV\n')
        state = home / ('Library/Application Support/velcro' if sys.platform == 'darwin' else '.local/state/velcro')
        self.assertTrue(list(state.glob('recordings-*.json')))
        st = subprocess.run(velcro + ['recordings'], env=env, capture_output=True, text=True)
        self.assertIn('s.WAV', st.stdout)
        self.assertIn('done', st.stdout)
        health = subprocess.run(velcro + ['recordings', 'check', '--require', 'deliver'], env=env, capture_output=True, text=True)
        self.assertEqual(health.returncode, 1, health.stderr)
        self.assertIn({'stage': 'deliver not configured'}, json.loads(health.stdout)['issues'])

    def test_no_engine_no_run(self):
        """Bobby 2026-10-21: claude is never the recorder path's default; without an engine nothing runs."""
        home = self.tmp / 'home2'
        (home / '.config/velcro').mkdir(parents=True)
        env = {k: v for k, v in os.environ.items() if k not in ('XDG_CONFIG_HOME', 'XDG_STATE_HOME')}
        env['HOME'] = str(home)
        velcro = ['zsh', str(ROOT / 'velcro')]
        self.add('n.WAV')
        subprocess.run(velcro + ['set', 'recordings.dir', str(self.dir)], env=env, check=True)
        r = subprocess.run(velcro + ['recordings', 'run'], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn('choose an engine first', r.stderr)
        self.assertFalse((self.dir / '_velcro').exists())
        self.assertFalse((home / '.local/state/velcro').exists())


class SharedFolder(Base):
    """recordings.dir and the output folder are often shares other people can write to."""

    def test_manifest_cannot_point_outside_the_folder(self):
        secret = self.tmp / 'secret.WAV'
        secret.write_bytes(b'not yours')
        (self.dir / 'link.WAV').symlink_to(secret)
        (self.dir / 'real.WAV').write_bytes(b'mine')
        (self.dir / 'inside.WAV').symlink_to(self.dir / 'real.WAV')  # a link that stays inside is fine
        with open(self.dir / '.velcro-import.jsonl', 'a') as f:
            for name in (str(secret), '../secret.WAV', 'link.WAV', 'inside.WAV'):
                f.write(json.dumps({'file': name, 'imported_at': '2026-10-21T10:00:00+09:00'}) + '\n')
        rc, log = self.run_helper()
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ['inside.json', 'inside.txt'])
        for name in (str(secret), '../secret.WAV', 'link.WAV'):
            self.assertIn(f'{name}: outside {self.dir}, skipped', log)

    def test_planted_part_link_does_not_overwrite_a_file_outside(self):
        self.add('p.WAV')
        victim = self.tmp / 'victim'
        victim.write_text('keep me')
        self.out.mkdir()
        (self.out / '.p.txt.part').symlink_to(victim)
        (self.out / '.p.json.part').symlink_to(victim)
        rc, _ = self.run_helper()
        self.assertEqual(rc, 0)
        self.assertEqual(victim.read_text(), 'keep me')
        self.assertEqual((self.out / 'p.txt').read_text(), 'heard: p.WAV\n')
        self.assertFalse((self.out / 'p.txt').is_symlink())


    def test_swapping_the_file_for_a_link_after_the_check_reads_nothing_outside(self):
        """A share user can turn x.WAV into a link between any check and the engine opening it."""
        secret = self.tmp / 'secret'
        secret.write_text('the server account\'s secret')
        rec = self.add('x.WAV')
        del rec['sha256']  # whoever writes the manifest can leave the hash out
        (self.dir / '.velcro-import.jsonl').write_text(json.dumps(rec) + '\n')
        self.engine = script(self.bin / 'cat-engine', 'cat "$1"\n')
        checked = vr.Run.inside

        def check_then_swap(run, path):  # every check sees the real file, whatever opens it next sees the link
            x = run.dir / 'x.WAV'
            x.unlink()
            x.write_bytes(b'RIFFaudio')
            ok = checked(run, path)
            x.unlink()
            x.symlink_to(secret)
            return ok
        with mock.patch.object(vr.Run, 'inside', check_then_swap):
            self.run_helper()
        for f in self.out.iterdir():
            self.assertNotIn('secret', f.read_text(), f.name)
        self.assertIn('outside', self.state_doc()['failures']['x.WAV']['error'])

    def test_without_proc_the_open_file_is_matched_by_inode(self):
        """A Mac has no /proc (F_GETPATH there); with neither, the open file must be the one at the checked path."""
        secret = self.tmp / 'secret'
        secret.write_text('secret')
        self.add('ok.WAV')
        rec = {'file': 'link.WAV', 'imported_at': '2026-10-21T10:00:00+09:00'}
        (self.dir / 'link.WAV').write_bytes(b'x')
        with open(self.dir / '.velcro-import.jsonl', 'a') as f:
            f.write(json.dumps(rec) + '\n')
        run = vr.Run(mock.Mock(dir=str(self.dir), out=str(self.out), state=str(self.state)))
        work = self.tmp / 'work'
        with mock.patch.object(vr, 'opened_path', return_value=None):
            for name in ('ok.WAV', 'link.WAV'):
                (work / name).mkdir(parents=True)
            copy, _ = run.copy_in({'file': 'ok.WAV'}, self.dir / 'ok.WAV', work / 'ok.WAV')
            self.assertEqual(copy.read_bytes(), b'RIFFaudio')
            (self.dir / 'link.WAV').unlink()
            (self.dir / 'link.WAV').symlink_to(secret)
            with self.assertRaisesRegex(RuntimeError, 'outside'):
                run.copy_in(rec, self.dir / 'link.WAV', work / 'link.WAV')

    def test_engine_json_is_private_and_a_planted_link_is_ignored(self):
        victim = self.tmp / 'server.json'
        victim.write_text('{"token": "server-secret"}')
        self.add('a.WAV')
        seen = self.tmp / 'json-path'
        # The engine plants the link itself: what a share user could do while it runs.
        self.engine = script(self.bin / 'engine3', f'echo "$2" > {seen}; ln -s {victim} "{self.out}/.a.engine.json"; echo text\n')
        self.engine += ' --json {json}'
        self.run_helper()
        self.assertNotIn('server-secret', (self.out / 'a.json').read_text())
        json_path = Path(seen.read_text().strip())
        for shared in (self.dir, self.out):
            self.assertNotEqual(os.path.commonpath([json_path, shared]), str(shared))
        self.assertFalse(json_path.parent.exists())  # removed afterwards
        self.assertEqual(victim.read_text(), '{"token": "server-secret"}')

    def private_folder(self):
        """A folder of the server account's, outside the share, with a file a transcript could replace."""
        private = self.tmp / 'private'
        private.mkdir()
        (private / 'notes.txt').write_text('keep me')
        return private

    def test_default_output_folder_planted_as_a_link_writes_nothing_outside(self):
        """The default output folder is <dir>/_velcro, inside the share: a share user can make it a link first."""
        private = self.private_folder()
        self.out = self.dir / '_velcro'
        self.out.symlink_to(private)
        self.add('notes.WAV')
        rc, _ = self.run_helper()
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(p.name for p in private.iterdir()), ['notes.txt'])
        self.assertEqual((private / 'notes.txt').read_text(), 'keep me')
        self.assertIn('outside', self.state_doc()['failures']['notes.WAV']['error'])

    def test_output_folder_swapped_for_a_link_while_the_engine_runs_writes_nothing_outside(self):
        private = self.private_folder()
        self.out = self.dir / '_velcro'
        self.add('notes.WAV')
        # The engine does the swap itself: what a share user could do while it runs.
        self.engine = script(self.bin / 'swap-engine',
                             f'mv "{self.out}" "{self.dir}/_old" && ln -s "{private}" "{self.out}"; echo text\n')
        rc, _ = self.run_helper()
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(p.name for p in private.iterdir()), ['notes.txt'])
        self.assertEqual((private / 'notes.txt').read_text(), 'keep me')

    def test_output_folder_outside_the_recordings_may_be_a_link(self):
        """recordings.out set by the user to a link of their own (outside the share) still works."""
        real = self.tmp / 'transcripts'
        real.mkdir()
        self.out = self.tmp / 'out-link'
        self.out.symlink_to(real)
        self.add('a.WAV')
        with self.share_mount(self.dir):
            rc, _ = self.run_helper()
        self.assertEqual(rc, 0)
        self.assertEqual((real / 'a.txt').read_text(), 'heard: a.WAV\n')

    def test_output_folder_may_be_the_recordings_folder_itself(self):
        """recordings.out = recordings.dir worked before the link check and still does."""
        self.out = self.dir
        self.add('a.WAV')
        rc, _ = self.run_helper()
        self.assertEqual(rc, 0)
        self.assertEqual((self.dir / 'a.txt').read_text(), 'heard: a.WAV\n')

    def test_planted_link_is_caught_when_the_two_settings_spell_the_share_differently(self):
        """recordings.dir through a link (~/nas/rec), recordings.out by its real path: still inside the share."""
        private = self.private_folder()
        real = self.dir
        self.dir = self.tmp / 'rec-link'
        self.dir.symlink_to(real)
        self.out = real / '_velcro'
        self.out.symlink_to(private)
        self.add('notes.WAV')
        rc, _ = self.run_helper()
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(p.name for p in private.iterdir()), ['notes.txt'])
        self.assertEqual((private / 'notes.txt').read_text(), 'keep me')

    def test_planted_link_is_caught_through_a_link_of_the_users_into_the_share(self):
        """recordings.out = <a link of the user's to the share>/_velcro: its last part is still the share's."""
        private = self.private_folder()
        into = self.tmp / 'share-link'
        into.symlink_to(self.dir)
        (self.dir / '_velcro').symlink_to(private)
        self.out = into / '_velcro'
        self.add('notes.WAV')
        rc, _ = self.run_helper()
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(p.name for p in private.iterdir()), ['notes.txt'])

    def test_a_fifo_where_the_owner_json_would_be_does_not_hang_the_run(self):
        """The output folder is in the share: a FIFO planted as <stem>.json must not stop every later run."""
        import threading
        self.out.mkdir()
        os.mkfifo(self.out / 'memo.json')
        self.add('memo.m4a')
        done = []
        t = threading.Thread(target=lambda: done.append(self.run_helper()), daemon=True)
        t.start()
        t.join(10)
        self.assertTrue(done, 'the run hung on the FIFO')


if __name__ == '__main__':
    unittest.main()


class ShareTrust(Base):
    """Security follow-up: output aliases, mount siblings, hook inputs, mkdir and file modes."""

    def test_user_output_link_into_share_rejects_swapped_subfolder(self):
        sub = self.dir / 'sub'
        sub.mkdir()
        private = self.tmp / 'private'
        private.mkdir()
        victim = private / 'notes.txt'
        victim.write_text('keep me')
        self.out.symlink_to(sub)
        self.add('notes.WAV')
        ran = self.tmp / 'engine-ran'
        self.engine = script(self.bin / 'swap',
                             f'echo ran > "{ran}"; mv "{sub}" "{self.dir}/old"; ln -s "{private}" "{sub}"; echo text\n')
        with self.share_mount(self.dir):
            self.run_helper()
        self.assertTrue(ran.exists(), 'the engine must run before the subfolder is swapped')
        self.assertEqual(victim.read_text(), 'keep me')
        self.assertFalse((private / 'notes.json').exists())

    def test_same_filesystem_output_rejects_link_planted_between_runs(self):
        self.add('first.WAV')
        self.run_helper()
        self.out.rename(self.tmp / 'old-output')
        private = self.tmp / 'private'
        private.mkdir()
        (private / 'notes.txt').write_text('keep me')
        self.out.symlink_to(private)
        self.add('notes.WAV')
        self.run_helper()
        self.assertEqual((private / 'notes.txt').read_text(), 'keep me')
        self.assertEqual(sorted(p.name for p in private.iterdir()), ['notes.txt'])
        self.assertIn('outside', self.state_doc()['failures']['notes.WAV']['error'])

    def test_output_ancestor_links_outside_share_are_resolved(self):
        real = self.tmp / 'real-home'
        (real / 'transcripts').mkdir(parents=True)
        (self.tmp / 'home').symlink_to(real)
        (self.tmp / 'home-alias').symlink_to('home')
        self.out = self.tmp / 'home-alias/transcripts/new'
        self.add('a.WAV')
        with self.share_mount(self.dir):
            rc, output = self.run_helper()
        self.assertEqual(rc, 0)
        self.assertTrue((real / 'transcripts/new/a.txt').is_file(), output)
        self.assertEqual((real / 'transcripts/new/a.txt').read_text(), 'heard: a.WAV\n', output)
        self.assertTrue((real / 'transcripts/new/a.json').is_file(), output)

    def test_default_output_through_recordings_dir_link(self):
        real = self.dir
        self.dir = self.tmp / 'rec-link'
        self.dir.symlink_to(real)
        self.out = self.dir / '_velcro'
        self.add('a.WAV')
        rc, output = self.run_helper()
        self.assertEqual(rc, 0)
        self.assertTrue((real / '_velcro/a.txt').is_file(), output)
        self.assertEqual((real / '_velcro/a.txt').read_text(), 'heard: a.WAV\n', output)
        self.assertTrue((real / '_velcro/a.json').is_file(), output)

    def test_existing_output_folders_are_opened_without_mkdir(self):
        self.out.mkdir()
        self.add('a.WAV')
        original = os.mkdir

        def deny_existing(path, *args, **kwargs):
            if kwargs.get('dir_fd') is not None:
                raise PermissionError('SMB denies mkdir on an existing folder')
            return original(path, *args, **kwargs)

        with mock.patch.object(vr.os, 'mkdir', deny_existing):
            rc, output = self.run_helper()
        self.assertEqual(rc, 0)
        self.assertTrue((self.out / 'a.json').is_file(), output)

    def test_same_filesystem_output_rejects_swapped_parent(self):
        # A sibling on the same mount, as in /mnt/nas/me/{recordings,transcripts}.
        parent = self.tmp / 'transcripts'
        self.out = parent / 'nested'
        self.out.mkdir(parents=True)
        private = self.tmp / 'private'
        (private / 'nested').mkdir(parents=True)
        victim = private / 'nested/notes.txt'
        victim.write_text('keep me')
        self.assertEqual(self.out.stat().st_dev, self.dir.stat().st_dev)
        self.add('notes.WAV')
        self.engine = script(self.bin / 'swap',
                             f'mv "{parent}" "{self.tmp}/old"; ln -s "{private}" "{parent}"; echo text\n')
        self.run_helper()
        self.assertEqual(victim.read_text(), 'keep me')
        self.assertFalse((private / 'nested/notes.json').exists())

    def test_hooks_receive_private_copies_removed_after_each_hook(self):
        self.add('a.WAV')
        self.run_helper()
        (self.out / 'a.tidy.txt').write_text('tidied\n')
        secret = self.tmp / 'secret'
        secret.write_text('server-secret')
        keys = ('VELCRO_TRANSCRIPT', 'VELCRO_TRANSCRIPT_JSON', 'VELCRO_TIDIED', 'VELCRO_RECORDING')
        originals = (self.out / 'a.txt', self.out / 'a.json', self.out / 'a.tidy.txt', self.dir / 'a.WAV')
        paths, modes, folders = [], [], []
        run = vr.run_limited

        def swap_then_run(argv, timeout, env=None):
            if env and 'VELCRO_TRANSCRIPT' in env:
                for key in keys:
                    path = Path(env[key])
                    paths.append(path)
                    modes.append(stat.S_IMODE(path.stat().st_mode))
                    folders.append(stat.S_IMODE(path.parent.stat().st_mode))
                for path in originals:
                    path.unlink()
                    path.symlink_to(secret)
            return run(argv, timeout, env)

        deliver = script(self.bin / 'deliver',
                         'cat "$VELCRO_TRANSCRIPT" "$VELCRO_TRANSCRIPT_JSON" "$VELCRO_TIDIED" "$VELCRO_RECORDING"\n')
        with mock.patch.object(vr, 'run_limited', swap_then_run):
            self.run_helper('--deliver', deliver)
        delivered = (self.out / 'a.deliver.txt').read_text()
        self.assertNotIn('server-secret', delivered)
        self.assertIn('heard: a.WAV', delivered)
        self.assertIn('RIFFaudio', delivered)
        self.assertEqual(modes, [0o600] * len(keys))
        self.assertEqual(folders, [0o700] * len(keys))
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertTrue(all(not path.parent.exists() for path in paths))

    def test_hook_refuses_transcript_link_planted_before_copy(self):
        self.add('a.WAV')
        self.run_helper()
        secret = self.tmp / 'secret'
        secret.write_text('server-secret')
        (self.out / 'a.txt').unlink()
        (self.out / 'a.txt').symlink_to(secret)
        seen = self.tmp / 'delivered'
        deliver = script(self.bin / 'deliver', f'cat "$VELCRO_TRANSCRIPT" > "{seen}"\n')
        self.run_helper('--deliver', deliver)
        self.assertFalse(seen.exists(), 'the hook ran with a planted transcript link')
        self.assertEqual(self.state_doc()['failures']['a.WAV']['step'], 'deliver')

    def test_intermediate_output_link_creates_no_folders_outside(self):
        private = self.tmp / 'private'
        private.mkdir()
        (self.dir / 'jump').symlink_to(private)
        self.out = self.dir / 'jump/new/deep'
        self.add('a.WAV')
        try:
            self.run_helper()
        except (OSError, RuntimeError):
            pass  # refusing the folder before processing is also safe
        self.assertEqual(list(private.iterdir()), [])

    def test_new_transcripts_and_private_recording_are_0600(self):
        self.add('a.WAV')
        audio_modes = []
        run = vr.run_limited

        def inspect_audio(argv, timeout, env=None):
            audio_modes.append(stat.S_IMODE(Path(argv[-1]).stat().st_mode))
            return run(argv, timeout, env)

        mask = os.umask(0o022)
        try:
            with mock.patch.object(vr, 'run_limited', inspect_audio):
                self.run_helper()
        finally:
            os.umask(mask)
        with self.subTest(file='private recording'):
            self.assertEqual(audio_modes, [0o600])
        for name in ('a.txt', 'a.json'):
            with self.subTest(file=name):
                self.assertEqual(stat.S_IMODE((self.out / name).stat().st_mode), 0o600)

    def test_replaced_files_keep_their_mode(self):
        self.out.mkdir()
        for writer in ('put', 'put_at'):
            with self.subTest(writer=writer):
                path = self.out / writer
                path.write_text('old')
                path.chmod(0o640)
                if writer == 'put':
                    vr.put(path, 'new')
                else:
                    fd = os.open(self.out, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        vr.put_at(fd, path.name, 'new')
                    finally:
                        os.close(fd)
                self.assertEqual(path.read_text(), 'new')
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_replaced_files_owned_by_another_user_are_0600(self):
        self.out.mkdir()
        for writer in ('put', 'put_at'):
            with self.subTest(writer=writer):
                path = self.out / writer
                path.write_text('planted')
                path.chmod(0o666)
                # Exercise real replacement without needing permission to chown to another user.
                with mock.patch.object(vr.os, 'geteuid', return_value=path.stat().st_uid + 1):
                    if writer == 'put':
                        vr.put(path, 'private transcript')
                    else:
                        fd = os.open(self.out, os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            vr.put_at(fd, path.name, 'private transcript')
                        finally:
                            os.close(fd)
                self.assertEqual(path.read_text(), 'private transcript')
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @unittest.skipUnless(shutil.which('zsh'), 'velcro import needs zsh')
    def test_imported_recording_is_created_0600(self):
        # Exercise the real import writer with portable hashing/stat stubs, without macOS services.
        source = self.tmp / 'a.WAV'
        source.write_bytes(b'RIFFaudio')
        self.out.mkdir()
        body = (ROOT / 'velcro').read_text().split('put_verified() {', 1)[1].split('\n}\n', 1)[0]
        code = ('setopt pipe_fail\nzmodload zsh/zselect\numask 022\n'
                'size_of() { wc -c < "$1" | tr -d " "; }\n'
                f'mktemp() {{ command mktemp {shlex.quote(str(self.tmp / "sum.XXXXXX"))}; }}\n'
                'sha256() { /usr/bin/openssl dgst -sha256 -r < "$1" | cut -d " " -f1; }\n'
                'progress() { :; }\nlog() { print -u2 -- "$*"; }\nLOG=/dev/null\n'
                'put_verified() {' + body + '\n}\n'
                f'put_verified {shlex.quote(str(source))} {shlex.quote(str(self.out))} ""\n')
        result = subprocess.run(['zsh', '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.out / source.name).read_bytes(), source.read_bytes())
        self.assertEqual(stat.S_IMODE((self.out / source.name).stat().st_mode), 0o600)


class Reliability(Base):
    """Restarts, overlapping runs and look-alike names must not lose a recording or its retry schedule."""

    def test_same_name_different_type_each_gets_a_transcript(self):
        # A recorder (or a manual upload) can leave memo.wav and memo.m4a side by side: both are recordings.
        self.add('memo.wav', b'RIFFone')
        self.add('memo.m4a', b'm4a-two')
        self.run_helper()
        texts = sorted(p.read_text() for p in self.out.glob('*.txt'))
        self.assertEqual(texts, ['heard: memo.m4a\n', 'heard: memo.wav\n'])
        _, out = self.run_helper(command='status')
        self.assertEqual(out.count(' done'), 2, out)
        # Again: nothing is transcribed twice.
        _, out = self.run_helper()
        self.assertIn('nothing new', out)

    def test_run_that_waited_for_the_lock_keeps_the_failures_of_the_run_before(self):
        # A timer run starts (reads the state) just as the previous run finishes: it must not act on, or
        # write back, the state from before that run.
        self.add('x.WAV')
        calls = self.tmp / 'calls'
        self.engine = script(self.bin / 'broken', f'echo x >> {calls}; exit 1\n')
        late = vr.Run(mock.Mock(dir=str(self.dir), out=str(self.out), state=str(self.state), engine=self.engine,
                                since='', tidy='', deliver='', alert='', timeout=0, budget=2400, lang=''))
        self.run_helper()  # the earlier run: fails once, retry in 10 minutes
        with mock.patch('sys.stdout', io.StringIO()):
            late.run()
        self.assertEqual(len(calls.read_text().splitlines()), 1)  # not retried early
        self.assertEqual(self.state_doc()['failures']['x.WAV']['count'], 1)

    def test_stopped_run_leaves_no_copy_and_no_engine_behind(self):
        # The timer (or the user) stops a run mid-engine: the private copy of the recording goes, and so
        # does the engine; the next run starts the step again.
        self.add('s.WAV')
        pidfile = self.tmp / 'engine.pid'
        self.engine = script(self.bin / 'slow', f'echo $$ > {pidfile}; sleep 60\n')
        p = subprocess.Popen([sys.executable, str(ROOT / 'velcro-recordings'), 'run', '--dir', str(self.dir),
                              '--out', str(self.out), '--state', str(self.state), '--engine', self.engine],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.1)
        p.terminate()
        p.wait(timeout=20)
        engine = int(pidfile.read_text())
        for _ in range(50):
            try:
                os.kill(engine, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(engine, 9)
            self.fail('the engine kept running after the run was stopped')
        self.assertEqual([q.name for q in self.state.glob('velcro-rec-*')], [])
        self.assertFalse((self.out / 's.json').exists())
        self.engine = script(self.bin / 'engine', 'echo "heard: $(basename "$1")"\n')
        self.run_helper()
        self.assertEqual((self.out / 's.txt').read_text(), 'heard: s.WAV\n')
