"""Exercise the folder browser at the desktop and execution-host entry points."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


FLEET = Path(__file__).resolve().parents[1] / "fleet"


class FolderBrowserTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.environment = dict(os.environ, HOME=str(self.home))

    def invoke(self, script, payload):
        reply = subprocess.run(
            [sys.executable, "-B", *script],
            input=json.dumps(payload), capture_output=True, text=True, check=True,
            env=self.environment, timeout=10,
        )
        return json.loads(reply.stdout)

    def test_desktop_browses_without_existing_workspace(self):
        folder = self.home / "Project Files"
        folder.mkdir()
        (self.home / "Shortcut").symlink_to(folder, target_is_directory=True)
        (self.home / "Broken").symlink_to(self.home / "missing", target_is_directory=True)
        (self.home / "file.txt").touch()
        host = {"id": "new", "label": "Local", "ssh": "", "repo": "/no/workspace/yet"}

        default = self.invoke([str(FLEET / "fleet_desktop.py")], {
            "host": host, "request": {"op": "browse_folders"},
        })
        self.assertEqual(default, {
            "directory": str(self.home), "parent": str(self.home.parent),
            "folders": ["Project Files", "Shortcut"], "cursor": None, "more": False,
        })
        selected = self.invoke([str(FLEET / "fleet_desktop.py")], {
            "host": host, "request": {"op": "browse_folders", "path": "$HOME/Shortcut"},
        })
        self.assertEqual(selected, {
            "directory": str(folder), "parent": str(self.home), "folders": [], "cursor": None, "more": False,
        })

    def test_host_rpc_paginates_sorted_folder_names(self):
        names = [f"Folder {number:03}" for number in range(203)]
        for name in reversed(names):
            (self.home / name).mkdir()
        rpc = [str(FLEET / "fleet.py"), "--rpc"]
        first = self.invoke(rpc, {"op": "browse_folders", "path": "~"})
        self.assertTrue(first["more"])
        self.assertEqual(first["folders"], names[:200])
        self.assertEqual(first["cursor"], names[199])
        second = self.invoke(rpc, {"op": "browse_folders", "path": "~", "cursor": first["cursor"]})
        self.assertFalse(second["more"])
        self.assertEqual(second["folders"], names[200:])
        self.assertIsNone(second["cursor"])
        self.assertIsNone(self.invoke(rpc, {"op": "browse_folders", "path": "/"})["parent"])
        invalid = self.invoke(rpc, {"op": "browse_folders", "cursor": "bad/path"})
        self.assertEqual(invalid["error"], "Invalid folder page cursor")


if __name__ == "__main__":
    unittest.main()
