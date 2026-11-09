# 녹음 클라우드 1단계

목표: 기기 로그인으로 녹음을 받아 R2에 보존하고, 녹음별 Durable Object가 받아쓰기 → 정리 → 전달을 재시도한다. 앱 연결과 실제 엔진·전달 규칙은 이 단계에 포함하지 않는다.

## 인증

`DEVICE_AUTH_URL`은 운영 설정 값이다. 공개 코드에는 계정·회원·기기 식별값을 넣지 않는다.

1. `GET /v1/auth/device`에서 기존 인증 서버의 기기 코드·토큰 주소를 읽는다.
2. 앱이 코드 주소에 `POST {name, tools:["velcro"]}`를 보낸다. 응답의 `verify_url`을 브라우저에서 열어 승인한다.
3. 토큰 주소에 `POST {device_code}`를 `interval`초마다 보내고, `state:"approved"`일 때의 `token`을 기기에 보관한다. 로컬 콜백·PKCE도 인증 서버가 지원한다.
4. 클라우드 요청에 `Authorization: Bearer <기기 토큰>`을 붙인다. API 키 입력 칸과 별도 클라우드 토큰은 없다.

클라우드는 인증 서버의 `/api/device/me`로 확인한다. 같은 Cloudflare 계정에서는 선택적 `BOARDER` 서비스 바인딩으로 직접 호출한다. 바인딩이 없으면 설정한 HTTPS 주소로 호출한다. `full` 또는 소유자의 `watch`만 허용하며 `harness`는 거절한다. 확인한 회원 이메일을 인증 서버 주소와 함께 해시해 소유권을 구분한다. 서로 다른 기기도 같은 회원이면 중복 검사를 공유한다. 성공 응답만 메모리에 30초 캐시하며 토큰 원문은 캐시에 남기지 않는다. 폐기 반영은 최대 30초 걸린다. 인증 서버 장애 시 접근을 허용하지 않는다.

## 업로드 계약

`POST /v1/uploads`에 `{size, sha256, ext, recordedAt}`를 보낸다. 원래 파일 이름이나 소유권 필드는 거절한다. 크기는 기본 1바이트~4 GiB(`MAX_UPLOAD_BYTES`, 기본 `4294967296`), SHA-256은 소문자 64자리, 시각은 유효한 UTC ISO 8601이다. 확장자는 `wav`, `mp3`, `m4a`, `mp4`, `ogg`, `flac`, `aac`, `wma` 중 하나다.

응답은 `{id, transport:"multipart", uploadSession, partSize, partCount, partUrl}`이다. 조각은 50 MiB씩이며 마지막만 더 작을 수 있다. 각 조각은 `PUT /v1/uploads/:id/parts/:n`으로 보낸다. `Upload-Session`과 정확한 `Content-Length`가 필요하다. 바이트는 R2 바인딩 `uploadPart`로 흘려보내고 메모리에 전부 담지 않는다.

`POST /v1/uploads/:id/complete`, 본문 `{}`, 같은 `Upload-Session`으로 조립·검증한다. R2 HEAD의 크기·SHA-256 메타데이터를 대조하고, 메타데이터 위조도 잡도록 R2 GET 스트림의 실제 SHA-256을 추가 계산한다. 성공은 `202`, 상태는 `GET /v1/uploads/:id`로 읽는다. 클라이언트는 성공 응답을 확인하기 전 원본을 보관해야 한다.

같은 회원·SHA-256에는 Durable Object 하나만 배정한다. 완료된 녹음의 덮어쓰기·삭제 API는 없다. 체크섬이 다른 파일도 보존하고, 다시 시작하면 같은 녹음 ID 아래 새 무작위 경로·업로드 세션을 발급한다. 다른 회원은 상태·조각·완료 경로에 접근할 수 없다. 미완성 조각이 만료되면 `409 upload_expired`를 반환하고, 다음 업로드 시작 요청에서 새 세션으로 복구한다.

운영자는 `MAX_UPLOAD_BYTES`로 상한을 조절할 수 있다. 50 MiB 조각 10,000개를 넘는 설정은 거절한다.

현재 전송은 `UploadTransport` 구현인 `MultipartTransport`다. S3 서명 열쇠 없이 동작하며, 나중에 서명 주소 방식으로 교체할 자리를 분리했다. 지금은 서명 주소를 반환하지 않는다.

## 처리·재시도

상태는 `uploading` → `received` → `transcribe` → `tidy` → `deliver` → `done`이다. 검증 뒤 저장한 알람이 큐로 보내며, 큐 전달이 실패하거나 알림이 유실돼도 다시 보낸다. 큐 소비자는 처리를 시작하고 단계마다 Durable Object 알람으로 이어 간다.

실패한 단계만 10·20·40분으로 재시도하고 이후 두 배씩 늘려 최대 24시간 간격을 유지한다. 녹음은 실패해도 남는다. 완료된 단계는 저장해 다시 실행하지 않는다. 외부 호출 성공 직후 상태 저장 전에 중단될 가능성은 있으므로 엔진·훅은 `Idempotency-Key`로 부수 효과 중복을 막아야 한다.

`TRANSCRIBE`, `TIDY`, `DELIVER`는 선택적 Worker 서비스 바인딩이다. HTTP `POST /v1/<단계>`에 `{id,key,sha256,recordedAt,idempotencyKey,text?}`를 보낸다. 받아쓰기·정리는 `{text}`, 전달은 `{receipt}`를 반환한다. 응답 상한은 256 KiB, 호출 시간은 20초다. 큰 파일의 비동기 받아쓰기는 다음 단계에서 이 계약을 확장한다. 바인딩이 없으면 재시도 상태로 남으며 가짜 결과를 만들지 않는다. 원문·엔진 오류 상세는 상태 응답과 로그에 쓰지 않는다.

## 개발·배포

```sh
pnpm install
pnpm check
pnpm test
```

시험은 Vitest의 Workers 실행 환경에서 실제 로컬 R2·Durable Object·가짜 엔진을 사용한다. 운영 설정은 저장소 밖 또는 무시되는 `wrangler.local.jsonc`에 `account_id`와 `vars.DEVICE_AUTH_URL`을 지정한다. Cloudflare 인증은 운영자의 OAuth 환경변수로 주입한다.

기존 버킷 `0bridge-backups`, 접두사 `datalake/velcro/recordings/`만 사용한다. 버킷 생성 명령은 없다. 먼저 `velcro-recordings` 큐를 만들고 `wrangler deploy --config wrangler.local.jsonc`로 배포한다. 실제 엔진을 붙이기 전에는 테스트 녹음만 보낸다.

이미 있는 버킷 수명 규칙도 확인해야 한다. 완료된 녹음에는 만료 규칙을 적용하지 않는다. 미완성 멀티파트 조각의 기존 정리 규칙은 클라이언트의 로컬 원본 보관과 함께 운영한다. 이 Worker는 삭제·중단 API를 제공하지 않는다.

되돌리기는 Worker를 이전 버전으로 배포하고 새 업로드를 멈추는 것이다. R2 원본과 Durable Object 저장소는 유지한다. 앱·기존 서버 타이머를 변경하지 않는다.

## 검증 증거

[운영 무음 WAV · 워커 시험 결과](evidence/README.md). 재현은 `VELCRO_DEVICE_TOKEN`을 환경변수로 주입한 뒤 `python3 scripts/smoke.py --base <Worker 주소> --output <증거 JSON>`으로 한다. 토큰·개인 녹음은 저장소에 두지 않는다.
