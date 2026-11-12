"""CRM HTTP contracts and permission boundaries; synthetic files, local HTTP only."""
import base64
import hashlib
import contextlib
import ctypes
import datetime
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest import mock
import wave
from http.server import BaseHTTPRequestHandler
import http.client
import types
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'windows'))
import velcro_crm as crm
import velcro_win as win
import crm_window

TOKEN = 'example_synthetic_test_credential'
SECRET = 'synthetic-device-secret'
TOKEN_EXPIRY = '2099-01-01T00:00:00Z'
LEAD = 'lead-synthetic'
FILE = 'file-synthetic'
BASE = '/api/rpc/sales/leads/' + LEAD + '/files'


class FakeStore:
    def __init__(self, token=TOKEN):
        self.token = token
        self.expires_at = None

    def get(self):
        return self.token

    def set(self, token, expires_at=None):
        self.token = token
        self.expires_at = expires_at

    def delete(self):
        self.token = None
        self.expires_at = None


class Clock:
    def __init__(self):
        self.at, self.sleeps = 0, []

    def time(self):
        return self.at

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.at += seconds


class CRMTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / 'silence.wav'
        with wave.open(str(self.file), 'wb') as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(8000)
            f.writeframes(b'\0\0' * 8000)
        self.calls, self.responses = [], {}
        test = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def handle_request(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                data = json.loads(raw) if raw and self.headers.get('Content-Type') == 'application/json' else raw
                test.calls.append((self.command, self.path, data, dict(self.headers)))
                replies = test.responses.get((self.command, self.path), [])
                status, body, headers = replies.pop(0) if replies else (500, {'error': 'unplanned'}, {})
                if isinstance(body, dict) or isinstance(body, list):
                    body = json.dumps(body).encode()
                elif isinstance(body, str):
                    body = body.encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                if body:
                    self.wfile.write(body)
            do_GET = do_POST = do_PUT = handle_request
        # The managed executor denies even AF_INET loopback sockets. Keep real
        # HTTP serialization/parsing, using an in-memory wire to a fake server.
        class Incoming:
            def __init__(self, request):
                self.request, self.response = io.BytesIO(request), io.BytesIO()
            def makefile(self, *args):
                return self.request
            def sendall(self, data):
                self.response.write(data)

        class Wire:
            def __init__(self):
                self.request = bytearray()
            def sendall(self, data):
                self.request.extend(data)
            def makefile(self, *args):
                incoming = Incoming(bytes(self.request))
                Handler(incoming, ('127.0.0.1', 1), types.SimpleNamespace())
                return io.BytesIO(incoming.response.getvalue())
            def close(self):
                pass

        class Connection(http.client.HTTPConnection):
            def connect(self):
                self.sock = Wire()

        class HTTPHandler(urllib.request.HTTPHandler):
            def http_open(self, request):
                return self.do_open(Connection, request)

        self.url = 'http://127.0.0.1:9876'
        self.clock, self.store = Clock(), FakeStore()
        self.client = crm.Client(self.url, self.store, self.root / 'state', self.clock.sleep, self.clock.time,
                                 wall_clock=self.clock.time)
        self.client.opener = urllib.request.build_opener(HTTPHandler(), crm.NoRedirect())
        settings = mock.patch.object(crm, 'configured_setting', return_value='')
        settings.start()
        self.addCleanup(settings.stop)

    def reply(self, method, path, status, body=b'', headers=None):
        self.responses.setdefault((method, path), []).append((status, body, headers or {}))

    def checksum(self, path=None):
        return base64.b64encode(hashlib.sha256(Path(path or self.file).read_bytes()).digest()).decode()

    def created(self, checksum=None, **changes):
        headers = {'X-Signed': 'synthetic', 'x-amz-checksum-sha256': checksum or self.checksum()}
        body = {'file': {'id': FILE}, 'upload': {'url': self.url + '/storage', 'headers': headers}}
        body.update(changes)
        self.reply('POST', BASE, 201, body)

    def complete(self, status=204, body=b''):
        self.reply('POST', BASE + '/' + FILE + '/complete', status, body)

    def start(self, interval=1, expires=10, user='SYNTH000', **changes):
        self.store.delete()
        body = {'deviceSecret': SECRET, 'userCode': user, 'interval': interval,
                'expiresAt': datetime.datetime.fromtimestamp(self.clock.at + expires, datetime.timezone.utc).isoformat()}
        body.update(changes)
        self.reply('POST', '/api/operator-auth/device/start', 201, body)

    def approved(self, **changes):
        body = {'status': 'approved', 'token': TOKEN, 'expiresAt': TOKEN_EXPIRY}
        body.update(changes)
        self.reply('POST', '/api/operator-auth/device/poll', 200, body)

    def test_connect_pending_approved_whoami_and_no_token_output(self):
        self.start()
        self.reply('POST', '/api/operator-auth/device/poll', 200, {'status': 'pending'})
        self.approved()
        self.reply('GET', '/api/rpc/session/whoami', 200, {'scope': 'sales_file_upload'})
        output, browser = [], []
        with mock.patch.object(crm, 'configured_setting', return_value='https://auth.example.com/device/'):
            self.client.connect('Synthetic desktop', output.append, browser.append)
        self.assertEqual(self.store.get(), TOKEN)
        self.assertEqual(self.store.expires_at, TOKEN_EXPIRY)
        self.assertEqual(self.clock.sleeps, [1, 1])
        self.assertEqual(self.calls[0][2], {'scope': 'sales_file_upload', 'displayName': 'Synthetic desktop'})
        for call in self.calls[1:3]:
            self.assertEqual(call[2], {'userCode': 'SYNTH000', 'deviceSecret': SECRET})
            self.assertNotIn('Authorization', call[3])
        self.assertNotIn('Authorization', self.calls[0][3])
        self.assertEqual(browser, ['https://auth.example.com/device/?code=SYNT-H000'])
        self.assertIn(browser[0], '\n'.join(output))
        self.assertIn('SYNT-H000', '\n'.join(output))
        self.assertNotIn(TOKEN, '\n'.join(output))
        self.assertNotIn(SECRET, '\n'.join(output))
        self.assertEqual(self.calls[-1][3]['Authorization'], 'Bearer ' + TOKEN)

    def test_connect_expired_and_denied(self):
        for status, message in [('expired', '코드가 만료됐어요, 다시 연결하세요'), ('denied', '승인이 거절됐어요')]:
            with self.subTest(status=status):
                self.start()
                self.reply('POST', '/api/operator-auth/device/poll', 200, {'status': status})
                with self.assertRaisesRegex(crm.CRMError, message):
                    self.client.connect(output=lambda _: None, browser=lambda _: None)
                self.assertIsNone(self.store.get())

    def test_connect_deadline_uses_absolute_start_expiry(self):
        self.start(interval=3, expires=8)
        for _ in range(2):
            self.reply('POST', '/api/operator-auth/device/poll', 200, {'status': 'pending'})
        with self.assertRaisesRegex(crm.CRMError, '만료'):
            self.client.connect(output=lambda _: None, browser=lambda _: None)
        self.assertEqual(self.clock.sleeps, [3, 3, 2])
        self.assertEqual(len(self.calls), 3)
        self.assertIsNone(self.store.get())

    def test_connect_failed_whoami_drops_credential(self):
        self.start()
        self.approved()
        self.reply('GET', '/api/rpc/session/whoami', 401, {'error': TOKEN})
        with self.assertRaisesRegex(crm.CRMError, '다시 연결'):
            self.client.connect(output=lambda _: None, browser=lambda _: None)
        self.assertIsNone(self.store.get())

    def test_connect_unknown_code_or_wrong_secret_is_expired(self):
        self.start()
        self.reply('POST', '/api/operator-auth/device/poll', 404, {'error': SECRET})
        with self.assertRaisesRegex(crm.CRMError, '코드가 만료됐어요, 다시 연결하세요') as raised:
            self.client.connect(output=lambda _: None, browser=lambda _: None)
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertIsNone(self.store.get())

    def test_connect_without_approval_url_does_not_open_browser(self):
        self.start(user='SHORT')
        self.reply('POST', '/api/operator-auth/device/poll', 200, {'status': 'denied'})
        output, browser = [], []
        with self.assertRaises(crm.CRMError):
            self.client.connect(output=output.append, browser=browser.append)
        self.assertEqual(browser, [])
        self.assertIn('SHORT', '\n'.join(output))
        self.assertIn('승인 화면에서 이 코드를 넣으세요', output)
        self.assertEqual(self.calls[0][2], {'scope': 'sales_file_upload'})

    def test_connect_rejects_invalid_device_name_before_request(self):
        for name in ('', 'a' * 81):
            with self.subTest(name=name), self.assertRaises(crm.CRMError):
                self.client.connect(name)
        self.assertEqual(self.calls, [])

    def test_connect_rejects_malformed_start_without_leaking_secret(self):
        for fields in ({'expiresAt': SECRET}, {'interval': 0}, {'deviceSecret': None}, {'userCode': None}):
            with self.subTest(fields=fields):
                self.start(**fields)
                with self.assertRaises(crm.CRMError) as raised:
                    self.client.connect(output=lambda _: None)
                self.assertNotIn(SECRET, str(raised.exception))
                self.assertIsNone(self.store.get())

    def test_connect_rejects_invalid_approved_response(self):
        for body in ({'status': 'approved'}, {'status': 'pending', 'token': TOKEN},
                     {'status': 'unexpected', 'token': TOKEN}, {'status': 'approved', 'token': TOKEN, 'expiresAt': SECRET}):
            with self.subTest(body=body):
                self.start(expires=2)
                self.reply('POST', '/api/operator-auth/device/poll', 200, body)
                with self.assertRaises(crm.CRMError) as raised:
                    self.client.connect(output=lambda _: None)
                self.assertNotIn(TOKEN, str(raised.exception))
                self.assertNotIn(SECRET, str(raised.exception))
                self.assertIsNone(self.store.get())

    def test_connect_approved_without_optional_token_expiry(self):
        self.start()
        self.reply('POST', '/api/operator-auth/device/poll', 200, {'status': 'approved', 'token': TOKEN})
        self.reply('GET', '/api/rpc/session/whoami', 200, {})
        self.client.connect(output=lambda _: None)
        self.assertEqual(self.store.get(), TOKEN)
        self.assertIsNone(self.store.expires_at)

    def test_card_id_query_url_and_path(self):
        for value in (LEAD, 'https://auth.example.com/sales?lead=' + LEAD,
                      'https://auth.example.com/sales/leads/' + LEAD,
                      '/sales?lead=' + LEAD, '/sales/leads/' + LEAD):
            with self.subTest(value=value):
                self.assertEqual(self.client.lead_id(value), LEAD)
        for value in ('https://auth.example.com/sales?lead=..%2Fsecret', '/sales?lead=a&lead=b'):
            with self.subTest(value=value), self.assertRaises(crm.CRMError):
                self.client.lead_id(value)

    def test_expired_stored_token_requests_reconnection_without_http(self):
        self.client.store = crm.TokenStore(self.url, self.root / 'credentials')
        self.client.store.set(TOKEN, '2000-01-01T00:00:00Z')
        with self.assertRaisesRegex(crm.CRMError, r'다시 연결하세요\(velcro crm connect\)'):
            self.client.send(self.file, LEAD)
        self.assertEqual(self.calls, [])

    def test_disconnect_is_local_only(self):
        self.assertIn('대시보드', self.client.disconnect())
        self.assertIsNone(self.store.get())
        self.assertEqual(self.calls, [])

    def test_send_upload_complete_history_and_local_dedup(self):
        self.created()
        self.reply('PUT', '/storage', 200)
        self.complete()
        result = self.client.send(self.file, 'https://api.example.com/sales/leads/' + LEAD)
        self.assertFalse(result['already'])
        self.assertEqual([x[:2] for x in self.calls], [('POST', BASE), ('PUT', '/storage'), ('POST', BASE + '/' + FILE + '/complete')])
        self.assertEqual(self.calls[0][2], {'extension': 'wav', 'sizeBytes': self.file.stat().st_size, 'sha256': result['sha256']})
        self.assertEqual(self.calls[1][2], self.file.read_bytes())
        self.assertNotIn('Authorization', self.calls[1][3])
        self.assertEqual(self.calls[1][3]['X-Signed'], 'synthetic')
        self.assertEqual({k.lower(): v for k, v in self.calls[1][3].items()}['x-amz-checksum-sha256'], self.checksum())
        history = list(crm.records(self.client.state / 'crm-sent.jsonl'))
        self.assertEqual(history, [result])
        self.assertEqual(set(result), {'sha256', 'leadId', 'fileId', 'at', 'already', 'url'})
        self.assertTrue(self.client.send(self.file, LEAD)['already'])
        self.assertEqual(len(self.calls), 3)

    def test_signed_put_must_carry_this_files_checksum(self):
        # A create answer whose checksum header is missing or belongs to other bytes is refused before any PUT.
        self.reply('POST', BASE, 201, {'file': {'id': FILE}, 'upload': {'url': self.url + '/storage', 'headers': {'X-Signed': 'synthetic'}}})
        with self.assertRaisesRegex(crm.CRMError, '생성 응답'):
            self.client.send(self.file, LEAD)
        self.created(checksum=base64.b64encode(b'\0' * 32).decode())
        with self.assertRaisesRegex(crm.CRMError, '생성 응답'):
            self.client.send(self.file, LEAD)
        self.assertEqual([x[0] for x in self.calls], ['POST', 'POST'])
        self.assertEqual(list(crm.records(self.client.state / 'crm-sent.jsonl')), [])

    def test_create_already_uploaded_skips_put_and_complete(self):
        self.reply('POST', BASE, 200, {'file': {'id': FILE}, 'alreadyUploaded': True})
        result = self.client.send(self.file, LEAD)
        self.assertTrue(result['already'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(list(crm.records(self.client.state / 'crm-sent.jsonl'))), 1)

    def test_complete_race_uses_existing_ready_file(self):
        self.created()
        self.reply('PUT', '/storage', 200)
        self.complete(200, {'file': {'id': 'file-existing'}, 'alreadyUploaded': True})
        result = self.client.send(self.file, LEAD)
        self.assertEqual(result['fileId'], 'file-existing')
        self.assertTrue(result['already'])

    def test_malformed_create_and_complete_never_record_success(self):
        self.reply('POST', BASE, 200, {'file': {'id': FILE}, 'upload': {'url': self.url + '/storage', 'headers': {}}})
        with self.assertRaisesRegex(crm.CRMError, '생성 응답'):
            self.client.send(self.file, LEAD)
        self.assertEqual(len(self.calls), 1)
        self.created()
        self.reply('PUT', '/storage', 200)
        self.complete(200, {'file': {'id': FILE}})
        with self.assertRaisesRegex(crm.CRMError, '완료 응답'):
            self.client.send(self.file, LEAD)
        self.assertEqual(list(crm.records(self.client.state / 'crm-sent.jsonl')), [])

    def test_expired_signed_url_restarts_create(self):
        self.created()
        self.reply('PUT', '/storage', 403, '<Error><Code>ExpiredToken</Code></Error>')
        self.created()
        self.reply('PUT', '/storage', 200)
        self.complete()
        self.client.send(self.file, LEAD)
        self.assertEqual([x[0] for x in self.calls], ['POST', 'PUT', 'POST', 'PUT', 'POST'])
        self.assertEqual(len(list(crm.records(self.client.state / 'crm-sent.jsonl'))), 1)

    def test_put_and_complete_retry_schedule(self):
        self.created()
        for status in (503, 503, 503, 200):
            self.reply('PUT', '/storage', status)
        for status in (503, 503, 503, 204):
            self.complete(status)
        self.client.send(self.file, LEAD)
        self.assertEqual(self.clock.sleeps, [1, 4, 16, 1, 4, 16])
        self.assertEqual(len(self.calls), 9)

    def test_failed_complete_does_not_record_success(self):
        self.created()
        self.reply('PUT', '/storage', 200)
        for _ in range(4):
            self.complete(503, {'error': TOKEN})
        with self.assertRaisesRegex(crm.CRMError, '재시도') as raised:
            self.client.send(self.file, LEAD)
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertEqual(list(crm.records(self.client.state / 'crm-sent.jsonl')), [])

    def test_401_403_safe_errors_at_create_and_complete(self):
        for phase in ('create', 'complete'):
            for status, message in [(401, '다시 연결'), (403, '이 카드에 올릴 권한 없음')]:
                with self.subTest(phase=phase, status=status):
                    if phase == 'complete':
                        self.created()
                        self.reply('PUT', '/storage', 200)
                        self.complete(status, {'error': TOKEN})
                    else:
                        self.reply('POST', BASE, status, {'error': TOKEN})
                    with self.assertRaisesRegex(crm.CRMError, message) as raised:
                        self.client.send(self.file, LEAD)
                    self.assertNotIn(TOKEN, str(raised.exception))
        self.assertEqual(self.clock.sleeps, [])

    def test_invalid_extension_size_and_lead_never_contact_server(self):
        unsupported = self.root / 'silence.exe'
        unsupported.write_bytes(b'synthetic')
        with self.assertRaisesRegex(crm.CRMError, '지원 형식'):
            self.client.send(unsupported, LEAD)
        large = self.root / 'large.wav'
        with large.open('wb') as f:
            f.truncate(crm.MAX_BYTES + 1)
        with self.assertRaisesRegex(crm.CRMError, '200 MiB'):
            self.client.send(large, LEAD)
        for lead in ('', '../lead', 'https://api.example.com/sales/leads/%2e%2e%2fsecret'):
            with self.assertRaises(crm.CRMError):
                self.client.send(self.file, lead)
        self.assertEqual(self.calls, [])

    def test_no_bearer_redirects(self):
        self.reply('POST', BASE, 307, b'', {'Location': self.url + '/unexpected'})
        with self.assertRaises(crm.CRMError):
            self.client.send(self.file, LEAD)
        self.assertEqual(len(self.calls), 1)

    def test_storage_redirect_not_followed(self):
        self.created()
        self.reply('PUT', '/storage', 307, b'', {'Location': self.url + '/unexpected'})
        with self.assertRaises(crm.CRMError):
            self.client.send(self.file, LEAD)
        self.assertEqual(len(self.calls), 2)

    def test_leads_recent_order_and_unavailable(self):
        self.reply('GET', '/api/rpc/sales/leads/recent', 200, [
            {'id': 'lead-older', 'name': 'Synthetic older', 'stage': 'new', 'updatedAt': '2026-01-01'},
            {'id': LEAD, 'name': 'Synthetic recent', 'stage': 'new', 'updatedAt': '2026-01-02'}])
        self.assertEqual(self.client.leads()[0]['id'], LEAD)
        self.reply('GET', '/api/rpc/sales/leads/recent', 404)
        with self.assertRaisesRegex(crm.CRMError, '카드 주소를 붙여 넣으세요'):
            self.client.leads()

    def test_recent_cards_compare_absolute_update_times(self):
        self.reply('GET', '/api/rpc/sales/leads/recent', 200, [
            {'id': 'lead-older', 'name': 'Synthetic older', 'stage': 'new', 'updatedAt': '2026-01-02T09:00:00+09:00'},
            {'id': LEAD, 'name': 'Synthetic recent', 'stage': 'new', 'updatedAt': '2026-01-02T01:00:00Z'}])
        self.assertEqual(self.client.leads()[0]['id'], LEAD)

    def test_cli_failure_never_prints_token_or_logs_response(self):
        self.reply('POST', BASE, 500, {'error': TOKEN, 'message': TOKEN})
        out, err, logs = io.StringIO(), io.StringIO(), io.StringIO()
        handler = logging.StreamHandler(logs)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        with mock.patch.object(crm, 'Client', return_value=self.client), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = crm.main(['--url', self.url, 'send', str(self.file), '--lead', LEAD])
        self.assertEqual(code, 1)
        self.assertNotIn(TOKEN, out.getvalue() + err.getvalue() + logs.getvalue())

    def test_cli_unexpected_exception_is_redacted(self):
        with mock.patch.object(crm, 'Client', side_effect=RuntimeError(TOKEN)):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(crm.main(['--url', self.url, 'disconnect']), 1)
            self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())

    def test_pending_requires_human_selection_and_skip_never_sends(self):
        pending = crm.Pending(self.url, self.client.state)
        pending.add(self.file, 'synthetic-hash', '2026-01-01T00:00:00Z', 1)
        pending.add(self.file, 'synthetic-hash', '2026-01-01T00:00:00Z', 1)
        item, = pending.items()
        with self.assertRaises(crm.CRMError):
            crm_window.upload_selected(self.client, pending, item, '')
        self.assertEqual(self.calls, [])
        pending.finish(item, skipped=True)
        pending.add(self.file, 'synthetic-hash', '2026-01-01T00:00:00Z', 1)
        self.assertEqual(pending.items(), [])
        self.assertEqual(self.calls, [])

    def test_pending_success_and_failure_boundaries(self):
        pending = crm.Pending(self.url, self.client.state)
        pending.add(self.file, 'synthetic-hash', '2026-01-01T00:00:00Z')
        item, = pending.items()
        self.reply('POST', BASE, 403)
        with self.assertRaises(crm.CRMError):
            crm_window.upload_selected(self.client, pending, item, LEAD)
        self.assertEqual(len(pending.items()), 1)
        self.reply('POST', BASE, 200, {'file': {'id': FILE}, 'alreadyUploaded': True})
        crm_window.upload_selected(self.client, pending, item, LEAD)
        self.assertEqual(pending.items(), [])

    def test_command_send_success_clears_only_that_file_from_pending(self):
        # velcro crm send (the Mac app's upload button) must drop the sent recording from the menu count.
        import hashlib
        pending = crm.Pending(self.url, self.client.state)
        sha = hashlib.sha256(Path(self.file).read_bytes()).hexdigest()
        pending.add(self.file, sha, '2026-01-01T00:00:00Z')
        pending.add(self.file, 'other-synthetic-hash', '2026-01-01T00:00:00Z')
        self.reply('POST', BASE, 200, {'file': {'id': FILE}, 'alreadyUploaded': True})
        result = self.client.send(self.file, LEAD)
        pending.finish_sent(self.file, result['sha256'])
        self.assertEqual([x['sha256'] for x in pending.items()], ['other-synthetic-hash'])

    def test_different_crm_origin_does_not_share_history_or_pending(self):
        self.reply('POST', BASE, 200, {'file': {'id': FILE}, 'alreadyUploaded': True})
        self.client.send(self.file, LEAD)
        other = crm.Client('https://api.example.com', FakeStore(), self.client.state)
        with mock.patch.object(other, 'request', side_effect=crm.HTTPFailure(403)) as request:
            with self.assertRaises(crm.CRMError):
                other.send(self.file, LEAD)
            request.assert_called_once()
        crm.Pending(self.url, self.client.state).add(self.file, 'synthetic-hash', '2026-01-01')
        self.assertEqual(crm.Pending(other.url, self.client.state).items(), [])


class LocalContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {'HOME': str(self.root), 'XDG_CONFIG_HOME': str(self.root / 'config'),
                    'XDG_STATE_HOME': str(self.root / 'state'), 'LOCALAPPDATA': str(self.root / 'local'),
                    'APPDATA': str(self.root / 'roaming')}
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'the private state file is the Linux credential store')
    def test_linux_private_token_and_delete(self):
        store = crm.TokenStore('https://api.example.com', self.root / 'state')
        self.assertIsNone(store.get())
        store.set(TOKEN, TOKEN_EXPIRY)
        self.assertEqual(store.get(), TOKEN)
        self.assertEqual(json.loads(store.path.read_text()), {'token': TOKEN, 'expiresAt': TOKEN_EXPIRY})
        self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
        store.path.chmod(0o644)
        store.set(TOKEN)
        self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
        store.delete()
        self.assertIsNone(store.get())
        self.assertIsNone(crm.TokenStore('https://other.example.com', self.root / 'state').get())

    @unittest.skipUnless(sys.platform.startswith('linux'), 'the private state file is the Linux credential store')
    def test_token_expiry_boundary_and_legacy_credential(self):
        store = crm.TokenStore('https://api.example.com', self.root / 'state')
        store.set(TOKEN, '2000-01-01T00:00:00Z')
        with mock.patch.object(crm.time, 'time', return_value=946684799):
            self.assertEqual(store.get(), TOKEN)
        with mock.patch.object(crm.time, 'time', return_value=946684800):
            with self.assertRaisesRegex(crm.CRMError, '다시 연결') as raised:
                store.get()
        self.assertNotIn(TOKEN, str(raised.exception))
        store.path.write_text(TOKEN)
        self.assertEqual(store.get(), TOKEN)

    def test_mac_keychain_adapter_with_fake_process(self):
        store = crm.TokenStore('https://api.example.com', self.root)
        saved = []
        def fake_run(args, **kwargs):
            if args == ['security', '-i']:
                words = kwargs['input'].decode().split()
                self.assertEqual(words[:-1], ['add-generic-password', '-U', '-s', 'velcro-crm', '-a', store.url, '-w'])
                saved.append(words[-1])
            return subprocess.CompletedProcess(args, 0, saved[-1].encode() if args[1] == 'find-generic-password' else b'', b'')
        with mock.patch.object(crm.sys, 'platform', 'darwin'), mock.patch.object(crm.subprocess, 'run', side_effect=fake_run) as run:
            store.set(TOKEN, TOKEN_EXPIRY)
            self.assertEqual(store.get(), TOKEN)
            with mock.patch.object(crm.time, 'time', return_value=crm.iso_timestamp(TOKEN_EXPIRY)):
                with self.assertRaisesRegex(crm.CRMError, '다시 연결'):
                    store.get()
            store.delete()
            self.assertEqual(run.call_args_list[0].args[0], ['security', '-i'])
            # The token never appears in any process's arguments.
            self.assertFalse(any(TOKEN in ' '.join(c.args[0]) for c in run.call_args_list))
            self.assertTrue(saved[0].startswith('b64:'))
            self.assertEqual(json.loads(base64.b64decode(saved[0][4:])), {'token': TOKEN, 'expiresAt': TOKEN_EXPIRY})

    def test_windows_credential_manager_fake_abi(self):
        """Exercise the ctypes adapter on Linux with fake Cred* calls, not the OS store."""
        values, retained, calls = {}, [], []
        class Function:
            def __init__(self, fn):
                self.fn = fn
            def __call__(self, *args):
                return self.fn(*args)
        class API:
            def write(self, ptr, flags):
                cred = ptr._obj
                values[cred.TargetName] = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
                calls.append(('write', cred.Type, cred.Persist))
                return True
            def read(self, target, kind, flags, output):
                cred_type = output._obj._type_
                blob = (ctypes.c_ubyte * len(values[target])).from_buffer_copy(values[target])
                cred = cred_type()
                cred.CredentialBlob, cred.CredentialBlobSize = blob, len(blob)
                ptr = ctypes.pointer(cred)
                retained.extend([blob, cred, ptr])
                ctypes.cast(output, ctypes.POINTER(type(ptr)))[0] = ptr
                return True
            def delete(self, target, *_):
                del values[target]
                return True
        api = API()
        api.CredWriteW, api.CredReadW, api.CredDeleteW, api.CredFree = map(Function, [api.write, api.read, api.delete, lambda _: calls.append(('free',))])
        with mock.patch.object(crm.sys, 'platform', 'win32'), mock.patch.object(ctypes, 'WinDLL', return_value=api, create=True):
            store = crm.TokenStore('https://api.example.com', self.root)
            store.set(TOKEN, TOKEN_EXPIRY)
            self.assertEqual(store.get(), TOKEN)
            self.assertEqual(json.loads(values['velcro-crm:https://api.example.com']), {'token': TOKEN, 'expiresAt': TOKEN_EXPIRY})
            with mock.patch.object(crm.time, 'time', return_value=crm.iso_timestamp(TOKEN_EXPIRY)):
                with self.assertRaisesRegex(crm.CRMError, '다시 연결'):
                    store.get()
            store.delete()
        self.assertEqual(values, {})
        self.assertEqual(calls, [('write', 1, 2), ('free',), ('free',)])

    def test_urls_no_default_or_remote_plain_http(self):
        for url in ('', 'http://api.example.com', 'https://user:pass@api.example.com', 'https://api.example.com/path', 'https://api.example.com?key=secret', 'https://[', 'https://api.example.com\n'):
            with self.subTest(url=url), self.assertRaises(crm.CRMError):
                crm.server_url(url)
        self.assertEqual(crm.server_url('https://api.example.com/'), 'https://api.example.com')

    @unittest.skipIf(shutil.which('zsh') is None, 'needs zsh (the Mac/Linux command)')
    def test_zsh_settings_and_dispatch_use_throwaway_home(self):
        def run(*args):
            return subprocess.run(['zsh', str(ROOT / 'velcro'), *args], capture_output=True, text=True)
        self.assertEqual(run('set', 'crm.url', 'https://api.example.com').returncode, 0)
        self.assertEqual(run('set', 'crm.approve', 'https://auth.example.com/device/').returncode, 0)
        self.assertIn('crm.url=https://api.example.com', run('settings').stdout)
        self.assertIn('crm.approve=https://auth.example.com/device/', run('settings').stdout)
        self.assertEqual(crm.configured_setting('crm.approve'), 'https://auth.example.com/device/')
        result = run('crm', 'pending')
        self.assertEqual((result.returncode, result.stdout.strip()), (0, '[]'))
        file = self.root / 'synthetic.wav'
        file.write_bytes(b'synthetic')
        result = run('crm', 'enqueue', str(file), '--sha256', 'synthetic-hash', '--at', '2026-01-01')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(run('crm', 'pending').stdout)), 1)
        self.assertEqual(run('set', 'crm.url').returncode, 0)
        self.assertEqual(run('set', 'crm.approve').returncode, 0)
        self.assertEqual(crm.configured_setting('crm.approve'), '')
        self.assertNotEqual(run('crm', 'pending').returncode, 0)

    @unittest.skipIf(shutil.which('zsh') is None, 'needs zsh (the Mac/Linux command)')
    def test_zsh_import_manifest_hook_is_local_and_opt_in(self):
        dest = self.root / 'Recordings'
        dest.mkdir()
        (dest / 'silence.wav').write_bytes(b'synthetic')
        config = self.root / 'config' / 'velcro'
        config.mkdir(parents=True)
        script = '\n'.join([
            'source "$1" version >/dev/null',
            'manifest_add "$2" silence.wav silence.wav 9 synthetic-hash 2026-01-01 "Synthetic recorder" "" 1 test-import'
        ])
        args = ['zsh', '-c', script, 'crm-test', str(ROOT / 'velcro'), str(dest)]
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((crm.state_dir() / 'crm-pending.jsonl').exists())
        (dest / win.MANIFEST).unlink()
        (config / 'settings').write_text('crm.url = https://api.example.com\n')
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        item, = crm.Pending('https://api.example.com').items()
        self.assertEqual(item['path'], str(dest / 'silence.wav'))
        self.assertEqual(item['duration_s'], 1)
        manifest = json.loads((dest / win.MANIFEST).read_text())
        self.assertNotIn('crm', manifest)

    def test_windows_import_queues_only_when_configured_and_never_uploads(self):
        source, dest = self.root / 'source.wav', self.root / 'Recordings'
        dest.mkdir()
        with wave.open(str(source), 'wb') as f:
            f.setnchannels(1); f.setsampwidth(2); f.setframerate(8000)
            f.writeframes(b'\0\0' * 8000)
        settings = {'import.dest': [str(dest)]}
        with mock.patch.object(crm.Client, 'request', side_effect=AssertionError('No network during import')):
            self.assertEqual(win.import_files([str(source)], settings, log=lambda _: None), (1, 0))
            self.assertFalse((Path(win.state_dir()) / 'crm-pending.jsonl').exists())
            source = self.root / 'second.wav'
            source.write_bytes(b'synthetic')
            settings['crm.url'] = ['https://api.example.com']
            self.assertEqual(win.import_files([str(source)], settings, log=lambda _: None), (1, 0))
        pending, = win.crm_pending(settings)
        self.assertEqual(pending['path'], str((dest / 'second.wav').resolve()))
        self.assertTrue(source.exists())
        self.assertEqual(pending['status'], 'pending')

    def test_windows_spool_move_preserves_queue_and_human_decision(self):
        for decision in ('pending', 'sent', 'skipped'):
            with self.subTest(decision=decision), mock.patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / decision)}):
                source = self.root / (decision + '.wav')
                source.write_bytes(b'synthetic')
                dest = self.root / ('missing-' + decision) / 'Recordings'
                settings = {'import.dest': [str(dest)], 'crm.url': ['https://api.example.com']}
                with mock.patch.object(crm.Client, 'request', side_effect=AssertionError('No automatic network')):
                    self.assertEqual(win.import_files([str(source)], settings, log=lambda _: None), (1, 0))
                    item, = win.crm_pending(settings)
                    self.assertEqual(item['path'], str((Path(win.queue_dir()) / source.name).resolve()))
                    pending = crm.Pending(settings['crm.url'][0], win.state_dir())
                    if decision != 'pending':
                        pending.finish(item, skipped=decision == 'skipped')
                    dest.mkdir(parents=True)
                    # A conflicting destination filename exercises rename on relocation.
                    (dest / source.name).write_bytes(b'different synthetic content')
                    self.assertEqual(win.flush_queue(settings, log=lambda _: None), 1)
                    if decision == 'pending':
                        moved, = pending.items()
                        self.assertEqual(moved['key'], item['key'])
                        self.assertEqual(moved['path'], str(dest / (decision + '-2.wav')))
                    else:
                        self.assertEqual(pending.items(), [])
                    self.assertFalse(Path(item['path']).exists())
                    self.assertTrue(source.exists())

    @unittest.skipIf(shutil.which('zsh') is None, 'needs zsh (the Mac/Linux command)')
    def test_zsh_spool_relocation_updates_existing_manifest_entry(self):
        config = self.root / 'config' / 'velcro'
        config.mkdir(parents=True)
        dest, spool = self.root / 'Recordings', self.root / 'spool.wav'
        dest.mkdir(); spool.write_bytes(b'synthetic')
        (dest / 'silence.wav').write_bytes(b'synthetic')
        (config / 'settings').write_text('crm.url = https://api.example.com\n')
        script = '\n'.join([
            'source "$1" version >/dev/null',
            'manifest_add "$2" silence.wav silence.wav 9 synthetic-hash 2026-01-01 "Synthetic recorder" "" 1 test-import',
            'crm_enqueue "$3" synthetic-hash 2026-01-01 1',
            'manifest_add "$2" silence.wav silence.wav 9 synthetic-hash 2026-01-01 "Synthetic recorder" "" 1 test-import "$3"'
        ])
        result = subprocess.run(['zsh', '-c', script, 'crm-test', str(ROOT / 'velcro'), str(dest), str(spool)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Both originally selected files remain distinct entries, but relocation
        # must not leave a stale reference to the deleted import spool.
        items = crm.Pending('https://api.example.com').items()
        self.assertTrue(items)
        self.assertTrue(all(item['path'] == str(dest / 'silence.wav') for item in items))
        self.assertEqual(len((dest / win.MANIFEST).read_text().splitlines()), 1)

    def test_crm_queue_failure_does_not_change_import(self):
        source, dest = self.root / 'source.wav', self.root / 'Recordings'
        source.write_bytes(b'synthetic'); dest.mkdir()
        settings = {'import.dest': [str(dest)], 'crm.url': ['https://api.example.com']}
        with mock.patch.object(crm.Pending, 'add', side_effect=OSError('synthetic failure')):
            self.assertEqual(win.import_files([str(source)], settings, log=lambda _: None), (1, 0))
        self.assertTrue((dest / source.name).exists())
        self.assertEqual(len(win.manifest_lines(dest)), 1)


if __name__ == '__main__':
    unittest.main()
