import { DurableObject } from 'cloudflare:workers';
import { hex, HttpError, readText, type Env } from './common';
import { AudioError } from './audio';
import { RepetitionError, WhisperEngine, prepareTranscription, transcribeNext, finishTranscription, type TranscriptionProgress } from './transcribe';
import { MultipartTransport, PART_SIZE, missingUpload, type UploadSession } from './transport';
export interface Upload {
  id: string; owner: string; key: string; size: number; sha256: string;
  ext: string; recordedAt: string;
}
export type Stage = 'transcribe' | 'tidy' | 'deliver';
interface Job {
  upload: Upload;
  session: UploadSession;
  parts: R2UploadedPart[];
  assembled: boolean;
  phase: 'uploading' | 'received' | Stage | 'done';
  attempts: number;
  nextAttemptAt?: number;
  error?: string;
  transcript?: string;
  transcriptKey?: string;
  transcription?: TranscriptionProgress;
  tidied?: string;
  receipt?: string;
}
export interface EngineRequest {
  id: string; key: string; sha256: string; recordedAt: string;
  idempotencyKey: string; text?: string; transcriptKey?: string;
}
export function retryDelay(attempt: number): number {
  return Math.min(600_000 * 2 ** Math.min(attempt - 1, 8), 86_400_000);
}
export class Recording extends DurableObject<Env> {
  private async exclusive<T>(callback: () => Promise<T>): Promise<T> {
    const result = await this.ctx.blockConcurrencyWhile(async () => {
      try { return { value: await callback() }; }
      catch (error) {
        // Expected client errors must not break the Durable Object input gate.
        if (error instanceof HttpError) return { error };
        throw error;
      }
    });
    if ('error' in result) throw result.error;
    return result.value;
  }
  async reserve(upload: Upload): Promise<{ upload: Upload; session: string; partSize: number; partCount: number }> {
    return this.exclusive(async () => {
      let job = await this.ctx.storage.get<Job>('job');
      if (job && (job.upload.owner !== upload.owner || job.upload.sha256 !== upload.sha256 || job.upload.size !== upload.size)) {
        throw new HttpError(409, 'upload_conflict');
      }
      if (!job || (job.phase === 'uploading' && job.error)) {
        // A failed checksum stays preserved. A retry gets a fresh, unguessable key.
        upload.key = `${this.env.R2_PREFIX}${upload.id}/${crypto.randomUUID()}.${upload.ext}`;
        const session = await new MultipartTransport(this.env.RECORDINGS).create(upload.key, upload.sha256);
        job = { upload, session, parts: [], assembled: false, phase: 'uploading', attempts: 0 };
        await this.ctx.storage.put('job', job);
      }
      return { upload: job.upload, session: job.session.uploadId, partSize: PART_SIZE,
        partCount: Math.ceil(job.upload.size / PART_SIZE) };
    });
  }
  async part(owner: string, session: string, number: number): Promise<{ session: UploadSession; size: number }> {
    const job = await this.owned(owner);
    if (job.phase !== 'uploading' || job.assembled || job.session.uploadId !== session) throw new HttpError(409, 'upload_closed');
    const count = Math.ceil(job.upload.size / PART_SIZE);
    if (!Number.isSafeInteger(number) || number < 1 || number > count) throw new HttpError(400, 'invalid_part');
    return { session: job.session, size: Math.min(PART_SIZE, job.upload.size - (number - 1) * PART_SIZE) };
  }
  async savePart(owner: string, session: string, part: R2UploadedPart): Promise<void> {
    await this.exclusive(async () => {
      await this.part(owner, session, part.partNumber);
      const job = await this.owned(owner);
      job.parts = [...job.parts.filter(p => p.partNumber !== part.partNumber), part].sort((a, b) => a.partNumber - b.partNumber);
      await this.ctx.storage.put('job', job);
    });
  }
  async expire(owner: string, session: string): Promise<void> {
    await this.exclusive(async () => {
      const job = await this.owned(owner);
      if (job.phase === 'uploading' && !job.assembled && job.session.uploadId === session) {
        job.error = 'upload_expired';
        await this.ctx.storage.put('job', job);
      }
    });
  }
  private async owned(owner: string): Promise<Job> {
    const job = await this.ctx.storage.get<Job>('job');
    if (!job || job.upload.owner !== owner) throw new HttpError(404, 'upload_not_found');
    return job;
  }
  async status(owner: string) {
    const job = await this.owned(owner);
    return { id: job.upload.id, phase: job.phase, attempts: job.attempts,
      nextAttemptAt: job.nextAttemptAt, error: job.error, transcriptKey: job.transcriptKey,
      transcription: job.transcription ? { completedChunks: job.transcription.next, totalChunks: job.transcription.cursor >= job.transcription.plan.length ? job.transcription.next : undefined,
        estimatedTotalChunks: job.transcription.next + Math.ceil((job.transcription.plan.length - job.transcription.cursor) / job.transcription.plan.chunkBytes) } : undefined };
  }
  async transcriptArtifact(owner: string, chunk?: number): Promise<string> {
    const job = await this.owned(owner);
    if (chunk === undefined) {
      if (!job.transcriptKey) throw new HttpError(409, 'transcript_pending');
      return job.transcriptKey;
    }
    if (!Number.isSafeInteger(chunk) || chunk < 0 || !job.transcription || chunk >= job.transcription.next) {
      throw new HttpError(404, 'transcript_chunk_not_found');
    }
    return `${job.upload.key}.transcripts/${job.transcription.engine}/chunks/${chunk}.json`;
  }
  async complete(owner: string, session: string): Promise<void> {
    const job = await this.exclusive(async () => {
      const job = await this.owned(owner);
      if (job.session.uploadId !== session) throw new HttpError(409, 'invalid_upload_session');
      if (job.phase !== 'uploading') return job;
      if (job.parts.length !== Math.ceil(job.upload.size / PART_SIZE)) throw new HttpError(409, 'missing_parts');
      if (!job.assembled) {
        // Completion may have committed just before an eviction. HEAD recovers it.
        if (!await this.env.RECORDINGS.head(job.upload.key)) {
          try { await new MultipartTransport(this.env.RECORDINGS).complete(job.session, job.parts); }
          catch (error) {
            if (!missingUpload(error)) throw error;
            job.error = 'upload_expired';
            await this.ctx.storage.put('job', job);
            throw new HttpError(409, 'upload_expired');
          }
        }
        job.assembled = true;
        await this.ctx.storage.put('job', job);
      }
      return job;
    });
    if (job.phase !== 'uploading') return;
    const { key, size, sha256 } = job.upload;
    const head = await this.env.RECORDINGS.head(key);
    if (!head || head.size !== size || head.customMetadata?.sha256 !== sha256) {
      await this.reject(session, 'object_mismatch');
      throw new HttpError(409, 'object_mismatch');
    }
    // R2 S3 has no full-object SHA-256 checksum. Metadata alone is not evidence.
    const object = await this.env.RECORDINGS.get(key, { onlyIf: { etagMatches: head.etag } });
    if (!object || !('body' in object)) throw new HttpError(409, 'object_changed');
    const hash = new crypto.DigestStream('SHA-256');
    await object.body.pipeTo(hash);
    if (hex(await hash.digest) !== sha256) {
      await this.reject(session, 'checksum_mismatch');
      throw new HttpError(409, 'checksum_mismatch');
    }
    await this.exclusive(async () => {
      const current = await this.owned(owner);
      if (current.session.uploadId !== session) throw new HttpError(409, 'invalid_upload_session');
      if (current.phase !== 'uploading') return;
      current.phase = 'received';
      delete current.error;
      // Persist the outbox wakeup with the transition, before contacting Queues.
      await this.ctx.storage.transaction(async tx => {
        await tx.put('job', current);
        await tx.setAlarm(Date.now() + 1000);
      });
    });
  }
  private async reject(session: string, error: string): Promise<void> {
    await this.exclusive(async () => {
      const job = await this.ctx.storage.get<Job>('job');
      if (job?.phase === 'uploading' && job.session.uploadId === session) {
        job.error = error;
        await this.ctx.storage.put('job', job);
      }
    });
  }
  async start(): Promise<void> {
    await this.exclusive(async () => {
      const job = await this.ctx.storage.get<Job>('job');
      if (!job || job.phase !== 'received') return;
      job.phase = 'transcribe';
      await this.ctx.storage.transaction(async tx => {
        await tx.put('job', job);
        await tx.setAlarm(Date.now() + 1000);
      });
    });
  }
  async alarm(): Promise<void> {
    // Alarms are serialized by the runtime. Do not hold the 30-second input gate across AI inference.
    const job = await this.ctx.storage.get<Job>('job');
    if (!job || job.phase === 'uploading' || job.phase === 'done') return;
    if (job.phase === 'received') {
      await this.ctx.storage.setAlarm(Date.now() + 60_000);
      await this.env.INGEST.send({ id: job.upload.id });
      return;
    }
    if (job.nextAttemptAt && job.nextAttemptAt > Date.now()) {
      await this.ctx.storage.setAlarm(job.nextAttemptAt);
      return;
    }
    const stage = job.phase;
    job.attempts++;
    job.nextAttemptAt = Date.now() + retryDelay(job.attempts);
    await this.ctx.storage.transaction(async tx => {
      await tx.put('job', job);
      await tx.setAlarm(job.nextAttemptAt!);
    });
    try {
      if (stage === 'transcribe' && this.env.TRANSCRIBE_ENGINE === 'workers-ai') {
        if (!this.env.AI) throw new Error('engine_not_configured');
        const engine = new WhisperEngine(this.env.AI, this.env.TRANSCRIBE_GLOSSARY);
        job.transcription ??= await prepareTranscription(this.env, job.upload, engine);
        if (await transcribeNext(this.env, job.upload, engine, job.transcription)) {
          job.transcriptKey = await finishTranscription(this.env, job.upload, job.transcription);
          if (job.transcriptKey) job.phase = 'tidy';
        }
      } else {
        const engine = { transcribe: this.env.TRANSCRIBE, tidy: this.env.TIDY, deliver: this.env.DELIVER }[stage];
        if (!engine) throw new Error('engine_not_configured');
        const input: EngineRequest = {
          id: job.upload.id, key: job.upload.key, sha256: job.upload.sha256,
          recordedAt: job.upload.recordedAt, idempotencyKey: `${job.upload.id}:${stage}`,
          text: stage === 'tidy' ? job.transcript : stage === 'deliver' ? job.tidied : undefined,
          transcriptKey: stage === 'tidy' ? job.transcriptKey : undefined,
        };
        const response = await engine.fetch('https://engine.internal/v1/' + stage, {
          method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': input.idempotencyKey },
          body: JSON.stringify(input), signal: AbortSignal.timeout(20_000),
        });
        if (!response.ok) throw new Error('engine_failed');
        const output = await readText(response.body, 262144);
        const data = JSON.parse(output) as Record<string, unknown>;
        const result = data[stage === 'deliver' ? 'receipt' : 'text'];
        if (typeof result !== 'string' || !result.trim()) throw new Error('invalid_engine_output');
        if (stage === 'transcribe') { job.transcript = result; job.phase = 'tidy'; }
        if (stage === 'tidy') { job.tidied = result; job.phase = 'deliver'; }
        if (stage === 'deliver') { job.receipt = result; job.phase = 'done'; }
      }
      job.attempts = 0;
      delete job.error;
      delete job.nextAttemptAt;
    } catch (error) {
      // Engine errors can contain transcript text or credentials. Never persist them.
      job.error = error instanceof AudioError ? error.code : error instanceof RepetitionError ? 'repetition_persisted' : 'stage_failed';
    }
    await this.ctx.storage.transaction(async tx => {
      await tx.put('job', job);
      if (job.phase === 'done') await tx.deleteAlarm();
      else await tx.setAlarm(job.nextAttemptAt ?? Date.now() + 1000);
    });
  }
}
