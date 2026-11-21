"""The one verdict parser for agentq reports.

A report opens with its verdict line. The line may carry markdown emphasis, a
``Verdict:`` label and trailing prose, so all of these read the same word::

    PASS
    **PASS** (no BLOCKED items)
    Verdict: PARTIAL
    **Verdict: REQUEST_CHANGES.** The direction is sound ...
    **VERDICT: APPROVE_WITH_CHANGES**

The vocabulary is two families. A work job (read, write) ends ``PASS``,
``PARTIAL`` or ``BLOCKED``. A review job ends ``APPROVE``,
``APPROVE_WITH_CHANGES`` (``APPROVE_WITH_NITS`` is the same word, accepted
because workers write it), ``REQUEST_CHANGES`` or ``REJECT``. Matching is
case-insensitive and the word is stored verbatim, uppercased.

The first verdict-looking line wins: the contract puts the verdict first, and a
later bullet that opens with ``BLOCKED`` quotes a sub-check, not the run. A
report with no such line has no verdict (``None`` here, ``none`` in the job
row), and the broker says so in the job detail.

``bucket`` folds a stored verdict into the three totals the dashboard shows:
``APPROVE*`` counts as ``PASS``, ``REQUEST_CHANGES`` and ``REJECT`` as
``PARTIAL``. ``BLOCKED``, ``DEAD`` (a failed job) and ``none`` stay out of those
totals, as they did before; ``has_verdict`` still counts ``BLOCKED`` as a real
verdict.
"""

from __future__ import annotations

import re

#: Longest first: the alternation must try ``APPROVE_WITH_CHANGES`` before
#: ``APPROVE``, or the shorter word would win the match.
VERDICT_WORDS = (
    "APPROVE_WITH_CHANGES",
    "APPROVE_WITH_NITS",
    "REQUEST_CHANGES",
    "APPROVE",
    "REJECT",
    "PASS",
    "PARTIAL",
    "BLOCKED",
)
WORK_VERDICTS = frozenset({"PASS", "PARTIAL", "BLOCKED"})
REVIEW_VERDICTS = frozenset({"APPROVE", "APPROVE_WITH_CHANGES", "APPROVE_WITH_NITS", "REQUEST_CHANGES", "REJECT"})
#: The dashboard's three finished buckets, and the verdicts that count in each.
VERDICT_BUCKETS = {
    "PASS": "PASS",
    "APPROVE": "PASS",
    "APPROVE_WITH_CHANGES": "PASS",
    "APPROVE_WITH_NITS": "PASS",
    "PARTIAL": "PARTIAL",
    "REQUEST_CHANGES": "PARTIAL",
    "REJECT": "PARTIAL",
    "FAIL": "FAIL",
}
#: A line that opens with a verdict word, after any non-word prefix (``**``,
#: ``- ``, ``## ``) and an optional ``verdict`` label.
VERDICT_LINE = re.compile(
    r"^\W*(?:verdict\W*)?(" + "|".join(VERDICT_WORDS) + r")\b",
    re.IGNORECASE,
)


def verdict_of(text: str) -> str | None:
    """The first verdict word in ``text``, uppercased; ``None`` when the report has no verdict line."""
    for line in (text or "").splitlines():
        match = VERDICT_LINE.match(line.strip())
        if match:
            return match.group(1).upper()
    return None


def is_review(verdict: str | None) -> bool:
    """True for a review verdict word (``APPROVE``, ``REQUEST_CHANGES``, ...)."""
    return (verdict or "").strip().upper() in REVIEW_VERDICTS


def has_verdict(verdict: str | None) -> bool:
    """True when the stored value is a real verdict word, not ``None``, ``none`` or ``DEAD``."""
    return (verdict or "").strip().upper() in VERDICT_WORDS


def bucket(verdict: str | None) -> str | None:
    """``PASS``, ``PARTIAL`` or ``FAIL`` for a stored verdict; ``None`` when it is not a verdict."""
    return VERDICT_BUCKETS.get((verdict or "").strip().upper())
