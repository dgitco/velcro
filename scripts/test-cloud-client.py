#!/usr/bin/env python3
"""Exercise the real client with generated silence and an in-memory device login on Linux."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import velcro_cloud as c

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--cloud", required=True)
p.add_argument("--output", required=True)
p.add_argument("--mib", type=int, default=210)
args = p.parse_args()


class Session:
    def get(self):
        return os.environ["VELCRO_DEVICE_TOKEN"]


ROOT.joinpath("build").mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix="cloud-silence-", dir=ROOT / "build") as temporary:
    folder = Path(temporary)
    audio = folder / "generated-silence.wav"
    with wave.open(str(audio), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        for _ in range(args.mib):
            w.writeframesraw(b"\0" * 1024 * 1024)
    with audio.open("rb") as f:
        sha = c.file_sha(f)
    record = {"file": audio.name, "size": audio.stat().st_size, "sha256": sha,
              "imported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
              "device": "Generated silence test", "device_uuid": "", "original": audio.name, "duration_s": args.mib * 1024 * 1024 / 32000}
    (folder / c.MANIFEST).write_text(json.dumps(record) + "\n")
    calls, interrupted, now = [], False, 1000

    def network(url, method="GET", data=None, headers=None):
        global interrupted
        route = url.removeprefix(args.cloud)
        print("smoke:", method, route, flush=True)
        if ("/parts/2" in route or route.endswith("/complete")) and not interrupted:
            interrupted = True
            raise c.CloudError("Simulated disconnect before next request")
        result = c.request(url, method, data, headers)
        calls.append({"method": method, "route": route, "phase": result.get("phase")})
        return result

    client = c.Uploader(args.cloud, folder / "state", Session(), network, lambda: now)
    failed = client.run(folder)
    assert failed == 1, "Fixture must cross a part boundary and interrupt"
    pending = c.read_manifest(folder)[0]["cloud"]
    assert pending["status"] == "pending" and pending["id"]
    now += 31
    client = c.Uploader(args.cloud, folder / "state", Session(), network, lambda: now)
    assert client.run(folder) == 0
    mark = c.read_manifest(folder)[0]["cloud"]
    assert mark["status"] == "uploaded"
    before = len(calls)
    assert client.run(folder, lambda _: None) == 0 and len(calls) == before
    first_parts = [entry for entry in calls if entry["method"] == "PUT" and entry["route"].endswith("/parts/1")]
    assert len(first_parts) == 1, "Acknowledged first part must not be resent"
    with audio.open("rb") as f:
        assert c.file_sha(f) == sha
    output = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "cloud": args.cloud,
              "fixture": {"kind": "generated-silence-wav", "size": record["size"], "sha256": sha},
              "id": mark["id"], "interruptedAndResumed": True, "confirmedPartNotResent": True,
              "duplicateSkipped": True, "backupUnchanged": True, "calls": calls}
    Path(args.output).write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output, indent=2))
