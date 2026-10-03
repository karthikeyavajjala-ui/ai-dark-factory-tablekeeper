"""HTTP-level tests for the task tracker API."""

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server import TaskRequestHandler, TaskStore, ThreadingHTTPServer


class TaskApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), TaskRequestHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join()

    def setUp(self):
        TaskRequestHandler.store = TaskStore()

    def request(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            response = urlopen(request)
        except HTTPError as error:
            response = error
        body = response.read()
        result = response.code, json.loads(body) if body else None
        response.close()
        return result

    def test_list_create_normalizes_title_and_returns_task(self):
        status, initial = self.request("GET", "/api/tasks")
        self.assertEqual((status, initial), (200, {"tasks": []}))
        status, response = self.request("POST", "/api/tasks", {"title": "  Plan release  "})
        self.assertEqual(status, 201)
        task = response["task"]
        self.assertEqual(task["title"], "Plan release")
        self.assertFalse(task["completed"])
        self.assertTrue(task["id"])
        self.assertTrue(task["created_at"].endswith("Z"))
        status, listing = self.request("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertIn(task, listing["tasks"])

    def test_create_rejects_empty_non_string_and_overlong_titles(self):
        for title in ("", " \t ", None, 17, "x" * 201):
            with self.subTest(title=repr(title)):
                status, error = self.request("POST", "/api/tasks", {"title": title})
                self.assertEqual(status, 400)
                self.assertIn("error", error)

    def test_create_accepts_title_at_limit(self):
        status, response = self.request("POST", "/api/tasks", {"title": "x" * 200})
        self.assertEqual(status, 201)
        task = response["task"]
        self.assertEqual(len(task["title"]), 200)

    def test_create_enforces_limit_by_non_bmp_code_points(self):
        exactly_200 = "😀" * 200
        status, response = self.request("POST", "/api/tasks", {"title": exactly_200})
        self.assertEqual(status, 201)
        self.assertEqual(response["task"]["title"], exactly_200)
        self.assertEqual(len(response["task"]["title"]), 200)

        over_limit_201 = "😀" * 201
        status, error = self.request("POST", "/api/tasks", {"title": over_limit_201})
        self.assertEqual(status, 400)
        self.assertIn("error", error)

    def test_update_completion_and_delete(self):
        _, response = self.request("POST", "/api/tasks", {"title": "Review PR"})
        task = response["task"]
        status, response = self.request("PATCH", f"/api/tasks/{task['id']}", {"completed": True})
        self.assertEqual(status, 200)
        updated = response["task"]
        self.assertTrue(updated["completed"])
        self.assertEqual(updated["created_at"], task["created_at"])
        status, response = self.request("PATCH", f"/api/tasks/{task['id']}", {"completed": False})
        self.assertEqual(status, 200)
        updated = response["task"]
        self.assertFalse(updated["completed"])
        status, body = self.request("DELETE", f"/api/tasks/{task['id']}")
        self.assertEqual((status, body), (204, None))
        status, error = self.request("PATCH", f"/api/tasks/{task['id']}", {"completed": True})
        self.assertEqual(status, 404)
        self.assertIn("error", error)

    def test_update_requires_boolean_and_unknown_routes_return_404(self):
        _, response = self.request("POST", "/api/tasks", {"title": "Write docs"})
        task = response["task"]
        for value in ("true", 1, None):
            status, error = self.request("PATCH", f"/api/tasks/{task['id']}", {"completed": value})
            self.assertEqual(status, 400)
            self.assertIn("error", error)
        status, error = self.request("DELETE", "/api/tasks/missing")
        self.assertEqual(status, 404)
        self.assertIn("error", error)

    def test_malformed_json_returns_400(self):
        request = Request(
            self.base_url + "/api/tasks",
            data=b"{nope",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request)
        self.assertEqual(caught.exception.code, 400)
        self.assertIn("error", json.loads(caught.exception.read()))
        caught.exception.close()

    def test_root_serves_frontend_when_present(self):
        with urlopen(self.base_url + "/") as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers.get("Content-Type", ""))
            self.assertIn(b"Task tracker", response.read())

        for path, content_type in (
            ("/web/app.js", "javascript"),
            ("/web/styles.css", "text/css"),
        ):
            with self.subTest(path=path), urlopen(self.base_url + path) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(content_type, response.headers.get("Content-Type", ""))
                self.assertGreater(int(response.headers["Content-Length"]), 0)


if __name__ == "__main__":
    unittest.main()
