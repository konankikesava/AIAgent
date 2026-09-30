from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from task_hub.catalog import AGENTS
from task_hub.config import Config
from task_hub.llm import Model, RetryableError, parse_action
from task_hub.runtime import Engine
from task_hub.scheduling import next_occurrence, stamp
from task_hub.store import Conflict, Stopped, Store
from task_hub.tools import Tools


class ScriptModel:
    def __init__(self, *actions):
        self.actions = list(actions)

    def next(self, _messages, _run):
        value = self.actions.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class ApprovalTools(Tools):
    def __init__(self, config, store, failure=None):
        super().__init__(config, store)
        self.writes = []
        self.failure = failure

    def execute_approved_issue(self, arguments, approval_id):
        self.validate("github_create_issue", arguments)
        self.writes.append((arguments, approval_id))
        if self.failure:
            raise self.failure
        return {"number": 7, "url": "https://github.com/example/project/issues/7", "title": arguments["title"]}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config(Path(self.temp.name), provider="demo")
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        self.task = {"agent_id": "chief-of-staff", "title": "Review work", "instruction": "Prioritize the supplied tasks", "context": {}}

    def create(self, **changes):
        return self.store.create_run({**self.task, **changes})

    def live_writes(self):
        return replace(self.config, provider="compatible", model="test", github_repos=("example/project",), github_token="test-fixture-not-a-secret", github_writes=True)

    def issue(self):
        return {"action": "github_create_issue", "arguments": {"repo": "example/project", "title": "Onboarding checklist", "body": "Review local setup instructions"}}

    def test_all_five_agents_complete_in_offline_demo(self):
        for agent in AGENTS:
            created = self.create(agent_id=agent["id"], title=agent["name"])
            run = self.store.claim()
            with patch("task_hub.llm.request_json", side_effect=AssertionError("Demo must not use the network")):
                Engine(self.config, self.store).process(run)
            result = self.store.detail(created["id"])
            self.assertEqual(result["status"], "succeeded")
            self.assertIn("demonstration", result["result"])
            self.assertTrue(result["events"])
        self.assertEqual(len(self.store.list_items()), 5)

    def test_concurrent_workers_cannot_claim_the_same_run(self):
        self.create()
        with ThreadPoolExecutor(max_workers=8) as pool:
            claimed = list(pool.map(lambda _: self.store.claim(), range(8)))
        self.assertEqual(sum(r is not None for r in claimed), 1)

    def test_invalid_agent_and_oversized_notes_rejected(self):
        with self.assertRaises(ValueError):
            self.create(agent_id="shell-agent")
        with self.assertRaises(ValueError):
            self.create(context={"notes": "x" * 24001})
        with self.assertRaises(ValueError):
            self.create(context={"url": "http://internal-service"})

    def test_tool_validation_blocks_shell_and_arbitrary_repositories(self):
        tools = Tools(replace(self.config, github_repos=("example/project",)), self.store)
        for action in ("exec", "run_shell", "http_request", "sql"):
            with self.assertRaises(ValueError):
                tools.validate(action, {})
        for repo in ("other/project", "example/../secret", "example/project?url=evil", "example/.."):
            with self.assertRaises(ValueError):
                tools.validate("github_snapshot", {"repo": repo})
        tools.validate("github_snapshot", {"repo": "EXAMPLE/project"})

    def test_demo_never_offers_github_writes(self):
        config = replace(self.config, github_repos=("example/project",), github_writes=True, github_token="fixture")
        self.assertNotIn("github_create_issue", Tools(config, self.store).specs())
        with patch("task_hub.tools.request_json", side_effect=AssertionError("No network")):
            snapshot = Tools(config, self.store).snapshot("example/project")
        self.assertEqual(snapshot["mode"], "demo")

    def test_local_action_is_idempotent_on_checkpoint_replay(self):
        self.create()
        run = self.store.claim()
        tools = Tools(self.config, self.store)
        arguments = {"title": "Review contract", "detail": "Owner not specified"}
        first = tools.execute("add_work_item", arguments, run, 0)
        second = tools.execute("add_work_item", arguments, run, 0)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.list_items()), 1)

    def test_cancellation_cannot_be_overwritten_by_worker(self):
        created = self.create()
        run = self.store.claim()
        self.store.cancel(created["id"])
        with self.assertRaises(Stopped):
            self.store.finish(run, "succeeded", result="late result")
        with self.assertRaises(Stopped):
            self.store.add_item("Late item", run=run, step=0)
        self.assertEqual(self.store.get_run(created["id"])["status"], "cancelled")

    def test_transient_read_errors_retry_three_times_and_then_fail(self):
        created = self.create()
        engine = Engine(self.config, self.store, model=ScriptModel(*[RetryableError("Temporarily unavailable") for _ in range(3)]))
        for expected in ("queued", "queued", "failed"):
            engine.process(self.store.claim())
            self.assertEqual(self.store.get_run(created["id"])["status"], expected)
            with self.store.connect() as db:
                db.execute("UPDATE runs SET next_at=? WHERE id=?", (stamp(), created["id"]))
        self.assertEqual(self.store.get_run(created["id"])["attempt"], 3)

    def test_unknown_model_tool_fails_without_execution(self):
        created = self.create()
        Engine(self.config, self.store, model=ScriptModel({"action": "run_shell", "arguments": {"command": "echo bad"}})).process(self.store.claim())
        self.assertEqual(self.store.get_run(created["id"])["status"], "failed")
        self.assertEqual(self.store.list_items(), [])

    def test_action_budget_stops_an_unfinished_agent(self):
        created = self.create()
        config = replace(self.config, max_steps=1)
        actions = [{"action": "list_work_items", "arguments": {}}] * 2
        Engine(config, self.store, model=ScriptModel(*actions)).process(self.store.claim())
        run = self.store.get_run(created["id"])
        self.assertEqual(run["status"], "failed")
        self.assertIn("action limit", run["error"])

    def test_external_issue_waits_for_exact_approval_and_resumes(self):
        created = self.create()
        config = self.live_writes()
        tools = ApprovalTools(config, self.store)
        engine = Engine(config, self.store, tools=tools, model=ScriptModel(self.issue(), {"final": "Issue created; see source URL"}))
        engine.process(self.store.claim())
        detail = self.store.detail(created["id"])
        self.assertEqual(detail["status"], "waiting_approval")
        self.assertEqual(tools.writes, [])
        approval = detail["approvals"][0]
        self.assertEqual(approval["arguments"], self.issue()["arguments"])
        self.store.decide(approval["id"], True)
        with self.assertRaises(Conflict):
            self.store.decide(approval["id"], True)
        engine.process(self.store.claim())
        self.assertEqual(len(tools.writes), 1)
        self.assertEqual(tools.writes[0][0], approval["arguments"])
        self.assertEqual(self.store.get_run(created["id"])["status"], "succeeded")

    def test_rejected_issue_does_not_execute(self):
        created = self.create()
        config = self.live_writes()
        tools = ApprovalTools(config, self.store)
        engine = Engine(config, self.store, tools=tools, model=ScriptModel(self.issue(), {"final": "Proposal rejected; no issue was created"}))
        engine.process(self.store.claim())
        approval = self.store.detail(created["id"])["approvals"][0]
        self.store.decide(approval["id"], False)
        engine.process(self.store.claim())
        self.assertEqual(tools.writes, [])
        self.assertEqual(self.store.get_run(created["id"])["status"], "succeeded")

    def test_rejection_is_enforced_even_if_model_proposes_again(self):
        created = self.create()
        config = self.live_writes()
        tools = ApprovalTools(config, self.store)
        engine = Engine(config, self.store, tools=tools, model=ScriptModel(self.issue(), self.issue()))
        engine.process(self.store.claim())
        self.store.decide(self.store.detail(created["id"])["approvals"][0]["id"], False)
        engine.process(self.store.claim())
        self.assertEqual(self.store.get_run(created["id"])["status"], "failed")
        self.assertEqual(len(self.store.detail(created["id"])["approvals"]), 1)
        self.assertEqual(tools.writes, [])

    def test_write_timeout_is_never_retried_automatically(self):
        created = self.create()
        config = self.live_writes()
        tools = ApprovalTools(config, self.store, failure=RetryableError("Timeout"))
        engine = Engine(config, self.store, tools=tools, model=ScriptModel(self.issue()))
        engine.process(self.store.claim())
        self.store.decide(self.store.detail(created["id"])["approvals"][0]["id"], True)
        engine.process(self.store.claim())
        detail = self.store.detail(created["id"])
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["approvals"][0]["status"], "uncertain")
        self.assertEqual(len(tools.writes), 1)
        self.assertIsNone(self.store.claim())

    def test_expired_read_worker_is_recovered(self):
        created = self.create()
        first = self.store.claim()
        with self.store.connect() as db:
            db.execute("UPDATE runs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (created["id"],))
        second = self.store.claim()
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["claim_token"], second["claim_token"])
        with self.assertRaises(Stopped):
            self.store.heartbeat(first)

    def test_expired_write_worker_is_flagged_uncertain(self):
        created = self.create()
        run = self.store.claim()
        approval = self.store.propose(run, 0, "github_create_issue", self.issue()["arguments"])
        with self.store.connect() as db:
            db.execute("UPDATE approvals SET status='executing' WHERE id=?", (approval["id"],))
            db.execute("UPDATE runs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (run["id"],))
        self.assertIsNone(self.store.claim())
        detail = self.store.detail(created["id"])
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["approvals"][0]["status"], "uncertain")

    def test_pending_approval_survives_database_reopen(self):
        created = self.create()
        config = self.live_writes()
        Engine(config, self.store, model=ScriptModel(self.issue())).process(self.store.claim())
        reopened = Store(self.store.path)
        self.assertEqual(reopened.get_run(created["id"])["status"], "waiting_approval")
        self.assertEqual(len(reopened.list_approvals()), 1)

    def test_schedule_catches_up_once_and_does_not_overlap(self):
        self.store.create_schedule({"label": "Daily review", "task": self.task, "spec": {"time": "08:00", "timezone": "Asia/Kolkata", "cadence": "daily"}})
        schedule = self.store.list_schedules()[0]
        now = datetime(2030, 1, 1, 4, tzinfo=timezone.utc)
        with self.store.connect() as db:
            db.execute("UPDATE schedules SET next_at='2029-12-25T02:30:00+00:00' WHERE id=?", (schedule["id"],))
        self.assertEqual(self.store.enqueue_due(now), 1)
        self.assertEqual(self.store.enqueue_due(now), 0)
        with self.store.connect() as db:
            db.execute("UPDATE schedules SET next_at='2029-12-26T02:30:00+00:00' WHERE id=?", (schedule["id"],))
        self.assertEqual(self.store.enqueue_due(now), 0)
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_schedule_and_approval_booleans_are_strict(self):
        self.store.create_schedule({"label": "Review", "task": self.task, "spec": {}})
        with self.assertRaises(ValueError):
            self.store.enable_schedule(self.store.list_schedules()[0]["id"], "true")
        with self.assertRaises(ValueError):
            self.store.decide("missing", 1)

    def test_model_json_contract_rejects_non_objects_and_mixed_commands(self):
        for value in ('[]', '{"final":"ok","action":"exec"}', '{"final":""}', 'plain text', '{"action":"list_work_items","arguments":[]}'):
            with self.assertRaises(ValueError):
                parse_action(value)

    def test_ollama_and_compatible_protocols_use_configured_model(self):
        messages = [{"role": "user", "content": "Return JSON"}]
        run = self.task
        config = replace(self.config, provider="ollama", model="my-local-model")
        with patch("task_hub.llm.request_json", return_value={"message": {"content": '{"final":"Done"}'}}) as request:
            self.assertEqual(Model(config).next(messages, run), {"final": "Done"})
            self.assertEqual(request.call_args.args[1]["model"], "my-local-model")
            self.assertFalse(request.call_args.args[1]["stream"])
        config = replace(config, provider="compatible", model_url="http://127.0.0.1:8000/v1")
        with patch("task_hub.llm.request_json", return_value={"choices": [{"message": {"content": '{"final":"Done"}'}}]}) as request:
            Model(config).next(messages, run)
            self.assertEqual(request.call_args.args[0], "http://127.0.0.1:8000/v1/chat/completions")

    def test_configuration_does_not_expose_secrets(self):
        config = replace(self.config, model_key="private-model-key", github_token="private-github-key", auth_token="private-hub-key")
        public = json.dumps(config.public())
        for secret in (config.model_key, config.github_token, config.auth_token):
            self.assertNotIn(secret, public)
        with self.assertRaises(ValueError):
            replace(self.config, host="0.0.0.0").validate()
        with self.assertRaises(ValueError):
            replace(self.config, model_url="https://user:secret@example.org").validate()


class SchedulingTests(unittest.TestCase):
    def test_kolkata_daily_time_converts_to_utc(self):
        after = datetime(2026, 9, 30, 2, tzinfo=timezone.utc)
        self.assertEqual(next_occurrence({"time": "08:00", "timezone": "Asia/Kolkata"}, after), "2026-09-30T02:30:00+00:00")
        after = datetime(2026, 9, 30, 2, 30, tzinfo=timezone.utc)
        self.assertEqual(next_occurrence({"time": "08:00", "timezone": "Asia/Kolkata"}, after), "2026-10-01T02:30:00+00:00")

    def test_weekly_only_fires_on_selected_weekday(self):
        after = datetime(2026, 9, 30, 4, tzinfo=timezone.utc)  # Wednesday
        self.assertEqual(next_occurrence({"cadence": "weekly", "weekday": 4, "time": "08:00", "timezone": "Asia/Kolkata"}, after), "2026-10-02T02:30:00+00:00")

    def test_dst_nonexistent_time_is_skipped(self):
        after = datetime(2026, 3, 8, 6, tzinfo=timezone.utc)
        self.assertEqual(next_occurrence({"time": "02:30", "timezone": "America/New_York"}, after), "2026-03-09T06:30:00+00:00")

    def test_dst_ambiguous_time_runs_only_first_occurrence(self):
        after = datetime(2026, 11, 1, 5, 20, tzinfo=timezone.utc)
        spec = {"time": "01:30", "timezone": "America/New_York"}
        self.assertEqual(next_occurrence(spec, after), "2026-11-01T05:30:00+00:00")
        self.assertEqual(next_occurrence(spec, datetime(2026, 11, 1, 6, tzinfo=timezone.utc)), "2026-11-02T06:30:00+00:00")

    def test_bad_time_timezone_and_naive_datetime_are_rejected(self):
        for spec in ({"time": "25:00"}, {"timezone": "Mars/Olympus"}, {"cadence": "hourly"}, {"weekday": True}, {"time": "8:00"}):
            with self.assertRaises(ValueError):
                next_occurrence(spec)
        with self.assertRaises(ValueError):
            next_occurrence({}, datetime(2026, 9, 30))


if __name__ == "__main__":
    unittest.main()
