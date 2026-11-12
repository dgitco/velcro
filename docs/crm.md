# CRM file upload client

`velcro-crm` is a Python entry point over `velcro_crm.py`, shared by the zsh
command and Windows tray. It uses only the standard library. No server is
configured by default; use `velcro set crm.url https://api.example.com` (Windows
source: `python windows/velcro_win.py set crm.url https://api.example.com`).
Only an HTTPS origin is accepted, without user info, path, query or fragment.
Loopback HTTP origins are supported for fake servers.

Set the approval page separately with
`velcro set crm.approve https://auth.example.com/device/`. The server does not
return an approval URL. With this setting, connect prints and opens that address
with `?code=XXXX-XXXX`, and prints the same code separately. Eight-character
codes are grouped 4-4; other lengths are left unchanged. Without `crm.approve`,
connect prints only the code and “승인 화면에서 이 코드를 넣으세요”, without opening
a browser. The API origin and approval origin may differ.

## Commands

- `velcro crm connect [--name <device name>]`: print/open approval, poll until
  approved, denied or expired, save the credential and verify with whoami.
  The optional device name must contain 1–80 characters.
- `velcro crm disconnect`: delete this origin's local credential. Server-side
  revocation must be done in the dashboard; no revoke endpoint is assumed.
- `velcro crm leads [--json]`: most recently updated cards first. An unavailable
  recent-cards endpoint (404/405/501) asks the user to paste a card address.
- `velcro crm send <file> --lead <id or card URL>`: validate, hash, create, upload,
  complete, then append success history. No card is ever selected automatically.
  Accepts a plain ID, `https://auth.example.com/sales?lead=<id>`, or
  `https://auth.example.com/sales/leads/<id>` (also either relative page path).
- `velcro crm pending`: JSON of local recordings awaiting a human's decision.
- Internal `enqueue <file> --sha256 <hash> --at <import time> [--duration <seconds>]`:
  append local bookkeeping only. This makes no HTTP request and reads no token.

Direct `velcro-crm` invocations use the same settings file; `--url <origin>` before
the subcommand overrides it. On Windows from source, substitute
`python windows/velcro_win.py crm` for `velcro crm`.

## HTTP contract

The upload response union is specified by the integration plan and implemented
as follows. This repository contains the client, not the server migration or
ready-file cleanup implementation.

| Request | Accepted success |
| --- | --- |
| `POST /api/rpc/sales/leads/:id/files` with `{extension,sizeBytes,sha256}` | `201 {fileId,file,upload:{method:"PUT",url,headers:{"x-amz-checksum-sha256":…},expiresAt}}` or `200 {file,alreadyUploaded:true}` |
| `PUT upload.url` with the file and `upload.headers` | HTTP 2xx; no operator bearer is attached |
| `POST /api/rpc/sales/leads/:id/files/:fileId/complete` with `{}` | `204`, or the ready-file race result `200 {file:{id},alreadyUploaded:true}` |

The extension is lowercase without a dot. The allowed extensions are `m4a`,
`mp3`, `wav`, `txt`, `md`, `pdf`; the limit is 200 MiB inclusive. The completion
race result replaces the pending file ID in local history with the ready ID.
A create response with `alreadyUploaded:true` skips PUT and complete entirely.
No duplicate file is created solely to query local history.

The device connection follows the supplied server contract:

| Request | Response / behavior |
| --- | --- |
| `POST /api/operator-auth/device/start` with `{scope:"sales_file_upload",displayName?:string}`; no other keys | `201 {userCode,deviceSecret,expiresAt,interval}`; expiry is an ISO timestamp, interval is seconds |
| Browser approval | `crm.approve` + `?code=<formatted userCode>`; no default page |
| `POST /api/operator-auth/device/poll` with `{userCode,deviceSecret}` | `200 {status:"pending"\|"approved"\|"denied"\|"expired",token?:"<opaque token>",expiresAt?:"<token expiry ISO>"}`; unknown code or wrong secret returns `404` |
| `GET /api/rpc/session/whoami` with bearer | HTTP 2xx confirms the connection; its body is not printed |

Pending polls wait `interval` seconds, up to the start response's absolute
`expiresAt`. Denied reports “승인이 거절됐어요”; expired, deadline reached or 404
reports “코드가 만료됐어요, 다시 연결하세요”. Approved must include a token, which
the server returns only once. OAuth error responses are not used. The device
secret is transient and is never printed, logged or included in error messages.

The recent-cards endpoint remains an assumption outside the supplied device
contract: `GET /api/rpc/sales/leads/recent` returns a direct array of
`[{id,name,stage,updatedAt}]`. No private server was contacted. The client does
not renew the device credential or assume access to any other operator scope.

Transient PUT and complete failures receive three retries after 1, 4, 16 seconds.
API 401 or an expired saved token reports “다시 연결하세요(velcro crm connect)”.
Saved token expiry is checked before authenticated HTTP requests. API 403
reports that this card cannot be uploaded to.
Storage 401/403 or a recognized expiration code obtains a new signed URL by
restarting create (at most three renewals, also with 1, 4, 16 second delays).
Authorization is attached only to requests to the configured CRM origin. HTTP
redirects are rejected for both CRM and storage. Server error bodies and raw
transport/subprocess exceptions are never shown.

## Local state and human approval

State is under `~/Library/Application Support/velcro` on Mac,
`%LOCALAPPDATA%\velcro` on Windows, or `~/.local/state/velcro` on Linux
(`XDG_STATE_HOME` is honored).

- `crm-sent.jsonl`: `{sha256,leadId,fileId,at,already,url}` per success. `url` is
  additional origin metadata so distinct CRM servers do not share deduplication.
  Matching origin + card + hash skips all HTTP requests, even when disconnected.
- `crm-pending.jsonl`: append-only queue/disposition events, separated by origin.
  Skip and successful upload persist across restarts. File modification time is
  shown as the recording time; import time is retained separately. WAV duration
  is available on Windows; unreadable/other audio durations show as unknown.
- Linux credential: an origin-hashed `crm-token-*` file created with mode 0600.
  JSONL files also use 0600. Mac uses Keychain service `velcro-crm` with the origin
  as account; Windows uses generic credential target `velcro-crm:<origin>`.
  Each credential stores `{token,expiresAt}` together, using the poll response's
  optional token expiry. Older token-only credentials remain readable and rely
  on server 401 for expiry. Disconnect removes the token and expiry together.

Verified imports with `crm.url` configured add to the queue. A recording waiting
in the import spool is available there too; moving it to its destination updates
the existing queue path, preserving sent/skipped decisions. Existing destination
manifests are not backfilled. CRM queue errors do not change verified import or
source-deletion decisions. Nothing changes recording engines, hooks, timers or
destination settings. A user may always send a file explicitly from the CLI.

The signed PUT must carry the file's own `x-amz-checksum-sha256` (base64 of the
SHA-256 the client computed); a create answer whose header is missing or different
is refused before any bytes are sent, so storage cannot accept other content.

The Windows tray item appears only after `crm.url` is set, and the window refuses
to connect until `crm.approve` is set; the approval code stays on screen while
connecting. The Windows window never preselects a card. An empty card selection cannot call
the upload client. Failed uploads remain pending. Skipping writes a local event
and sends nothing. The Mac app (menu, window, Settings → CRM) only runs these
`velcro crm` commands; it has no network or credential code of its own.

## Verification

`tests/test_crm.py` uses synthetic WAV files, a fake credential store, and an
in-memory HTTP wire: urllib/http.client serialize real HTTP bytes, and
BaseHTTPRequestHandler parses and responds to them. This runs even in an
executor that prohibits loopback sockets. It covers authentication, retries,
response unions, permission errors, history, origin separation, local-only
queueing and credential output suppression. Windows Cred* calls are replaced by
fake functions for ctypes adapter tests; this does not establish behavior on
actual Windows. Windows packaging/tray rendering still require validation on Windows; the Keychain
write and the Mac app were checked on a test Mac.
