"""velcro install: a recorder plugged in is imported whether or not velcro.app is running.

  python3 -m unittest discover -s tests

Runs the real script with a throwaway HOME and stub launchctl, defaults, pgrep, open and osascript
first on PATH, so nothing on this Mac changes (see AGENTS.md).
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
IMPORT_PLIST = 'Library/LaunchAgents/io.github.dgitco.velcro.import.plist'


@unittest.skipUnless(shutil.which('zsh'), 'needs zsh')
@unittest.skipUnless(sys.platform == 'darwin', 'velcro install is macOS-only (BSD ln -h, launchd)')
class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='velcro-install-test-'))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home, self.stubs = self.tmp / 'home', self.tmp / 'stubs'
        self.home.mkdir()
        self.stubs.mkdir()
        self.calls = self.tmp / 'launchctl.calls'
        # launchctl: remember each call; "print" finds an agent only once it was bootstrapped.
        self.stub('launchctl', f'echo "$*" >> "{self.calls}"\n'
                               f'if [ "$1" = print ]; then grep -q "bootstrap .*${{2##*/}}.plist" "{self.calls}"; exit $?; fi\n'
                               'exit 0\n')
        for name in ('defaults', 'open', 'osascript'):
            self.stub(name, 'exit 1\n')
        self.stub('pgrep', 'exit 1\n')  # velcro.app is not running

    def stub(self, name, body):
        p = self.stubs / name
        p.write_text('#!/bin/sh\n' + body)
        p.chmod(0o755)

    def velcro(self, script, *args):
        env = {k: v for k, v in os.environ.items() if k != 'XDG_CONFIG_HOME'}
        env.update(HOME=str(self.home), PATH=f'{self.stubs}:{os.environ["PATH"]}')
        return subprocess.run(['zsh', str(script), *args], env=env, capture_output=True, text=True, timeout=60)

    def app_copy(self):
        """The script where velcro.app keeps it, so `velcro install` takes the app's branch."""
        res = self.tmp / 'Applications/velcro.app/Contents/Resources'
        res.mkdir(parents=True)
        shutil.copy(ROOT / 'velcro', res / 'velcro')
        return res / 'velcro'

    def assert_import_agent(self, out):
        plist = self.home / IMPORT_PLIST
        self.assertTrue(plist.is_file(), out)
        text = plist.read_text()
        self.assertIn('<key>StartOnMount</key><true/>', text)
        self.assertIn('<string>import</string><string>--auto</string>', text)
        # A missed mount is caught by looking again every few minutes.
        self.assertIn('<key>StartInterval</key><integer>300</integer>', text)
        self.assertRegex(self.calls.read_text(), r'bootstrap gui/\d+ .*io\.github\.dgitco\.velcro\.import\.plist')

    def test_app_install_keeps_an_import_agent_for_when_the_app_is_not_running(self):
        r = self.velcro(self.app_copy(), 'install')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.home / '.local/bin/velcro').is_symlink())
        self.assert_import_agent(r.stdout + r.stderr)
        # The reconnect agent stays retired: the app reconnects while it runs.
        self.assertFalse((self.home / 'Library/LaunchAgents/io.github.dgitco.velcro.plist').exists())

    def test_command_line_install_still_has_both_agents(self):
        r = self.velcro(ROOT / 'velcro', 'install')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_import_agent(r.stdout + r.stderr)
        self.assertTrue((self.home / 'Library/LaunchAgents/io.github.dgitco.velcro.plist').is_file())
        # The installed CRM entry point must remain importable away from the checkout.
        for name in ('velcro-crm', 'velcro_crm.py'):
            self.assertTrue((self.home / '.local/bin' / name).is_file())
        r = self.velcro(self.home / '.local/bin/velcro', 'crm', '--help')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_status_says_when_nothing_imports_a_plugged_in_recorder(self):
        conf = self.home / '.config/velcro'
        conf.mkdir(parents=True)
        (conf / 'settings').write_text('recorder=uuid:0000-TEST VOICE\nimport.dest=~/Recordings\n')
        warning = 'nothing imports a recorder plugged in now'
        r = self.velcro(ROOT / 'velcro', 'status')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(warning, r.stdout)
        self.velcro(self.app_copy(), 'install')
        r = self.velcro(ROOT / 'velcro', 'status')
        self.assertNotIn(warning, r.stdout)


    def recorder(self):
        """A folder standing in for a plugged-in recorder, with diskutil reporting its volume ID."""
        conf = self.home / '.config/velcro'
        conf.mkdir(parents=True)
        (conf / 'settings').write_text('recorder=uuid:0000-TEST VOICE\nimport.folder=RECORD\n'
                                       f'import.dest={self.home}/Recordings\n')
        (self.home / 'Recordings').mkdir()
        vol = self.tmp / 'VOICE'
        (vol / 'RECORD').mkdir(parents=True)
        (vol / 'RECORD/20261110115038.WAV').write_bytes(b'RIFF')
        self.stub('diskutil', 'cat <<EOF\n<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
                              '<key>VolumeUUID</key><string>0000-TEST</string>'
                              '<key>VolumeName</key><string>VOICE</string></dict></plist>\nEOF\n')
        return vol

    def test_import_agent_leaves_importing_to_the_running_app(self):
        # The agent's zsh can't read a removable drive; holding the lock would leave the app waiting.
        self.recorder()
        self.stub('pgrep', 'exit 0\n')  # velcro.app is running
        r = self.velcro(ROOT / 'velcro', 'import', '--auto')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.home / '.config/velcro/.import.lock').exists())

    def test_unreadable_recorder_is_not_reported_as_empty(self):
        vol = self.recorder()
        (vol / 'RECORD').chmod(0)
        self.addCleanup((vol / 'RECORD').chmod, 0o755)
        for _ in range(2):
            self.velcro(ROOT / 'velcro', 'import', str(vol))
        log = (self.home / 'Library/Logs/velcro.log').read_text()
        self.assertNotIn('nothing to import', log)
        self.assertEqual(log.count("import: can't read VOICE"), 2, log)
        # Notified once per plug-in, not on every look.
        self.assertEqual(log.count("Can't import from VOICE"), 1, log)
        (vol / 'RECORD').chmod(0o755)
        self.assertTrue((vol / 'RECORD/20261110115038.WAV').exists())


if __name__ == '__main__':
    unittest.main()
