from contextlib import contextmanager
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import uuid

from .catalog import agent_by_id
from .scheduling import next_occurrence, stamp, utc_now


class Missing(ValueError):
    pass


class Conflict(ValueError):
    pass


class Stopped(Exception):
    pass


def uid():
    return uuid.uuid4().hex


def text_field(value, name, maximum, required=True):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError(f"{name} must be {'a nonempty ' if required else 'a '}string of at most {maximum} characters")
    return value.strip()


def run_input(data):
    if not isinstance(data, dict) or not agent_by_id(data.get("agent_id")):
        raise ValueError("Choose an existing agent")
    context = data.get("context", {})
    if not isinstance(context, dict) or set(context) - {"repo", "notes"}:
        raise ValueError("Context supports only repo and notes")
    clean_context = {k: text_field(v, k, 24000 if k == "notes" else 200, False) for k, v in context.items()}
    return {
        "agent_id": data["agent_id"],
        "title": text_field(data.get("title"), "Title", 160),
        "instruction": text_field(data.get("instruction"), "Instruction", 12000),
        "context": clean_context,
    }


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, title TEXT NOT NULL,
                    instruction TEXT NOT NULL, context TEXT NOT NULL, status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0, checkpoint TEXT NOT NULL DEFAULT '{}',
                    result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, next_at TEXT NOT NULL,
                    lease_until TEXT, claim_token TEXT, schedule_id TEXT, scheduled_for TEXT,
                    UNIQUE(schedule_id, scheduled_for)
                );
                CREATE INDEX IF NOT EXISTS runs_queue ON runs(status, next_at);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
                    kind TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), step INTEGER NOT NULL,
                    tool TEXT NOT NULL, arguments TEXT NOT NULL, status TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(run_id, step)
                );
                CREATE TABLE IF NOT EXISTS work_items (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open', run_id TEXT REFERENCES runs(id), step INTEGER,
                    created_at TEXT NOT NULL, UNIQUE(run_id, step)
                );
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY, label TEXT NOT NULL, task TEXT NOT NULL,
                    spec TEXT NOT NULL, next_at TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            raise Missing("Record not found")
        result = dict(row)
        for key in ("context", "checkpoint", "arguments", "task", "spec"):
            if key in result:
                result[key] = json.loads(result[key])
        return result

    @staticmethod
    def event_in(db, run_id, kind, message):
        db.execute("INSERT INTO events(run_id,kind,message,created_at) VALUES(?,?,?,?)", (run_id, kind, message[:2000], stamp()))

    def event(self, run_id, kind, message):
        with self.connect() as db:
            self.event_in(db, run_id, kind, message)

    def _insert_run(self, db, data, schedule_id=None, scheduled_for=None):
        identity, now = uid(), stamp()
        db.execute("""INSERT INTO runs(id,agent_id,title,instruction,context,status,created_at,updated_at,next_at,schedule_id,scheduled_for)
                      VALUES(?,?,?,?,?,'queued',?,?,?,?,?)""",
                   (identity, data["agent_id"], data["title"], data["instruction"], json.dumps(data["context"]), now, now, now, schedule_id, scheduled_for))
        self.event_in(db, identity, "queued", "Task queued for automatic execution")
        return identity

    def create_run(self, data):
        data = run_input(data)
        with self.connect() as db:
            identity = self._insert_run(db, data)
        return self.get_run(identity)

    def get_run(self, identity):
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM runs WHERE id=?", (identity,)).fetchone())

    def list_runs(self):
        with self.connect() as db:
            rows = db.execute("SELECT id,agent_id,title,status,attempt,error,created_at,updated_at,schedule_id FROM runs ORDER BY created_at DESC,rowid DESC LIMIT 100").fetchall()
            return [dict(r) for r in rows]

    def detail(self, identity):
        run = self.get_run(identity)
        run.pop("claim_token", None)
        run.pop("checkpoint", None)
        with self.connect() as db:
            run["events"] = [dict(r) for r in db.execute("SELECT * FROM events WHERE run_id=? ORDER BY id", (identity,))]
            run["approvals"] = [self.decode(r) for r in db.execute("SELECT * FROM approvals WHERE run_id=? ORDER BY step", (identity,))]
        return run

    def claim(self, lease_seconds=240):
        now = stamp()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            expired = db.execute("SELECT id,attempt FROM runs WHERE status='running' AND lease_until<=?", (now,)).fetchall()
            for row in expired:
                writing = db.execute("SELECT 1 FROM approvals WHERE run_id=? AND status='executing'", (row["id"],)).fetchone()
                status = "failed" if writing or row["attempt"] >= 3 else "queued"
                error = "Worker stopped during a GitHub write; reconcile the external result before retrying" if writing else "Worker lease expired"
                if writing:
                    db.execute("UPDATE approvals SET status='uncertain',updated_at=? WHERE run_id=? AND status='executing'", (now, row["id"]))
                db.execute("UPDATE runs SET status=?,error=?,lease_until=NULL,claim_token=NULL,updated_at=? WHERE id=?", (status, error, now, row["id"]))
                self.event_in(db, row["id"], "recovered", error)
            row = db.execute("SELECT id FROM runs WHERE status='queued' AND next_at<=? ORDER BY created_at,rowid LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            token = uid()
            db.execute("UPDATE runs SET status='running',attempt=attempt+1,updated_at=?,lease_until=?,claim_token=? WHERE id=?", (now, stamp(utc_now() + timedelta(seconds=lease_seconds)), token, row["id"]))
            self.event_in(db, row["id"], "started", "Worker started the task")
            return self.decode(db.execute("SELECT * FROM runs WHERE id=?", (row["id"],)).fetchone())

    def assert_active(self, db, run):
        active = db.execute("SELECT 1 FROM runs WHERE id=? AND status='running' AND claim_token=?", (run["id"], run["claim_token"])).fetchone()
        if not active:
            raise Stopped("Run is no longer active")

    def heartbeat(self, run):
        with self.connect() as db:
            count = db.execute("UPDATE runs SET lease_until=? WHERE id=? AND status='running' AND claim_token=?", (stamp(utc_now() + timedelta(seconds=240)), run["id"], run["claim_token"])).rowcount
            if not count:
                raise Stopped("Run is no longer active")

    def checkpoint(self, run, value):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assert_active(db, run)
            db.execute("UPDATE runs SET checkpoint=?,updated_at=? WHERE id=?", (json.dumps(value), stamp(), run["id"]))

    def finish(self, run, status, result="", error="", retry=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assert_active(db, run)
            if retry and run["attempt"] < 3:
                status = "queued"
            next_at = stamp(utc_now() + timedelta(seconds=15 * 2 ** (run["attempt"] - 1))) if status == "queued" else stamp()
            db.execute("UPDATE runs SET status=?,result=?,error=?,updated_at=?,next_at=?,lease_until=NULL,claim_token=NULL WHERE id=?", (status, result, error[:2000], stamp(), next_at, run["id"]))
            self.event_in(db, run["id"], status, error or ("Result saved" if status == "succeeded" else "Waiting for your decision"))

    def cancel(self, identity):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.decode(db.execute("SELECT * FROM runs WHERE id=?", (identity,)).fetchone())
            if row["status"] not in ("queued", "running", "waiting_approval"):
                raise Conflict("Only active tasks can be cancelled")
            db.execute("UPDATE runs SET status='cancelled',lease_until=NULL,claim_token=NULL,updated_at=? WHERE id=?", (stamp(), identity))
            db.execute("UPDATE approvals SET status='rejected',updated_at=? WHERE run_id=? AND status IN ('pending','approved')", (stamp(), identity))
            self.event_in(db, identity, "cancelled", "Cancelled by user; an already in-flight request may still finish")
        return self.detail(identity)

    def add_item(self, title, detail="", run=None, step=None):
        title = text_field(title, "Work item title", 200)
        detail = text_field(detail, "Work item detail", 4000, False)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if run:
                self.assert_active(db, run)
                existing = db.execute("SELECT * FROM work_items WHERE run_id=? AND step=?", (run["id"], step)).fetchone()
                if existing:
                    return dict(existing)
            identity = uid()
            db.execute("INSERT INTO work_items(id,title,detail,run_id,step,created_at) VALUES(?,?,?,?,?,?)", (identity, title, detail, run["id"] if run else None, step, stamp()))
            return dict(db.execute("SELECT * FROM work_items WHERE id=?", (identity,)).fetchone())

    def list_items(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM work_items ORDER BY created_at DESC,rowid DESC LIMIT 200")]

    def item_status(self, identity, status):
        if status not in ("open", "done"):
            raise ValueError("Status must be open or done")
        with self.connect() as db:
            if not db.execute("UPDATE work_items SET status=? WHERE id=?", (status, identity)).rowcount:
                raise Missing("Work item not found")

    def propose(self, run, step, tool, arguments):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assert_active(db, run)
            existing = db.execute("SELECT * FROM approvals WHERE run_id=? AND step=?", (run["id"], step)).fetchone()
            if existing:
                return self.decode(existing)
            if db.execute("SELECT 1 FROM approvals WHERE run_id=? AND tool=? AND status='rejected'", (run["id"], tool)).fetchone():
                raise ValueError("User rejected this external action; no further proposals are permitted in this run")
            identity, now = uid(), stamp()
            db.execute("INSERT INTO approvals(id,run_id,step,tool,arguments,status,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?)", (identity, run["id"], step, tool, json.dumps(arguments), now, now))
            self.event_in(db, run["id"], "approval_requested", f"Review the exact {tool} proposal before it can execute")
            return self.decode(db.execute("SELECT * FROM approvals WHERE id=?", (identity,)).fetchone())

    def decide(self, identity, approved):
        if type(approved) is not bool:
            raise ValueError("approved must be a boolean")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            approval = self.decode(db.execute("SELECT * FROM approvals WHERE id=?", (identity,)).fetchone())
            run = self.decode(db.execute("SELECT * FROM runs WHERE id=?", (approval["run_id"],)).fetchone())
            if approval["status"] != "pending" or run["status"] != "waiting_approval":
                raise Conflict("This proposal is not awaiting a decision")
            status = "approved" if approved else "rejected"
            db.execute("UPDATE approvals SET status=?,updated_at=? WHERE id=?", (status, stamp(), identity))
            db.execute("UPDATE runs SET status='queued',attempt=0,next_at=?,updated_at=? WHERE id=?", (stamp(), stamp(), run["id"]))
            self.event_in(db, run["id"], status, "User approved the exact proposal" if approved else "User rejected the proposal")
        return {"status": status}

    def begin_write(self, run, identity):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assert_active(db, run)
            if not db.execute("UPDATE approvals SET status='executing',updated_at=? WHERE id=? AND status='approved'", (stamp(), identity)).rowcount:
                raise Conflict("Proposal is no longer approved")

    def finish_write(self, identity, status, result):
        with self.connect() as db:
            db.execute("UPDATE approvals SET status=?,result=?,updated_at=? WHERE id=? AND status='executing'", (status, json.dumps(result), stamp(), identity))

    def list_approvals(self):
        with self.connect() as db:
            return [self.decode(r) for r in db.execute("SELECT a.*,r.title AS run_title FROM approvals a JOIN runs r ON a.run_id=r.id WHERE a.status='pending' AND r.status='waiting_approval' ORDER BY a.created_at")]

    def create_schedule(self, data):
        task = run_input(data.get("task", {}))
        spec = data.get("spec", {})
        if not isinstance(spec, dict) or set(spec) - {"cadence", "timezone", "time", "weekday"}:
            raise ValueError("Invalid schedule specification")
        next_at = next_occurrence(spec)
        label = text_field(data.get("label"), "Schedule label", 160)
        with self.connect() as db:
            db.execute("INSERT INTO schedules(id,label,task,spec,next_at,created_at) VALUES(?,?,?,?,?,?)", (uid(), label, json.dumps(task), json.dumps(spec), next_at, stamp()))

    def list_schedules(self):
        with self.connect() as db:
            return [self.decode(r) for r in db.execute("SELECT * FROM schedules ORDER BY created_at DESC,rowid DESC")]

    def enable_schedule(self, identity, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self.connect() as db:
            row = self.decode(db.execute("SELECT * FROM schedules WHERE id=?", (identity,)).fetchone())
            db.execute("UPDATE schedules SET enabled=?,next_at=? WHERE id=?", (int(enabled), next_occurrence(row["spec"]) if enabled else row["next_at"], identity))

    def enqueue_due(self, now=None):
        now = now or utc_now()
        queued = 0
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT * FROM schedules WHERE enabled=1 AND next_at<=?", (stamp(now),)).fetchall():
                schedule = self.decode(row)
                # Catch up once after downtime; do not flood the queue with every missed slot.
                if not db.execute("SELECT 1 FROM runs WHERE schedule_id=? AND status IN ('queued','running','waiting_approval')", (schedule["id"],)).fetchone():
                    self._insert_run(db, schedule["task"], schedule["id"], schedule["next_at"])
                    queued += 1
                db.execute("UPDATE schedules SET next_at=? WHERE id=?", (next_occurrence(schedule["spec"], now), schedule["id"]))
        return queued
