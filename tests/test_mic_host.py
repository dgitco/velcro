"""velcro-mic-host: one Mac at a time, even when two connect at once. PipeWire is faked: `pipewire`
reads the FIFO, `pw-link` prints nothing, so this runs on any Linux (or Mac) with python3.

  python3 -m unittest discover -s tests
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(Path('/proc/self/cmdline').exists(), 'velcro-mic-host finds the previous one in /proc')
class MicHost(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='velcro-mic-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        runtime = self.tmp / 'run'
        runtime.mkdir()
        (runtime / 'pipewire-0').touch()
        self.state = runtime / 'velcro-mic'
        bin = self.tmp / 'bin'
        bin.mkdir()
        (bin / 'pipewire').write_text(
            '#!/usr/bin/env python3\n'
            'import os, re, sys\n'
            'fifo = re.search(r\'pipe.filename = "([^"]+)"\', open(sys.argv[2]).read()).group(1)\n'
            'fd = os.open(fifo, os.O_RDONLY)\n'
            'while True:\n'
            '    os.read(fd, 65536)\n')
        (bin / 'pw-link').write_text('#!/bin/sh\nexec sleep 600\n')
        for tool in ('pipewire', 'pw-link'):
            (bin / tool).chmod(0o755)
        self.helper = bin / 'velcro-mic-host'
        shutil.copy(ROOT / 'velcro-mic-host', self.helper)
        self.env = {'PATH': f'{bin}:/usr/bin:/bin', 'XDG_RUNTIME_DIR': str(runtime), 'HOME': str(self.tmp)}
        self.procs = []
        self.addCleanup(self.kill_all)

    def kill_all(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stdout.close()
        out = subprocess.run(['pgrep', '-f', str(self.tmp)], capture_output=True, text=True).stdout
        for pid in out.split():
            try:
                os.kill(int(pid), 9)
            except OSError:
                pass

    def start(self, mac):
        p = subprocess.Popen([sys.executable, str(self.helper), mac], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=self.env)
        self.procs.append(p)
        return p

    def wait_for_one(self, a, b, timeout=4):
        end = time.time() + timeout
        while time.time() < end and a.poll() is None and b.poll() is None:
            time.sleep(0.05)
        time.sleep(0.3)

    def test_two_macs_connecting_at_once_leave_one_microphone(self):
        # Two Macs whose sessions start in the same moment (both waking up, say). The newest wins and
        # the other is told, as when they connect one after the other; never both, each sure it has
        # the machine, with two "velcro-mic" microphones there.
        for trial in range(12):
            with self.subTest(trial=trial):
                self.kill_all()
                self.procs = []
                shutil.rmtree(self.state, True)
                a, b = self.start('mac-a'), self.start('mac-b')
                self.wait_for_one(a, b)
                alive = [p for p in (a, b) if p.poll() is None]
                self.assertEqual(len(alive), 1, 'both velcro-mic-hosts kept running')
                gone = b if alive[0] is a else a
                winner = 'mac-a' if alive[0] is a else 'mac-b'
                said = gone.stdout.read().decode()
                self.assertIn(f'error taken over by {winner}', said)
                self.assertEqual((self.state / 'pid').read_text().split()[1], winner)
                alive[0].stdin.close()
                alive[0].wait(3)

    def test_newer_mac_takes_over_and_older_is_told(self):
        a = self.start('mac-a')
        self.assertTrue(a.stdout.readline().decode().startswith('hello '))
        b = self.start('mac-b')
        self.assertTrue(b.stdout.readline().decode().startswith('hello '))
        self.assertEqual(a.wait(3), 0)
        self.assertIn('error taken over by mac-b', a.stdout.read().decode())
        self.assertIsNone(b.poll())


if __name__ == '__main__':
    unittest.main()
