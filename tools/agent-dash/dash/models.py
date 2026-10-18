"""Model families and model keys.

The same rules live in ``static/models.js``. A run maps to a family (claude,
cursor, muse, glm, deepseek, dagster) and a model key such as ``grok-4.7`` or
``composer-2.5``. DeepSeek ids map to ``deepseek-flash`` (including the legacy
``deepseek-v4-flash`` and ``deepseek-v4-flash-vision-exp`` aliases) or
``deepseek-v4-pro``. A run whose model id was never recorded gets
``<family>-unknown``. A Dagster job keeps the job name after ``dagster:``.
"""

from __future__ import annotations

import re

_CLAUDE = re.compile(r"(opus|fable|sonnet|haiku)")


def _ver(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    return "-" + match.group(1) if match else ""


def model_key(model: str | None, hint: str | None = None) -> tuple[str, str]:
    """Return ``(family, model_key)`` for a model id, using ``hint`` (tier or source) when the id is empty."""
    m = (model or "").lower()
    h = (hint or "").lower()
    if "dagster" in h or m.startswith("dagster:"):
        job = m[len("dagster:") :] if m.startswith("dagster:") else m
        return "dagster", "dagster:" + (job or "job")
    if "composer" in m:
        return "cursor", "composer" + _ver(r"composer[-_ ]?(\d+(?:\.\d+)?)", m)
    if "grok" in m:
        return "cursor", "grok" + _ver(r"grok[-_ ]?(\d+(?:\.\d+)?)", m)
    if "glm" in m:
        return "glm", "glm" + _ver(r"glm[-_ ]?(\d+(?:\.\d+)?)", m) + ("-flash" if "flash" in m else "")
    if "deepseek" in m:
        if "pro" in m:
            return "deepseek", "deepseek-v4-pro"
        return "deepseek", "deepseek-flash"
    if "muse" in m:
        match = re.search(r"muse-(spark|wonder)(?:-full)?-(\d+(?:\.\d+)?)", m)
        return "muse", f"muse-{match.group(1)}-{match.group(2)}" if match else "muse-unknown"
    match = _CLAUDE.search(m) or _CLAUDE.search(h)
    if match:
        return "claude", match.group(1)
    for fam, words in (("cursor", ("grok", "cursor", "composer")), ("muse", ("muse",)), ("glm", ("glm",)), ("claude", ("claude",))):
        if any(word in h for word in words):
            return fam, f"{fam}-unknown"
    return "other", "other"


def run_model_key(run: dict) -> str:
    """Model key for a run row."""
    hint = " ".join(str(run.get(name) or "") for name in ("tier", "source"))
    return model_key(run.get("model"), hint)[1]
