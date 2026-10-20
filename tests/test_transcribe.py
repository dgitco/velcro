"""velcro-transcribe: the login boundary and the voice_stream protocol.

  python3 -m unittest discover -s tests
"""
import importlib.machinery
import sys
sys.dont_write_bytecode = True  # no __pycache__ next to velcro-transcribe
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent


def load_helper():
    loader = importlib.machinery.SourceFileLoader('velcro_transcribe', str(ROOT / 'velcro-transcribe'))
    spec = importlib.util.spec_from_loader('velcro_transcribe', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


vt = load_helper()
SECRET = 'sk-ant-oat01-test-not-a-real-token'


def write_login(folder, expires_ms):
    folder.mkdir(parents=True, exist_ok=True)
    creds = {'claudeAiOauth': {'accessToken': SECRET, 'refreshToken': 'refresh-test', 'expiresAt': expires_ms}}
    (folder / '.credentials.json').write_text(json.dumps(creds))
    return folder / '.credentials.json'


class Login(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.now = time.time()

    def test_first_unexpired_login_wins(self):
        write_login(self.tmp / 'a', (self.now - 60) * 1000)
        write_login(self.tmp / 'b', (self.now + 3600) * 1000)
        token, folder = vt.claude_token([self.tmp / 'missing', self.tmp / 'a', self.tmp / 'b'], now=self.now)
        self.assertEqual((token, folder), (SECRET, str(self.tmp / 'b')))

    def test_expired_login_is_not_renewed_and_not_shown(self):
        path = write_login(self.tmp / 'a', (self.now + 60) * 1000)  # inside the 2 minute margin
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with self.assertRaises(vt.Failed) as caught:
            vt.claude_token([self.tmp / 'a'], now=self.now)
        message = str(caught.exception)
        self.assertIn('expired', message)
        self.assertNotIn(SECRET, message)
        self.assertNotIn('refresh-test', message)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_no_login(self):
        with self.assertRaises(vt.Failed) as caught:
            vt.claude_token([self.tmp / 'nothing'], now=self.now)
        self.assertIn('no Claude Code login', str(caught.exception))

    def test_only_the_folders_given_are_read(self):
        asked = []
        vt.claude_token([self.tmp / 'x'], now=self.now, read=lambda d: asked.append(d) or
                        {'accessToken': SECRET, 'expiresAt': (self.now + 3600) * 1000})
        self.assertEqual(asked, [self.tmp / 'x'])


class Protocol(unittest.TestCase):
    def test_frames_round_trip(self):
        for size in (0, 125, 126, 65535, 65536):
            payload = os.urandom(size)
            fin, op, got, used = vt.ws_parse(vt.ws_frame(0x2, payload, mask=b'\x01\x02\x03\x04'))
            self.assertEqual((fin, op, got), (True, 0x2, payload))
            self.assertIsNone(vt.ws_parse(vt.ws_frame(0x2, payload)[:-1] if size else b'\x82'))

    def test_transcript_follows_claude_code(self):
        tr = vt.Transcript()
        for msg in ({'type': 'TranscriptInterim', 'data': 'hel'}, {'type': 'TranscriptText', 'data': 'hello'},
                    {'type': 'TranscriptEndpoint'}, {'type': 'TranscriptInterim', 'data': 'wor'}):
            tr.feed(json.dumps(msg))
        tr.feed(json.dumps({'type': 'TranscriptText', 'data': 'world'}), closing=True)
        tr.feed(json.dumps({'type': 'TranscriptEndpoint'}), closing=True)
        self.assertEqual(tr.text(), 'hello world')
        self.assertTrue(tr.endpoint_after_close)

    def test_server_error_is_kept(self):
        tr = vt.Transcript()
        tr.feed(json.dumps({'type': 'TranscriptError', 'error_code': 'bad_audio'}))
        self.assertEqual((tr.text(), tr.error), ('', 'bad_audio'))

    def test_afconvert_wav_data_chunk(self):
        audio = os.urandom(1001)
        body = (b'WAVE' + b'fmt ' + struct.pack('<I', 40) + bytes(40) + b'FLLR' + struct.pack('<I', 3) + b'abc\0'
                + b'data' + struct.pack('<I', len(audio)) + audio)
        with tempfile.NamedTemporaryFile(suffix='.wav') as f:
            f.write(b'RIFF' + struct.pack('<I', len(body)) + body)
            f.flush()
            self.assertEqual(vt.riff_data(f.name), audio)

    def test_query_asks_for_16k_mono_pcm(self):
        q = dict(p.split('=') for p in vt.voice_query('ko').split('&'))
        self.assertEqual((q['encoding'], q['sample_rate'], q['channels'], q['language']), ('linear16', '16000', '1', 'ko'))


if __name__ == '__main__':
    unittest.main()
