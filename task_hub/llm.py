import json
import urllib.error
import urllib.request


class RetryableError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, payload=None, headers=None, timeout=30, maximum=2_000_000):
    """Bound responses and never forward credentials through redirects."""
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, headers={"Accept": "application/json", **(headers or {})})
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            raw = response.read(maximum + 1)
            if len(raw) > maximum:
                raise ValueError("Upstream response exceeded the size limit")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        # Do not surface upstream bodies: they can contain credentials or private data.
        error = f"Upstream HTTP {exc.code}"
        if exc.code == 429 or exc.code >= 500:
            raise RetryableError(error) from None
        raise ValueError(error) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        raise RetryableError("Endpoint unavailable or timed out; check the configured service") from None


def parse_action(raw):
    if not isinstance(raw, str) or len(raw) > 32000:
        raise ValueError("Model response is missing or exceeds the limit")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("Model must return one JSON object; choose a model that follows JSON instructions") from None
    if not isinstance(result, dict):
        raise ValueError("Model must return one JSON object")
    if set(result) == {"final"} and isinstance(result["final"], str) and 0 < len(result["final"].strip()) <= 24000:
        return result
    if set(result) == {"action", "arguments"} and isinstance(result["action"], str) and isinstance(result["arguments"], dict):
        return result
    raise ValueError("Model response must contain final, or action plus arguments")


class Model:
    def __init__(self, config):
        self.config = config

    def next(self, messages, run):
        if self.config.provider == "demo":
            return self._demo(messages, run)
        cfg = self.config
        headers = {"Authorization": "Bearer " + cfg.model_key} if cfg.model_key else {}
        if cfg.provider == "ollama":
            response = request_json(cfg.model_url + "/api/chat", {
                "model": cfg.model, "messages": messages, "format": "json", "stream": False,
                "options": {"temperature": 0.1, "num_predict": 3000},
            }, headers, cfg.http_timeout)
            raw = response.get("message", {}).get("content")
        else:
            # MODEL_URL includes /v1 when required by the configured provider.
            response = request_json(cfg.model_url + "/chat/completions", {
                "model": cfg.model, "messages": messages, "temperature": 0.1,
                "response_format": {"type": "json_object"},
            }, headers, cfg.http_timeout)
            choices = response.get("choices", [])
            raw = choices[0].get("message", {}).get("content") if choices else None
        return parse_action(raw)

    @staticmethod
    def _demo(messages, run):
        """Deterministic demonstration, not an LLM or a hidden network call."""
        outputs = []
        for message in messages:
            if message["role"] != "user":
                continue
            try:
                value = json.loads(message["content"])
            except (ValueError, TypeError):
                continue
            if "tool_result" in value:
                outputs.append(value["tool_result"])
        actions = [o["tool"] for o in outputs]
        if "list_work_items" not in actions:
            return {"action": "list_work_items", "arguments": {}}
        repo = run["context"].get("repo")
        if repo and "github_snapshot" not in actions:
            return {"action": "github_snapshot", "arguments": {"repo": repo}}
        if "add_work_item" not in actions:
            return {"action": "add_work_item", "arguments": {
                "title": "[Demo] Review: " + run["title"][:140],
                "detail": "Demonstration follow-up created by the task runner. A real model produces context-specific actions in live mode.",
            }}
        from .catalog import agent_by_id
        name = agent_by_id(run["agent_id"])["name"]
        notes = run["context"].get("notes", "")
        report = (
            f"# {name} — demonstration\n\n"
            "This is an offline workflow demonstration. No language model or live GitHub data was used.\n\n"
            f"## Task input\n{run['instruction']}\n\n"
            + (f"## Supplied notes\n{notes}\n\n" if notes else "")
            + "## Actions completed\n- Read the local work-item list.\n- Saved a demonstration follow-up item.\n"
            + ("- Read an explicitly labeled sample repository snapshot.\n" if repo else "")
            + "\n## Next step\nRun in Ollama or compatible mode to produce an actual analysis from these inputs.\n"
        )
        return {"final": report}
