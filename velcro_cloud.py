#!/usr/bin/env python3
"""Shared Mac/Windows cloud login and resumable uploads. Python standard library only."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from velcro_credentials import Credentials, CredentialError

MANIFEST = ".velcro-import.jsonl"
RECEIVED = {"received", "transcribe", "tidy", "deliver", "done"}


class CloudError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


def cloud_url(value):
    u = urllib.parse.urlsplit(value)
    if (u.scheme != "https" or not u.hostname or u.username or u.password or
            u.path not in ("", "/") or u.query or u.fragment or any(x.isspace() for x in value)):
        raise CloudError("Cloud address must be an HTTPS server address without a path or password")
    return value.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward credentials or recordings to a redirected address.


def request(url, method="GET", data=None, headers=None):
    h = {"User-Agent": "velcro-cloud-app/1", **(headers or {})}
    if isinstance(data, dict):
        data = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    # A 50 MiB part can take several minutes on a slow uplink. Keep control requests short.
    timeout = max(120, len(data) / (128 * 1024) + 30) if method == "PUT" and data is not None else 120
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as r:
            result = json.loads(r.read(65537))
            if not isinstance(result, dict):
                raise ValueError()
            return result
    except urllib.error.HTTPError as e:
        # Response bodies and URLs can contain credentials. Report only a fixed message and status.
        status = e.code
        e.close()
        raise CloudError("Cloud request failed (HTTP %d)" % status, status) from None
    except (OSError, ValueError) as e:
        kind = type(e.reason).__name__ if isinstance(e, urllib.error.URLError) else type(e).__name__
        raise CloudError("Cloud unavailable (%s); recording kept for retry" % kind) from None


def login(base, credentials=None, notify=print, open_browser=webbrowser.open):
    base = cloud_url(base)
    credentials = credentials or Credentials(base)
    endpoints = request(base + "/v1/auth/device")
    code_url, token_url = endpoints["deviceAuthorizationEndpoint"], endpoints["tokenEndpoint"]
    auth = cloud_url(urllib.parse.urlunsplit((*urllib.parse.urlsplit(code_url)[:2], "", "", "")))
    if code_url != auth + "/api/device/code" or token_url != auth + "/api/device/token":
        raise CloudError("Invalid device login endpoints")
    code = request(code_url, "POST", {"name": "velcro (" + socket.gethostname()[:60] + ")", "tools": ["velcro"]})
    verify = code["verify_url"]
    if not verify.startswith(auth + "/device?"):
        raise CloudError("Invalid device approval address")
    # The user code is deliberately visible. The device code and bearer token never are.
    notify("Approve velcro in your browser: " + verify)
    open_browser(verify)
    deadline = time.monotonic() + min(max(int(code["expires_in"]), 1), 600)
    interval = max(int(code.get("interval", 3)), 1)
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            result = request(token_url, "POST", {"device_code": code["device_code"]})
        except CloudError as e:
            if e.status in (400, 403, 410):
                raise CloudError("Login denied or expired; run velcro login again") from None
            raise
        if result.get("state") == "approved":
            token = result.get("token", "")
            if not re.fullmatch(r"bxd_[A-Za-z0-9_-]{30,80}", token):
                raise CloudError("Invalid device login response")
            credentials.set(token)
            notify("Signed in. Device login saved in the system credential store.")
            return
        if result.get("state") != "pending":
            raise CloudError("Login denied; run velcro login again")
    raise CloudError("Login expired; run velcro login again")


def read_manifest(folder):
    path = Path(folder) / MANIFEST
    if not path.exists():
        return []
    latest, statuses = {}, {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("cloud_status") is True:
                statuses[(row["file"], row["sha256"])] = row["cloud"]
                continue
            latest[row["file"]] = row
    for row in latest.values():
        status = statuses.get((row["file"], row["sha256"]))
        if status is not None:
            row["cloud"] = status
    return list(latest.values())


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + ".tmp")
    with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, separators=(",", ":"))
        f.flush(); os.fsync(f.fileno())
    os.replace(temp, path)


def cloud_mark(folder, record, status):
    # A NAS can have several importers. Append a full record, never replace their manifest.
    # velcro-recordings already uses the last row for each filename; keep every original field.
    path = Path(folder) / MANIFEST
    current = next((r for r in read_manifest(folder) if r["file"] == record["file"]), None)
    if current is None or current["sha256"] != record["sha256"]:
        return  # A new recording has reused this filename since this upload started.
    row = {**record, "cloud": status, "cloud_status": True}
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        f.flush(); os.fsync(f.fileno())


class Uploader:
    def __init__(self, base, state, credentials=None, transport=request, clock=time.time):
        self.base, self.state = cloud_url(base), Path(state)
        self.credentials = credentials or Credentials(self.base)
        self.transport, self.clock = transport, clock

    def account(self):
        token = self.credentials.get()
        if not token:
            raise CloudError("Run velcro login to upload recordings")
        return hashlib.sha256(token.encode()).hexdigest()

    def upload(self, folder, record):
        path = Path(folder) / record["file"]
        # A manifest must never cause files outside the backup folder to be sent.
        if path.name != record["file"] or path.is_symlink():
            raise CloudError("Unsafe recording path; nothing uploaded")
        size = path.stat().st_size
        with path.open("rb") as f:
            sha = hashlib.file_digest(f, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else file_sha(f)
        if size != record["size"] or sha != record["sha256"]:
            raise CloudError("Backup checksum changed; nothing uploaded")
        token = self.credentials.get()
        if not token:
            raise CloudError("Run velcro login to upload recordings")
        headers = {"Authorization": "Bearer " + token}

        def call(route, method="GET", data=None, extra=None):
            return self.transport(self.base + route, method, data, {**headers, **(extra or {})})

        account = hashlib.sha256(token.encode()).hexdigest()
        key = hashlib.sha256((self.base + "\n" + account + "\n" + sha).encode()).hexdigest()
        checkpoint = self.state / (key + ".json")
        saved = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
        at = dt.datetime.fromisoformat(record["imported_at"].replace("Z", "+00:00"))
        meta = {"size": size, "sha256": sha, "ext": path.suffix[1:].lower(),
                "recordedAt": at.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
        start = call("/v1/uploads", "POST", meta)
        if start.get("transport") != "multipart":
            raise CloudError("Unsupported cloud upload transport; recording kept")
        ident, session = start["id"], start["uploadSession"]
        if not re.fullmatch("[a-f0-9]{64}", ident):
            raise CloudError("Invalid upload identifier")
        route = "/v1/uploads/" + ident
        remote = call(route)
        if remote["phase"] in RECEIVED:
            cloud_mark(folder, record, {"url": self.base, "account": account, "id": ident, "status": "uploaded"})
            return ident
        part_size, count = start["partSize"], start["partCount"]
        if not isinstance(part_size, int) or not 1 <= part_size <= 128 * 1024 * 1024 or count != (size + part_size - 1) // part_size:
            raise CloudError("Invalid upload part sizes")
        parts = saved.get("parts", []) if saved.get("session") == session else []
        saved = {"session": session, "parts": parts}
        cloud_mark(folder, record, {"url": self.base, "account": account, "id": ident, "status": "uploading"})
        with path.open("rb") as f:
            for number in range(1, count + 1):
                if number in parts:
                    continue
                f.seek((number - 1) * part_size)
                chunk = f.read(min(part_size, size - f.tell()))
                call(route + "/parts/" + str(number), "PUT", chunk,
                     {"Upload-Session": session, "Content-Length": str(len(chunk))})
                parts.append(number)
                save_json(checkpoint, saved)
        complete = call(route + "/complete", "POST", {}, {"Upload-Session": session})
        if complete.get("phase") not in RECEIVED:
            raise CloudError("Cloud has not verified the recording; kept for retry")
        cloud_mark(folder, record, {"url": self.base, "account": account, "id": ident, "status": "uploaded"})
        return ident

    def run(self, folder, log=print, missing_ok=False):
        failures = 0
        account, login_error = "", None
        try:
            account = self.account()
        except (CloudError, CredentialError) as error:
            login_error = error
        for record in read_manifest(folder):
            # Offline import queues move verified files to their destination independently of upload retries.
            if missing_ok and not (Path(folder) / record["file"]).exists():
                continue
            prior = record.get("cloud", {})
            try:
                same = prior.get("url") == self.base and prior.get("account") == account
                if same and (prior.get("status") == "uploaded" or prior.get("retry_at", 0) > self.clock()):
                    continue
                if login_error:
                    raise login_error
                self.upload(folder, record)
                log("cloud: recording uploaded and verified")
            except (CloudError, CredentialError, OSError, ValueError, KeyError) as error:
                failures += 1
                attempts = prior.get("attempts", 0) + 1 if prior.get("url") == self.base else 1
                status = {"url": self.base, "account": account, "status": "pending", "attempts": attempts,
                          "retry_at": self.clock() + min(30 * 2 ** min(attempts - 1, 10), 3600)}
                latest = next((r.get("cloud", {}) for r in read_manifest(folder)
                               if (r.get("file"), r.get("sha256")) == (record.get("file"), record.get("sha256"))), {})
                if latest.get("url") == self.base and latest.get("account") == account and latest.get("id"):
                    status["id"] = latest["id"]
                cloud_mark(folder, record, status)
                detail = str(error) if isinstance(error, (CloudError, CredentialError)) else "Could not read backup or upload state"
                log("cloud: " + detail + "; backup kept, retry queued")
        return failures


def file_sha(f):
    h = hashlib.sha256()
    for chunk in iter(lambda: f.read(1024 * 1024), b""):
        h.update(chunk)
    return h.hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("login", "upload", "validate"))
    p.add_argument("--cloud", required=True)
    p.add_argument("--folder")
    p.add_argument("--state")
    p.add_argument("--queue", action="store_true", help="skip files already flushed from the offline queue")
    args = p.parse_args()
    try:
        base = cloud_url(args.cloud)
        if args.command == "login":
            login(base)
        elif args.command == "upload":
            if not args.folder or not args.state:
                p.error("upload needs --folder and --state")
            return bool(Uploader(base, args.state).run(args.folder, missing_ok=args.queue))
        return 0
    except (CloudError, CredentialError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print("Could not read cloud response or local upload state; backup kept", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
