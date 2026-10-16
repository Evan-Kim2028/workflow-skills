"""Token price table. Unpriced families cost None. Reasoning is not priced."""

from __future__ import annotations

import json
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "prices.json"
FAMILIES = ("opus", "sonnet", "haiku")
BILLED = ("input", "output", "cache_read", "cache_write")

_lock = threading.Lock()
_cache: dict = {}


def family(model: str | None, table: dict | None = None) -> str | None:
    if not model:
        return None
    text = model.lower()
    names = tuple((table or _table()).keys()) or FAMILIES
    for name in names:
        if name in text:
            return name
    return None


def cost(
    model: str | None,
    input_tokens,
    output_tokens,
    cache_read,
    cache_write,
    table: dict | None = None,
) -> float | None:
    """USD for one event. None when the family is unpriced or a billed field is missing."""
    prices = table if table is not None else _table()
    fam = family(model, prices)
    if fam is None or fam not in prices:
        return None
    values = (input_tokens, output_tokens, cache_read, cache_write)
    if any(v is None for v in values):
        return None
    rates = prices[fam]
    total = 0.0
    for value, key in zip(values, BILLED):
        total += float(value) * float(rates[key])
    return total / 1_000_000


def load(path: Path | None = None) -> dict:
    path = Path(path) if path is not None else DEFAULT_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for name, rates in data.items():
        out[str(name).lower()] = {key: float(rates[key]) for key in BILLED}
    return out


def series_key(model: str | None) -> str:
    fam = family(model)
    if fam:
        return fam
    text = (model or "").lower()
    for name in ("grok", "muse", "glm"):
        if name in text:
            return name
    return "other"


def _table(path: Path | None = None) -> dict:
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
            table = load(path)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            table = hit[1] if hit and hit[1] is not None else {}
        _cache[key] = (mtime, table)
        return table
