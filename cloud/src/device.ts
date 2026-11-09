import { digest, HttpError, readText, type Env } from './common';

// Cache hashes, never bearer tokens. Revocation takes effect within 30 seconds.
const identities = new Map<string, { owner: string; expires: number }>();
function authority(env: Env): URL {
  if (!env.DEVICE_AUTH_URL) throw new HttpError(503, 'identity_not_configured');
  const url = new URL(env.DEVICE_AUTH_URL);
  if (url.protocol !== 'https:' || url.username || url.password || url.pathname !== '/') {
    throw new HttpError(503, 'invalid_identity_configuration');
  }
  return url;
}
export function deviceEndpoints(env: Env) {
  const base = authority(env);
  return {
    deviceAuthorizationEndpoint: new URL('/api/device/code', base).href,
    tokenEndpoint: new URL('/api/device/token', base).href,
  };
}
export async function deviceOwner(request: Request, env: Env): Promise<string> {
  const token = request.headers.get('Authorization')?.match(/^Bearer (bxd_[A-Za-z0-9_-]{30,80})$/)?.[1];
  if (!token) throw new HttpError(401, 'device_token_required');
  const base = authority(env);
  const key = await digest(`${base.origin}:${token}`);
  const cached = identities.get(key);
  if (cached && cached.expires > Date.now()) return cached.owner;
  let response: Response;
  try {
    const url = new URL('/api/device/me', base);
    const init = {
      headers: { Authorization: `Bearer ${token}`, 'User-Agent': 'velcro-cloud/1' },
      redirect: 'manual' as const, signal: AbortSignal.timeout(10_000),
    };
    response = env.BOARDER ? await env.BOARDER.fetch(new Request(url, init)) : await fetch(url, init);
  } catch {
    console.warn('device_authority_fetch_failed');
    throw new HttpError(503, 'identity_unavailable');
  }
  if (response.status === 401 || response.status === 403) throw new HttpError(401, 'invalid_device_token');
  if (!response.ok) {
    console.warn('device_authority_unavailable', response.status);
    throw new HttpError(503, 'identity_unavailable');
  }
  const me = JSON.parse(await readText(response.body, 8192));
  if (typeof me.email !== 'string' || !me.email.trim() ||
      !(me.scope === 'full' || (me.scope === 'watch' && me.owner === true))) {
    throw new HttpError(403, 'device_scope_denied');
  }
  // The authority's /me contract identifies members by email, not device.id.
  const owner = await digest(`${base.origin}:${me.email.trim().toLowerCase()}`);
  if (identities.size >= 512) identities.delete(identities.keys().next().value!);
  identities.set(key, { owner, expires: Date.now() + 30_000 });
  return owner;
}
