"""velcro-clip-host: what reaches the server (PNG only), and which xclip / wl-paste calls it answers.

  python3 -m unittest discover -s tests
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
PNG = b'\x89PNG\r\n\x1a\n' + b'fake image body'


class ClipHost(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='velcro-clip-test-'))
        self.addCleanup(shutil.rmtree, self.tmp)
        # The helper as `velcro clip setup` leaves it: ~/.local/bin/velcro-clip-host, links next to it.
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        self.helper = self.bin / 'velcro-clip-host'
        shutil.copy(ROOT / 'velcro-clip-host', self.helper)
        # Where a real xclip / wl-paste would be: they print what they were asked, so a pass-through shows.
        self.real = self.tmp / 'real'
        self.real.mkdir()
        self.env = {
            'PATH': f'{self.bin}:{self.real}:/usr/bin:/bin',
            'HOME': str(self.tmp),
        }

    def run_helper(self, *args, input=None, name='velcro-clip-host'):
        return subprocess.run([str(self.bin / name), *args], input=input, env=self.env, capture_output=True)

    def fake_real_tools(self):
        for tool in ('xclip', 'wl-paste'):
            f = self.real / tool
            f.write_text(f'#!/bin/sh\necho "real {tool} $*"\n')
            f.chmod(0o755)

    def install(self):
        r = self.run_helper('install')
        self.assertEqual(r.returncode, 0, r.stderr)

    def kept(self):
        return self.tmp / '.local' / 'state' / 'velcro' / 'clip' / 'image.png'

    def test_put_keeps_png_only_readable_by_owner(self):
        r = self.run_helper('put', input=PNG)
        self.assertEqual(r.stdout.decode().strip(), 'ok 1')
        self.assertEqual(self.kept().read_bytes(), PNG)
        self.assertEqual(self.kept().stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.kept().parent.stat().st_mode & 0o777, 0o700)

    def test_put_refuses_anything_but_png(self):
        r = self.run_helper('put', input=b'my password')
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.kept().exists())

    def test_claude_code_paste_gets_the_image(self):
        self.install()
        self.run_helper('put', input=PNG)
        # The two calls Claude Code makes for ⌃V on Linux.
        r = self.run_helper('-selection', 'clipboard', '-t', 'TARGETS', '-o', name='xclip')
        self.assertEqual(r.stdout, b'image/png\n')
        r = self.run_helper('-selection', 'clipboard', '-t', 'image/png', '-o', name='xclip')
        self.assertEqual(r.stdout, PNG)
        r = self.run_helper('-l', name='wl-paste')
        self.assertEqual(r.stdout, b'image/png\n')
        r = self.run_helper('--type', 'image/png', name='wl-paste')
        self.assertEqual(r.stdout, PNG)

    def test_everything_else_goes_to_the_real_tool(self):
        self.install()
        self.fake_real_tools()
        self.run_helper('put', input=PNG)
        for name, args in [
            ('xclip', ['-selection', 'clipboard', '-o']),          # text
            ('xclip', ['-selection', 'primary', '-t', 'image/png', '-o']),
            ('xclip', ['-selection', 'clipboard', '-i']),          # copying, not pasting
            ('wl-paste', ['--no-newline']),
        ]:
            r = self.run_helper(*args, name=name)
            self.assertEqual(r.stdout.decode(), f'real {name} {" ".join(args)}\n', args)

    def test_old_image_no_longer_pastes(self):
        self.install()
        self.run_helper('put', input=PNG)
        old = time.time() - 601
        os.utime(self.kept(), (old, old))
        r = self.run_helper('-selection', 'clipboard', '-t', 'TARGETS', '-o', name='xclip')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, b'')
        self.fake_real_tools()
        r = self.run_helper('-selection', 'clipboard', '-t', 'TARGETS', '-o', name='xclip')
        self.assertEqual(r.stdout, b'real xclip -selection clipboard -t TARGETS -o\n')

    def test_no_image_and_no_real_tool_is_an_empty_clipboard(self):
        self.install()
        r = self.run_helper('-selection', 'clipboard', '-t', 'image/png', '-o', name='xclip')
        self.assertEqual((r.returncode, r.stdout), (1, b''))

    def test_install_leaves_other_programs_alone(self):
        other = self.bin / 'xclip'
        other.write_text('#!/bin/sh\n# someone else\n')
        r = self.run_helper('install')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(b'another program', r.stderr)
        self.assertEqual(other.read_text(), '#!/bin/sh\n# someone else\n')
        self.assertFalse((self.bin / 'wl-paste').exists())

    def test_remove_takes_only_its_own_links(self):
        self.install()
        self.run_helper('put', input=PNG)
        (self.bin / 'wl-paste').unlink()
        (self.bin / 'wl-paste').write_text('#!/bin/sh\n')
        self.assertEqual(self.run_helper('remove').returncode, 0)
        self.assertFalse((self.bin / 'xclip').exists())
        self.assertTrue((self.bin / 'wl-paste').exists())
        self.assertFalse(self.kept().exists())


if __name__ == '__main__':
    unittest.main()
