import { env } from 'cloudflare:workers';
import { runInDurableObject, runDurableObjectAlarm, reset } from 'cloudflare:test';
import { afterEach, describe, expect, it, vi } from 'vitest';
import worker from '../src/index';
import { digest, type Env } from '../src/common';
import { retryDelay } from '../src/recording';

const bindings = env as unknown as Env;
const token = () => 'bxd_' + crypto.randomUUID().replaceAll('-', '') + 'x'.repeat(11);
const auth = () => ({ Authorization: `Bearer ${token()}` });
function identity(email = 'member@example.com', scope = 'full', owner = false) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async () => Response.json({ email, scope, owner, device: { id: 'test-device' } }));
}
async function call(path: string, method = 'GET', value?: unknown, headers: Record<string, string> = auth()) {
  const request = new Request('https://velcro.example.com' + path, {
    method, headers: { 'Content-Type': 'application/json', ...headers },
    body: value === undefined ? undefined : JSON.stringify(value),
  });
  return worker.fetch(request, bindings);
}
async function reserve(bytes = new TextEncoder().encode('audio fixture')) {
  const sha256 = await crypto.subtle.digest('SHA-256', bytes);
  const hash = Array.from(new Uint8Array(sha256), x => x.toString(16).padStart(2, '0')).join('');
  const metadata = { size: bytes.length, sha256: hash, ext: 'wav', recordedAt: '2026-01-01T00:00:00Z' };
  const response = await call('/v1/uploads', 'POST', metadata);
  expect(response.status).toBe(201);
  const upload = await response.json() as { id: string; uploadSession: string; partSize: number; partCount: number };
  return { ...upload, metadata, bytes };
}
async function put(upload: Awaited<ReturnType<typeof reserve>>, bytes = upload.bytes) {
  return worker.fetch(new Request(`https://velcro.example.com/v1/uploads/${upload.id}/parts/1`, {
    method: 'PUT', headers: { ...auth(), 'Upload-Session': upload.uploadSession, 'Content-Length': String(bytes.length) }, body: bytes,
  }), bindings);
}
async function complete(upload: Awaited<ReturnType<typeof reserve>>) {
  return call(`/v1/uploads/${upload.id}/complete`, 'POST', {}, { ...auth(), 'Upload-Session': upload.uploadSession });
}
const stub = (id: string) => bindings.JOBS.get(bindings.JOBS.idFromName(id));
afterEach(async () => { vi.restoreAllMocks(); await reset(); });

describe('device authority permission boundary', () => {
  it('requires a real device bearer token', async () => {
    const fetch = identity();
    expect((await call('/v1/uploads', 'POST', {}, {})).status).toBe(401);
    expect((await call('/v1/uploads', 'POST', {}, { Authorization: 'Bearer admin-key' })).status).toBe(401);
    expect(fetch).not.toHaveBeenCalled();
  });
  it('discovers the existing browser pairing authority without proxying or minting tokens', async () => {
    const fetch = identity();
    const response = await call('/v1/auth/device', 'GET', undefined, {});
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      deviceAuthorizationEndpoint: 'https://identity.example.com/api/device/code',
      tokenEndpoint: 'https://identity.example.com/api/device/token',
    });
    expect(fetch).not.toHaveBeenCalled();
  });
  it('checks authority and caches successful identity for at most 30 seconds', async () => {
    const fetch = identity(), headers = auth();
    await call('/missing', 'GET', undefined, headers);
    await call('/missing', 'GET', undefined, headers);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(String(fetch.mock.calls[0][0])).toBe('https://identity.example.com/api/device/me');
    expect(fetch.mock.calls[0][1]?.headers).toMatchObject(headers);
    const now = Date.now();
    vi.spyOn(Date, 'now').mockReturnValue(now + 30001);
    fetch.mockResolvedValue(new Response('', { status: 401 }));
    expect((await call('/missing', 'GET', undefined, headers)).status).toBe(401);
    expect(fetch).toHaveBeenCalledTimes(2);
  });
  it('uses the optional same-account service binding without public fetch', async () => {
    const publicFetch = identity();
    const fetch = vi.fn(async (_request: Request) => Response.json({ email: 'binding@example.com', scope: 'full' }));
    const request = new Request('https://velcro.example.com/missing', { headers: auth() });
    const response = await worker.fetch(request, { ...bindings, BOARDER: { fetch } as unknown as Fetcher });
    expect(response.status).toBe(404);
    expect(publicFetch).not.toHaveBeenCalled();
    const sent = fetch.mock.calls[0][0] as unknown as Request;
    expect(sent.url).toBe('https://identity.example.com/api/device/me');
    expect(sent.headers.get('Authorization')).toBe(request.headers.get('Authorization'));
  });
  it('fails closed on revoked tokens and an unavailable identity authority', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('', { status: 401 }));
    expect((await call('/v1/uploads', 'POST', {})).status).toBe(401);
    fetch.mockResolvedValue(new Response('', { status: 503 }));
    expect((await call('/v1/uploads', 'POST', {})).status).toBe(503);
  });
  it('rejects harness tokens, permits owner watch devices, and never accepts a caller owner field', async () => {
    const fetch = identity('member@example.com', 'harness');
    expect((await call('/v1/uploads', 'POST', {})).status).toBe(403);
    fetch.mockImplementation(async () => Response.json({ email: 'owner@example.com', scope: 'watch', owner: true }));
    expect((await call('/v1/uploads', 'POST', { owner: 'forged' })).status).toBe(400);
  });
});

describe('recording ingestion contract', () => {
  it('streams a multipart upload, verifies content, and preserves it after completion', async () => {
    identity();
    const upload = await reserve();
    expect(upload.partSize).toBe(50 * 1024 * 1024);
    expect((await put(upload)).status).toBe(201);
    const response = await complete(upload);
    expect(response.status).toBe(202);
    expect((await response.json() as { phase: string }).phase).toBe('received');
    const objects = await bindings.RECORDINGS.list();
    expect(objects.objects).toHaveLength(1);
    expect(await (await bindings.RECORDINGS.get(objects.objects[0].key))!.text()).toBe('audio fixture');
    expect((await put(upload)).status).toBe(409);
    expect((await call(`/v1/uploads/${upload.id}`, 'DELETE')).status).toBe(404);
  });
  it('deduplicates concurrent reservations and completions within a member', async () => {
    identity();
    const a = await reserve(), b = await reserve();
    expect(a.id).toBe(b.id); expect(a.uploadSession).toBe(b.uploadSession);
    await put(a);
    const responses = await Promise.all([complete(a), complete(b)]);
    expect(responses.map(r => r.status)).toEqual([202, 202]);
    expect((await bindings.RECORDINGS.list()).objects).toHaveLength(1);
  });
  it('isolates members and shares deduplication across their devices', async () => {
    const fetch = identity();
    const a = await reserve();
    fetch.mockImplementation(async () => Response.json({ email: 'another@example.com', scope: 'full' }));
    expect((await call(`/v1/uploads/${a.id}`)).status).toBe(404);
    expect((await put(a)).status).toBe(404);
    expect((await complete(a)).status).toBe(404);
    const b = await reserve(); expect(a.id).not.toBe(b.id);
  });
  it('rejects missing parts, wrong part sizes, wrong sessions and original filenames', async () => {
    identity(); const upload = await reserve();
    expect((await complete(upload)).status).toBe(409);
    expect((await put(upload, new Uint8Array(2))).status).toBe(400);
    expect((await complete({ ...upload, uploadSession: 'wrong' })).status).toBe(409);
    expect((await call('/v1/uploads', 'POST', { ...upload.metadata, filename: 'secret.wav' })).status).toBe(400);
  });
  it('checks actual SHA-256, preserves failed bytes and allows a fresh session', async () => {
    identity(); const upload = await reserve();
    expect((await put(upload, new Uint8Array(upload.bytes.length))).status).toBe(201);
    const response = await complete(upload);
    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ error: 'checksum_mismatch' });
    const retry = await reserve();
    expect(retry.id).toBe(upload.id); expect(retry.uploadSession).not.toBe(upload.uploadSession);
    expect((await put(retry)).status).toBe(201);
    expect((await complete(retry)).status).toBe(202);
    expect((await bindings.RECORDINGS.list()).objects).toHaveLength(2);
  });
  it.each([0, -1, 200 * 1024 * 1024 + 1, 1.5])('rejects invalid size %s', async size => {
    identity(); const upload = await reserve();
    expect((await call('/v1/uploads', 'POST', { ...upload.metadata, size })).status).toBe(400);
  });
  it('rejects invalid calendar dates and oversized JSON', async () => {
    identity(); const upload = await reserve();
    expect((await call('/v1/uploads', 'POST', { ...upload.metadata, recordedAt: '2026-02-30T00:00:00Z' })).status).toBe(400);
    expect((await call('/v1/uploads', 'POST', { padding: 'x'.repeat(5000) })).status).toBe(413);
  });
});

describe('processing retry and idempotency contract', () => {
  it('uses 10, 20, 40 minute retries with a 24 hour ceiling', () => {
    expect([1, 2, 3, 99].map(retryDelay)).toEqual([600000, 1200000, 2400000, 86400000]);
  });
  it('retries missing engines without deleting recordings or requeueing completed stages', async () => {
    identity(); const upload = await reserve(); await put(upload); await complete(upload);
    const job = stub(upload.id);
    await job.start();
    await runDurableObjectAlarm(job);
    const owner = await digest('https://identity.example.com:member@example.com');
    const status = await job.status(owner);
    expect(status.phase).toBe('transcribe'); expect(status.attempts).toBe(1);
    expect(status.nextAttemptAt! - Date.now()).toBeGreaterThan(590000);
    await job.start();
    expect((await job.status(owner)).attempts).toBe(1);
    expect((await bindings.RECORDINGS.list()).objects).toHaveLength(1);
  });
  it('runs fake engines once per stage and ignores duplicate queue delivery', async () => {
    identity(); const upload = await reserve(); await put(upload); await complete(upload);
    const job = stub(upload.id);
    const requests: string[] = [];
    await runInDurableObject(job, async instance => {
      const engine = { fetch: async (_url: string, init: RequestInit) => {
        const request = JSON.parse(init.body as string);
        requests.push(request.idempotencyKey);
        return Response.json({ text: 'fixture transcript', receipt: 'fixture receipt' });
      } } as unknown as Fetcher;
      const e = (instance as unknown as { env: Env }).env;
      e.TRANSCRIBE = engine; e.TIDY = engine; e.DELIVER = engine;
    });
    await job.start(); await job.start();
    await runDurableObjectAlarm(job); await runDurableObjectAlarm(job); await runDurableObjectAlarm(job);
    const owner = await digest('https://identity.example.com:member@example.com');
    expect((await job.status(owner)).phase).toBe('done');
    expect(requests).toEqual(['transcribe', 'tidy', 'deliver'].map(s => `${upload.id}:${s}`));
    await job.start(); expect(await runDurableObjectAlarm(job)).toBe(false);
    expect((await bindings.RECORDINGS.list()).objects).toHaveLength(1);
  });
});
