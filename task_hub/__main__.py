import argparse
from dataclasses import replace
import logging
import os
from pathlib import Path
import re
import signal
import threading

from .config import Config
from .server import App, make_server


def load_env(path):
    """Read simple KEY=value configuration without shell evaluation or expansion."""
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"Invalid environment assignment on line {number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def main():
    parser = argparse.ArgumentParser(description="Run the work-assistant task hub")
    parser.add_argument("--demo", action="store_true", help="Use deterministic offline demonstration data; no LLM or GitHub requests")
    parser.add_argument("--env", help="Read a local KEY=value config file; existing environment variables win")
    parser.add_argument("--host", help="Override HUB_HOST; network binding requires an access token")
    parser.add_argument("--port", type=int, help="Override HUB_PORT")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.env:
            load_env(args.env)
        config = Config.from_env(demo=args.demo)
        config = replace(config, host=args.host or config.host, port=args.port or config.port)
        app = App(config)
        server = make_server(app)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    app.workers.start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    LOG = logging.getLogger(__name__)
    LOG.info("Task hub running at http://%s:%s (%s)", config.host, config.port, config.provider)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        app.workers.close()
        server.server_close()


if __name__ == "__main__":
    main()
