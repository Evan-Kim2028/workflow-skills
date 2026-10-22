"""Model families and model keys.

The same rules live in ``static/models.js``. A run maps to a family (claude,
cursor, muse, glm, grok, deepseek, codex, dagster) and a model key such as
``grok-4.7`` or ``composer-2.5``. DeepSeek ids map to ``deepseek-flash``
(including the legacy ``deepseek-v4-flash`` and
``deepseek-v4-flash-vision-exp`` aliases) or ``deepseek-v4-pro``. Codex ids
(``gpt-*``, ``codex-*``, ``gpt-reserve``) map to the ``codex`` family under
their lowercased id. A run whose model id was never recorded gets
``<family>-unknown``. A Grok Build run (the ``grok-build`` launcher kind:
tier ``grok`` with a ``glm`` source or usage tool, or one of the known
Grok Build ids below) maps to the ``grok`` family under its recorded id,
so every model it runs (``glm-5.3``, ``glm-5.3-flash``, ``deepseek-flash``,
``reflection-beam``, and any future id) keeps a per-model breakdown. A
non-Grok-Build run whose model id matches no known family (``mimo-*``,
``step-*``, or any other unknown id) maps to ``other``. A Dagster job
keeps the job name after ``dagster:``. A Command Code run (tier or source
``commandcode``, or one of the five free ids) maps to the ``commandcode``
family under its id, and an empty id reads as ``commandcode-unknown``.
``reflection-beam`` (Grok Build, free) is distinct from
``reflection/beam:free`` (Command Code, free): only the latter reads as
``commandcode``.
"""

from __future__ import annotations

import re

_CLAUDE = re.compile(r"(opus|fable|sonnet|haiku)")

#: Command Code free-tier ids (``cmd -m``). A run whose tier or source is
#: ``commandcode`` takes any id; these ids alone are enough outside that hint.
COMMANDCODE_FREE = frozenset(
    {
        "reflection/beam:free",
        "minimax/minimax-m3.1-flash:free",
        "ornith/ornith-1.5-122b:free",
        "poolside/laguna-s-2.1-free",
        "stealth/glyph-cluster:free",
    }
)

#: Grok Build model ids (``grok -m`` through the ``grok-build`` launcher
#: kind). These group under the grok provider even with no run hint; any
#: other grok-hosted id groups the same way once a run hint names the
#: Grok Build launcher (``glm`` in the tier/source hint: the manifest
#: source and usage tool stay ``glm`` while the run tier reads ``grok``).
#: ``reflection-beam`` here is the Grok Build free id, distinct from the
#: Command Code ``reflection/beam:free`` id above.
GROK_BUILD_MODELS = frozenset(
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
    if "commandcode" in h or m in COMMANDCODE_FREE:
        return "commandcode", m or "commandcode-unknown"
    if "glm" in h:
        # Grok Build launcher kind (manifest source and usage tool stay
        # ``glm`` while the run tier reads ``grok``): the recorded id is
        # the per-model breakdown, never collapsed to ``other``.
        return "grok", m or "grok-unknown"
    if m in GROK_BUILD_MODELS or m == "grok-unknown":
        return "grok", m
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
    if "codex" in h or m == "codex-unknown":
        return "codex", m or "codex-unknown"
    if m.startswith("gpt-") or m == "gpt-reserve" or m.startswith("codex-"):
        return "codex", m
    match = _CLAUDE.search(m) or _CLAUDE.search(h)
    if match:
        return "claude", match.group(1)
    for fam, words in (("cursor", ("grok", "cursor", "composer")), ("muse", ("muse",)), ("claude", ("claude",))):
        if any(word in h for word in words):
            return fam, f"{fam}-unknown"
    return "other", "other"


def run_model_key(run: dict) -> str:
    """Model key for a run row."""
    hint = " ".join(str(run.get(name) or "") for name in ("tier", "source"))
    return model_key(run.get("model"), hint)[1]
