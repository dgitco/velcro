"""velcro transcribe: the login boundaries, the voice_stream and ChatGPT protocols, engine order, and the fallback contract.

  python3 -m unittest discover -s tests
"""
import importlib.machinery
import sys
sys.dont_write_bytecode = True  # no __pycache__ next to velcro-transcribe
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
import unittest.mock
import urllib.error
import wave

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
        vt.claude_token([self.tmp / 'x'], now=self.now, sources=[lambda d: asked.append(d) or
                        {'accessToken': SECRET, 'expiresAt': (self.now + 3600) * 1000}])
        self.assertEqual(asked, [self.tmp / 'x'])

    def test_stale_file_does_not_hide_the_keychain_login(self):
        stale = {'accessToken': 'old', 'expiresAt': (self.now - 60) * 1000}
        fresh = {'accessToken': SECRET, 'expiresAt': (self.now + 3600) * 1000}
        got = vt.claude_token([self.tmp / 'x'], now=self.now, sources=[lambda d: stale, lambda d: fresh])
        self.assertEqual(got[0], SECRET)


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


def jwt(claims):
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b'=').decode()
    return f"{part({'alg': 'RS256'})}.{part(claims)}.c2lnbmF0dXJl"


CODEX_SECRET_CLAIMS = {'exp': 0, 'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-from-jwt'}}


def write_codex(home, exp, account='acct-123', mode='chatgpt', api_key=None, tokens=True):
    home.mkdir(parents=True, exist_ok=True)
    token = jwt({**CODEX_SECRET_CLAIMS, 'exp': exp})
    auth = {'auth_mode': mode, 'OPENAI_API_KEY': api_key, 'last_refresh': '2026-10-16T00:00:00Z'}
    if tokens:
        auth['tokens'] = {'access_token': token, 'refresh_token': 'codex-refresh-test', 'id_token': 'id-test',
                          **({'account_id': account} if account else {})}
    (home / 'auth.json').write_text(json.dumps(auth))
    return home / 'auth.json', token


class CodexLogin(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.now = time.time()

    def test_chatgpt_login_is_read(self):
        _, token = write_codex(self.tmp, self.now + 3600)
        self.assertEqual(vt.codex_login(self.tmp, now=self.now), (token, 'acct-123'))

    def test_account_from_the_token_when_the_file_has_none(self):
        _, token = write_codex(self.tmp, self.now + 3600, account=None)
        self.assertEqual(vt.codex_login(self.tmp, now=self.now), (token, 'acct-from-jwt'))

    def test_expired_login_is_not_renewed_and_not_shown(self):
        path, token = write_codex(self.tmp, self.now + 60)  # inside the 2 minute margin
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with self.assertRaises(vt.Failed) as caught:
            vt.codex_login(self.tmp, now=self.now)
        message = str(caught.exception)
        self.assertIn('expired', message)
        for secret in (token, 'codex-refresh-test', 'id-test'):
            self.assertNotIn(secret, message)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_api_key_is_never_used(self):
        write_codex(self.tmp, self.now + 3600, mode='apikey', api_key='sk-proj-test', tokens=False)
        with self.assertRaises(vt.Failed) as caught:
            vt.codex_login(self.tmp, now=self.now)
        self.assertIn('not a ChatGPT sign-in', str(caught.exception))
        self.assertNotIn('sk-proj-test', str(caught.exception))

    def test_no_login(self):
        with self.assertRaises(vt.Failed) as caught:
            vt.codex_login(self.tmp / 'nothing', now=self.now)
        self.assertIn('no Codex login', str(caught.exception))


class FakeReply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ChatGPT(unittest.TestCase):
    def test_request_carries_the_login_and_the_file(self):
        sent = []
        def opener(req, timeout):
            sent.append(req)
            return FakeReply(json.dumps({'text': ' 안녕하세요 ', 'asset_ttl': '30d'}).encode())
        text = vt.chatgpt_transcribe('tok', 'acct', 'memo.wav', 'audio/wav', b'AUDIO', opener=opener)
        self.assertEqual(text, '안녕하세요')
        req = sent[0]
        self.assertEqual((req.full_url, req.get_method()), (vt.CHATGPT_TRANSCRIBE, 'POST'))
        self.assertEqual((req.get_header('Authorization'), req.get_header('Chatgpt-account-id')), ('Bearer tok', 'acct'))
        self.assertIn(b'name="file"; filename="memo.wav"\r\nContent-Type: audio/wav\r\n\r\nAUDIO\r\n', req.data)

    def test_refusal_is_a_failure_without_the_token(self):
        def opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, 'no', {}, io.BytesIO(b'{"detail":"bad token"}'))
        with self.assertRaises(vt.Failed) as caught:
            vt.chatgpt_transcribe('tok-secret', 'acct', 'm.wav', 'audio/wav', b'x', opener=opener)
        self.assertIn('401', str(caught.exception))
        self.assertNotIn('tok-secret', str(caught.exception))

    def test_long_audio_goes_in_pieces_cut_where_it_is_quiet(self):
        loud = struct.pack('<h', 8000) * vt.RATE
        quiet_at = 290  # seconds: a quiet second inside the last 15 s of the first 5 minutes
        pcm = b''.join(bytes(2 * vt.RATE) if s == quiet_at else loud for s in range(700))
        parts = vt.pieces(pcm)
        self.assertEqual(b''.join(parts), pcm)
        self.assertTrue(all(len(p) <= 2 * vt.RATE * vt.PIECE_S for p in parts))
        self.assertEqual(len(parts), 3)
        self.assertTrue(quiet_at <= len(parts[0]) / 2 / vt.RATE < quiet_at + 1)

    def test_a_sliver_at_the_end_is_not_a_piece_of_its_own(self):
        pcm = bytes(2 * (vt.PIECE_S * vt.RATE + 100))
        self.assertEqual([len(p) for p in vt.pieces(pcm, quiet=0)], [len(pcm)])

    def test_pieces_are_16k_wavs(self):
        with wave.open(io.BytesIO(vt.wav_bytes(b'\x01\x00' * 160))) as w:
            self.assertEqual((w.getnchannels(), w.getframerate(), w.getsampwidth(), w.getnframes()), (1, 16000, 2, 160))

    def test_without_ffmpeg_a_short_common_file_goes_whole(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        write_codex(tmp / 'codex', time.time() + 3600)
        sent = []
        for name, ok in (('memo.ogg', True), ('memo.wma', False)):
            (tmp / name).write_bytes(b'OggS...')
            audio = vt.Audio(tmp / name)
            audio._failed = vt.Failed('this file needs ffmpeg to read it')
            send = lambda *a: sent.append(a[2:5]) or 'text'
            if ok:
                with unittest.mock.patch('sys.stderr', io.StringIO()):
                    self.assertEqual(vt.codex_transcribe(audio, tmp / 'codex', send=send), 'text')
            else:
                self.assertRaises(vt.Failed, vt.codex_transcribe, audio, tmp / 'codex', send=send)
        self.assertEqual(sent, [('memo.ogg', 'audio/ogg', b'OggS...')])


class Ffmpeg(unittest.TestCase):
    """A file on a share is decoded as audio only: no playlists, no concat lists, nothing but local files."""

    def test_ffmpeg_reads_only_local_audio_formats(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / 'bin').mkdir()
        argv_file = tmp / 'argv.json'
        stub = tmp / 'bin' / 'ffmpeg'
        stub.write_text(f'#!{sys.executable}\nimport json, sys\nopen({str(argv_file)!r}, "w").write(json.dumps(sys.argv[1:]))\n'
                        'open(sys.argv[-1], "wb").write(b"\\0\\0")\n')
        stub.chmod(0o755)
        memo = tmp / 'memo.m3u8'
        memo.write_text('#EXTM3U\n/etc/hostname\n')
        with unittest.mock.patch.dict(os.environ, {'PATH': f"{tmp / 'bin'}{os.pathsep}{os.environ['PATH']}"}):
            self.assertEqual(vt.to_pcm(memo), b'\0\0')
        argv = json.loads(argv_file.read_text())
        i = argv.index('-i')
        opts = dict(zip(argv[:i], argv[1:i]))
        self.assertEqual(opts.get('-protocol_whitelist'), 'file')
        allowed = set(opts.get('-format_whitelist', '').split(','))
        self.assertTrue({'wav', 'mp3', 'mov', 'ogg'} <= allowed, allowed)
        self.assertFalse(allowed & {'hls', 'concat', 'applehttp', 'ffconcat', 'image2', 'lavfi'}, allowed)
        self.assertEqual(argv[i + 1], f'file:{memo}')


class Engines(unittest.TestCase):
    def test_first_engine_with_text_wins_in_order(self):
        tried = []
        def ok(name):
            return lambda audio: tried.append(name) or f'from {name}'
        def no(name, why):
            def run(audio):
                tried.append(name)
                raise vt.Failed(why)
            return run
        with unittest.mock.patch('sys.stderr', io.StringIO()):
            got = vt.run_engines(None, ['claude', 'codex'], {'claude': no('claude', 'no Claude Code login'), 'codex': ok('codex')})
        self.assertEqual((got, tried), (('codex', 'from codex'), ['claude', 'codex']))
        tried.clear()
        self.assertEqual(vt.run_engines(None, ['codex', 'claude'], {'codex': ok('codex'), 'claude': ok('claude')}),
                         ('codex', 'from codex'))
        self.assertEqual(tried, ['codex'])

    def test_every_reason_when_none_works(self):
        runs = {'claude': lambda a: (_ for _ in ()).throw(vt.Failed('claude expired')), 'codex': lambda a: '  '}
        with unittest.mock.patch('sys.stderr', io.StringIO()), self.assertRaises(vt.Failed) as caught:
            vt.run_engines(None, ['claude', 'codex'], runs)
        self.assertIn('claude: claude expired', str(caught.exception))
        self.assertIn('codex: no speech heard', str(caught.exception))

    def test_engine_list(self):
        self.assertEqual(vt.engine_list('codex,claude'), ['codex', 'claude'])
        for bad in ('', 'whisper', 'claude,claude'):
            self.assertRaises(Exception, vt.engine_list, bad)


class Fallback(unittest.TestCase):
    """velcro transcribe with a stand-in helper: what the fallback setting does."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        shutil.copy(ROOT / 'velcro', self.tmp / 'velcro')
        self.helper = self.tmp / 'velcro-transcribe'
        self.audio = self.tmp / 'memo.m4a'
        self.audio.write_bytes(b'not really audio')
        self.fallback = self.tmp / 'other-engine'
        self.fallback.write_text('#!/bin/sh\necho "from the fallback: $*"\n')
        self.fallback.chmod(0o755)
        self.env = {**os.environ, 'XDG_CONFIG_HOME': str(self.tmp / 'config')}
        self.env.pop('VELCRO_APP', None)

    def helper_says(self, code, text='', engine='claude'):
        self.helper.write_text(f'import sys\nprint({text!r})\nprint("velcro-transcribe: helper said no", file=sys.stderr)\n'
                               f'a = sys.argv\nopen(a[a.index("--engine-file") + 1], "w").write({engine!r})\n'
                               f'sys.exit({code})\n')

    def helper_args(self):
        """A stand-in that prints its arguments, without --engine-file and its (temporary) path."""
        self.helper.write_text('import sys, json\na = sys.argv[1:]\ni = a.index("--engine-file")\n'
                               'print(json.dumps(a[:i] + a[i + 2:]))\n')

    def velcro(self, *args):
        return subprocess.run(['zsh', str(self.tmp / 'velcro'), *args], env=self.env, capture_output=True,
                              text=True, timeout=30)

    def test_claude_text_is_printed(self):
        self.helper_says(0, 'hello')
        r = self.velcro('transcribe', '--json', str(self.audio))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {'engine': 'claude', 'file': str(self.audio), 'text': 'hello'})

    def test_without_fallback_the_failure_is_reported(self):
        self.helper_says(1)
        r = self.velcro('transcribe', str(self.audio))
        self.assertEqual((r.returncode, r.stdout), (1, ''))
        self.assertIn('helper said no', r.stderr)

    def test_fallback_runs_with_the_file_when_claude_fails(self):
        self.helper_says(1)
        self.velcro('set', 'transcribe.fallback', f'{self.fallback} --some-flag')
        r = self.velcro('transcribe', '--json', str(self.audio))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['engine'], 'fallback')
        self.assertEqual(json.loads(r.stdout)['text'], f'from the fallback: --some-flag {self.audio}')
        self.assertIn('helper said no', r.stderr)

    def test_no_fallback_flag_skips_it(self):
        self.helper_says(1)
        self.velcro('set', 'transcribe.fallback', str(self.fallback))
        r = self.velcro('transcribe', '--no-fallback', str(self.audio))
        self.assertEqual((r.returncode, r.stdout), (1, ''))

    def test_fallback_not_used_when_claude_works(self):
        self.helper_says(0, 'hello')
        self.velcro('set', 'transcribe.fallback', str(self.fallback))
        r = self.velcro('transcribe', str(self.audio))
        self.assertEqual(r.stdout, 'hello\n')

    def test_bad_input_is_a_usage_error(self):
        self.helper_says(0, 'hello')
        for args in (['transcribe', '--lang'], ['transcribe', '--lang', str(self.audio)],
                     ['set', 'transcribe.fallback', 'whisper-cli', '-nt', '-f']):
            self.assertEqual(self.velcro(*args).returncode, 2, args)

    def test_settings_passed_to_the_helper(self):
        self.helper_args()
        self.velcro('set', 'transcribe.lang', 'ko')
        self.velcro('set', 'transcribe.login', '~/.claude-work', '/srv/claude')
        self.velcro('set', 'transcribe.codex', '~/.codex-work')
        r = self.velcro('transcribe', str(self.audio))
        home = os.environ['HOME']
        self.assertEqual(json.loads(r.stdout), ['--lang', 'ko', '--engine', 'claude,codex', '--login', f'{home}/.claude-work',
                                                '--login', '/srv/claude', '--codex', f'{home}/.codex-work', '--', str(self.audio)])

    def test_engine_order_setting_and_flag(self):
        self.helper_args()
        engine = lambda *args: json.loads(self.velcro('transcribe', *args, str(self.audio)).stdout)[3]
        self.assertEqual(engine(), 'claude,codex')
        self.assertEqual(self.velcro('set', 'transcribe.engine', 'codex', 'claude').returncode, 0)
        self.assertEqual(engine(), 'codex,claude')
        self.velcro('set', 'transcribe.engine', 'claude')
        self.assertEqual(engine(), 'claude')
        self.assertEqual(engine('--engine', 'codex'), 'codex')
        self.velcro('set', '--add', 'transcribe.engine', 'claude')
        self.assertEqual(engine(), 'claude')
        for bad in (['whisper'], ['claude', 'claude'], ['claude codex']):
            self.assertEqual(self.velcro('set', 'transcribe.engine', *bad).returncode, 2, bad)
        self.assertEqual(self.velcro('transcribe', '--engine', 'both', str(self.audio)).returncode, 2)

    def test_engine_file_lives_in_a_folder_of_its_own(self):
        """The helper's engine file was <random temp file>.engine: a name anyone could guess once the temp file showed up."""
        tmpdir = self.tmp / 'tmp'
        tmpdir.mkdir()
        self.env['TMPDIR'] = str(tmpdir)
        seen = self.tmp / 'seen.json'
        self.helper.write_text('import json, os, stat, sys\na = sys.argv\nf = a[a.index("--engine-file") + 1]\n'
                               'd = os.path.dirname(f)\nopen(f, "w").write("codex")\n'
                               f'open({str(seen)!r}, "w").write(json.dumps([d, stat.S_IMODE(os.stat(d).st_mode)]))\n'
                               'print("hello")\n')
        r = self.velcro('transcribe', '--json', str(self.audio))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['engine'], 'codex')
        folder, mode = json.loads(seen.read_text())
        self.assertEqual(Path(folder).parent, tmpdir)
        self.assertEqual(mode, 0o700)
        self.assertEqual(list(tmpdir.iterdir()), [])  # the folder is gone afterwards

    def test_engine_that_answered_is_in_the_json(self):
        self.helper_says(0, 'hello', engine='codex')
        r = self.velcro('transcribe', '--json', str(self.audio))
        self.assertEqual(json.loads(r.stdout)['engine'], 'codex')


if __name__ == '__main__':
    unittest.main()


class StreamEnd(unittest.TestCase):
    """How voice_stream ends decides whether the text is whole: a server error close is a failure (so the
    next engine, or the next recordings run, gets the whole file), a normal close is not."""

    def fake_ws(self, code):
        class FakeWS:
            close_code = None

            def __init__(self, *a, **k):
                # Text of the first part, then the stream ends before the final endpoint.
                self.msgs = [json.dumps({'type': 'TranscriptText', 'data': 'first half'})]
                self.closed = False

            def send_text(self, s):
                self.ended = self.ended or 'CloseStream' in s

            def send_binary(self, b):
                pass

            ended = False

            def recv(self, timeout):
                if not self.ended:
                    return None
                if self.msgs:
                    return self.msgs.pop(0)
                self.closed, self.close_code = True, code
                raise EOFError(f'closed (code {code})')

            def close(self):
                pass
        return FakeWS

    def run_stream(self, code):
        with unittest.mock.patch.object(vt, 'WS', self.fake_ws(code)), \
             unittest.mock.patch.object(vt, 'claude_token', return_value=('t', 'login')), \
             unittest.mock.patch.object(vt, 'claude_version', return_value='1'), \
             unittest.mock.patch.object(vt, 'say'):
            return vt.transcribe(b'\0\0' * vt.RATE, [], 'en', pace=1000)

    def test_server_error_close_is_a_failure(self):
        with self.assertRaises(vt.Failed):
            self.run_stream(1011)

    def test_normal_close_keeps_the_text(self):
        for code in (1000, 1005):
            self.assertEqual(self.run_stream(code), 'first half')
