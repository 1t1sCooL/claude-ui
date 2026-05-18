"""
Smoke tests — basic endpoint availability and contract checks.
"""
import os
import importlib
import unittest

os.environ.setdefault("APP_PASSWORD", "test-secret")
os.environ.setdefault("UPLOAD_DIR", "/tmp/claude_smoke_uploads")
os.environ.setdefault("WORKSPACE_DIR", "/tmp/claude_smoke_workspace")

app_module = importlib.import_module("app")


def client():
    from fastapi.testclient import TestClient
    return TestClient(app_module.app)


def auth():
    return {"X-Token": app_module._TOKEN}


class TestHealthEndpoint(unittest.TestCase):
    def test_health_authenticated_returns_200(self):
        r = client().get("/claude/health", headers=auth())
        self.assertEqual(r.status_code, 200)

    def test_health_unauthenticated_returns_401(self):
        r = client().get("/claude/health", headers={"X-Token": "bad"})
        self.assertEqual(r.status_code, 401)

    def test_health_has_status_ok(self):
        data = client().get("/claude/health", headers=auth()).json()
        self.assertEqual(data["status"], "ok")

    def test_health_has_sessions_field(self):
        data = client().get("/claude/health", headers=auth()).json()
        self.assertIn("sessions", data)
        self.assertIn("total", data["sessions"])


class TestIndexPage(unittest.TestCase):
    def test_index_returns_html(self):
        r = client().get("/claude")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])

    def test_index_contains_app(self):
        r = client().get("/claude/")
        self.assertIn(b"Claude", r.content)
        self.assertIn(b"<script", r.content)


class TestAuthEndpoint(unittest.TestCase):
    def test_wrong_password_returns_401(self):
        r = client().post("/claude/auth", json={"password": "wrong"})
        self.assertEqual(r.status_code, 401)

    def test_correct_password_returns_token(self):
        r = client().post("/claude/auth", json={"password": "test-secret"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("token", r.json())

    def test_valid_token_check(self):
        token = app_module._TOKEN
        r = client().post("/claude/auth", json={"token": token})
        self.assertEqual(r.status_code, 200)

    def test_invalid_token_check_returns_401(self):
        r = client().post("/claude/auth", json={"token": "bad-token"})
        self.assertEqual(r.status_code, 401)


class TestSessionsEndpoint(unittest.TestCase):
    def test_unauthenticated_returns_401(self):
        r = client().get("/claude/sessions", headers={"X-Token": "bad"})
        self.assertEqual(r.status_code, 401)

    def test_authenticated_returns_list(self):
        r = client().get("/claude/sessions", headers=auth())
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("sessions", data)
        self.assertIsInstance(data["sessions"], list)

    def test_archived_param_works(self):
        r = client().get("/claude/sessions?archived=true", headers=auth())
        self.assertEqual(r.status_code, 200)


class TestCommandsEndpoint(unittest.TestCase):
    def test_unauthenticated_returns_401(self):
        r = client().get("/claude/commands", headers={"X-Token": "bad"})
        self.assertEqual(r.status_code, 401)

    def test_returns_commands_list(self):
        r = client().get("/claude/commands", headers=auth())
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("commands", data)
        self.assertTrue(len(data["commands"]) > 0)


class TestSkillsEndpoint(unittest.TestCase):
    def test_unauthenticated_returns_401(self):
        r = client().get("/claude/skills", headers={"X-Token": "bad"})
        self.assertEqual(r.status_code, 401)

    def test_returns_skills_list(self):
        r = client().get("/claude/skills", headers=auth())
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("skills", data)
        self.assertIsInstance(data["skills"], list)


class TestUploadEndpoint(unittest.TestCase):
    def test_unauthenticated_returns_401(self):
        r = client().post("/claude/upload",
                          files=[("files", ("f.txt", b"hi", "text/plain"))],
                          headers={"X-Token": "bad"})
        self.assertEqual(r.status_code, 401)


class TestWorkspaceEndpoints(unittest.TestCase):
    def test_tree_unauthenticated_401(self):
        r = client().get("/claude/workspace/tree", headers={"X-Token": "bad"})
        self.assertEqual(r.status_code, 401)

    def test_tree_authenticated_200(self):
        r = client().get("/claude/workspace/tree", headers=auth())
        self.assertEqual(r.status_code, 200)
        self.assertIn("tree", r.json())

    def test_ws_upload_unauthenticated_401(self):
        r = client().post("/claude/workspace/upload",
                          files=[("files", ("f.txt", b"hi", "text/plain"))],
                          headers={"X-Token": "bad"})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
