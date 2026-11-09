import { env } from 'cloudflare:workers';
import { reset } from 'cloudflare:test';
import { afterEach, describe, expect, it, vi } from 'vitest';
import worker from '../src/index';
import { type Env } from '../src/common';

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
  it('fails closed on revoked tokens and an unavailable identity authority', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('', { status: 401 }));
    expect((await call('/v1/uploads', 'POST', {})).status).toBe(401);
    fetch.mockResolvedValue(new Response('', { status: 503 }));
    expect((await call('/v1/uploads', 'POST', {})).status).toBe(503);
  });
  it('rejects harness tokens and permits owner watch devices', async () => {
    const fetch = identity('member@example.com', 'harness');
    expect((await call('/v1/uploads', 'POST', {})).status).toBe(403);
    fetch.mockImplementation(async () => Response.json({ email: 'owner@example.com', scope: 'watch', owner: true }));
    expect((await call('/v1/uploads', 'POST', { owner: 'forged' })).status).toBe(404);
  });
});

