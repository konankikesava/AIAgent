from dataclasses import replace
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from task_hub.config import Config
from task_hub.llm import request_json
from task_hub.server import App, Handler, Server


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.temp.name), provider="demo", auth_token="test-access-token-with-32-characters")
        self.app = App(self.config)
        self.server = Server(("127.0.0.1", 0), partial(Handler, app=self.app))
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.app.workers.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, path, body=None, headers=None, authenticated=True):
        request_headers = {"Content-Type": "application/json"}
        if authenticated:
            request_headers["Authorization"] = "Bearer " + self.config.auth_token
        request_headers.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    def test_api_requires_token_but_public_assets_are_safe(self):
        self.assertEqual(self.request("/api/state", authenticated=False)[0], 401)
        status, raw, headers = self.request("/", authenticated=False)
        self.assertEqual(status, 200)
        self.assertNotIn(self.config.auth_token.encode(), raw)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_cross_origin_actions_and_unknown_hosts_are_blocked(self):
        self.assertEqual(self.request("/api/work-items", {"title": "No"}, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/state", headers={"Host": "evil.example"})[0], 421)
        self.assertEqual(self.app.store.list_items(), [])

    def test_wrong_content_type_and_scalar_json_rejected(self):
        self.assertEqual(self.request("/api/work-items", {"title": "No"}, {"Content-Type": "text/plain"})[0], 400)
        self.assertEqual(self.request("/api/work-items", ["not an object"])[0], 400)

    def test_static_traversal_and_missing_records_do_not_leak_files(self):
        self.assertEqual(self.request("/../config.py")[0], 404)
        self.assertEqual(self.request("/api/runs/" + "0" * 32)[0], 404)

    def test_run_is_automatically_completed_and_downloadable(self):
        self.app.workers.start()
        task = {"agent_id": "meeting-debrief", "title": "Team meeting", "instruction": "Review supplied meeting notes", "context": {"notes": "Maya will review the API by Friday."}}
        status, raw, _ = self.request("/api/runs", task)
        self.assertEqual(status, 201)
        identity = json.loads(raw)["id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = json.loads(self.request("/api/runs/" + identity)[1])
            if result["status"] == "succeeded":
                break
            threading.Event().wait(0.05)
        self.assertEqual(result["status"], "succeeded")
        status, raw, headers = self.request("/api/runs/" + identity + "/result")
        self.assertEqual(status, 200)
        self.assertIn(b"demonstration", raw)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(len(self.app.store.list_items()), 1)

    def test_work_items_can_be_added_and_completed(self):
        status, raw, _ = self.request("/api/work-items", {"title": "Review weekly brief"})
        self.assertEqual(status, 201)
        identity = json.loads(raw)["id"]
        self.assertEqual(self.request("/api/work-items/" + identity, {"status": "done"})[0], 200)
        self.assertEqual(self.app.store.list_items()[0]["status"], "done")

    def test_schedule_can_be_saved_and_paused(self):
        task = {"agent_id": "weekly-status", "title": "Weekly review", "instruction": "Write a status update"}
        status, _, _ = self.request("/api/schedules", {"label": "Friday report", "task": task, "spec": {"cadence": "weekly", "weekday": 4, "time": "08:00", "timezone": "Asia/Kolkata"}})
        self.assertEqual(status, 201)
        schedule = self.app.store.list_schedules()[0]
        self.assertEqual(self.request("/api/schedules/" + schedule["id"], {"enabled": False})[0], 200)
        self.assertEqual(self.app.store.list_schedules()[0]["enabled"], 0)

    def test_repository_not_in_allowlist_is_rejected_at_submission(self):
        task = {"agent_id": "weekly-status", "title": "Review", "instruction": "Read repository", "context": {"repo": "other/private"}}
        self.assertEqual(self.request("/api/runs", task)[0], 400)
        self.assertEqual(self.app.store.list_runs(), [])

    def test_result_is_not_downloadable_before_completion(self):
        created = self.app.store.create_run({"agent_id": "call-prep", "title": "Call", "instruction": "Prepare call"})
        self.assertEqual(self.request("/api/runs/" + created["id"] + "/result")[0], 409)


class UpstreamHandler(BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):
        type(self).hits.append(self.path)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/target")
            self.end_headers()
        else:
            data = json.dumps({"content": "x" * 100}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def log_message(self, *_args):
        pass


class UpstreamTests(unittest.TestCase):
    def setUp(self):
        UpstreamHandler.hits = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def test_auth_headers_are_not_forwarded_through_redirects(self):
        with self.assertRaises(ValueError):
            request_json(self.url + "/redirect", headers={"Authorization": "Bearer fixture"})
        self.assertEqual(UpstreamHandler.hits, ["/redirect"])

    def test_upstream_response_size_is_bounded(self):
        with self.assertRaisesRegex(ValueError, "size limit"):
            request_json(self.url + "/target", maximum=20)


if __name__ == "__main__":
    unittest.main()
