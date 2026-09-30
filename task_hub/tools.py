import base64
from datetime import timedelta
import re
from urllib.parse import urlencode

from .llm import request_json
from .scheduling import stamp, utc_now
from .store import text_field


TOOL_SPECS = {
    "list_work_items": {"description": "Read open and completed local follow-up items.", "arguments": {}},
    "add_work_item": {"description": "Save one concrete local follow-up. This does not run another agent or contact anyone.", "arguments": {"title": "string, max 200", "detail": "string, max 4000 (optional)"}},
    "github_snapshot": {"description": "Read repository metadata, README, up to 20 commits from the last 7 days, and up to 20 recently updated open issues. Returned data is a bounded sample, not a full history.", "arguments": {"repo": "owner/name from configured allowlist"}},
    "github_create_issue": {"description": "Propose a GitHub issue. The exact repository, title, and body require user approval before writing.", "arguments": {"repo": "owner/name from configured allowlist", "title": "string, max 200", "body": "string, max 8000"}},
}


class Tools:
    def __init__(self, config, store):
        self.config, self.store = config, store

    def specs(self):
        allowed = ["list_work_items", "add_work_item"]
        if self.config.github_repos:
            allowed.append("github_snapshot")
            if self.config.github_writes and self.config.github_token and self.config.provider != "demo":
                allowed.append("github_create_issue")
        return {k: TOOL_SPECS[k] for k in allowed}

    def repo(self, value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", value) or value.split("/")[-1] in (".", ".."):
            raise ValueError("Repository must use owner/name")
        if value.lower() not in {r.lower() for r in self.config.github_repos}:
            raise ValueError("Repository is outside GITHUB_ALLOWED_REPOS")
        return value

    def validate_context(self, context):
        if context.get("repo"):
            self.repo(context["repo"])

    def validate(self, action, arguments):
        if action not in self.specs():
            raise ValueError("Tool is not permitted: " + str(action)[:100])
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        if action == "list_work_items":
            if arguments:
                raise ValueError("list_work_items accepts no arguments")
        elif action == "add_work_item":
            if set(arguments) - {"title", "detail"}:
                raise ValueError("Unsupported work-item arguments")
            text_field(arguments.get("title"), "Title", 200)
            text_field(arguments.get("detail", ""), "Detail", 4000, False)
        elif action == "github_snapshot":
            if set(arguments) != {"repo"}:
                raise ValueError("github_snapshot requires only repo")
            self.repo(arguments["repo"])
        elif action == "github_create_issue":
            if set(arguments) != {"repo", "title", "body"}:
                raise ValueError("Issue requires repo, title, and body")
            self.repo(arguments["repo"])
            text_field(arguments["title"], "Title", 200)
            text_field(arguments["body"], "Body", 8000)

    def execute(self, action, arguments, run, step):
        self.validate(action, arguments)
        self.store.heartbeat(run)
        if action == "list_work_items":
            return self.store.list_items()
        if action == "add_work_item":
            return self.store.add_item(arguments["title"], arguments.get("detail", ""), run, step)
        if action == "github_snapshot":
            return self.snapshot(arguments["repo"])
        # There is deliberately no generic shell, Python, SQL, or HTTP tool.
        raise ValueError("External writes can only execute through the approval path")

    def github(self, repo, suffix="", payload=None):
        repo = self.repo(repo)
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "WorkAssistantTaskHub/1.0"}
        if self.config.github_token:
            headers["Authorization"] = "Bearer " + self.config.github_token
        return request_json("https://api.github.com/repos/" + repo + suffix, payload, headers, timeout=25)

    def snapshot(self, repo):
        self.repo(repo)
        if self.config.provider == "demo":
            return {"mode": "demo", "repo": repo, "notice": "Illustrative fixture; no live GitHub request was made", "readme": "Sample repository setup instructions for demonstration only.", "commits": [], "issues": [], "sample_limit": 20}
        metadata = self.github(repo)
        result = {"mode": "live", "repo": repo, "url": metadata["html_url"], "description": metadata.get("description"), "default_branch": metadata["default_branch"], "sample_limit": 20, "warnings": []}
        since = stamp(utc_now() - timedelta(days=7))
        resources = {
            "readme": "/readme",
            "commits": "/commits?" + urlencode({"per_page": 20, "since": since}),
            "issues": "/issues?" + urlencode({"per_page": 20, "state": "open", "sort": "updated", "direction": "desc"}),
        }
        result["commit_window_start"] = since
        for key, suffix in resources.items():
            try:
                data = self.github(repo, suffix)
            except ValueError as exc:
                if str(exc) not in ("Upstream HTTP 404", "Upstream HTTP 409"):
                    raise
                result[key] = "" if key == "readme" else []
                result["warnings"].append(f"{key}: unavailable ({exc})")
                continue
            if key == "readme":
                result[key] = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")[:12000]
            elif key == "commits":
                result[key] = [{"sha": c["sha"], "message": c["commit"]["message"][:1000], "date": c["commit"].get("committer", {}).get("date"), "url": c["html_url"]} for c in data[:20]]
            else:
                result[key] = [{"number": i["number"], "title": i["title"], "body": (i.get("body") or "")[:1000], "state": i["state"], "updated_at": i["updated_at"], "url": i["html_url"]} for i in data[:20] if "pull_request" not in i]
        return result

    def execute_approved_issue(self, arguments, approval_id):
        self.validate("github_create_issue", arguments)
        marker = "\n\n<!-- task-hub-approval:" + approval_id + " -->"
        response = self.github(arguments["repo"], "/issues", {"title": arguments["title"], "body": arguments["body"] + marker})
        return {"number": response["number"], "url": response["html_url"], "title": response["title"]}
