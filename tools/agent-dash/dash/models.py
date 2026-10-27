"""Model families and model keys.

The same rules live in ``static/models.js``. A run maps to a family (claude,
cursor, muse, glm, deepseek, beam, codex, commandcode, dagster) and a model
key such as ``grok-4.7`` or ``composer-2.5``. DeepSeek ids map to
``deepseek-flash`` (including the legacy ``deepseek-v4-flash`` and
``deepseek-v4-flash-vision-exp`` aliases) or ``deepseek-v4-pro``. Any model
id containing ``beam`` (case-insensitive) maps to the ``beam`` family under
``beam-<ver>`` when the id carries a version (``reflection/Beam-1.0`` reads
``beam-1.0``) else ``beam``, whichever harness ran it. Codex ids (``gpt-*``,
``codex-*``, ``gpt-reserve``) map to the ``codex`` family under their
lowercased id. ``harness_of`` reads the runner (``pi``, ``commandcode``,
``grok-cli`` for a ``glm`` source, ...) from the source (and the tier when
the source is generic); run views carry it as ``harness`` beside the model,
the way they carry ``outcome_class``. A run whose model id was never
recorded gets
``<family>-unknown``. A Grok CLI run whose model id is recorded but matches
no known family (``mimo-*``, ``step-*``, or any other grok-hosted id) maps
to ``other``: only an empty id falls back to ``glm-unknown``. A Dagster job
keeps the job name after ``dagster:``. A Command Code run (tier or source
``commandcode``, or one of the free ids) maps to the ``commandcode`` family
under its id, and an empty id reads as ``commandcode-unknown`` (a Beam id
under Command Code still reads as ``beam``). Display strings live apart
from keys: ``model_name``/``full_name`` mirror ``nameOf``/``fullName`` in
``static/models.js`` exactly, so charts, counts and pricing (which match
on keys) never shift when a name is normalised.
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
    if "beam" in m or (not m and "beam" in h):
        if not m:
            return "beam", "beam-unknown"
        return "beam", "beam" + _ver(r"beam[-_ ]?(\d+(?:\.\d+)?)", m)
    if "commandcode" in h or m in COMMANDCODE_FREE:
        return "commandcode", m or "commandcode-unknown"
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
    for fam, words in (("cursor", ("grok", "cursor", "composer")), ("muse", ("muse",)), ("glm", ("glm",)), ("claude", ("claude",))):
        if any(word in h for word in words):
            if fam == "glm" and m:
                return "other", "other"
            return fam, f"{fam}-unknown"
    return "other", "other"


def run_model_key(run: dict) -> str:
    """Model key for a run row."""
    hint = " ".join(str(run.get(name) or "") for name in ("tier", "source"))
    return model_key(run.get("model"), hint)[1]


#: Display words for model-id tokens. ``model_name`` extends these with a
#: generic capitalize fallback instead of special-casing each id, the way
#: ``static/models.js`` extends its ``WORD`` map.
_WORD = {
    "grok": "Grok",
    "composer": "Composer",
    "glm": "GLM",
    "flash": "Flash",
    "beam": "Beam",
    "spark": "Spark",
    "wonder": "Wonder",
    "muse": "muse",
    "opus": "Opus",
    "fable": "Fable",
    "sonnet": "Sonnet",
    "haiku": "Haiku",
    "other": "Other",
    "deepseek": "DeepSeek",
    "pro": "Pro",
    "codex": "Codex",
    "gpt": "GPT",
    "sol": "Sol",
    "luna": "Luna",
    "astra": "Astra",
    "terra": "Terra",
    "reserve": "Reserve",
    "auto": "Auto",
    "review": "Review",
    "claude": "Claude",
    "minimax": "MiniMax",
    "ornith": "Ornith",
    "laguna": "Laguna",
    "glyph": "Glyph",
    "cluster": "Cluster",
    "s": "S",
}

_FAM_NAMES = {
    "claude": "Claude",
    "cursor": "Cursor",
    "muse": "muse",
    "glm": "GLM",
    "deepseek": "DeepSeek",
    "beam": "Beam",
    "codex": "Codex",
    "commandcode": "Command Code",
    "dagster": "Dagster",
    "other": "Other",
}


def _clean_id(key: str | None) -> str:
    """Strip provider prefixes, free/contributor tails, bracketed launcher
    options, parameter-count tails and build numbers from a model key or
    raw id, mirroring ``cleanId`` in ``static/models.js``."""
    s = str(key or "")
    s = re.sub(r"\[.*\]$", "", s)
    s = re.sub(r":[^/]*$", "", s)
    s = re.sub(r"-(free|contributor)$", "", s, flags=re.IGNORECASE)
    if "/" in s:
        s = s[s.rindex("/") + 1 :]
    s = re.sub(r"-\d+[bmkBKM](-.*)?$", "", s)
    s = re.sub(r"-\d{3,}$", "", s)
    s = re.sub(r"-(\d)-(\d)$", r"-\1.\2", s)
    return s


def _title_word(word: str) -> str:
    low = word.lower()
    if low in _WORD:
        return _WORD[low]
    if word[:1].isalpha():
        return word[0].upper() + word[1:]
    return word


def model_name(key: str | None) -> str:
    """Display name for a model key or a raw model id.

    Mirrors ``nameOf`` in ``static/models.js`` exactly: ``keyOf`` keys
    never change (grouping, hues and pricing still match on them); only
    the rendered string is normalised here.
    """
    s = str(key or "")
    if s.startswith("dagster:"):
        return s[len("dagster:") :] or "job"
    if s.endswith("-unknown"):
        return "model not recorded"
    if s == "deepseek-flash":
        return "DeepSeek V4.1 Flash"
    if s == "deepseek-v4-pro":
        return "DeepSeek V4 Pro"
    parts = [w for w in _clean_id(s).split("-") if w]
    words = [_title_word(w) for w in parts]
    if len(parts) > 1 and parts[0].lower() == "gpt" and parts[1][:1].isdigit():
        rest = " " + " ".join(words[2:]) if len(words) > 2 else ""
        return "GPT-" + words[1] + rest
    return " ".join(words)


def full_name(key: str | None) -> str:
    """Family-aware display name, mirroring ``fullName`` in
    ``static/models.js``: the bare name when it already opens with the
    family word, else family plus name."""
    s = str(key or "")
    name = model_name(s)
    fam = _FAM_NAMES.get(model_key(s)[0], "Other")
    if name == fam or name.startswith(fam + " "):
        return name
    return fam + " · " + name


#: Harness display names. ``harness_of`` reads the runner from a run row;
#: the page mirrors it as ``ADM.harnessOf``.
HARNESS_NAMES = {
    "claude": "Claude Code",
    "muse": "muse",
    "cursor": "Cursor",
    "grok": "Grok Build",
    "grok-cli": "Grok Build",
    "pi": "pi",
    "commandcode": "Command Code",
    "codex": "Codex",
    "dagster": "dagster",
    "generic": "generic",
}


#: Harness keys this module answers for; anything else is a raw source.
_HARNESS_KEYS = frozenset(
    ("claude", "muse", "cursor", "composer", "grok", "grok-cli", "pi", "commandcode", "dagster", "generic")
) | frozenset(HARNESS_NAMES)


def harness_of(run: dict) -> str:
    """Harness that ran a run, from its source (and its tier when generic).

    A recorded ``harness`` on the row wins: the launcher knows which CLI it
    started, and Grok Build records ``grok`` so no GLM-source run can read
    as anything but Grok Build (or as a raw source string).

    ``claude-*`` reads ``claude``, ``muse`` reads ``muse``, ``grok`` and
    ``cursor`` read ``cursor``, a ``glm`` source reads ``grok-cli``, ``pi``
    reads ``pi``, ``commandcode`` reads ``commandcode``, ``codex*`` reads
    ``codex``, ``dagster`` reads ``dagster`` and ``generic`` reads
    ``generic``; anything else reads back the source string. A generic
    run's tier is the closest harness bucket the reporter chose, so the
    same mapping reads it (a ``beam`` tier still means the generic
    harness running a Beam model).
    """
    recorded = str(run.get("harness") or "").strip().lower()
    if recorded in _HARNESS_KEYS:
        return "grok-cli" if recorded == "grok" else recorded
    source = str(run.get("source") or "")
    tier = str(run.get("tier") or "")
    key = (tier if source == "generic" else source).lower()
    if key.startswith("claude") or key in ("opus", "sonnet", "haiku", "fable"):
        return "claude"
    if key == "muse":
        return "muse"
    if key in ("grok", "cursor", "composer"):
        return "cursor"
    if key == "grok-cli":
        return "grok-cli"
    if key == "glm":
        return "grok-cli"
    if key == "pi":
        return "pi"
    if key == "commandcode":
        return "commandcode"
    if key.startswith("codex"):
        return "codex"
    if key == "dagster":
        return "dagster"
    if key == "generic":
        return "generic"
    return source or "generic"
