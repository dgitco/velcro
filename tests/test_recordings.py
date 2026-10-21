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
import shutil
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
        self.tmp = Path(tempfile.mkdtemp(prefix='velcro-rec-test-'))
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
        self.assertEqual((self.out / 'a.txt').read_text(), f'text of {self.dir / "a.WAV"}\n')
        self.assertEqual(json.loads((self.out / 'a.json').read_text())['detail'], {'chunks': 3})
        self.assertEqual([p.name for p in self.out.glob('.*')], [])  # the engine's JSON is not left behind

    def test_claude_engine_runs_velcro_transcribe_json(self):
        self.add('memo.m4a')
        fake_velcro = script(self.bin / 'velcro', 'echo "$*" > "$(dirname "$0")/args"; '
                                                   'echo \'{"engine":"claude","file":"x","text":"안녕하세요"}\'\n')
        argv = ['run', '--dir', str(self.dir), '--out', str(self.out), '--state', str(self.state),
                '--engine', 'claude', '--velcro', fake_velcro, '--lang', 'ko']
        with mock.patch('sys.stdout', io.StringIO()):
            vr.main(argv)
        self.assertEqual((self.bin / 'args').read_text().strip(), f'transcribe --json --lang ko {self.dir / "memo.m4a"}')
        self.assertEqual((self.out / 'memo.txt').read_text(), '안녕하세요\n')
        self.assertEqual(json.loads((self.out / 'memo.json').read_text())['engine'], 'claude')

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
        self.assertTrue(list((home / '.local/state/velcro').glob('recordings-*.json')))
        st = subprocess.run(velcro + ['recordings'], env=env, capture_output=True, text=True)
        self.assertIn('s.WAV', st.stdout)
        self.assertIn('done', st.stdout)


if __name__ == '__main__':
    unittest.main()
