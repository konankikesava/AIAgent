from functools import partial
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import re
from urllib.parse import urlsplit

from .catalog import public_agents
from .runtime import Engine, Workers
from .store import Conflict, Missing, Store

STATIC = Path(__file__).parent / "static"
LOG = logging.getLogger(__name__)


class App:
    def __init__(self, config):
        config.validate()
        self.config = config
        config.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            config.data_dir.chmod(0o700)
        except OSError:
            pass
        self.store = Store(config.data_dir / "hub.sqlite3")
        self.engine = Engine(config, self.store)
        self.workers = Workers(self.engine)

    def validate_task(self, value):
        from .store import run_input
        data = run_input(value)
        self.engine.tools.validate_context(data["context"])
        return data

    def snapshot(self):
        return {
            "config": self.config.public(), "agents": public_agents(),
            "runs": self.store.list_runs(), "work_items": self.store.list_items(),
            "schedules": self.store.list_schedules(), "approvals": self.store.list_approvals(),
        }


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(15)
        return sock, address


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, app, **kwargs):
        self.app = app
        super().__init__(*args, **kwargs)

    def log_message(self, *_args):
        # Do not log auth headers, request bodies, or query strings.
        pass

    def send(self, code, body, content_type="application/json; charset=utf-8", download=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        self.wfile.write(body)

    def check_access(self, mutating=False):
        hosts = self.headers.get_all("Host", [])
        try:
            hostname = urlsplit("//" + hosts[0]).hostname if len(hosts) == 1 else None
        except ValueError:
            hostname = None
        if hostname not in self.app.config.allowed_hosts:
            self.send(421, {"error": "Host is not in HUB_ALLOWED_HOSTS"})
            return False
        token = self.app.config.auth_token
        if token and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
            self.send(401, {"error": "Enter the configured hub access token"})
            return False
        if mutating:
            origin = self.headers.get("Origin")
            try:
                parsed = urlsplit(origin) if origin else None
                invalid = parsed and (parsed.scheme not in ("http", "https") or parsed.netloc != hosts[0])
            except ValueError:
                invalid = True
            if invalid or self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.send(403, {"error": "Cross-origin actions are not allowed"})
                return False
        return True

    def read_json(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid Content-Length") from None
        if not 0 < length <= 65536:
            raise ValueError("Request body must be 1–65536 bytes")
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Chunked request bodies are not supported")
        try:
            data = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError):
            raise ValueError("Request body must be valid JSON") from None
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object")
        return data

    def do_GET(self):
        path = urlsplit(self.path).path
        # Static assets contain no credentials or personal data.
        assets = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8")}
        if path in assets:
            filename, mime = assets[path]
            self.send(200, (STATIC / filename).read_bytes(), mime)
            return
        if path == "/favicon.ico":
            self.send(204, b"")
            return
        if not self.check_access():
            return
        try:
            if path == "/api/health":
                self.send(200, {"ok": True})
            elif path == "/api/state":
                self.send(200, self.app.snapshot())
            elif match := re.fullmatch(r"/api/runs/([a-f0-9]{32})(/result)?", path):
                run = self.app.store.detail(match[1])
                if match[2]:
                    if run["status"] != "succeeded":
                        raise Conflict("This task does not have a completed result")
                    self.send(200, run["result"], "text/markdown; charset=utf-8", "task-" + run["id"] + ".md")
                else:
                    self.send(200, run)
            else:
                self.send(404, {"error": "Route not found"})
        except Missing as exc:
            self.send(404, {"error": str(exc)})
        except Conflict as exc:
            self.send(409, {"error": str(exc)})
        except Exception as exc:
            LOG.error("GET failed (%s)", type(exc).__name__)
            self.send(500, {"error": "Internal request error"})

    def do_POST(self):
        if not self.check_access(mutating=True):
            return
        path = urlsplit(self.path).path
        try:
            body = self.read_json()
            store = self.app.store
            if path == "/api/runs":
                self.send(201, store.detail(store.create_run(self.app.validate_task(body))["id"]))
            elif match := re.fullmatch(r"/api/runs/([a-f0-9]{32})/(cancel|retry)", path):
                if match[2] == "cancel":
                    self.send(200, store.cancel(match[1]))
                else:
                    previous = store.detail(match[1])
                    if previous["status"] not in ("failed", "cancelled"):
                        raise Conflict("Only failed or cancelled tasks can be retried")
                    if any(a["status"] in ("executing", "uncertain") for a in previous["approvals"]):
                        raise Conflict("External write outcome is uncertain. Reconcile GitHub and create a fresh task")
                    task = {k: previous[k] for k in ("agent_id", "title", "instruction", "context")}
                    self.send(201, store.detail(store.create_run(self.app.validate_task(task))["id"]))
            elif path == "/api/work-items":
                self.send(201, store.add_item(body.get("title"), body.get("detail", "")))
            elif match := re.fullmatch(r"/api/work-items/([a-f0-9]{32})", path):
                store.item_status(match[1], body.get("status"))
                self.send(200, {"updated": True})
            elif path == "/api/schedules":
                self.app.validate_task(body.get("task", {}))
                store.create_schedule(body)
                self.send(201, {"created": True})
            elif match := re.fullmatch(r"/api/schedules/([a-f0-9]{32})", path):
                store.enable_schedule(match[1], body.get("enabled"))
                self.send(200, {"updated": True})
            elif match := re.fullmatch(r"/api/approvals/([a-f0-9]{32})", path):
                self.send(200, store.decide(match[1], body.get("approved")))
            else:
                self.send(404, {"error": "Route not found"})
        except Missing as exc:
            self.send(404, {"error": str(exc)})
        except Conflict as exc:
            self.send(409, {"error": str(exc)})
        except ValueError as exc:
            self.send(400, {"error": str(exc)})
        except Exception as exc:
            LOG.error("POST failed (%s)", type(exc).__name__)
            self.send(500, {"error": "Internal request error"})


def make_server(app):
    return Server((app.config.host, app.config.port), partial(Handler, app=app))
