// A future S3 signer can supply a different transport without changing job ownership.
export const PART_SIZE = 50 * 1024 * 1024;
export interface UploadSession { key: string; uploadId: string }
export interface UploadTransport {
  create(key: string, sha256: string): Promise<UploadSession>;
  put(session: UploadSession, part: number, bytes: ReadableStream): Promise<R2UploadedPart>;
  complete(session: UploadSession, parts: R2UploadedPart[]): Promise<void>;
}
export class MultipartTransport implements UploadTransport {
  constructor(private bucket: R2Bucket) {}
  async create(key: string, sha256: string): Promise<UploadSession> {
    const upload = await this.bucket.createMultipartUpload(key, { customMetadata: { sha256 } });
    return { key, uploadId: upload.uploadId };
  }
  async put(session: UploadSession, part: number, bytes: ReadableStream): Promise<R2UploadedPart> {
    return this.bucket.resumeMultipartUpload(session.key, session.uploadId).uploadPart(part, bytes);
  }
  async complete(session: UploadSession, parts: R2UploadedPart[]): Promise<void> {
    await this.bucket.resumeMultipartUpload(session.key, session.uploadId).complete(parts);
  }
}

export function missingUpload(error: unknown): boolean {
  return error instanceof Error && /NoSuchUpload|multipart upload does not exist|\(10024\)/i.test(error.message);
}
