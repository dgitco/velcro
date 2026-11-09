import { env } from 'cloudflare:workers';
import { reset, runDurableObjectAlarm, runInDurableObject } from 'cloudflare:test';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AUDIO_CHUNK_BYTES, audioChunk, planAudio, wavHeader, type PcmFormat } from '../src/audio';
import worker from '../src/index';
import { digest, hex, type Env } from '../src/common';
import type { Upload } from '../src/recording';
import { finishTranscription, prepareTranscription, transcribeNext, transcriptPrefix, type TranscriptionEngine, WhisperEngine, repetitive } from '../src/transcribe';
const bindings = env as unknown as Env;
const pcm: PcmFormat = { channels: 1, sampleRate: 16000, byteRate: 32000, blockAlign: 2, bits: 16 };
function wav(seconds: number, format = pcm): Uint8Array {
  const size = seconds * format.byteRate, bytes = new Uint8Array(size + 44);
  bytes.set(wavHeader(format, size));
  for (let i = 44; i < bytes.length; i++) bytes[i] = i % 251;
  return bytes;
}
async function fixture(seconds = 320): Promise<Upload> {
  const bytes = wav(seconds), sha256 = hex(await crypto.subtle.digest('SHA-256', bytes));
  const id = await digest(crypto.randomUUID());
  const upload = { id, owner: await digest('https://identity.example.com:transcriber@example.com'), key: `datalake/velcro/recordings/${id}/fixture.wav`,
    size: bytes.length, sha256, ext: 'wav', recordedAt: '2026-01-01T00:00:00Z' };
  await bindings.RECORDINGS.put(upload.key, bytes, { customMetadata: { sha256 } });
  return upload;
}
async function seed(upload: Upload, run: (model: string, input: unknown, options?: unknown) => Promise<unknown>) {
  const stub = bindings.JOBS.get(bindings.JOBS.idFromName(upload.id));
  await runInDurableObject(stub, async (instance, state) => {
    const e = (instance as unknown as { env: Env }).env;
    e.TRANSCRIBE_ENGINE = 'workers-ai'; e.AI = { run } as unknown as Ai;
    await state.storage.put('job', { upload, phase: 'transcribe', attempts: 0 });
    await state.storage.setAlarm(Date.now() + 60000);
  });
  return stub;
}
afterEach(async () => { vi.restoreAllMocks(); await reset(); });

describe('PCM chunking contract', () => {
  it('plans a three-hour 16kHz mono recording using header ranges, not a full download', async () => {
    const length = pcm.byteRate * 3 * 3600, header = wavHeader(pcm, length);
    let bytesRead = 0;
    const bucket = { get: async (_key: string, options: R2GetOptions) => {
      const { offset, length: count } = options.range as { offset: number; length: number };
      bytesRead += count;
      expect(offset + count).toBeLessThanOrEqual(44);
      return { arrayBuffer: async () => header.slice(offset, offset + count).buffer };
    } } as unknown as R2Bucket;
    const plan = await planAudio(bucket, 'fixture.wav', length + 44, 'wav');
    expect(Math.ceil(plan.length / plan.chunkBytes)).toBe(36); expect(plan.chunkBytes).toBe(9600000);
    expect(plan.length).toBe(345600000); expect(bytesRead).toBe(44);
  });
  it('covers every PCM byte once and writes valid headers for the short final chunk', async () => {
    const upload = await fixture();
    const plan = await planAudio(bindings.RECORDINGS, upload.key, upload.size, 'wav');
    const first = await audioChunk(bindings.RECORDINGS, upload.key, plan, 0);
    const last = await audioChunk(bindings.RECORDINGS, upload.key, plan, first.end);
    expect(first.end / pcm.byteRate).toBeGreaterThanOrEqual(290);
    expect(first.end / pcm.byteRate).toBeLessThanOrEqual(310);
    expect(first.audio.byteLength).toBe(first.end + 44);
    expect(last.end).toBe(plan.length);
    expect(new DataView(last.audio).getUint32(40, true)).toBe(plan.length - first.end);
    const original = wav(320);
    const hash = async (bytes: ArrayBuffer) => hex(await crypto.subtle.digest('SHA-256', bytes));
    expect(await hash(first.audio.slice(44))).toBe(await hash((original.buffer as ArrayBuffer).slice(44, first.end + 44)));
    expect(await hash(last.audio.slice(44))).toBe(await hash((original.buffer as ArrayBuffer).slice(first.end + 44)));
  });
  it('cuts in the lowest-energy half-second within ten seconds of the five-minute target', async () => {
    const bytes = wav(320);
    bytes.fill(0, 44 + 307 * pcm.byteRate, 44 + 307.5 * pcm.byteRate);
    await bindings.RECORDINGS.put('quiet.wav', bytes);
    const plan = await planAudio(bindings.RECORDINGS, 'quiet.wav', bytes.length, 'wav');
    const chunk = await audioChunk(bindings.RECORDINGS, 'quiet.wav', plan, 0);
    expect(chunk.end / pcm.byteRate).toBe(307.25);
    expect(chunk.end % pcm.blockAlign).toBe(0);
  });
  it('skips odd-sized metadata chunks with RIFF padding', async () => {
    const plain = wav(1), bytes = new Uint8Array(plain.length + 12);
    bytes.set(plain.slice(0, 36)); bytes.set(new TextEncoder().encode('JUNK'), 36);
    new DataView(bytes.buffer).setUint32(40, 3, true); bytes.set([1, 2, 3], 44);
    bytes.set(plain.slice(36), 48); new DataView(bytes.buffer).setUint32(4, bytes.length - 8, true);
    await bindings.RECORDINGS.put('padded.wav', bytes);
    const plan = await planAudio(bindings.RECORDINGS, 'padded.wav', bytes.length, 'wav');
    expect(plan.offset).toBe(56); expect(plan.length).toBe(32000);
  });
  it('rejects malformed frame alignment and unsupported PCM formats', async () => {
    const bytes = wav(1); new DataView(bytes.buffer).setUint32(40, 31999, true);
    await bindings.RECORDINGS.put('bad.wav', bytes);
    await expect(planAudio(bindings.RECORDINGS, 'bad.wav', bytes.length, 'wav')).rejects.toThrow('invalid_wav');
    new DataView(bytes.buffer).setUint16(20, 3, true);
    await bindings.RECORDINGS.put('float.wav', bytes);
    await expect(planAudio(bindings.RECORDINGS, 'float.wav', bytes.length, 'wav')).rejects.toThrow('unsupported_audio');
  });
  it('caps high sample-rate chunks to 10MiB and defers long compressed containers', async () => {
    const upload = await fixture(1);
    await expect(planAudio(bindings.RECORDINGS, upload.key, AUDIO_CHUNK_BYTES + 1, 'm4a')).rejects.toThrow('long_compressed_audio_pending');
    const format = { channels: 2, sampleRate: 192000, byteRate: 1536000, blockAlign: 8, bits: 32 };
    const bytes = wav(1, format); await bindings.RECORDINGS.put('high.wav', bytes);
    const plan = await planAudio(bindings.RECORDINGS, 'high.wav', bytes.length, 'wav');
    expect(plan.chunkBytes + 44).toBeLessThanOrEqual(AUDIO_CHUNK_BYTES); expect(plan.chunkBytes % format.blockAlign).toBe(0);
  });
});

describe('Whisper repetition and vocabulary contract', () => {
  const phrase = '첫째 둘째 셋째 넷째 다섯째 여섯째 ';
  it('flags a six-word sequence only after more than three occurrences', () => {
    expect(repetitive(phrase.repeat(3))).toBe(false);
    expect(repetitive(phrase.repeat(4))).toBe(true);
  });
  it('passes the configured glossary and retries only the repetitive chunk once with a different beam', async () => {
    const run = vi.fn().mockResolvedValueOnce({ text: phrase.repeat(4) }).mockResolvedValueOnce({ text: '정상 시험 문장' });
    const engine = new WhisperEngine({ run } as unknown as Ai, '범용 용어집');
    expect(await engine.transcribe(new ArrayBuffer(4))).toBe('정상 시험 문장');
    expect(run).toHaveBeenCalledTimes(2);
    expect(run.mock.calls[0][1]).toMatchObject({ initial_prompt: '범용 용어집', condition_on_previous_text: false, vad_filter: true, beam_size: 5 });
    expect(run.mock.calls[1][1]).toMatchObject({ beam_size: 1, vad_filter: true });
  });
  it('rejects persistent repetition after one immediate retry, without publishing invented text', async () => {
    const run = vi.fn().mockResolvedValue({ text: phrase.repeat(4) });
    const engine = new WhisperEngine({ run } as unknown as Ai);
    await expect(engine.transcribe(new ArrayBuffer(4))).rejects.toThrow('repetition_persisted');
    expect(run).toHaveBeenCalledTimes(2);
  });
});

describe('transcription artifact and retry contract', () => {
  it('reuses saved chunks after an interrupted checkpoint and streams the full UTF-8 transcript to R2', async () => {
    const upload = await fixture();
    const call = vi.fn(async () => '첫 조각');
    const engine: TranscriptionEngine = { id: 'test-engine', transcribe: call };
    const progress = await prepareTranscription(bindings, upload, engine);
    const checkpoint = structuredClone(progress);
    expect(await transcribeNext(bindings, upload, engine, progress)).toBe(false);
    await transcribeNext(bindings, upload, engine, checkpoint);
    expect(call).toHaveBeenCalledTimes(1);
    call.mockResolvedValue('마지막 조각');
    expect(await transcribeNext(bindings, upload, engine, checkpoint)).toBe(true);
    const key = await finishTranscription(bindings, upload, checkpoint);
    expect(await (await bindings.RECORDINGS.get(key!))!.text()).toBe('첫 조각\n마지막 조각');
    expect(await finishTranscription(bindings, upload, checkpoint)).toBe(key);
    expect(call).toHaveBeenCalledTimes(2);
    expect((await bindings.RECORDINGS.get(upload.key))!.size).toBe(upload.size);
  });
  it('merges more than 128 saved chunks through resumable pages in the original order', async () => {
    const upload = await fixture(1), engine = { id: 'paged-engine', transcribe: async () => '' };
    const progress = await prepareTranscription(bindings, upload, engine);
    progress.cursor = progress.plan.length; progress.next = 260;
    const texts = Array.from({ length: 260 }, (_, i) => `시험 글 ${i}`);
    const prefix = transcriptPrefix(upload, engine.id);
    await Promise.all(texts.map(async (text, index) => {
      const length = new TextEncoder().encode((index ? '\n' : '') + text).length;
      progress.textBytes += length;
      await bindings.RECORDINGS.put(`${prefix}chunks/${index}.json`, JSON.stringify({ index, text, endByte: index + 1 }), {
        customMetadata: { textBytes: String(length) },
      });
    }));
    expect(await finishTranscription(bindings, upload, progress)).toBeUndefined();
    expect(await finishTranscription(bindings, upload, progress)).toBeUndefined();
    expect(await finishTranscription(bindings, upload, progress)).toBeUndefined();
    const key = await finishTranscription(bindings, upload, progress);
    expect(key).toBeDefined();
    expect(await (await bindings.RECORDINGS.get(key!))!.text()).toBe(texts.join('\n'));
  });
  it('allows empty speech and writes an empty transcript rather than retrying silence forever', async () => {
    const upload = await fixture(1);
    const engine = { id: 'silence-engine', transcribe: async () => '' };
    const progress = await prepareTranscription(bindings, upload, engine);
    await transcribeNext(bindings, upload, engine, progress);
    const key = await finishTranscription(bindings, upload, progress);
    expect((await bindings.RECORDINGS.head(key!))!.size).toBe(0);
    expect(await bindings.RECORDINGS.head(`${transcriptPrefix(upload, engine.id)}complete.json`)).not.toBeNull();
  });
  it('uses Whisper ko through the binding, checkpoints one chunk per alarm, and keeps transcript text out of DO storage', async () => {
    const upload = await fixture();
    const run = vi.fn(async (_model: string, _input: unknown) => ({ text: '시험 문장' }));
    const stub = await seed(upload, run);
    await runDurableObjectAlarm(stub);
    expect(await stub.status(upload.owner)).toMatchObject({ phase: 'transcribe', transcription: { completedChunks: 1, estimatedTotalChunks: 2 } });
    await runDurableObjectAlarm(stub);
    const status = await stub.status(upload.owner);
    expect(status.phase).toBe('tidy'); expect(status.transcriptKey).toContain('transcript.txt');
    expect(run).toHaveBeenCalledTimes(2);
    expect(run.mock.calls[0][0]).toBe('@cf/openai/whisper-large-v3-turbo');
    expect(run.mock.calls[0][1]).toMatchObject({ language: 'ko', task: 'transcribe', vad_filter: true });
    const stored = await runInDurableObject(stub, async (_instance, state) => JSON.stringify(await state.storage.get('job')));
    expect(stored).not.toContain('시험 문장');
    expect(await (await bindings.RECORDINGS.get(status.transcriptKey!))!.text()).toBe('시험 문장\n시험 문장');
    const identity = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => Response.json({ email: 'transcriber@example.com', scope: 'full' }));
    const request = (suffix: string) => new Request(`https://velcro.example.com/v1/uploads/${upload.id}/transcript${suffix}`, {
      headers: { Authorization: 'Bearer bxd_' + crypto.randomUUID().replaceAll('-', '') + 'x'.repeat(11) },
    });
    expect(await (await worker.fetch(request(''), bindings)).text()).toBe('시험 문장\n시험 문장');
    const chunk = await worker.fetch(request('/chunks/0'), bindings);
    expect(chunk.status).toBe(200); expect(await chunk.json()).toMatchObject({ index: 0, text: '시험 문장' });
    identity.mockImplementation(async () => Response.json({ email: 'other@example.com', scope: 'full' }));
    expect((await worker.fetch(request(''), bindings)).status).toBe(404);
    expect((await worker.fetch(request('/chunks/0'), bindings)).status).toBe(404);
  });
  it('retries the failed chunk only and preserves successful chunks and the original', async () => {
    const upload = await fixture();
    const run = vi.fn().mockResolvedValueOnce({ text: '첫 문장' }).mockRejectedValueOnce(new Error('private engine error'))
      .mockResolvedValueOnce({ text: '둘째 문장' });
    const stub = await seed(upload, run);
    await runDurableObjectAlarm(stub); await runDurableObjectAlarm(stub);
    expect(await stub.status(upload.owner)).toMatchObject({ phase: 'transcribe', attempts: 1, error: 'stage_failed', transcription: { completedChunks: 1 } });
    await runInDurableObject(stub, async (_instance, state) => {
      const job = await state.storage.get<{ nextAttemptAt: number }>('job');
      job!.nextAttemptAt = Date.now() - 1; await state.storage.put('job', job);
    });
    await runDurableObjectAlarm(stub);
    expect((await stub.status(upload.owner)).phase).toBe('tidy');
    expect(run).toHaveBeenCalledTimes(3);
    expect(await bindings.RECORDINGS.head(upload.key)).not.toBeNull();
    const objects = await bindings.RECORDINGS.list({ prefix: transcriptPrefix(upload, 'cf-whisper-large-v3-turbo-ko-v2') });
    expect(objects.objects.filter(o => o.key.includes('/chunks/'))).toHaveLength(2);
  });
});
