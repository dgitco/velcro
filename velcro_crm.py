"""CRM file uploads shared by the CLI and Windows tray. Python standard library only.

No default server. Device credentials belong to the configured origin; neither HTTP
response bodies nor transport exceptions are displayed (they can contain secrets).
"""
import argparse
import ctypes
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

MAX_BYTES = 200 * 1024 * 1024
EXTENSIONS = {'m4a', 'mp3', 'wav', 'txt', 'md', 'pdf'}
RETRY_DELAYS = (1, 4, 16)


class CRMError(Exception):
    pass


class HTTPFailure(CRMError):
    def __init__(self, status, code=''):
        self.status, self.code = status, code
        message = {401: '다시 연결하세요(velcro crm connect)',
                   403: '이 카드에 올릴 권한 없음'}.get(status, 'CRM 요청 실패. 다시 시도하세요.')
        super().__init__(message)


def server_url(value):
    try:
        p = urllib.parse.urlsplit(value)
        p.port
    except ValueError:
        raise CRMError('crm.url 주소가 올바르지 않습니다.') from None
    if any(ord(char) <= 32 for char in value):
        raise CRMError('crm.url 주소가 올바르지 않습니다.')
    # HTTP is permitted only for local test servers. Credentials never cross redirects.
    if (p.scheme != 'https' and not (p.scheme == 'http' and p.hostname in ('127.0.0.1', 'localhost', '::1'))) \
            or not p.hostname or p.username or p.password or p.path not in ('', '/') or p.query or p.fragment:
        raise CRMError('crm.url에 서버 주소를 넣으세요 (예: https://api.example.com).')
    return value.rstrip('/')


def state_dir():
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'velcro'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'velcro'
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local' / 'state')) / 'velcro'


def iso_timestamp(value):
    stamp = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError()
    return stamp.timestamp()


def configured_setting(key_name):
    root = (Path(os.environ.get('APPDATA', Path.home() / 'AppData' / 'Roaming')) if sys.platform == 'win32'
            else Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')))
    value = ''
    try:
        for line in (root / 'velcro' / 'settings').read_text(encoding='utf-8').splitlines():
            key, sep, val = line.partition('=')
            if sep and key.strip() == key_name:
                value = val.strip()
    except FileNotFoundError:
        pass
    return value


def configured_url():
    return configured_setting('crm.url')


def approval_url(value, code):
    try:
        p = urllib.parse.urlsplit(value)
        p.port
        if (p.scheme != 'https' and not (p.scheme == 'http' and p.hostname in ('127.0.0.1', 'localhost', '::1'))) \
                or not p.hostname or p.username or p.password or p.fragment or any(ord(c) <= 32 for c in value):
            raise ValueError()
        query = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if k != 'code']
        return urllib.parse.urlunsplit(p._replace(query=urllib.parse.urlencode(query + [('code', code)])))
    except ValueError:
        raise CRMError('crm.approve에 승인 화면 주소를 넣으세요 (예: https://auth.example.com/device/).') from None


def private_append(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    try:
        if os.name != 'nt':
            os.fchmod(fd, 0o600)
        data = (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
        if os.write(fd, data) != len(data):
            raise CRMError('CRM 기록을 저장하지 못했습니다.')
        os.fsync(fd)
    finally:
        os.close(fd)


def records(path):
    try:
        with path.open(encoding='utf-8') as f:
            for line in f:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        yield value
                except ValueError:
                    continue  # a partial last line from an interrupted append
    except FileNotFoundError:
        pass


class TokenStore:
    """Keychain / Windows Credential Manager / private Linux state file."""
    def __init__(self, url, state=None):
        self.url = url
        self.path = Path(state or state_dir()) / ('crm-token-' + hashlib.sha256(url.encode()).hexdigest())

    def _security(self, *args, missing_ok=False):
        result = subprocess.run(['security', *args], capture_output=True)
        if result.returncode and not (missing_ok and result.returncode == 44):
            raise CRMError('키체인 작업 실패. 키체인을 잠금 해제하고 다시 시도하세요.')
        return result.stdout.decode('utf-8').strip() if not result.returncode else None

    def _windows(self, action, token=None):
        from ctypes import wintypes as w
        class Credential(ctypes.Structure):
            _fields_ = [('Flags', w.DWORD), ('Type', w.DWORD), ('TargetName', w.LPWSTR),
                        ('Comment', w.LPWSTR), ('LastWritten', w.FILETIME), ('CredentialBlobSize', w.DWORD),
                        ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', w.DWORD),
                        ('AttributeCount', w.DWORD), ('Attributes', ctypes.c_void_p),
                        ('TargetAlias', w.LPWSTR), ('UserName', w.LPWSTR)]
        api = ctypes.WinDLL('advapi32', use_last_error=True)
        ptr = ctypes.POINTER(Credential)
        api.CredReadW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(ptr)]
        api.CredReadW.restype = w.BOOL
        api.CredWriteW.argtypes = [ptr, w.DWORD]
        api.CredWriteW.restype = w.BOOL
        api.CredDeleteW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD]
        api.CredDeleteW.restype = w.BOOL
        api.CredFree.argtypes = [ctypes.c_void_p]
        api.CredFree.restype = None
        target = 'velcro-crm:' + self.url
        if action == 'get':
            result = ptr()
            if not api.CredReadW(target, 1, 0, ctypes.byref(result)):
                if ctypes.get_last_error() == 1168:
                    return None
                raise CRMError('Windows 자격 증명 저장소 읽기 실패.')
            try:
                return ctypes.string_at(result.contents.CredentialBlob, result.contents.CredentialBlobSize).decode('utf-8')
            finally:
                api.CredFree(result)
        elif action == 'set':
            blob = token.encode('utf-8')
            buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
            cred = Credential()
            cred.Type, cred.TargetName, cred.Persist = 1, target, 2
            cred.CredentialBlob, cred.CredentialBlobSize = buf, len(blob)
            if not api.CredWriteW(ctypes.byref(cred), 0):
                raise CRMError('Windows 자격 증명 저장 실패.')
        elif not api.CredDeleteW(target, 1, 0) and ctypes.get_last_error() != 1168:
            raise CRMError('Windows 자격 증명 삭제 실패.')

    def _read(self):
        if sys.platform == 'darwin':
            return self._security('find-generic-password', '-s', 'velcro-crm', '-a', self.url, '-w', missing_ok=True)
        if sys.platform == 'win32':
            return self._windows('get')
        try:
            return self.path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return None

    def get(self):
        value = self._read()
        if not value:
            return None
        if not value.startswith('{'):
            return value  # credentials saved before expiration metadata was supported
        try:
            credential = json.loads(value)
            token, expires = credential['token'], credential.get('expiresAt')
            if not isinstance(token, str) or not token or (expires is not None and iso_timestamp(expires) <= time.time()):
                raise ValueError()
            return token
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            raise HTTPFailure(401) from None

    def set(self, token, expires_at=None):
        token = json.dumps({'token': token, 'expiresAt': expires_at}, separators=(',', ':'))
        if sys.platform == 'darwin':
            self._security('add-generic-password', '-U', '-s', 'velcro-crm', '-a', self.url, '-w', token)
        elif sys.platform == 'win32':
            self._windows('set', token)
        else:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                os.fchmod(f.fileno(), 0o600)
                f.write(token)

    def delete(self):
        if sys.platform == 'darwin':
            self._security('delete-generic-password', '-s', 'velcro-crm', '-a', self.url, missing_ok=True)
        elif sys.platform == 'win32':
            self._windows('delete')
        else:
            self.path.unlink(missing_ok=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, url, store=None, state=None, sleep=time.sleep, clock=time.monotonic, wall_clock=time.time):
        self.url = server_url(url)
        self.state = Path(state or state_dir())
        self.store = store if store is not None else TokenStore(self.url, self.state)
        self.sleep, self.clock = sleep, clock
        self.wall_clock = wall_clock
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, data=None, auth=True, headers=None, raw=False):
        url = path if raw else self.url + path
        h = dict(headers or {})
        if raw and not any(key.lower() == 'content-type' for key in h):
            h['Content-Type'] = 'application/octet-stream'
        if auth:
            token = self.store.get()
            if not token:
                raise HTTPFailure(401)
            h['Authorization'] = 'Bearer ' + token
        if data is not None and not raw:
            data = json.dumps(data).encode('utf-8')
            h['Content-Type'] = 'application/json'
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with self.opener.open(req, timeout=60) as response:
                body = response.read() if not raw else b''
                return response.status, json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            code = ''
            try:
                body = json.loads(e.read(65536))
                code = body.get('error', body.get('code', ''))
                if not isinstance(code, str):
                    code = ''
            except (ValueError, OSError, AttributeError):
                pass
            finally:
                e.close()
            # Retain only known machine codes, never arbitrary server text.
            allowed = {'ExpiredToken', 'RequestExpired', 'SignatureExpired'}
            raise HTTPFailure(e.code, code if code in allowed else '') from None
        except (OSError, ValueError, urllib.error.URLError):
            raise CRMError('CRM 통신 실패. 연결을 확인하고 다시 시도하세요.') from None

    def connect(self, name=None, output=print, browser=webbrowser.open, approve=None):
        payload = {'scope': 'sales_file_upload'}
        if name is not None:
            if not isinstance(name, str) or not 1 <= len(name) <= 80:
                raise CRMError('기기 이름은 1~80자로 넣으세요.')
            payload['displayName'] = name
        approve = configured_setting('crm.approve') if approve is None else approve
        if approve:
            approval_url(approve, '')  # validate before starting a device session
        response_status, start = self.request('POST', '/api/operator-auth/device/start', payload, auth=False)
        try:
            secret, user = start['deviceSecret'], start['userCode']
            interval = float(start['interval'])
            expires = iso_timestamp(start['expiresAt']) - self.wall_clock()
            if response_status != 201 or not isinstance(secret, str) or not secret or not isinstance(user, str) or not user \
                    or not math.isfinite(interval) or interval <= 0:
                raise ValueError()
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            raise CRMError('CRM 기기 승인 응답이 올바르지 않습니다.') from None
        deadline = self.clock() + expires
        code = user[:4] + '-' + user[4:] if len(user) == 8 else user
        output('승인 코드: ' + code)
        if approve:
            approval = approval_url(approve, code)
            output('브라우저에서 잠재 고객 카드 파일 올리기만 승인하세요: ' + approval)
            try:
                browser(approval)
            except Exception:
                pass  # the printed address is sufficient
        else:
            output('승인 화면에서 이 코드를 넣으세요')
        while self.clock() < deadline:
            self.sleep(min(interval, max(0, deadline - self.clock())))
            if self.clock() >= deadline:
                break
            try:
                response_status, result = self.request('POST', '/api/operator-auth/device/poll',
                                                       {'userCode': user, 'deviceSecret': secret}, auth=False)
            except HTTPFailure as e:
                if e.status == 404:
                    break
                raise
            if response_status != 200 or not isinstance(result, dict):
                raise CRMError('CRM 기기 승인 응답이 올바르지 않습니다.')
            status = result.get('status')
            if status == 'pending':
                continue
            if status == 'denied':
                raise CRMError('승인이 거절됐어요')
            if status == 'expired':
                break
            token, expires_at = result.get('token'), result.get('expiresAt')
            if status != 'approved' or not isinstance(token, str) or not token.startswith('taxyop_') or '\n' in token or '\r' in token:
                raise CRMError('CRM 기기 승인 응답이 올바르지 않습니다.')
            if expires_at is not None:
                try:
                    if iso_timestamp(expires_at) <= self.wall_clock():
                        raise HTTPFailure(401)
                except (AttributeError, TypeError, ValueError, OverflowError):
                    raise CRMError('CRM 기기 승인 응답이 올바르지 않습니다.') from None
            self.store.set(token, expires_at)
            try:
                self.request('GET', '/api/rpc/session/whoami')
            except CRMError:
                self.store.delete()
                raise
            output('CRM 연결 완료.')
            return
        raise CRMError('코드가 만료됐어요, 다시 연결하세요')

    def disconnect(self):
        self.store.delete()
        return '저장된 연결을 지웠습니다. 서버 토큰은 대시보드에서 철회하세요.'

    def leads(self):
        try:
            _, result = self.request('GET', '/api/rpc/sales/leads/recent')
        except HTTPFailure as e:
            if e.status in (404, 405, 501):
                raise CRMError('카드 주소를 붙여 넣으세요.') from None
            raise
        if not isinstance(result, list) or any(not isinstance(x, dict) or not all(isinstance(x.get(k), str) for k in ('id', 'name', 'stage', 'updatedAt')) for x in result):
            raise CRMError('CRM 최근 카드 응답이 올바르지 않습니다.')
        def modified(card):
            try:
                stamp = datetime.datetime.fromisoformat(card['updatedAt'].replace('Z', '+00:00'))
                return stamp.replace(tzinfo=stamp.tzinfo or datetime.timezone.utc).timestamp()
            except ValueError:
                raise CRMError('CRM 최근 카드의 수정 시각이 올바르지 않습니다.') from None
        return sorted(result, key=modified, reverse=True)

    def lead_id(self, value):
        if '://' in value or value.startswith('/'):
            try:
                p = urllib.parse.urlsplit(value)
            except ValueError:
                raise CRMError('카드 ID 또는 카드 주소를 넣으세요.') from None
            # Dashboard and API may have separate origins; only the identifier is used.
            if (p.scheme and (p.scheme not in ('https', 'http') or not p.hostname)) or p.netloc and not p.scheme or p.username or p.password:
                raise CRMError('카드 ID 또는 카드 주소를 넣으세요.')
            leads = urllib.parse.parse_qs(p.query).get('lead', [])
            match = re.fullmatch(r'/sales/leads/([^/]+)/?', p.path)
            if leads and len(leads) == 1:
                value = leads[0]
            elif match:
                value = urllib.parse.unquote(match[1])
            else:
                raise CRMError('카드 ID 또는 카드 주소를 넣으세요.')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
            raise CRMError('카드 ID 또는 카드 주소를 넣으세요.')
        return value

    def _retry(self, action, upload=False):
        for attempt in range(4):
            try:
                return action()
            except HTTPFailure as e:
                # An expired signed capability needs a new create response. Never send
                # the operator bearer to storage, including on redirects.
                if upload and (e.status in (401, 403) or e.code in ('ExpiredToken', 'RequestExpired', 'SignatureExpired')):
                    raise
                if e.status < 500 and e.status not in (408, 429):
                    raise
            except CRMError:
                pass
            if attempt == 3:
                raise CRMError('CRM 업로드 재시도 실패. 다시 시도하세요.')
            self.sleep(RETRY_DELAYS[attempt])

    def send(self, filename, lead):
        path, lead = Path(filename).expanduser(), self.lead_id(lead)
        ext = path.suffix.lower().lstrip('.')
        if ext not in EXTENSIONS:
            raise CRMError('지원 형식: m4a, mp3, wav, txt, md, pdf.')
        if not path.is_file():
            raise CRMError('올릴 파일을 찾을 수 없습니다.')
        size = path.stat().st_size
        if size > MAX_BYTES:
            raise CRMError('파일은 200 MiB 이하여야 합니다.')
        digest = hashlib.sha256()
        with path.open('rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                digest.update(chunk)
        sha = digest.hexdigest()
        for entry in records(self.state / 'crm-sent.jsonl'):
            if entry.get('url') == self.url and entry.get('sha256') == sha and entry.get('leadId') == lead:
                return dict(entry, already=True)
        endpoint = '/api/rpc/sales/leads/' + lead + '/files'
        for renewal in range(4):
            status, created = self.request('POST', endpoint, {'extension': ext, 'sizeBytes': size, 'sha256': sha})
            try:
                file_id = created['file']['id']
                already = created.get('alreadyUploaded') is True
                if not isinstance(file_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', file_id) or (status, already) not in ((200, True), (201, False)):
                    raise ValueError()
                if not already:
                    upload = created['upload']
                    url, headers = upload['url'], upload['headers']
                    target = urllib.parse.urlsplit(url)
                    if (target.scheme != 'https' and not (target.scheme == 'http' and target.hostname in ('localhost', '127.0.0.1', '::1'))) \
                            or target.username or target.password or not target.hostname or not isinstance(headers, dict):
                        raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise CRMError('CRM 파일 생성 응답이 올바르지 않습니다.') from None
            if not already:
                def put():
                    with path.open('rb') as source:
                        # Explicit length avoids chunked transfer (signed object uploads).
                        return self.request('PUT', url, source, auth=False,
                                            headers=dict(headers, **{'Content-Length': str(size)}), raw=True)
                try:
                    self._retry(put, upload=True)
                except HTTPFailure as e:
                    if e.status in (401, 403) or e.code in ('ExpiredToken', 'RequestExpired', 'SignatureExpired'):
                        if renewal < 3:
                            self.sleep(RETRY_DELAYS[renewal])
                            continue
                        raise CRMError('서명 주소가 만료되었습니다. 다시 시도하세요.') from None
                    raise
                status, completed = self._retry(lambda: self.request('POST', endpoint + '/' + file_id + '/complete', {}))
                if status != 204:
                    try:
                        if status != 200 or completed.get('alreadyUploaded') is not True:
                            raise ValueError()
                        file_id = completed['file']['id']
                        if not isinstance(file_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', file_id):
                            raise ValueError()
                    except (KeyError, TypeError, ValueError):
                        raise CRMError('CRM 파일 완료 응답이 올바르지 않습니다.') from None
                    already = True
            entry = {'sha256': sha, 'leadId': lead, 'fileId': file_id,
                     'at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'already': already, 'url': self.url}
            private_append(self.state / 'crm-sent.jsonl', entry)
            return entry
        raise CRMError('서명 주소가 만료되었습니다. 다시 시도하세요.')


class Pending:
    """Append-only local queue. Nothing here opens a network connection."""
    def __init__(self, url, state=None):
        self.url = server_url(url)
        self.path = Path(state or state_dir()) / 'crm-pending.jsonl'

    def _entries(self):
        latest = {}
        for item in records(self.path):
            if item.get('url') == self.url and 'key' in item:
                latest[item['key']] = item
        return latest

    def add(self, filename, sha, at, duration=None, previous=None):
        filename = str(Path(filename).resolve())
        key = hashlib.sha256((self.url + '\0' + filename + '\0' + sha).encode()).hexdigest()
        entries = self._entries()
        if previous:
            old_path = str(Path(previous).resolve())
            for item in entries.values():
                if item.get('path') == old_path and item.get('sha256') == sha:
                    if item.get('status') == 'pending':
                        private_append(self.path, dict(item, path=filename))
                    return  # completed/skipped items must not reappear on a move
        if key not in entries and not any(item.get('path') == filename and item.get('sha256') == sha for item in entries.values()):
            recorded = datetime.datetime.fromtimestamp(Path(filename).stat().st_mtime).astimezone().isoformat()
            private_append(self.path, {'key': key, 'url': self.url, 'path': filename, 'sha256': sha,
                                       'at': at, 'recorded_at': recorded, 'duration_s': duration, 'status': 'pending'})

    def items(self):
        return [x for x in self._entries().values() if x.get('status') == 'pending']

    def finish(self, item, skipped=False):
        private_append(self.path, dict(item, status='skipped' if skipped else 'sent'))

    def finish_sent(self, filename, sha):
        """Mark the waiting entries for this exact file and hash as sent (the menu count drops)."""
        filename = str(Path(filename).expanduser().resolve())
        for item in self.items():
            if item.get('path') == filename and item.get('sha256') == sha:
                self.finish(item)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='velcro crm', description='선택한 카드에 파일 올리기 (자동 전송 없음)')
    parser.add_argument('--url', default=None)
    sub = parser.add_subparsers(dest='command', required=True)
    connect = sub.add_parser('connect')
    connect.add_argument('--name')
    sub.add_parser('disconnect')
    leads = sub.add_parser('leads')
    leads.add_argument('--json', action='store_true')
    send = sub.add_parser('send')
    send.add_argument('file')
    send.add_argument('--lead', required=True)
    enqueue = sub.add_parser('enqueue', help='가져온 녹음을 로컬 대기 목록에만 추가')
    enqueue.add_argument('file')
    enqueue.add_argument('--sha256', required=True)
    enqueue.add_argument('--at', required=True)
    enqueue.add_argument('--duration', type=float)
    enqueue.add_argument('--previous', help=argparse.SUPPRESS)
    sub.add_parser('pending')
    args = parser.parse_args(argv)
    try:
        url = server_url(args.url if args.url is not None else configured_url())
        if args.command == 'enqueue':
            Pending(url).add(args.file, args.sha256, args.at, args.duration, previous=args.previous)
        elif args.command == 'pending':
            print(json.dumps(Pending(url).items(), ensure_ascii=False))
        else:
            client = Client(url)
            if args.command == 'connect':
                client.connect(args.name)
            elif args.command == 'disconnect':
                print(client.disconnect())
            elif args.command == 'leads':
                result = client.leads()
                token = client.store.get() or ''
                output = json.dumps(result, ensure_ascii=False) if args.json else '\n'.join(
                    f"{x['id']}\t{x['name']}\t{x['stage']}\t{x['updatedAt']}" for x in result)
                print(output.replace(token, '[redacted]') if token else output)
            elif args.command == 'send':
                result = client.send(args.file, args.lead)
                Pending(url).finish_sent(args.file, result['sha256'])
                print('이미 보낸 파일입니다.' if result['already'] else 'CRM 파일 올리기 완료.')
        return 0
    except CRMError as e:
        print(str(e), file=sys.stderr)
    except Exception:
        # Do not interpolate exceptions: OS/HTTP/subprocess errors can hold credentials.
        print('CRM 작업 실패. 설정과 연결을 확인하세요.', file=sys.stderr)
    return 1
