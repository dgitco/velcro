export const AUDIO_CHUNK_BYTES = 10 * 1024 * 1024;
export class AudioError extends Error {
  constructor(public code: 'invalid_wav' | 'unsupported_audio' | 'long_compressed_audio_pending') { super(code); }
}
export interface PcmFormat { channels: number; sampleRate: number; byteRate: number; blockAlign: number; bits: number }
export interface AudioPlan { offset: number; length: number; chunkBytes: number; pcm?: PcmFormat }
export async function range(bucket: R2Bucket, key: string, offset: number, length: number): Promise<ArrayBuffer> {
  const object = await bucket.get(key, { range: { offset, length } });
  if (!object) throw new Error('recording_missing');
  const bytes = await object.arrayBuffer();
  if (bytes.byteLength !== length) throw new AudioError('invalid_wav');
  return bytes;
}
const tag = (bytes: ArrayBuffer, offset = 0) => String.fromCharCode(...new Uint8Array(bytes, offset, 4));
export async function planAudio(bucket: R2Bucket, key: string, size: number, ext: string): Promise<AudioPlan> {
  if (ext !== 'wav') {
    // Compressed containers cannot safely be cut at arbitrary upload boundaries.
    if (size > AUDIO_CHUNK_BYTES) throw new AudioError('long_compressed_audio_pending');
    return { offset: 0, length: size, chunkBytes: AUDIO_CHUNK_BYTES };
  }
  if (size < 44) throw new AudioError('invalid_wav');
  const header = await range(bucket, key, 0, 12);
  if (tag(header) !== 'RIFF' || tag(header, 8) !== 'WAVE') throw new AudioError('invalid_wav');
  const end = new DataView(header).getUint32(4, true) + 8;
  if (end > size || end < 44) throw new AudioError('invalid_wav');
  let offset = 12, pcm: PcmFormat | undefined;
  for (let n = 0; n < 128 && offset + 8 <= end; n++) {
    const chunk = await range(bucket, key, offset, 8);
    const length = new DataView(chunk).getUint32(4, true);
    if (offset + 8 + length > end) throw new AudioError('invalid_wav');
    if (tag(chunk) === 'fmt ') {
      if (length < 16) throw new AudioError('invalid_wav');
      const fmt = new DataView(await range(bucket, key, offset + 8, 16));
      if (fmt.getUint16(0, true) !== 1) throw new AudioError('unsupported_audio');
      pcm = { channels: fmt.getUint16(2, true), sampleRate: fmt.getUint32(4, true),
        byteRate: fmt.getUint32(8, true), blockAlign: fmt.getUint16(12, true), bits: fmt.getUint16(14, true) };
      if (![1, 2].includes(pcm.channels) || ![8, 16, 24, 32].includes(pcm.bits) ||
          pcm.sampleRate < 8000 || pcm.sampleRate > 192000 ||
          pcm.blockAlign !== pcm.channels * pcm.bits / 8 || pcm.byteRate !== pcm.sampleRate * pcm.blockAlign) {
        throw new AudioError('unsupported_audio');
      }
    }
    if (tag(chunk) === 'data') {
      if (!pcm || length % pcm.blockAlign !== 0) throw new AudioError('invalid_wav');
      const chunkBytes = Math.floor(Math.min(AUDIO_CHUNK_BYTES - 44, pcm.byteRate * 300) / pcm.blockAlign) * pcm.blockAlign;
      return { offset: offset + 8, length, chunkBytes, pcm };
    }
    offset += 8 + length + (length % 2);
  }
  throw new AudioError('invalid_wav');
}
export function wavHeader(pcm: PcmFormat, length: number): Uint8Array {
  const bytes = new Uint8Array(44), view = new DataView(bytes.buffer);
  const write = (offset: number, value: string) => bytes.set(new TextEncoder().encode(value), offset);
  write(0, 'RIFF'); view.setUint32(4, length + 36, true); write(8, 'WAVE'); write(12, 'fmt ');
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, pcm.channels, true);
  view.setUint32(24, pcm.sampleRate, true); view.setUint32(28, pcm.byteRate, true);
  view.setUint16(32, pcm.blockAlign, true); view.setUint16(34, pcm.bits, true);
  write(36, 'data'); view.setUint32(40, length, true);
  return bytes;
}
function frameEnergy(view: DataView, frame: number, pcm: PcmFormat): number {
  let energy = 0;
  for (let channel = 0; channel < pcm.channels; channel++) {
    const offset = frame * pcm.blockAlign + channel * pcm.bits / 8;
    let value: number;
    if (pcm.bits === 8) value = view.getUint8(offset) - 128;
    else if (pcm.bits === 16) value = view.getInt16(offset, true);
    else if (pcm.bits === 24) value = (view.getUint8(offset) | view.getUint8(offset + 1) << 8 | view.getInt8(offset + 2) << 16);
    else value = view.getInt32(offset, true);
    energy += value * value;
  }
  return energy;
}
async function quietEnd(bucket: R2Bucket, key: string, plan: AudioPlan, cursor: number): Promise<number> {
  const pcm = plan.pcm!, target = Math.min(cursor + plan.chunkBytes, plan.length);
  const maximum = Math.min(plan.length, cursor + Math.floor((AUDIO_CHUNK_BYTES - 44) / pcm.blockAlign) * pcm.blockAlign);
  // Keep a short final tail together. Smaller high-rate chunks use a proportional search window.
  const radius = Math.floor(Math.min(pcm.sampleRate * 10, plan.chunkBytes / pcm.blockAlign * 0.05));
  if (plan.length <= target + radius * pcm.blockAlign && plan.length <= maximum) return plan.length;
  const half = Math.floor(pcm.sampleRate / 4), width = 2 * half;
  const targetFrame = target / pcm.blockAlign;
  const first = Math.max(cursor / pcm.blockAlign + 1, targetFrame - radius);
  const last = Math.min(maximum / pcm.blockAlign, targetFrame + radius);
  const readStart = Math.max(cursor / pcm.blockAlign, first - half);
  const readEnd = Math.min(plan.length / pcm.blockAlign, last + half);
  const view = new DataView(await range(bucket, key, plan.offset + readStart * pcm.blockAlign, (readEnd - readStart) * pcm.blockAlign));
  let energy = 0, best = Infinity, cut = targetFrame;
  for (let i = 0; i < width; i++) energy += frameEnergy(view, i, pcm);
  const hop = Math.max(1, Math.floor(pcm.sampleRate / 100));
  for (let start = 0; start + width <= readEnd - readStart; start++) {
    if (start) energy += frameEnergy(view, start + width - 1, pcm) - frameEnergy(view, start - 1, pcm);
    const center = readStart + start + half;
    if (center < first || center > last || start % hop) continue;
    if (energy < best || (energy === best && Math.abs(center - targetFrame) < Math.abs(cut - targetFrame))) {
      best = energy; cut = center;
    }
  }
  return cut * pcm.blockAlign;
}
export async function audioChunk(bucket: R2Bucket, key: string, plan: AudioPlan, cursor: number) {
  if (!Number.isSafeInteger(cursor) || cursor < 0 || cursor >= plan.length || (plan.pcm && cursor % plan.pcm.blockAlign)) throw new AudioError('invalid_wav');
  const end = plan.pcm ? await quietEnd(bucket, key, plan, cursor) : plan.length;
  const data = await range(bucket, key, plan.offset + cursor, end - cursor);
  if (!plan.pcm) return { audio: data, end };
  const result = new Uint8Array(data.byteLength + 44);
  result.set(wavHeader(plan.pcm, data.byteLength)); result.set(new Uint8Array(data), 44);
  return { audio: result.buffer, end };
}
