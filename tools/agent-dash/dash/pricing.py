"""Per-model price catalog. A missing rate never becomes zero."""

from __future__ import annotations

import json
import threading
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "catalog" / "prices.json"
FAMILIES = ("opus", "sonnet", "haiku")
PRICE_KEYS = (
    "input",
    "output",
    "cache_read",
    "cache_write_5m",
    "cache_write_1h",
    "reasoning",
)
_MILLION = Decimal(1_000_000)

_lock = threading.Lock()
_cache: dict = {}


def family(model: str | None, table: dict | None = None) -> str | None:
    """Chart family. This is not the price lookup."""
    if not model:
        return None
    text = model.lower()
    names = tuple(table.keys()) if table else FAMILIES
    for name in names:
        if name in text:
            return name
    return None


def series_key(model: str | None) -> str:
    fam = family(model)
    if fam:
        return fam
    text = (model or "").lower()
    for name in ("grok", "muse", "glm"):
        if name in text:
            return name
    return "other"


def load(path: Path | None = None) -> list:
    path = Path(path) if path is not None else DEFAULT_PATH
    data = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    models = data["models"] if isinstance(data, dict) else data
    out = []
    for row in models:
        if isinstance(row, dict) and row.get("model"):
            out.append(row)
    return out


def catalog(path: Path | None = None) -> list:
    path = Path(path) if path is not None else DEFAULT_PATH
    key = str(path)
    with _lock:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        hit = _cache.get(key)
        if hit and hit[0] == mtime and hit[1] is not None:
            return hit[1]
        try:
            rows = load(path)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            rows = hit[1] if hit and hit[1] is not None else []
        _cache[key] = (mtime, rows)
        return rows


def status(model: str | None, table=None) -> dict:
    row = _lookup(model, catalog() if table is None else table)
    if row is None:
        return {
            "priced": False,
            "billing_mode": "unknown",
            "confidence": "unknown",
            "provider": None,
        }
    return {
        "priced": row.get("input") is not None and row.get("output") is not None,
        "billing_mode": row.get("billing_mode") or "unknown",
        "confidence": row.get("confidence") or "unknown",
        "provider": row.get("provider"),
    }


def cost(
    model: str | None,
    input_tokens,
    output_tokens,
    cache_read,
    cache_write,
    table=None,
    *,
    cache_write_5m=None,
    cache_write_1h=None,
    reasoning=None,
) -> float | None:
    """USD for one request. None when a billed class has no rate."""
    row = _lookup(model, catalog() if table is None else table)
    if row is None or row.get("input") is None or row.get("output") is None:
        return None
    if any(value is None for value in (input_tokens, output_tokens, cache_read, cache_write)):
        return None
    prompt = _prompt_len(row, input_tokens, cache_read, cache_write)
    rates = _apply_tier(row, prompt)
    bill_input = int(input_tokens)
    if row.get("input_includes_cache_read"):
        if int(cache_read) > bill_input:
            return None
        bill_input -= int(cache_read)
    parts = [
        _class(bill_input, rates.get("input")),
        _class(output_tokens, rates.get("output")),
        _class(cache_read, rates.get("cache_read")),
        _write_charge(rates, int(cache_write), cache_write_5m, cache_write_1h),
    ]
    if rates.get("reasoning") is not None:
        if reasoning is None:
            return None
        parts.append(_class(reasoning, rates.get("reasoning")))
    total = Decimal(0)
    for part in parts:
        if part is None:
            return None
        total += part
    return float(total)


def health_block(stats: list[dict]) -> dict:
    rows = {row["model"]: row for row in catalog()}
    seen = set()
    models = []
    for stat in stats:
        model = stat.get("model")
        seen.add(model)
        models.append(_health_row(model, rows.get(model), stat))
    for model, row in rows.items():
        if model not in seen:
            models.append(_health_row(model, row, None))
    models.sort(key=lambda item: (-item["rows"], item["model"] or ""))
    return {"models": models}


def reprice(store, projects_root: str | None) -> dict:
    """Recompute est_cost_usd. Reread Claude usage only where the 5m/1h split is empty."""
    events = store.usage_events_for_cost()
    before = _tally((event["model"], event["est_cost_usd"]) for event in events)
    needed: dict[str, set] = {}
    for event in events:
        if event["tool"] != "claude":
            continue
        if event["cache_write_5m"] is not None or event["cache_write_1h"] is not None:
            continue
        if not event["cache_write"]:
            continue
        needed.setdefault(event["session_id"], set()).add(event["message_id"])
    found = {}
    if needed and projects_root:
        from dash.sources.claude import read_cache_splits

        found = read_cache_splits(projects_root, needed)
    updates = []
    after_pairs = []
    still_missing = 0
    for event in events:
        five = event["cache_write_5m"]
        one = event["cache_write_1h"]
        key = (event["session_id"], event["message_id"])
        if key in found:
            five, one = found[key]
        if (
            event["tool"] == "claude"
            and event["cache_write"]
            and five is None
            and one is None
        ):
            still_missing += 1
        est = cost(
            event["model"],
            event["input_tokens"],
            event["output_tokens"],
            event["cache_read"],
            event["cache_write"],
            cache_write_5m=five,
            cache_write_1h=one,
            reasoning=event["reasoning"],
        )
        updates.append(
            (est, five, one, event["tool"], event["session_id"], event["message_id"])
        )
        after_pairs.append((event["model"], est))
    store.write_costs(updates)
    return {
        "models": _compare(before, _tally(after_pairs)),
        "splits_needed": sum(len(ids) for ids in needed.values()),
        "splits_filled": len(found),
        "splits_still_missing": still_missing,
    }


def _lookup(model, table):
    if not model:
        return None
    if isinstance(table, dict):
        return table.get(model)
    for row in table:
        if row.get("model") == model:
            return row
    return None


def _prompt_len(row, input_tokens, cache_read, cache_write) -> int:
    tier = row.get("long_context") or {}
    counts = tier.get("counts") or ("input", "cache_read", "cache_write")
    includes = bool(row.get("input_includes_cache_read"))
    values = {
        "input": int(input_tokens),
        "cache_read": int(cache_read),
        "cache_write": int(cache_write),
    }
    total = 0
    for name in counts:
        if name == "cache_read" and includes and "input" in counts:
            continue
        total += values.get(name, 0)
    return total


def _apply_tier(row, prompt: int) -> dict:
    tier = row.get("long_context")
    if not isinstance(tier, dict):
        return row
    threshold = tier.get("threshold")
    if threshold is None:
        return row
    over = prompt > int(threshold) if tier.get("comparison", "gt") == "gt" else prompt >= int(threshold)
    if tier.get("comparison") not in (None, "gt", "gte"):
        return row
    if not over:
        return row
    merged = dict(row)
    for key in PRICE_KEYS:
        if tier.get(key) is not None:
            merged[key] = tier[key]
    return merged


def _class(tokens, rate) -> Decimal | None:
    number = int(tokens)
    if number == 0:
        return Decimal(0)
    if rate is None:
        return None
    return Decimal(number) * _dec(rate) / _MILLION


def _write_charge(rates, cache_write: int, five, one) -> Decimal | None:
    rate_5m = rates.get("cache_write_5m")
    rate_1h = rates.get("cache_write_1h")
    if five is None and one is None:
        if cache_write == 0:
            return Decimal(0)
        if rate_5m is not None and rate_1h is not None and _dec(rate_5m) == _dec(rate_1h):
            return _class(cache_write, rate_5m)
        if rate_5m is not None and rate_1h is None:
            return _class(cache_write, rate_5m)
        if rate_1h is not None and rate_5m is None:
            return _class(cache_write, rate_1h)
        return None
    five_n = 0 if five is None else int(five)
    one_n = 0 if one is None else int(one)
    if five_n + one_n != cache_write:
        return None
    left = _class(five_n, rate_5m)
    right = _class(one_n, rate_1h)
    if left is None or right is None:
        return None
    return left + right


def _dec(rate) -> Decimal:
    if isinstance(rate, Decimal):
        return rate
    return Decimal(str(rate))


def _health_row(model, row, stat) -> dict:
    info = status(model, [row] if row else [])
    tokens = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "reasoning": 0,
    }
    count = 0
    if stat:
        count = int(stat.get("rows") or 0)
        for key in tokens:
            tokens[key] = int(stat.get(key) or 0)
    return {
        "model": model,
        "provider": info["provider"],
        "rows": count,
        "tokens": tokens,
        "priced": info["priced"],
        "confidence": info["confidence"],
        "billing_mode": info["billing_mode"],
    }


def _tally(pairs) -> dict:
    acc: dict = {}
    for model, amount in pairs:
        slot = acc.setdefault(model, {"rows": 0, "priced_rows": 0, "usd": Decimal(0)})
        slot["rows"] += 1
        if amount is None:
            continue
        slot["priced_rows"] += 1
        slot["usd"] += _dec(amount)
    return acc


def _money(amount: Decimal):
    return float(amount.quantize(Decimal("0.000001")))


def _compare(before: dict, after: dict) -> list:
    models = []
    for model in sorted(set(before) | set(after), key=lambda item: item or ""):
        old = before.get(model) or {"rows": 0, "priced_rows": 0, "usd": Decimal(0)}
        new = after.get(model) or {"rows": 0, "priced_rows": 0, "usd": Decimal(0)}
        models.append(
            {
                "model": model,
                "rows": new["rows"],
                "before_usd": _money(old["usd"]) if old["priced_rows"] else None,
                "after_usd": _money(new["usd"]) if new["priced_rows"] else None,
                "priced_rows_before": old["priced_rows"],
                "priced_rows_after": new["priced_rows"],
            }
        )
    return models
