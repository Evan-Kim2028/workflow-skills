"""Per-model price catalog. A missing rate never becomes zero."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
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

# Mainland China public holidays 2026 (UTC dates). Best-effort: the days
# below are the certain ones (New Year's Day, the Spring Festival week
# around Chinese New Year on Feb 17, the observed Qingming day, the Labour
# Day week, Dragon Boat Festival on Jun 19, Mid-Autumn Festival on Sep 25,
# and the National Day week). Adjusted working weekends are NOT listed.
# DeepSeek bills these whole days off-peak; any day not listed here follows
# the normal weekday rule (an unlisted day is treated as a normal weekday,
# i.e. peak hours apply).
CHINA_HOLIDAYS_2026 = frozenset(
    [
        "2026-01-01",
        "2026-02-16",
        "2026-02-17",
        "2026-02-18",
        "2026-02-19",
        "2026-02-20",
        "2026-02-21",
        "2026-02-22",
        "2026-04-06",
        "2026-05-01",
        "2026-05-02",
        "2026-05-03",
        "2026-05-04",
        "2026-05-05",
        "2026-06-19",
        "2026-09-25",
        "2026-10-01",
        "2026-10-02",
        "2026-10-03",
        "2026-10-04",
        "2026-10-05",
        "2026-10-06",
        "2026-10-07",
    ]
)

# DeepSeek's weekday peak windows in UTC (start inclusive, end exclusive):
# 01:00-04:00 and 06:00-10:00 Monday through Friday.
DEEPSEEK_PEAK_WEEKDAYS = (0, 1, 2, 3, 4)
DEEPSEEK_PEAK_WINDOWS = ((1, 4), (6, 10))


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


_COMMANDCODE_FREE = frozenset(
    {
        "reflection/beam:free",
        "minimax/minimax-m3.1-flash:free",
        "ornith/ornith-1.5-122b:free",
        "poolside/laguna-s-2.1-free",
        "stealth/glyph-cluster:free",
    }
)


#: Grok Build model ids (``grok -m`` through the ``grok-build`` launcher
#: kind). Mirrors ``dash.models.GROK_BUILD_MODELS``: these group under the
#: grok provider. ``reflection-beam`` here is the Grok Build free id,
#: distinct from the Command Code ``reflection/beam:free`` id.
_GROK_BUILD_MODELS = frozenset(
    {
        "glm-5.3",
        "glm-5.3-flash",
        "deepseek-flash",
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
        "deepseek-v4-pro",
        "reflection-beam",
    }
)


def series_key(model: str | None, hint: str | None = None) -> str:
    fam = family(model)
    if fam:
        return fam
    text = (model or "").lower()
    if text in _COMMANDCODE_FREE:
        return "commandcode"
    if text in _GROK_BUILD_MODELS:
        return "grok"
    if hint and "glm" in hint.lower():
        # Grok Build launcher kind (manifest source and usage tool stay
        # ``glm`` while the run tier reads ``grok``): any current or
        # future model it runs groups under the grok provider.
        return "grok"
    for name in ("grok", "muse", "glm", "deepseek", "beam", "codex"):
        if name in text:
            return name
    if text.startswith("gpt-") or text == "gpt-reserve":
        return "codex"
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


def band_for(model: str | None, ts, table=None) -> str | None:
    """``"peak"`` or ``"offpeak"`` for a time-banded row, else None.

    ``ts`` is epoch seconds (UTC). A missing ``ts`` falls back to the
    row's ``unknown_ts`` band (``"peak"`` unless the row says otherwise),
    so an undated record prices conservatively instead of silently
    picking the cheaper band.
    """
    row = _lookup(model, catalog() if table is None else table)
    if row is None:
        return None
    spec = row.get("time_bands")
    if not isinstance(spec, dict):
        return None
    if ts is None:
        fallback = spec.get("unknown_ts") or "peak"
        return fallback if fallback in ("peak", "offpeak") else "peak"
    try:
        moment = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return "peak"
    if moment.strftime("%Y-%m-%d") in CHINA_HOLIDAYS_2026:
        return "offpeak"
    if moment.weekday() not in DEEPSEEK_PEAK_WEEKDAYS:
        return "offpeak"
    hour = moment.hour + moment.minute / 60.0 + moment.second / 3600.0
    for start, end in DEEPSEEK_PEAK_WINDOWS:
        if start <= hour < end:
            return "peak"
    return "offpeak"


#: Resolved per-class Decimal rate vectors:
#: ``(id(table), model, band, tier_over) -> rates``. The merge (tier,
#: time band) and the ``Decimal`` conversion run once per key instead of
#: once per usage event; the arithmetic below consumes the same values
#: in the same order as the uncached path, so totals are unchanged.
_RATES_CACHE: dict = {}
_RATES_LIMIT = 4096

#: Time-band answers: ``(id(table), model, hour_bucket) -> band``. Band
#: edges sit on hour and day boundaries, so every timestamp in one UTC
#: hour bucket shares its band exactly.
_BAND_CACHE: dict = {}
_BAND_LIMIT = 8192


def _tier_over(row: dict, prompt: int) -> bool:
    """Whether ``prompt`` clears the row's long-context threshold.

    Mirrors :func:`_apply_tier` exactly: an unknown comparison never
    clears, ``None`` and ``"gte"`` compare with ``>=``, ``"gt"``
    (the default) with ``>``.
    """
    tier = row.get("long_context")
    if not isinstance(tier, dict):
        return False
    threshold = tier.get("threshold")
    if threshold is None:
        return False
    comparison = tier.get("comparison", "gt")
    if comparison not in (None, "gt", "gte"):
        return False
    if comparison == "gt":
        return prompt > int(threshold)
    return prompt >= int(threshold)


def _rate_vector(table, model: str, band: str | None, over: bool) -> dict:
    """Cached resolved rates for one price shape, Decimals included.

    Applies the tier merge then the time-band merge exactly as
    :func:`_apply_tier` plus :func:`_apply_time_band` do, and converts
    every rate with :func:`_dec` once. ``table`` stays alive through the
    entry like :data:`_index_memo`, so an id is never reused underneath.
    """
    key = (id(table), model, band, over)
    hit = _RATES_CACHE.get(key)
    if hit is not None and hit[0] is table:
        return hit[1]
    row = _lookup(model, table)
    merged = dict(row)
    if over:
        tier = row.get("long_context")
        if isinstance(tier, dict):
            for name in PRICE_KEYS:
                if tier.get(name) is not None:
                    merged[name] = tier[name]
    if band == "offpeak":
        spec = merged.get("time_bands") if isinstance(merged, dict) else None
        off = spec.get("offpeak") if isinstance(spec, dict) else None
        if isinstance(off, dict):
            merged = dict(merged)
            for name in ("input", "output", "cache_read"):
                if off.get(name) is not None:
                    merged[name] = off[name]
    vector = {name: (_dec(merged[name]) if merged.get(name) is not None else None) for name in PRICE_KEYS}
    with _lock:
        if len(_RATES_CACHE) >= _RATES_LIMIT:
            _RATES_CACHE.clear()
        _RATES_CACHE[key] = (table, vector)
    return vector


def _band_cached(table, model: str, bucket) -> str | None:
    """Band for one model and UTC hour bucket (or the ``None``-ts slot)."""
    key = (id(table), model, bucket)
    hit = _BAND_CACHE.get(key)
    if hit is not None and hit[0] is table:
        return hit[1]
    if bucket is None:
        row = _lookup(model, table)
        spec = row.get("time_bands") if isinstance(row, dict) else None
        fallback = spec.get("unknown_ts") if isinstance(spec, dict) else None
        band = fallback if fallback in ("peak", "offpeak") else "peak"
    else:
        ts = bucket * 3600
        try:
            moment = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            moment = None
        if moment is None:
            band = "peak"
        elif moment.strftime("%Y-%m-%d") in CHINA_HOLIDAYS_2026:
            band = "offpeak"
        elif moment.weekday() not in DEEPSEEK_PEAK_WEEKDAYS:
            band = "offpeak"
        else:
            band = "offpeak"
            hour = moment.hour + moment.minute / 60.0 + moment.second / 3600.0
            for start, end in DEEPSEEK_PEAK_WINDOWS:
                if start <= hour < end:
                    band = "peak"
                    break
    with _lock:
        if len(_BAND_CACHE) >= _BAND_LIMIT:
            _BAND_CACHE.clear()
        _BAND_CACHE[key] = (table, band)
    return band


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
    ts=None,
) -> float | None:
    """USD for one request. None when a billed class has no rate.

    ``ts`` is the usage record's own epoch timestamp (UTC). Rows with a
    ``time_bands`` block (DeepSeek) pick the peak or off-peak rate set
    from it; rows without one ignore ``ts`` entirely. Tier and band
    resolution plus the ``Decimal`` conversion are cached per price
    shape; the per-class arithmetic is unchanged.
    """
    resolved = catalog() if table is None else table
    row = _lookup(model, resolved)
    if row is None or row.get("input") is None or row.get("output") is None:
        return None
    if any(value is None for value in (input_tokens, output_tokens, cache_read, cache_write)):
        return None
    prompt = _prompt_len(row, input_tokens, cache_read, cache_write)
    over = _tier_over(row, prompt)
    if isinstance(row.get("time_bands"), dict):
        bucket = int(float(ts) // 3600) if ts is not None else None
        try:
            band: str | None = _band_cached(resolved, model, bucket)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError, OSError):
            band = "peak"
    else:
        band = None
    rates = _rate_vector(resolved, model, band, over)  # type: ignore[arg-type]
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
    table = catalog()
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
            table,
            cache_write_5m=five,
            cache_write_1h=one,
            reasoning=event["reasoning"],
            ts=event.get("ts"),
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


#: ``id(table) -> (table, {model: first row})`` for list catalogs. The
#: list is kept alive by the entry, so an id is never reused while cached;
#: a reloaded catalog is a new list object and gets its own index.
_index_memo: dict = {}
_INDEX_LIMIT = 16


def _lookup(model, table):
    if not model:
        return None
    if isinstance(table, dict):
        return table.get(model)
    memo = _index_memo.get(id(table))
    if memo is None or memo[0] is not table:
        index: dict = {}
        for row in table:
            index.setdefault(row.get("model"), row)
        with _lock:
            if len(_index_memo) >= _INDEX_LIMIT:
                _index_memo.clear()
            _index_memo[id(table)] = (table, index)
        memo = (table, index)
    try:
        return memo[1].get(model)
    except TypeError:
        return None


def _prompt_len(row, input_tokens, cache_read, cache_write) -> int:
    if not row.get("long_context") and not row.get("input_includes_cache_read"):
        return int(input_tokens) + int(cache_read) + int(cache_write)
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


def _apply_time_band(rates: dict, band: str | None) -> dict:
    """Swap in the off-peak rate set. Peak (or no band) keeps base rates.

    Copies before merging, so catalog rows are never mutated. Only the
    input/output/cache_read classes have banded rates; reasoning and
    cache-write rates ride through unchanged.
    """
    if band != "offpeak":
        return rates
    spec = rates.get("time_bands") if isinstance(rates, dict) else None
    off = spec.get("offpeak") if isinstance(spec, dict) else None
    if not isinstance(off, dict):
        return rates
    merged = dict(rates)
    for key in ("input", "output", "cache_read"):
        if off.get(key) is not None:
            merged[key] = off[key]
    return merged


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
