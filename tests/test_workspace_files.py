"""
Tests for workspace snapshot/diff utilities and workspace file routes.
"""
import os
import sys
import importlib
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("APP_PASSWORD", "test-secret")
os.environ.setdefault("UPLOAD_DIR", "/tmp/claude_test_uploads")
os.environ.setdefault("WORKSPACE_DIR", "/tmp/claude_test_workspace")

app_module = importlib.import_module("app")


class TestSnapshotDiff(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_ws = app_module.WORKSPACE_DIR
        app_module.WORKSPACE_DIR = Path(self._tmp)

    def tearDown(self):
        app_module.WORKSPACE_DIR = self._orig_ws
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, rel, content=b"data"):
        p = Path(self._tmp) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def test_snapshot_empty_dir(self):
        snap = app_module._snapshot_workspace()
        self.assertEqual(snap, {})

    def test_snapshot_finds_files(self):
        self._write("a.txt")
        self._write("sub/b.py")
        snap = app_module._snapshot_workspace()
        self.assertIn("a.txt", snap)
        self.assertIn("sub/b.py", snap)

    def test_snapshot_excludes_uploads(self):
        self._write(".uploads/batch/img.png")
        self._write("visible.txt")
        snap = app_module._snapshot_workspace()
        self.assertNotIn(".uploads/batch/img.png", snap)
        self.assertIn("visible.txt", snap)

    def test_snapshot_excludes_hidden_dirs(self):
        self._write(".hidden/file.txt")
        self._write("normal.txt")
        snap = app_module._snapshot_workspace()
        for k in snap:
            self.assertFalse(k.startswith(".hidden"), f"hidden file leaked: {k}")
        self.assertIn("normal.txt", snap)

    def test_diff_new_file(self):
        before = {}
        self._write("new.txt", b"hello")
        after = app_module._snapshot_workspace()
        result = app_module._diff_workspace(before, after)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "new.txt")
        self.assertEqual(result[0]["rel_path"], "new.txt")

    def test_diff_modified_file(self):
        p = self._write("mod.txt", b"v1")
        before = app_module._snapshot_workspace()
        import time; time.sleep(0.02)
        p.write_bytes(b"v2 longer")
        os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 1))
        after = app_module._snapshot_workspace()
        result = app_module._diff_workspace(before, after)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "mod.txt")

    def test_diff_unchanged(self):
        self._write("same.txt", b"no change")
        snap = app_module._snapshot_workspace()
        result = app_module._diff_workspace(snap, snap)
        self.assertEqual(result, [])

    def test_is_image_detection(self):
        self._write("photo.png")
        self._write("doc.txt")
        before = {}
        after = app_module._snapshot_workspace()
        result = app_module._diff_workspace(before, after)
        by_name = {r["name"]: r for r in result}
        self.assertTrue(by_name["photo.png"]["is_image"])
        self.assertFalse(by_name["doc.txt"]["is_image"])

    def test_diff_size_field(self):
        self._write("sized.bin", b"x" * 100)
        after = app_module._snapshot_workspace()
        result = app_module._diff_workspace({}, after)
        self.assertEqual(result[0]["size"], 100)


class TestWorkspaceRoutes(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_ws = app_module.WORKSPACE_DIR
        app_module.WORKSPACE_DIR = Path(self._tmp)

    def tearDown(self):
        app_module.WORKSPACE_DIR = self._orig_ws
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _client(self):
        from fastapi.testclient import TestClient
        return TestClient(app_module.app)

    def _write(self, rel, content=b"hello"):
        p = Path(self._tmp) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def _auth(self):
        return {"X-Token": app_module._TOKEN}

    def test_serve_file_returns_200(self):
        self._write("result.txt", b"output content")
        r = self._client().get("/claude/workspace/file/result.txt", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"output content")

    def test_serve_file_in_subdir(self):
        self._write("sub/dir/file.py", b"print('hi')")
        r = self._client().get("/claude/workspace/file/sub/dir/file.py", headers=self._auth())
        self.assertEqual(r.status_code, 200)

    def test_serve_file_404(self):
        r = self._client().get("/claude/workspace/file/nonexistent.txt", headers=self._auth())
        self.assertEqual(r.status_code, 404)

    def test_serve_file_traversal_blocked(self):
        r = self._client().get("/claude/workspace/file/../../../etc/passwd", headers=self._auth())
        self.assertIn(r.status_code, (403, 404))

    def test_serve_unauthenticated(self):
        self._write("f.txt")
        r = self._client().get("/claude/workspace/file/f.txt", headers={"X-Token": "wrong"})
        self.assertEqual(r.status_code, 401)

    def test_delete_file_200(self):
        p = self._write("todel.txt")
        r = self._client().delete("/claude/workspace/file/todel.txt", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertFalse(p.exists())

    def test_delete_file_404(self):
        r = self._client().delete("/claude/workspace/file/ghost.txt", headers=self._auth())
        self.assertEqual(r.status_code, 404)

    def test_delete_unauthenticated(self):
        self._write("f2.txt")
        r = self._client().delete("/claude/workspace/file/f2.txt", headers={"X-Token": "bad"})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
