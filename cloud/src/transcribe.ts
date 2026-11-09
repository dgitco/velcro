import { audioChunk, planAudio, type AudioPlan } from './audio';
import { readText, type Env } from './common';
import type { Upload } from './recording';
export interface TranscriptionEngine { readonly id: string; transcribe(audio: ArrayBuffer): Promise<string> }
export class RepetitionError extends Error {}
export function repetitive(text: string): boolean {
  const words = text.toLowerCase().replace(/[^\p{L}\p{N}\s]/gu, ' ').split(/\s+/).filter(Boolean);
  const counts = new Map<string, number>();
  for (let i = 0; i + 6 <= words.length; i++) {
    const phrase = words.slice(i, i + 6).join(' '), count = (counts.get(phrase) ?? 0) + 1;
    if (count > 3) return true;
    counts.set(phrase, count);
  }
  return false;
}
export class WhisperEngine implements TranscriptionEngine {
  readonly id = 'cf-whisper-large-v3-turbo-ko-v2';
  constructor(private ai: Ai, private glossary = '') {}
  async transcribe(audio: ArrayBuffer): Promise<string> {
    let binary = '';
    const bytes = new Uint8Array(audio);
    for (let i = 0; i < bytes.length; i += 32768) binary += String.fromCharCode(...bytes.subarray(i, i + 32768));
    const encoded = btoa(binary);
    for (let attempt = 0; attempt < 2; attempt++) {
      const result = await this.ai.run('@cf/openai/whisper-large-v3-turbo', {
        audio: encoded, language: 'ko', task: 'transcribe', initial_prompt: this.glossary,
        vad_filter: true, condition_on_previous_text: false, beam_size: attempt ? 1 : 5,
      }, { signal: AbortSignal.timeout(60_000) });
      if (typeof result.text !== 'string' || new TextEncoder().encode(result.text).length > 262144) throw new Error('invalid_transcript');
      if (!repetitive(result.text)) return result.text;
    }
    throw new RepetitionError('repetition_persisted');
  }
}
export interface TranscriptionProgress {
  engine: string; plan: AudioPlan; cursor: number; next: number; textBytes: number;
  merge?: { level: number; next: number; count: number };
}
interface ChunkResult { index: number; startMs?: number; endMs?: number; endByte: number; text: string }
export function transcriptPrefix(upload: Upload, engine: string): string { return `${upload.key}.transcripts/${engine}/`; }
export async function prepareTranscription(env: Env, upload: Upload, engine: TranscriptionEngine): Promise<TranscriptionProgress> {
  return { engine: engine.id, plan: await planAudio(env.RECORDINGS, upload.key, upload.size, upload.ext), cursor: 0, next: 0, textBytes: 0 };
}
export async function transcribeNext(env: Env, upload: Upload, engine: TranscriptionEngine, progress: TranscriptionProgress): Promise<boolean> {
  if (engine.id !== progress.engine) throw new Error('transcription_engine_changed');
  if (progress.cursor >= progress.plan.length) return true;
  const index = progress.next, plan = progress.plan;
  const key = `${transcriptPrefix(upload, engine.id)}chunks/${index}.json`;
  const existing = await env.RECORDINGS.get(key);
  let result: ChunkResult;
  if (existing) result = JSON.parse(await readText(existing.body, 1048576));
  else {
    const chunk = await audioChunk(env.RECORDINGS, upload.key, plan, progress.cursor);
    result = { index, endByte: chunk.end, text: await engine.transcribe(chunk.audio), ...(plan.pcm ? {
      startMs: progress.cursor / plan.pcm.byteRate * 1000, endMs: chunk.end / plan.pcm.byteRate * 1000,
    } : {}) };
    const textBytes = new TextEncoder().encode((index ? '\n' : '') + result.text).length;
    await env.RECORDINGS.put(key, JSON.stringify(result), {
      customMetadata: { textBytes: String(textBytes) }, httpMetadata: { contentType: 'application/json; charset=utf-8' },
    });
  }
  progress.textBytes += new TextEncoder().encode((index ? '\n' : '') + result.text).length;
  progress.cursor = result.endByte; progress.next++;
  return progress.cursor >= plan.length;
}
// Merge at most 128 objects per alarm. Hierarchical pages keep large recordings below subrequest limits.
export async function finishTranscription(env: Env, upload: Upload, progress: TranscriptionProgress): Promise<string | undefined> {
  const prefix = transcriptPrefix(upload, progress.engine), key = `${prefix}transcript.txt`;
  if (await env.RECORDINGS.head(`${prefix}complete.json`)) return key;
  const merge = progress.merge ??= { level: 0, next: 0, count: progress.next };
  const end = Math.min(merge.next + 128, merge.count), final = merge.count <= 128;
  const inputs = Array.from({ length: end - merge.next }, (_, i) => {
    const index = merge.next + i;
    return { index, key: merge.level ? `${prefix}pages/${merge.level - 1}/${index}.txt` : `${prefix}chunks/${index}.json` };
  });
  const heads = await Promise.all(inputs.map(input => env.RECORDINGS.head(input.key)));
  const length = heads.reduce((sum, head) => {
    if (!head) throw new Error('transcript_chunk_missing');
    const size = merge.level ? head.size : Number(head.customMetadata?.textBytes);
    if (!Number.isSafeInteger(size) || size < 0) throw new Error('invalid_transcript_size');
    return sum + size;
  }, 0);
  const stream = new FixedLengthStream(length);
  const writing = (async () => {
    const writer = stream.writable.getWriter();
    try {
      for (const input of inputs) {
        const object = await env.RECORDINGS.get(input.key);
        if (!object) throw new Error('transcript_chunk_missing');
        if (!merge.level) {
          const result: ChunkResult = JSON.parse(await readText(object.body, 1048576));
          await writer.write(new TextEncoder().encode((input.index ? '\n' : '') + result.text));
        } else {
          const reader = object.body.getReader();
          for (;;) { const { value, done } = await reader.read(); if (done) break; await writer.write(value); }
        }
      }
      await writer.close();
    } catch (error) { await writer.abort(error); throw error; }
  })();
  const output = final ? key : `${prefix}pages/${merge.level}/${Math.floor(merge.next / 128)}.txt`;
  await Promise.all([writing, env.RECORDINGS.put(output, stream.readable, { httpMetadata: { contentType: 'text/plain; charset=utf-8' } })]);
  if (!final) {
    merge.next = end;
    if (end === merge.count) progress.merge = { level: merge.level + 1, next: 0, count: Math.ceil(merge.count / 128) };
    return;
  }
  await env.RECORDINGS.put(`${prefix}complete.json`, JSON.stringify({
    engine: progress.engine, language: 'ko', recordingSha256: upload.sha256,
    chunks: progress.next, textBytes: progress.textBytes, transcriptKey: key,
  }), { httpMetadata: { contentType: 'application/json; charset=utf-8' } });
  return key;
}
