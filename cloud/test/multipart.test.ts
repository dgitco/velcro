import { env } from 'cloudflare:workers';
import { afterEach, expect, it, vi } from 'vitest';
import { reset } from 'cloudflare:test';
import worker from '../src/index';
import { hex, type Env } from '../src/common';
import { PART_SIZE, MultipartTransport } from '../src/transport';
const bindings = env as unknown as Env;
function zeros(size: number) {
  let remaining = size;
  return new ReadableStream<Uint8Array>({ pull(controller) {
    if (!remaining) { controller.close(); return; }
    const chunk = new Uint8Array(Math.min(65536, remaining));
    remaining -= chunk.length; controller.enqueue(chunk);
  } });
}
afterEach(async () => { vi.restoreAllMocks(); await reset(); });
it('streams two R2 parts across the 50 MiB boundary without buffering the recording', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(Response.json({ email: 'multipart@example.com', scope: 'full' }));
  const headers = { Authorization: 'Bearer bxd_' + 'm'.repeat(43) };
  const size = PART_SIZE + 100;
  const hash = new crypto.DigestStream('SHA-256');
  await zeros(size).pipeTo(hash);
  const sha256 = hex(await hash.digest);
  const created = await worker.fetch(new Request('https://velcro.example.com/v1/uploads', {
    method: 'POST', headers,
    body: JSON.stringify({ size, sha256, ext: 'wav', recordedAt: '2026-01-01T00:00:00Z' }),
  }), bindings);
  expect(created.status).toBe(201);
  const upload = await created.json() as { id: string; uploadSession: string; partCount: number };
  expect(upload.partCount).toBe(2);
  for (const [index, partSize] of [PART_SIZE, 100].entries()) {
    const fixed = new FixedLengthStream(partSize);
    const piping = zeros(partSize).pipeTo(fixed.writable);
    const response = await worker.fetch(new Request(`https://velcro.example.com/v1/uploads/${upload.id}/parts/${index + 1}`, {
      method: 'PUT', headers: { ...headers, 'Upload-Session': upload.uploadSession, 'Content-Length': String(partSize) },
      body: fixed.readable,
    }), bindings);
    await piping;
    expect(response.status).toBe(201);
  }
  const completed = await worker.fetch(new Request(`https://velcro.example.com/v1/uploads/${upload.id}/complete`, {
    method: 'POST', headers: { ...headers, 'Upload-Session': upload.uploadSession }, body: '{}',
  }), bindings);
  expect(completed.status).toBe(202);
  const list = await bindings.RECORDINGS.list();
  expect(list.objects).toHaveLength(1); expect(list.objects[0].size).toBe(size);
}, 15000);

it('uploads unknown-length input streams through the declared-size transport contract', async () => {
  const bytes = new Uint8Array(16);
  const stream = () => new ReadableStream({ start(controller) { controller.enqueue(bytes); controller.close(); } });
  const transport = new MultipartTransport(bindings.RECORDINGS);
  const session = await transport.create('known-length-contract.wav', 'a'.repeat(64));
  await expect(bindings.RECORDINGS.resumeMultipartUpload(session.key, session.uploadId).uploadPart(1, stream())).rejects.toThrow();
  const part = await transport.put(session, 1, stream(), bytes.length);
  expect(part.partNumber).toBe(1);
  await transport.complete(session, [part]);
  expect((await bindings.RECORDINGS.head(session.key))!.size).toBe(bytes.length);
});
