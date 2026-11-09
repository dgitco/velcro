"""Mac-compatible import manifest and Windows upload integration contracts."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import test_cloud as fixture
c = fixture.c


class ImportContract(unittest.TestCase):
    def setUp(self):
        fixture.Upload.setUp(self)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "windows"))
        import velcro_win
        self.windows = velcro_win
        self.settings = {"import.dest": [str(self.folder)], "upload.cloud": ["https://cloud.test"], "import.delete": ["no"]}
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": str(Path(self.temp.name) / "local")})
        self.env.start(); self.addCleanup(self.env.stop)
        factory = patch.object(velcro_win, "Uploader", return_value=self.uploader)
        factory.start(); self.addCleanup(factory.stop)

    def test_manual_and_hotkey_import_upload_verified_backup_and_keep_source(self):
        source = Path(self.temp.name) / "manual.wav"
        source.write_bytes(b"generated silence")
        self.manifest.unlink()
        self.assertEqual(self.windows.import_files([str(source)], self.settings, lambda _: None), (1, 0))
        self.assertTrue(source.exists())
        self.assertEqual(c.read_manifest(self.folder)[0]["cloud"]["status"], "uploaded")

    def test_disabled_cloud_does_not_contact_worker(self):
        self.windows._upload_cloud({"import.dest": [str(self.folder)]}, lambda _: None)
        self.assertFalse(self.worker.calls)

    def test_verified_offline_queue_uploads_without_attached_destination(self):
        source = Path(self.temp.name) / "manual.wav"
        source.write_bytes(b"generated silence")
        self.settings["import.dest"] = [str(Path(self.temp.name) / "detached" / "backup")]
        self.assertEqual(self.windows.import_files([str(source)], self.settings, lambda _: None), (1, 0))
        queue = Path(self.windows.queue_dir())
        self.assertTrue(source.exists())
        self.assertTrue((queue / source.name).exists())
        self.assertEqual(c.read_manifest(queue)[0]["cloud"]["status"], "uploaded")
        destination = Path(self.settings["import.dest"][0])
        destination.mkdir(parents=True)
        self.windows.flush_queue(self.settings, lambda _: None)
        self.windows.upload_cloud(self.settings, lambda _: None)
        self.assertEqual(c.read_manifest(destination)[0]["cloud"]["status"], "uploaded")
        # The server recognizes the same hash after its local queue copy moves to the backup folder.
        self.assertEqual(len([route for method, route in self.worker.calls if method == "PUT"]), 4)

