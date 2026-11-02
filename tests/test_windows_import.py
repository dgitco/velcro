"""velcro for Windows: import logic on a fake recorder folder (runs on Linux).

  python3 -m unittest discover -s tests
"""
import json, os, sys, tempfile, unittest, wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "windows"))
import velcro_win as v

# A line the Mac velcro writes (key order matters to anything reading it as text).
MAC_KEYS = ["file", "size", "sha256", "imported_at", "device", "device_uuid", "original", "duration_s"]


def wav(path, secs=1):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(b"\0\0" * 8000 * secs)


class T(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.rec, self.dest = os.path.join(self.t, "rec"), os.path.join(self.t, "dest")
        os.makedirs(os.path.join(self.rec, "RECORD")); os.makedirs(self.dest)
        os.makedirs(os.path.join(self.rec, "MUSIC"))
        wav(os.path.join(self.rec, "RECORD", "a.WAV")); wav(os.path.join(self.rec, "MUSIC", "song.wav"))
        os.environ["LOCALAPPDATA"] = os.path.join(self.t, "local")
        self.s = {"import.dest": [self.dest], "import.folder": ["RECORD"]}

    def test_copy_verify_manifest_delete(self):
        self.assertEqual(v.import_volume(self.rec, self.s, "R", "AB12", log=lambda *_: 0), (1, 0))
        self.assertTrue(os.path.exists(os.path.join(self.dest, "a.WAV")))
        self.assertFalse(os.path.exists(os.path.join(self.rec, "RECORD", "a.WAV")))
        self.assertTrue(os.path.exists(os.path.join(self.rec, "MUSIC", "song.wav")))  # outside import.folder
        line = open(os.path.join(self.dest, v.MANIFEST)).read().splitlines()
        self.assertEqual(list(json.loads(line[0])), MAC_KEYS)
        self.assertEqual(json.loads(line[0])["duration_s"], 1.0)
        self.assertEqual(json.loads(line[0])["sha256"], v.sha256_file(os.path.join(self.dest, "a.WAV")))

    def test_keep_on_recorder_skips_second_time(self):
        self.s["import.delete"] = ["no"]
        v.import_volume(self.rec, self.s, "R", "AB12", log=lambda *_: 0)
        v.import_volume(self.rec, self.s, "R", "AB12", log=lambda *_: 0)
        self.assertEqual(len(open(os.path.join(self.dest, v.MANIFEST)).read().splitlines()), 1)
        self.assertTrue(os.path.exists(os.path.join(self.rec, "RECORD", "a.WAV")))

    def test_different_file_same_name_is_numbered(self):
        open(os.path.join(self.dest, "a.WAV"), "wb").write(b"other")
        v.import_volume(self.rec, self.s, "R", "AB12", log=lambda *_: 0)
        self.assertTrue(os.path.exists(os.path.join(self.dest, "a-2.WAV")))

    def test_unreachable_dest_queues_then_flushes(self):
        self.s["import.dest"] = [os.path.join(self.t, "nope", "deeper")]
        v.import_volume(self.rec, self.s, "R", "AB12", log=lambda *_: 0)
        self.assertTrue(os.path.exists(os.path.join(v.queue_dir(), "a.WAV")))
        os.makedirs(self.s["import.dest"][0])
        self.assertEqual(v.flush_queue(self.s, log=lambda *_: 0), 1)
        self.assertTrue(os.path.exists(os.path.join(self.s["import.dest"][0], "a.WAV")))
        self.assertFalse(os.path.exists(os.path.join(v.queue_dir(), "a.WAV")))
        self.assertEqual(json.loads(open(os.path.join(self.s["import.dest"][0], v.MANIFEST)).read())["device_uuid"], "AB12")

    def test_failed_copy_deletes_nothing(self):
        orig = v.put_verified
        v.put_verified = lambda *a, **k: None
        try:
            self.assertEqual(v.import_volume(self.rec, self.s, "R", "AB12", log=lambda *_: 0), (0, 1))
        finally:
            v.put_verified = orig
        self.assertTrue(os.path.exists(os.path.join(self.rec, "RECORD", "a.WAV")))


if __name__ == "__main__":
    unittest.main()
