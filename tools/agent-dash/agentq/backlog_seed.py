"""Create the standing backlog templates. Run once by the owner or a lead; the broker never runs it.

    python3 -m agentq.backlog_seed                    # config at $AGENTQ_HOME/backlog-seed.json
    python3 -m agentq.backlog_seed --config PATH      # or any other JSON file

The config holds the local folders the templates review. It is not tracked, so the
repo carries no personal paths. Shape:

    {"merged-review-lor": {"workspace": "~/src/repo-a", "report_dir": "~/src/repo-a-state/reports"},
     "merged-review-ws":  {"workspace": "~/src/repo-b", "report_dir": "~/src/repo-b-state/reports"}}

Each prompt in ``agentq/backlog_prompts/`` names ``{workspace}`` and ``{report_dir}``;
the seed fills them in and copies the result into the template.

Idempotent: a template that already exists is left as it is, so a second run creates
nothing. Each run prints one ``ANSWER:`` line; exit 1 when the config is missing or incomplete.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from agentq import backlog as backlog_lib
from agentq import db

PROMPTS = Path(__file__).resolve().parent / "backlog_prompts"
CONFIG_NAME = "backlog-seed.json"
SEEDS = (
    {"name": "merged-review-lor", "prompt": "merged-review-lor.md", "every": "6h", "kind": "read", "res": "light"},
    {"name": "merged-review-ws", "prompt": "merged-review-ws.md", "every": "6h", "kind": "read", "res": "light"},
)  # fmt: skip


def load_config(path: Path) -> dict[str, dict[str, str]]:
    """The per-template folders from the config file. Raises ValueError when it is unreadable or incomplete."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"no seed config at {path} ({exc.strerror}); see the backlog_seed docstring") from exc
    except ValueError as exc:
        raise ValueError(f"seed config {path} is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"seed config {path} must be a JSON object keyed by template name")
    missing = [spec["name"] for spec in SEEDS if not isinstance(data.get(spec["name"]), dict)]
    missing += [
        spec["name"] for spec in SEEDS
        if isinstance(data.get(spec["name"]), dict)
        and not all(isinstance(data[spec["name"]].get(key), str) and data[spec["name"]][key].strip()
                    for key in ("workspace", "report_dir"))
    ]
    if missing:
        raise ValueError(f"seed config {path} lacks workspace and report_dir for: {', '.join(missing)}")
    return data


def render(prompt: Path, values: dict[str, str]) -> str:
    """The prompt text with ``{workspace}`` and ``{report_dir}`` filled in."""
    text = prompt.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def seed(conn, home, now: float, config: dict, prompts: Path = PROMPTS) -> tuple[list[str], list[str]]:
    """Add every seed template that is missing. Returns (created names, names already present)."""
    created, present = [], []
    with tempfile.TemporaryDirectory() as scratch:
        for spec in SEEDS:
            if backlog_lib.get(conn, spec["name"]) is not None:
                present.append(spec["name"])
                continue
            folders = config[spec["name"]]
            values = {
                "workspace": os.path.abspath(os.path.expanduser(folders["workspace"])),
                "report_dir": os.path.abspath(os.path.expanduser(folders["report_dir"])),
            }
            rendered = Path(scratch) / f"{spec['name']}.md"
            rendered.write_text(render(prompts / spec["prompt"], values), encoding="utf-8")
            with db.tx(conn):
                backlog_lib.add(
                    conn, home, name=spec["name"], prompt_file=rendered,
                    every_s=backlog_lib.parse_duration(spec["every"]), kind=spec["kind"], res=spec["res"],
                    workspace=values["workspace"], tier=None, max_runtime_s=None, now=now,
                )
            created.append(spec["name"])
    return created, present


def main(argv=None, env=None, out=None, now=time.time) -> int:
    parser = argparse.ArgumentParser(prog="agentq.backlog_seed", description=__doc__.split("\n")[0])
    parser.add_argument("--home", help="agentq home (default $AGENTQ_HOME or ~/.local/state/agentq)")
    parser.add_argument("--config", help="seed config JSON (default <home>/backlog-seed.json)")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    env = os.environ if env is None else env
    out = out or sys.stdout
    home = Path(os.path.expanduser(args.home)) if args.home else db.home_dir(env)
    config_path = Path(os.path.expanduser(args.config)) if args.config else home / CONFIG_NAME
    try:
        config = load_config(config_path)
    except ValueError as exc:
        print(f"ANSWER: {exc}", file=out)
        return 1
    conn = db.connect(home)
    created, present = seed(conn, home, now(), config)
    for name in created:
        print(f"created backlog {name}", file=out)
    for name in present:
        print(f"already present: backlog {name}", file=out)
    print(f"ANSWER: {len(created)} template(s) created, {len(present)} already present", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
