import { body, digest, exact, HttpError, json, type Env } from './common';
import { deviceEndpoints, deviceOwner } from './device';
import { MultipartTransport, PART_SIZE, missingUpload } from './transport';
export { Recording } from './recording';
function identifier(value: unknown): string {
  if (typeof value !== 'string' || !/^[a-f0-9]{64}$/.test(value)) throw new HttpError(400, 'invalid_id');
  return value;
}
function session(request: Request): string {
  const value = request.headers.get('Upload-Session');
  if (!value || value.length > 1024) throw new HttpError(400, 'upload_session_required');
  return value;
}
async function fetch(request: Request, env: Env): Promise<Response> {
  const { pathname } = new URL(request.url);
  if (request.method === 'GET' && pathname === '/health') return json({ ok: true });
  if (request.method === 'GET' && pathname === '/v1/auth/device') return json(deviceEndpoints(env));
  const owner = await deviceOwner(request, env);
  if (request.method === 'POST' && pathname === '/v1/uploads') {
    const data = await body(request); exact(data, ['size', 'sha256', 'ext', 'recordedAt']);
    const maxSize = Number(env.MAX_UPLOAD_BYTES ?? 4 * 1024 ** 3);
    if (!Number.isSafeInteger(maxSize) || maxSize < 1 || maxSize > PART_SIZE * 10000) throw new HttpError(503, 'invalid_upload_limit');
    if (!Number.isSafeInteger(data.size) || (data.size as number) < 1 || (data.size as number) > maxSize) {
      throw new HttpError(400, 'invalid_size');
    }
    const sha256 = identifier(data.sha256);
    if (typeof data.ext !== 'string' || !/^(wav|mp3|m4a|mp4|ogg|flac|aac|wma)$/.test(data.ext)) throw new HttpError(400, 'invalid_extension');
    if (typeof data.recordedAt !== 'string' || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,3})?Z$/.test(data.recordedAt) || !Number.isFinite(Date.parse(data.recordedAt))) {
      throw new HttpError(400, 'invalid_recorded_at');
    }
    const normalized = data.recordedAt.replace(/(?:\.(\d{1,3}))?Z$/, (_m, fraction) => '.' + (fraction ?? '').padEnd(3, '0') + 'Z');
    if (new Date(data.recordedAt).toISOString() !== normalized) throw new HttpError(400, 'invalid_recorded_at');
    const id = await digest(`${owner}:${sha256}`);
    const reserved = await env.JOBS.get(env.JOBS.idFromName(id)).reserve({
      id, owner, size: data.size as number, sha256, ext: data.ext, recordedAt: data.recordedAt, key: '',
    });
    return json({ id, transport: 'multipart', uploadSession: reserved.session,
      partSize: reserved.partSize, partCount: reserved.partCount,
      partUrl: `/v1/uploads/${id}/parts/{n}` }, 201);
  }
  const transcript = pathname.match(/^\/v1\/uploads\/([a-f0-9]{64})\/transcript(?:\/chunks\/(\d+))?$/);
  if (request.method === 'GET' && transcript) {
    const job = env.JOBS.get(env.JOBS.idFromName(transcript[1]));
    const key = await job.transcriptArtifact(owner, transcript[2] === undefined ? undefined : Number(transcript[2]));
    const object = await env.RECORDINGS.get(key);
    if (!object) throw new HttpError(404, 'transcript_not_found');
    return new Response(object.body, { headers: {
      'Cache-Control': 'no-store', 'Content-Length': String(object.size),
      'Content-Type': transcript[2] === undefined ? 'text/plain; charset=utf-8' : 'application/json; charset=utf-8',
    } });
  }
  const match = pathname.match(/^\/v1\/uploads\/([a-f0-9]{64})(?:\/(complete|parts\/(\d+)))?$/);
  if (match) {
    const job = env.JOBS.get(env.JOBS.idFromName(match[1]));
    if (request.method === 'PUT' && match[3]) {
      const uploadSession = session(request), number = Number(match[3]);
      const part = await job.part(owner, uploadSession, number);
      if (request.headers.get('Content-Length') !== String(part.size) || !request.body) throw new HttpError(400, 'invalid_part_size');
      let uploaded: R2UploadedPart;
      try { uploaded = await new MultipartTransport(env.RECORDINGS).put(part.session, number, request.body); }
      catch (error) {
        if (!missingUpload(error)) throw error;
        await job.expire(owner, uploadSession);
        throw new HttpError(409, 'upload_expired');
      }
      await job.savePart(owner, uploadSession, uploaded);
      return json({ partNumber: uploaded.partNumber }, 201);
    }
    if (request.method === 'POST' && match[2] === 'complete') {
      const data = await body(request); exact(data, []);
      await job.complete(owner, session(request));
      return json(await job.status(owner), 202);
    }
    if (request.method === 'GET' && !match[2]) return json(await job.status(owner));
  }
  throw new HttpError(404, 'not_found');
}
export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    try { return await fetch(request, env); }
    catch (error) {
      // Durable Object RPC transfers Error messages, not custom error properties.
      const match = error instanceof Error && error.message.match(/^(\d{3}) ([a-z_]+)$/);
      return match ? json({ error: match[2] }, Number(match[1])) : json({ error: 'internal_error' }, 500);
    }
  },
  async queue(batch: MessageBatch<{ id: string }>, env: Env): Promise<void> {
    for (const message of batch.messages) {
      try {
        const id = identifier(message.body.id);
        await env.JOBS.get(env.JOBS.idFromName(id)).start();
        message.ack();
      } catch { message.retry(); }
    }
  },
} satisfies ExportedHandler<Env, { id: string }>;
