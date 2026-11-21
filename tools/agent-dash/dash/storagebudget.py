"""Storage budget: the daily footprint of the agent-log stack, and days to full.

The laptop holds the agent history in several places at once (the
VictoriaLogs store, the lossless session archive, the log archive, the
agentq job folders, worker scratch and the session originals that have not
been reclaimed yet). Nothing measured the total, so the disk filled up
without warning. This module measures it once a day, appends one line to a
history file, and projects the days left at the 7-day growth rate.

Pieces:

* :func:`measure` reads the store's own counters from the VictoriaLogs
  ``/metrics`` endpoint (``vl_compressed_data_size_bytes``,
  ``vl_uncompressed_data_size_bytes``, ``vl_free_disk_space_bytes``) and
  walks the five on-disk trees. It never writes to the store and never
  reads a log line.
* :func:`append_history` appends one JSON line to
  ``<state>/storage-budget.jsonl``. The file is the only state: the
  dashboard card, the doctor check and the CLI all read its last line, so
  a measurement is taken once and read everywhere.
* :func:`growth` is the per-component 7-day rate (bytes/day) from the
  history, and :func:`project` turns it into days until the data volume
  reaches 70/78/85% used.
* :func:`state_of` is the one verdict: ``alert`` at 78% used or under 180
  days to 80%, ``warn`` at 70% or under 365 days, else ``ok``.

Owner rule: logs are historical data kept with maximum retention. Nothing
here deletes, prunes or re-encodes anything; it only measures and reports.

Entry points: ``main(argv)`` (the CLI, ``agent_dash.py storage budget``),
:func:`run` (the once-a-day hook in ``agent_dash.py logs scheduled``) and
:func:`latest` (the read-only payload behind ``GET /api/storage/budget``
and the doctor check).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from dash.dataroot import state_dir as _state_dir

#: The history file, under the state dir. One JSON line per measurement.
HISTORY_NAME = "storage-budget.jsonl"
#: Warn at this share of the data volume used, or under this many days to 80%.
WARN_DISK_PCT = 70.0
ALERT_DISK_PCT = 78.0
WARN_DAYS_TO_80 = 365.0
ALERT_DAYS_TO_80 = 180.0
#: The projection points. 80 is the threshold the state uses; the CLI and the
#: card print 70/78/85.
PROJECT_PCTS = (70.0, 78.0, 80.0, 85.0)
#: The growth window: the rate is measured over this many days of history.
GROWTH_WINDOW_DAYS = 7.0
#: A rate needs at least this much history to be reported (a same-hour pair
#: divides by almost nothing and reports a wild number).
MIN_SPAN_DAYS = 0.25
#: The scheduled hook measures at most once per this interval.
SCHEDULED_MIN_INTERVAL_S = 20 * 3600.0
#: The store's metrics endpoint (loopback only, same store as ``dash.vlogs``).
DEFAULT_URL = "http://127.0.0.1:19428"
URL_ENV = "AGENT_DASH_VLOGS_URL"
METRICS_PATH = "/metrics"
METRICS_TIMEOUT_S = 3.0
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

#: The measured components, in report order. ``vlogs_uncompressed`` is the
#: same rows as ``vlogs_compressed`` and is never added to the footprint.
COMPONENTS = (
    ("vlogs_compressed", "VictoriaLogs store (compressed)"),
    ("vlogs_uncompressed", "VictoriaLogs store (uncompressed)"),
    ("session_archive", "session archive (tar.zst)"),
    ("session_manifest", "session manifest"),
    ("log_archive", "log archive"),
    ("agentq_jobs", "agentq job folders"),
    ("scratch", "worker scratch"),
    ("session_originals", "session originals"),
)
#: Components that add up to the footprint (no double counting).
FOOTPRINT = (
    "vlogs_compressed",
    "session_archive",
    "session_manifest",
    "log_archive",
    "agentq_jobs",
    "scratch",
    "session_originals",
)
LABELS = dict(COMPONENTS)


# --------------------------------------------------------------- helpers

def human_bytes(count) -> str:
    """GiB with one decimal, the unit the storage audit used."""
    try:
        value = float(count)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            return ("%d %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024.0
    return "%.1f TiB" % value  # pragma: no cover - the loop always returns


def human_days(days) -> str:
    """``812 d`` / ``1.2 y`` / ``-`` for an unknown projection."""
    if days is None:
        return "-"
    try:
        value = float(days)
    except (TypeError, ValueError):
        return "-"
    if value < 0:
        return "past"
    if value >= 730:
        return "%.1f y" % (value / 365.0)
    return "%d d" % round(value)


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def history_path(cfg: dict | None = None) -> Path:
    """``<state>/storage-budget.jsonl``; ``AGENT_DASH_STORAGE_BUDGET`` overrides."""
    override = os.environ.get("AGENT_DASH_STORAGE_BUDGET")
    if override:
        return Path(os.path.expanduser(override))
    return Path(_state_dir(cfg)) / HISTORY_NAME


def _dir_bytes(path) -> tuple[int, int]:
    """``(bytes, files)`` under ``path``; a missing path is ``(0, 0)``.

    Symlinks are counted as their own size and never followed, so a browsing
    symlink into the archive cannot double count it. ``os.scandir`` is used
    because the dirent already says whether an entry is a dir, so a tree of a
    million files costs one stat per file instead of two.
    """
    total = 0
    files = 0
    root = os.path.expanduser(str(path))
    if os.path.islink(root) or not os.path.isdir(root):
        return 0, 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            total += st.st_size
            files += 1
    return total, files


def _file_bytes(path) -> tuple[int, int]:
    """``(bytes, 1)`` for one regular file, ``(0, 0)`` when it is absent."""
    try:
        st = os.lstat(os.path.expanduser(str(path)))
    except OSError:
        return 0, 0
    if not os.path.isfile(os.path.expanduser(str(path))) or os.path.islink(os.path.expanduser(str(path))):
        return 0, 0
    return int(st.st_size), 1


def _glob_bytes(root, pattern: str) -> tuple[int, int]:
    """``(bytes, files)`` of the files matching ``pattern`` under ``root``."""
    total = 0
    files = 0
    base = Path(os.path.expanduser(str(root)))
    if not base.is_dir():
        return 0, 0
    for path in base.glob(pattern):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            total += path.stat().st_size
            files += 1
        except OSError:
            continue
    return total, files


# --------------------------------------------------------------- metrics

def _loopback(url: str) -> bool:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    return parsed.scheme == "http" and (parsed.hostname or "") in LOOPBACK_HOSTS


def read_metrics(url: str | None = None, timeout: float = METRICS_TIMEOUT_S) -> dict:
    """The three store counters from ``/metrics``, or ``{}`` when it is down.

    A down store is not an error: the on-disk components are still measured
    and the sample records ``store: false``.
    """
    base = (url or os.environ.get(URL_ENV) or DEFAULT_URL).rstrip("/")
    if not _loopback(base):
        return {}
    try:
        with urllib.request.urlopen(base + METRICS_PATH, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return {}
    return parse_metrics(text)


def parse_metrics(text: str) -> dict:
    """Sum the store counters out of a Prometheus text body.

    ``vl_compressed_data_size_bytes`` and ``vl_uncompressed_data_size_bytes``
    are per storage type (``inmemory``, ``small``, ``big``) and are summed;
    ``vl_free_disk_space_bytes`` is per path and the first is taken.
    """
    out = {"compressed": 0, "uncompressed": 0, "free_disk": None, "data_path": None}
    seen = False
    for line in (text or "").splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, rest = line.partition("{")
        if not rest:
            name, _, rest = line.partition(" ")
            value = rest.strip()
        else:
            _labels, _, tail = rest.partition("}")
            value = tail.strip()
        try:
            number = float(value.split()[0])
        except (IndexError, ValueError):
            continue
        if name == "vl_compressed_data_size_bytes":
            out["compressed"] += int(number)
            seen = True
        elif name == "vl_uncompressed_data_size_bytes":
            out["uncompressed"] += int(number)
            seen = True
        elif name == "vl_free_disk_space_bytes" and out["free_disk"] is None:
            out["free_disk"] = int(number)
            marker = 'path="'
            if marker in line:
                out["data_path"] = line.split(marker, 1)[1].split('"', 1)[0]
    if not seen:
        return {}
    return out


# --------------------------------------------------------------- measure

def _archive_dest(cfg: dict | None) -> Path:
    from dash import sessionarchive

    try:
        return Path(sessionarchive.load_config(cfg)["dest"])
    except Exception:  # a broken config must not stop the measurement
        return Path(os.path.expanduser(sessionarchive.DEFAULT_DEST))


def _originals_roots() -> list[str]:
    from dash import sessionarchive

    return [os.path.expanduser(root) for root, _fn in sessionarchive.TOOLS.values()]


def _scratch_root() -> Path:
    from dash import scratchdir

    return Path(scratchdir.scratch_root())


def _agentq_home() -> Path:
    override = os.environ.get("AGENTQ_HOME")
    if override:
        return Path(os.path.expanduser(override))
    return Path.home() / ".local" / "state" / "agentq"


def measure(
    cfg: dict | None = None,
    *,
    now: float | None = None,
    url: str | None = None,
    metrics: dict | None = None,
    walk: bool = True,
) -> dict:
    """One sample: the store counters, the disk, and every on-disk component.

    ``metrics`` and ``walk`` are seams for tests: a test passes the counters
    it wants and ``walk=False`` to skip the trees (the components then read
    zero). Nothing here writes.
    """
    now = time.time() if now is None else float(now)
    state = Path(_state_dir(cfg))
    dest = _archive_dest(cfg)
    store = dict(metrics) if metrics is not None else read_metrics(url)

    components: dict[str, dict] = {}
    components["vlogs_compressed"] = {"bytes": int(store.get("compressed") or 0), "units": 0}
    components["vlogs_uncompressed"] = {"bytes": int(store.get("uncompressed") or 0), "units": 0}

    if walk:
        archive_bytes, archive_units = _glob_bytes(dest / "archive", "**/*.tar.zst")
        components["session_archive"] = {"bytes": archive_bytes, "units": archive_units}
        manifest_bytes, manifest_files = _file_bytes(dest / "state" / "manifest.jsonl")
        seg_bytes, seg_files = _dir_bytes(dest / "state" / "manifest-segments")
        components["session_manifest"] = {
            "bytes": manifest_bytes + seg_bytes,
            "units": manifest_files + seg_files,
        }
        log_bytes, log_files = _dir_bytes(state / "log-archive")
        components["log_archive"] = {"bytes": log_bytes, "units": log_files}
        jobs_bytes, jobs_units = _dir_bytes(_agentq_home() / "jobs")
        components["agentq_jobs"] = {"bytes": jobs_bytes, "units": jobs_units}
        scratch_bytes, scratch_units = _dir_bytes(_scratch_root())
        components["scratch"] = {"bytes": scratch_bytes, "units": scratch_units}
        orig_bytes = 0
        orig_units = 0
        for root in _originals_roots():
            part_bytes, part_files = _dir_bytes(root)
            orig_bytes += part_bytes
            orig_units += part_files
        components["session_originals"] = {"bytes": orig_bytes, "units": orig_units}
    else:
        for name, _label in COMPONENTS:
            components.setdefault(name, {"bytes": 0, "units": 0})

    disk_path = store.get("data_path") or str(state)
    try:
        usage = shutil.disk_usage(disk_path)
        total, used, free = int(usage.total), int(usage.used), int(usage.free)
    except OSError:
        total = used = free = 0
    if not free and store.get("free_disk"):
        free = int(store["free_disk"])
        used = max(0, total - free)
    pct = (100.0 * used / total) if total else 0.0

    return {
        "ts": round(now, 3),
        "iso": iso(now),
        "store": bool(store),
        "disk": {
            "path": disk_path,
            "total": total,
            "used": used,
            "free": free,
            "pct": round(pct, 2),
        },
        "components": components,
        "footprint": sum(int(components[name]["bytes"]) for name in FOOTPRINT),
    }


# --------------------------------------------------------------- history

def load_history(path) -> list[dict]:
    """Every parseable sample in the history file, oldest first."""
    rows: list[dict] = []
    try:
        with open(os.path.expanduser(str(path)), "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and isinstance(rec.get("ts"), (int, float)):
                    rows.append(rec)
    except OSError:
        return []
    rows.sort(key=lambda rec: float(rec["ts"]))
    return rows


def last_sample(path) -> dict | None:
    """The newest sample, read from the tail of the file (never the whole file)."""
    target = os.path.expanduser(str(path))
    try:
        with open(target, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            window = min(size, 1 << 16)
            fh.seek(size - window)
            tail = fh.read()
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("ts"), (int, float)):
            return rec
    return None


def append_history(path, sample: dict) -> None:
    """Append one sample. The parent dir is made; the line is fsynced."""
    target = Path(os.path.expanduser(str(path)))
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(sample, separators=(",", ":"), sort_keys=True) + "\n"
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


# --------------------------------------------------------------- growth

def _component_bytes(sample: dict, name: str) -> int:
    comp = (sample.get("components") or {}).get(name) or {}
    try:
        return int(comp.get("bytes") or 0)
    except (TypeError, ValueError):
        return 0


def growth(history: list[dict], now: float, window_days: float = GROWTH_WINDOW_DAYS) -> dict:
    """Per-component bytes/day over the window, plus the footprint total.

    The rate is ``(newest - oldest) / span_days`` over the samples inside the
    window. A component with less than :data:`MIN_SPAN_DAYS` of history, or
    with no earlier sample, reports ``None`` (unknown, not zero).
    """
    now = float(now)
    cutoff = now - float(window_days) * 86400.0
    inside = [rec for rec in history if float(rec["ts"]) >= cutoff]
    out: dict = {"window_days": float(window_days), "span_days": 0.0, "components": {}}
    if len(inside) < 2:
        for name, _label in COMPONENTS:
            out["components"][name] = None
        out["footprint"] = None
        out["disk_used"] = None
        return out
    first, last = inside[0], inside[-1]
    span = (float(last["ts"]) - float(first["ts"])) / 86400.0
    out["span_days"] = round(span, 3)
    if span < MIN_SPAN_DAYS:
        for name, _label in COMPONENTS:
            out["components"][name] = None
        out["footprint"] = None
        out["disk_used"] = None
        return out
    for name, _label in COMPONENTS:
        delta = _component_bytes(last, name) - _component_bytes(first, name)
        out["components"][name] = round(delta / span, 1)
    out["footprint"] = round(
        sum(_component_bytes(last, name) - _component_bytes(first, name) for name in FOOTPRINT) / span, 1
    )
    try:
        used_delta = float((last.get("disk") or {}).get("used") or 0) - float(
            (first.get("disk") or {}).get("used") or 0
        )
        out["disk_used"] = round(used_delta / span, 1)
    except (TypeError, ValueError):
        out["disk_used"] = None
    return out


def project(sample: dict, rates: dict, pcts=PROJECT_PCTS) -> dict:
    """Days until the data volume reaches each share, at the footprint rate.

    ``None`` means "not at this rate" (flat, shrinking, or no history yet).
    """
    disk = sample.get("disk") or {}
    total = float(disk.get("total") or 0)
    used = float(disk.get("used") or 0)
    rate = rates.get("footprint")
    out: dict = {"rate_bytes_per_day": rate, "days": {}}
    for pct in pcts:
        key = "%g" % pct
        if not total or rate is None or rate <= 0:
            out["days"][key] = None
            continue
        target = total * float(pct) / 100.0
        out["days"][key] = round(max(0.0, (target - used) / float(rate)), 1)
    return out


def state_of(sample: dict, projection: dict) -> str:
    """``alert``, ``warn`` or ``ok`` from the disk share and the days to 80%."""
    disk = sample.get("disk") or {}
    pct = float(disk.get("pct") or 0.0)
    days80 = (projection.get("days") or {}).get("80")
    if pct >= ALERT_DISK_PCT or (days80 is not None and days80 < ALERT_DAYS_TO_80):
        return "alert"
    if pct >= WARN_DISK_PCT or (days80 is not None and days80 < WARN_DAYS_TO_80):
        return "warn"
    return "ok"


def thresholds() -> dict:
    return {
        "warn_disk_pct": WARN_DISK_PCT,
        "alert_disk_pct": ALERT_DISK_PCT,
        "warn_days_to_80": WARN_DAYS_TO_80,
        "alert_days_to_80": ALERT_DAYS_TO_80,
        "project_pcts": list(PROJECT_PCTS),
        "growth_window_days": GROWTH_WINDOW_DAYS,
    }


def latest(cfg: dict | None = None, *, now: float | None = None, path=None) -> dict:
    """The read-only payload: the last sample, its rates, projection and state.

    This is what ``GET /api/storage/budget`` and the doctor check read. It
    never measures (no walk, no store call), so it is cheap enough for a
    polled endpoint and a doctor pass.
    """
    now = time.time() if now is None else float(now)
    target = Path(path) if path is not None else history_path(cfg)
    sample = last_sample(target)
    if sample is None:
        return {
            "state": "unknown",
            "sample": None,
            "growth": None,
            "projection": None,
            "thresholds": thresholds(),
            "history": str(target),
            "install": "python3 agent_dash.py storage budget",
        }
    history = load_history(target)
    rates = growth(history, now)
    projection = project(sample, rates)
    return {
        "state": state_of(sample, projection),
        "sample": sample,
        "growth": rates,
        "projection": projection,
        "thresholds": thresholds(),
        "history": str(target),
        "samples": len(history),
    }


# --------------------------------------------------------------- report

def _rate_text(value) -> str:
    if value is None:
        return "-"
    return "%+.2f GiB/d" % (float(value) / (1024.0 ** 3))


def format_report(payload: dict, out=None) -> None:
    """The CLI text: disk, one line per component, the projection, the verdict."""
    out = out or sys.stdout
    sample = payload.get("sample")
    if not sample:
        print("storage budget: no measurement yet", file=out)
        print("  run: python3 agent_dash.py storage budget", file=out)
        return
    disk = sample.get("disk") or {}
    rates = payload.get("growth") or {}
    projection = payload.get("projection") or {}
    comp_rates = rates.get("components") or {}
    print(
        "storage budget  %s  state: %s"
        % (sample.get("iso") or iso(sample.get("ts", 0)), payload.get("state")),
        file=out,
    )
    print(
        "disk  %s  %.1f GiB total, %.1f GiB used (%.1f%%), %.1f GiB free"
        % (
            disk.get("path") or "-",
            float(disk.get("total") or 0) / 1024.0 ** 3,
            float(disk.get("used") or 0) / 1024.0 ** 3,
            float(disk.get("pct") or 0.0),
            float(disk.get("free") or 0) / 1024.0 ** 3,
        ),
        file=out,
    )
    print("", file=out)
    print("%-34s %12s %14s %8s" % ("component", "size", "7-day rate", "units"), file=out)
    for name, label in COMPONENTS:
        comp = (sample.get("components") or {}).get(name) or {}
        print(
            "%-34s %12s %14s %8s"
            % (
                label,
                human_bytes(comp.get("bytes") or 0),
                _rate_text(comp_rates.get(name)),
                comp.get("units") if comp.get("units") else "-",
            ),
            file=out,
        )
    print(
        "%-34s %12s %14s"
        % ("footprint (no double count)", human_bytes(sample.get("footprint") or 0), _rate_text(rates.get("footprint"))),
        file=out,
    )
    print("", file=out)
    days = projection.get("days") or {}
    print(
        "days to 70%%: %s   to 78%%: %s   to 85%%: %s   (at %s, %s of history)"
        % (
            human_days(days.get("70")),
            human_days(days.get("78")),
            human_days(days.get("85")),
            _rate_text(projection.get("rate_bytes_per_day")),
            ("%.1f d" % rates["span_days"]) if rates.get("span_days") else "no",
        ),
        file=out,
    )
    if rates.get("disk_used") is not None:
        print("whole-volume rate: %s" % _rate_text(rates.get("disk_used")), file=out)
    print(
        "warn: %.0f%% used or under %.0f days to 80%%; alert: %.0f%% or under %.0f days"
        % (WARN_DISK_PCT, WARN_DAYS_TO_80, ALERT_DISK_PCT, ALERT_DAYS_TO_80),
        file=out,
    )


# --------------------------------------------------------------- entry points

def run(opts: dict, out=None, *, now: float | None = None) -> int:
    """One measurement. Returns 0 ok/warn, 1 alert, 2 bad options.

    ``opts["scheduled"]`` makes it a no-op when the last sample is younger
    than :data:`SCHEDULED_MIN_INTERVAL_S`, so the hourly archive job records
    once a day. ``opts["record"]`` (default true) appends the sample.
    """
    out = out or sys.stdout
    now = time.time() if now is None else float(now)
    path = Path(opts.get("history") or history_path())
    if opts.get("scheduled"):
        prior = last_sample(path)
        if prior is not None:
            age = now - float(prior["ts"])
            if age < SCHEDULED_MIN_INTERVAL_S:
                print(
                    "storage budget: skip (last sample %.1f h ago)" % (age / 3600.0),
                    file=out,
                )
                return 0
    sample = measure(now=now, url=opts.get("url"), metrics=opts.get("metrics"), walk=opts.get("walk", True))
    if opts.get("record", True):
        append_history(path, sample)
    history = load_history(path)
    rates = growth(history, now)
    projection = project(sample, rates)
    payload = {
        "state": state_of(sample, projection),
        "sample": sample,
        "growth": rates,
        "projection": projection,
        "thresholds": thresholds(),
        "history": str(path),
        "samples": len(history),
    }
    if opts.get("json"):
        print(json.dumps(payload, indent=2, sort_keys=True), file=out)
    else:
        format_report(payload, out)
    return 1 if payload["state"] == "alert" else 0


_DEFAULTS = {
    "json": False,
    "record": True,
    "scheduled": False,
    "history": None,
    "url": None,
    "metrics": None,
    "walk": True,
}


def parse_args(args: list[str]):
    opts = dict(_DEFAULTS)
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--json":
            opts["json"] = True
        elif arg == "--no-record":
            opts["record"] = False
        elif arg == "--no-walk":
            opts["walk"] = False
        elif arg == "--scheduled":
            opts["scheduled"] = True
        elif arg in ("--history", "--url"):
            if i + 1 >= len(args):
                return "%s needs a value" % arg
            opts["history" if arg == "--history" else "url"] = args[i + 1]
            i += 1
        else:
            return "unknown storage budget argument: %s" % arg
        i += 1
    return opts


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "budget":
        args = args[1:]
    opts = parse_args(args)
    if isinstance(opts, str):
        print(opts, file=sys.stderr)
        return 2
    return run(opts, out=out)
