import { json, type Env } from './common';
import { deviceEndpoints, deviceOwner } from './device';
export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    try {
      const { pathname } = new URL(request.url);
      if (request.method === 'GET' && pathname === '/health') return json({ ok: true });
      if (request.method === 'GET' && pathname === '/v1/auth/device') return json(deviceEndpoints(env));
      await deviceOwner(request, env);
      return json({ error: 'not_found' }, 404);
    } catch (error) {
      const match = error instanceof Error && error.message.match(/^(\d{3}) ([a-z_]+)$/);
      return match ? json({ error: match[2] }, Number(match[1])) : json({ error: 'internal_error' }, 500);
    }
  },
} satisfies ExportedHandler<Env>;
