#!/usr/bin/env python3
"""개인 녹음 없이 5분 20초 무음 WAV로 조각 받아쓰기·R2 결과를 검증한다."""
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
base, token = args.base.rstrip('/'), os.environ['VELCRO_DEVICE_TOKEN']


def request(path, method='GET', data=None, extra=None):
    headers = {'Authorization': 'Bearer ' + token, 'User-Agent': 'velcro-cloud-smoke/1'}
    if isinstance(data, dict):
        data = json.dumps(data).encode()
        headers['Content-Type'] = 'application/json'
    headers.update(extra or {})
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=600) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


now = datetime.datetime.now(datetime.timezone.utc)
buffer = io.BytesIO()
with wave.open(buffer, 'wb') as audio:
    audio.setnchannels(1)
    audio.setsampwidth(2)
    audio.setframerate(16000)
    audio.writeframes(b'\x00\x00' * 16000 * 320)
raw = buffer.getvalue()
metadata = {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'ext': 'wav',
            'recordedAt': now.isoformat(timespec='seconds').replace('+00:00', 'Z')}
created, body = request('/v1/uploads', 'POST', metadata)
assert created == 201, (created, body)
upload = json.loads(body)
headers = {'Upload-Session': upload['uploadSession']}
_, body = request(f"/v1/uploads/{upload['id']}")
if json.loads(body)['phase'] == 'uploading':
    for start in range(0, len(raw), upload['partSize']):
        chunk = raw[start:start + upload['partSize']]
        number = start // upload['partSize'] + 1
        status, body = request(f"/v1/uploads/{upload['id']}/parts/{number}", 'PUT', chunk,
                               {**headers, 'Content-Length': str(len(chunk))})
        assert status == 201, (status, body)
completed, body = request(f"/v1/uploads/{upload['id']}/complete", 'POST', {}, headers)
assert completed == 202, (completed, body)
deadline = time.monotonic() + 180
while True:
    checked, body = request(f"/v1/uploads/{upload['id']}")
    assert checked == 200, (checked, body)
    state = json.loads(body)
    if state.get('transcriptKey'):
        break
    assert not state.get('error'), state
    assert time.monotonic() < deadline, state
    time.sleep(1)
transcript_status, transcript = request(f"/v1/uploads/{upload['id']}/transcript")
assert transcript_status == 200, transcript_status
count = state['transcription']['totalChunks']
assert count >= 2, count
chunks = []
for index in range(count):
    status, body = request(f"/v1/uploads/{upload['id']}/transcript/chunks/{index}")
    assert status == 200, (status, body)
    chunk = json.loads(body)
    assert chunk['index'] == index, chunk
    chunks.append(chunk)
assert chunks[0]['startMs'] == 0 and chunks[-1]['endMs'] == 320000, chunks
assert all(left['endMs'] == right['startMs'] for left, right in zip(chunks, chunks[1:])), chunks
assert transcript.decode() == '\n'.join(chunk['text'] for chunk in chunks), '합친 글 불일치'
result = {'utc': now.isoformat(), 'base': base, 'fixture': {'kind': 'generated-silence-wav',
          'durationSeconds': 320, 'sampleRate': 16000, 'channels': 1, **metadata},
          'uploadId': upload['id'], 'created': created, 'completed': completed, 'status': checked,
          'phase': state['phase'], 'transcriptStatus': transcript_status, 'transcriptBytes': len(transcript),
          'chunks': [{'index': c['index'], 'startMs': c['startMs'], 'endMs': c['endMs'],
                      'textBytes': len(c['text'].encode())} for c in chunks],
          'coverageComplete': True, 'aggregateMatchesChunks': True,
          'silenceHasNoSpeech': all(not c['text'].strip() for c in chunks)}
Path(args.output).write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
