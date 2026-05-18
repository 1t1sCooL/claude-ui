"""
Tests for GET /claude/commands and _load_commands().
"""
import os
import importlib
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("APP_PASSWORD", "test-secret")
os.environ.setdefault("UPLOAD_DIR", "/tmp/claude_test_uploads")
os.environ.setdefault("WORKSPACE_DIR", "/tmp/claude_test_workspace")
os.environ.setdefault("COMMANDS_DIR", "/tmp/claude_test_commands_nonexistent")

app_module = importlib.import_module("app")


class TestLoadCommands(unittest.TestCase):

    def setUp(self):
        self._orig_dir = app_module.COMMANDS_DIR
        self._tmp = tempfile.mkdtemp()
        app_module.COMMANDS_DIR = Path(self._tmp)
        app_module._commands_cache = (None, 0)  # invalidate TTL cache

    def tearDown(self):
        app_module.COMMANDS_DIR = self._orig_dir
        app_module._commands_cache = (None, 0)  # invalidate TTL cache
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_builtin_commands_always_present(self):
        cmds = app_module._load_commands()
        names = [c["name"] for c in cmds]
        self.assertIn("/clear", names)
        self.assertIn("/help", names)
        self.assertIn("/compact", names)

    def test_builtin_category(self):
        cmds = app_module._load_commands()
        for c in cmds:
            if c["name"] in ("/clear", "/help", "/compact", "/model"):
                self.assertEqual(c["category"], "builtin", f"{c['name']} should be builtin")

    def test_custom_command_from_file(self):
        (Path(self._tmp) / "my_skill.md").write_text("# My cool skill\nDoes stuff", encoding="utf-8")
        cmds = app_module._load_commands()
        names = [c["name"] for c in cmds]
        self.assertIn("/my_skill", names)
        custom = next(c for c in cmds if c["name"] == "/my_skill")
        self.assertEqual(custom["category"], "custom")
        self.assertEqual(custom["description"], "My cool skill")

    def test_description_strips_hash(self):
        (Path(self._tmp) / "tool.md").write_text("## Tool description\nExtra", encoding="utf-8")
        cmds = app_module._load_commands()
        tool = next((c for c in cmds if c["name"] == "/tool"), None)
        self.assertIsNotNone(tool)
        self.assertNotIn("#", tool["description"])
        self.assertEqual(tool["description"], "Tool description")

    def test_description_from_plain_first_line(self):
        (Path(self._tmp) / "plain.md").write_text("Just a plain description", encoding="utf-8")
        cmds = app_module._load_commands()
        plain = next((c for c in cmds if c["name"] == "/plain"), None)
        self.assertIsNotNone(plain)
        self.assertEqual(plain["description"], "Just a plain description")

    def test_empty_commands_dir_returns_only_builtins(self):
        cmds = app_module._load_commands()
        categories = {c["category"] for c in cmds}
        self.assertEqual(categories, {"builtin"})

    def test_nonexistent_commands_dir_returns_builtins(self):
        app_module.COMMANDS_DIR = Path("/tmp/this_does_not_exist_12345")
        cmds = app_module._load_commands()
        self.assertTrue(len(cmds) >= 5)
        for c in cmds:
            self.assertEqual(c["category"], "builtin")

    def test_commands_have_required_fields(self):
        cmds = app_module._load_commands()
        for c in cmds:
            self.assertIn("name", c)
            self.assertIn("description", c)
            self.assertIn("category", c)
            self.assertTrue(c["name"].startswith("/"))


class TestCommandsRoute(unittest.TestCase):

    def _client(self):
        from fastapi.testclient import TestClient
        return TestClient(app_module.app)

    def _auth(self):
        return {"X-Token": app_module._TOKEN}

    def test_unauthenticated_returns_401(self):
        r = self._client().get("/claude/commands", headers={"X-Token": "wrong"})
        self.assertEqual(r.status_code, 401)

    def test_returns_200_with_commands(self):
        r = self._client().get("/claude/commands", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("commands", data)
        self.assertIsInstance(data["commands"], list)
        self.assertTrue(len(data["commands"]) > 0)

    def test_response_has_name_and_description(self):
        r = self._client().get("/claude/commands", headers=self._auth())
        for cmd in r.json()["commands"]:
            self.assertIn("name", cmd)
            self.assertIn("description", cmd)

    def test_builtin_commands_in_response(self):
        r = self._client().get("/claude/commands", headers=self._auth())
        names = [c["name"] for c in r.json()["commands"]]
        self.assertIn("/clear", names)
        self.assertIn("/help", names)


if __name__ == "__main__":
    unittest.main()
