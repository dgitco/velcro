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
        rc, _ = self.run_helper()
        self.assertEqual(rc, 0)
        self.assertEqual((real / 'a.txt').read_text(), 'heard: a.WAV\n')


if __name__ == '__main__':
    unittest.main()
