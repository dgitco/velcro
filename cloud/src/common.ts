import type { Recording } from './recording';
export interface Env {
  JOBS: DurableObjectNamespace<Recording>;
  RECORDINGS: R2Bucket;
  INGEST: Queue<{ id: string }>;
  DEVICE_AUTH_URL: string;
  BOARDER?: Fetcher;
  R2_PREFIX: string;
  MAX_UPLOAD_BYTES?: string;
  TRANSCRIBE?: Fetcher;
  TRANSCRIBE_ENGINE?: string;
  AI?: Ai;
  TRANSCRIBE_GLOSSARY?: string;
  TIDY?: Fetcher;
  DELIVER?: Fetcher;
}
export class HttpError extends Error {
  constructor(public status: number, message: string) { super(`${status} ${message}`); }
}
export function json(value: unknown, status = 200): Response {
  return Response.json(value, { status, headers: { 'Cache-Control': 'no-store' } });
}
export async function readText(stream: ReadableStream<Uint8Array> | null, limit: number): Promise<string> {
  if (!stream) return '';
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let size = 0, text = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > limit) { await reader.cancel(); throw new HttpError(413, 'request_too_large'); }
    text += decoder.decode(value, { stream: true });
  }
  return text + decoder.decode();
}
export async function body(request: Request): Promise<Record<string, unknown>> {
  let value;
  const text = await readText(request.body, 4096);
  try { value = JSON.parse(text); } catch { throw new HttpError(400, 'invalid_json'); }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new HttpError(400, 'invalid_json');
  return value;
}
export function exact(value: Record<string, unknown>, keys: string[]): void {
  if (Object.keys(value).some(k => !keys.includes(k))) throw new HttpError(400, 'unknown_field');
}
export function hex(value: ArrayBuffer): string {
  return Array.from(new Uint8Array(value), b => b.toString(16).padStart(2, '0')).join('');
}
export async function digest(value: string): Promise<string> {
  return hex(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)));
}
