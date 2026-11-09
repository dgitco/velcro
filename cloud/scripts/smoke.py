#!/usr/bin/env python3
"""생성한 무음 WAV만 업로드한다. 토큰은 환경변수로 받고 출력하지 않는다."""
import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
import wave

parser = argparse.ArgumentParser()
parser.add_argument('--base', required=True)
parser.add_argument('--output', required=True)
args = parser.parse_args()
token = os.environ['VELCRO_DEVICE_TOKEN']
base = args.base.rstrip('/')


def request(path, method='GET', data=None, extra=None, authenticated=True):
    headers = {'User-Agent': 'velcro-cloud-smoke/1'}
    if authenticated:
        headers['Authorization'] = 'Bearer ' + token
    if isinstance(data, dict):
        data = json.dumps(data).encode()
        headers['Content-Type'] = 'application/json'
    headers.update(extra or {})
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


now = datetime.datetime.now(datetime.timezone.utc)
buffer = io.BytesIO()
with wave.open(buffer, 'wb') as audio:
    audio.setnchannels(1)
    audio.setsampwidth(2)
    audio.setframerate(8000)
    audio.writeframes(b'\x00\x00' * 8000)
raw = buffer.getvalue()
metadata = {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'ext': 'wav', 'recordedAt': now.isoformat(timespec='seconds').replace('+00:00', 'Z')}
unauthorized, _ = request('/v1/uploads', 'POST', metadata, authenticated=False)
assert unauthorized == 401, unauthorized
created, upload = request('/v1/uploads', 'POST', metadata)
assert created == 201, (created, upload)
headers = {'Upload-Session': upload['uploadSession']}
parts = []
_, prior = request(f"/v1/uploads/{upload['id']}")
if prior['phase'] == 'uploading':
    for start in range(0, len(raw), upload['partSize']):
        number = start // upload['partSize'] + 1
        chunk = raw[start:start + upload['partSize']]
        status, response = request(f"/v1/uploads/{upload['id']}/parts/{number}", 'PUT', chunk,
                                   {**headers, 'Content-Length': str(len(chunk))})
        assert status == 201, (status, response)
        parts.append(status)
completed, response = request(f"/v1/uploads/{upload['id']}/complete", 'POST', {}, headers)
assert completed == 202, (completed, response)
checked, state = request(f"/v1/uploads/{upload['id']}")
assert checked == 200, (checked, state)
# Wait for the actual queue event and first missing-engine retry, never a paid engine.
for _ in range(40):
    if state['phase'] == 'transcribe' and state.get('attempts', 0) >= 1:
        break
    time.sleep(0.5)
    checked, state = request(f"/v1/uploads/{upload['id']}")
    assert checked == 200, (checked, state)
assert state['phase'] == 'transcribe' and state['attempts'] >= 1, state
repeated, duplicate = request('/v1/uploads', 'POST', metadata)
assert repeated == 201 and duplicate['id'] == upload['id'], (repeated, duplicate)
closed, _ = request(f"/v1/uploads/{upload['id']}/parts/1", 'PUT', raw,
                    {**headers, 'Content-Length': str(len(raw))})
assert closed == 409, closed
limit = 4 * 1024 ** 3
boundary_metadata = {**metadata, 'size': limit,
                     'sha256': hashlib.sha256(b'upload-limit-reservation-fixture').hexdigest()}
large_status, large = request('/v1/uploads', 'POST', boundary_metadata)
assert large_status == 201 and large['partCount'] == 82, (large_status, large)
over_limit, _ = request('/v1/uploads', 'POST', {**boundary_metadata, 'size': limit + 1})
assert over_limit == 400, over_limit
result = {'utc': now.isoformat(), 'base': base,
          'fixture': {'kind': 'generated-silence-wav', 'durationSeconds': 1, **metadata},
          'uploadId': upload['id'], 'unauthenticated': unauthorized, 'created': created,
          'parts': parts, 'completed': completed, 'status': checked, 'phase': state['phase'], 'attempts': state['attempts'],
          'nextAttemptAt': state.get('nextAttemptAt'),
          'deduplicated': True, 'overwriteRejected': closed,
          'limitReservation': {'bytes': limit, 'status': large_status, 'partCount': large['partCount'],
                               'overLimitStatus': over_limit, 'bytesTransferred': 0}}
Path(args.output).write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
