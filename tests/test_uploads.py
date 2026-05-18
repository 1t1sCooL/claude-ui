"""
Tests for file upload logic in /claude/upload and _build_prompt.
"""
import io
import os
import sys
import importlib
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("APP_PASSWORD", "test-secret")
os.environ.setdefault("UPLOAD_DIR", "/tmp/claude_test_uploads")

app_module = importlib.import_module("app")


class TestBuildPrompt(unittest.TestCase):

    def test_empty_attachments_returns_prompt_unchanged(self):
        result = app_module._build_prompt("hello world", [])
        self.assertEqual(result, "hello world")

    def test_augmented_prompt_embeds_text_file(self):
        """Text files should have their content embedded directly in the prompt."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write("print('hello')")
            tmp = f.name
        try:
            attachments = [{"path": tmp, "name": "code.py", "is_image": False}]
            result = app_module._build_prompt("analyze this", attachments)
            self.assertIn("print('hello')", result)  # content embedded
            self.assertIn("code.py", result)
            self.assertTrue(result.startswith("analyze this"))
        finally:
            os.unlink(tmp)

    def test_augmented_prompt_image_placeholder(self):
        """Images should have a placeholder note (actual data passed separately as base64)."""
        attachments = [{"path": "/some/image.png", "name": "image.png", "is_image": True}]
        result = app_module._build_prompt("describe", attachments)
        self.assertIn("image.png", result)
        self.assertIn("visual content", result)


class TestSafeFilename(unittest.TestCase):

    def test_strips_path_components(self):
        result = app_module._safe_filename("../../../etc/passwd")
        self.assertNotIn("/", result)
        self.assertNotIn("..", result)

    def test_replaces_spaces(self):
        result = app_module._safe_filename("my file name.txt")
        self.assertNotIn(" ", result)

    def test_preserves_extension(self):
        result = app_module._safe_filename("photo.jpg")
        self.assertTrue(result.endswith(".jpg"))

    def test_truncates_long_names(self):
        result = app_module._safe_filename("a" * 200 + ".txt")
        self.assertLessEqual(len(result), 120)


class TestUploadEndpoint(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp()
        self._orig_upload_dir = app_module.UPLOAD_DIR
        app_module.UPLOAD_DIR = Path(self._tmp)

    def tearDown(self):
        app_module.UPLOAD_DIR = self._orig_upload_dir
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _client(self):
        from fastapi.testclient import TestClient
        return TestClient(app_module.app)

    def test_unauthenticated_returns_401(self):
        tiny_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
        resp = self._client().post(
            "/claude/upload",
            files=[("files", ("x.png", tiny_png, "image/png"))],
            headers={"X-Token": "wrong-token"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_single_image_upload(self):
        tiny_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
        resp = self._client().post(
            "/claude/upload",
            files=[("files", ("img.png", tiny_png, "image/png"))],
            headers={"X-Token": app_module._TOKEN},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["files"]), 1)
        f = data["files"][0]
        self.assertTrue(f["is_image"])
        self.assertEqual(f["name"], "img.png")
        self.assertTrue(Path(f["path"]).exists())

    def test_text_file_upload(self):
        resp = self._client().post(
            "/claude/upload",
            files=[("files", ("script.py", b"print('hello')", "text/plain"))],
            headers={"X-Token": app_module._TOKEN},
        )
        self.assertEqual(resp.status_code, 200)
        f = resp.json()["files"][0]
        self.assertFalse(f["is_image"])
        self.assertEqual(f["mime_type"], "text/plain")

    def test_file_too_large_returns_413(self):
        big = b"x" * (21 * 1024 * 1024)
        resp = self._client().post(
            "/claude/upload",
            files=[("files", ("big.bin", big, "application/octet-stream"))],
            headers={"X-Token": app_module._TOKEN},
        )
        self.assertEqual(resp.status_code, 413)


if __name__ == "__main__":
    unittest.main()
