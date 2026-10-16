"""Render a plan bundle's page.html from plan.json, visuals.html, and layout.json.

Section order is fixed: Approvals, Why, End state, Plan, Progress, Decided, Agent-friendly, Reference.
The Progress section is a placeholder. The plan page fills it from the live plan.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path


class RenderError(Exception):
    pass


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def sections(path: Path) -> dict:
    """Map an h2 title to the markup inside that section. Keep the header."""
    text = path.read_text(encoding="utf-8")
    out = {}
    for match in re.finditer(r'<section class="pl-sec lay"><h2>(.*?)</h2>(.*?)</section>', text, re.S):
        out[match.group(1)] = match.group(2)
    head = re.search(r"<header.*?</header>", text, re.S)
    out["__header"] = head.group(0) if head else ""
    return out


def sec(anchor: str, eyebrow: str, title: str, body: str, lead: str | None = None) -> str:
    lead_html = f'<p class="lead2">{lead}</p>' if lead else ""
    return (
        f'<section id="{anchor}" class="pl-sec lay"><h2><span class="eb">{esc(eyebrow)}</span>{esc(title)}</h2>'
        f"{lead_html}{body}</section>\n"
    )


def more(summary: str, hint: str, body: str) -> str:
    if not body:
        return ""
    return (
        f'<details class="more"><summary>{esc(summary)} <span class="hint">{esc(hint)}</span></summary>'
        f'<div class="db">{body}</div></details>\n'
    )


_VERDICT = {
    "WORTH_IT": ("p-target", "Worth it"),
    "WORTH_IT_IF": ("s-extend", "Worth it, with conditions"),
    "NOT_WORTH_IT": ("p-del", "Not worth it"),
}


def why_block(mot: dict) -> str:
    mot = mot or {}
    css, label = _VERDICT.get(mot.get("verdict", ""), ("p-pass", mot.get("verdict", "")))

    def cards(items, key_a, key_b, tone):
        return "".join(
            f'<div class="why-c {tone}"><p>{esc(item.get(key_a))}</p><small>{esc(item.get(key_b))}</small></div>'
            for item in items or []
            if isinstance(item, dict)
        )

    not_better = "".join(f"<li>{esc(item)}</li>" for item in mot.get("not_better") or [])
    conditions = "".join(f"<li>{esc(item)}</li>" for item in mot.get("conditions") or [])
    conditions_html = (
        f'<div class="box" style="margin-top:12px"><h3>Conditions</h3><ul>{conditions}</ul></div>' if conditions else ""
    )
    grid = (
        f'<div class="why-grid"><div><h3 class="sh">Today</h3>{cards(mot.get("today"), "pain", "measure", "t")}</div>'
        f'<div><h3 class="sh">After</h3>{cards(mot.get("after"), "gain", "measure", "a")}</div>'
        f'<div><h3 class="sh">What it costs</h3>{cards(mot.get("costs"), "cost", "measure", "c")}</div></div>'
    )
    return (
        f'<div class="why-v"><span class="pill {css} pillw">{esc(label)}</span><span>{esc(mot.get("verdict_reason"))}</span></div>'
        f'<div class="box" style="margin-top:12px;border-style:dashed"><h3>Where it is not better</h3><ul>{not_better}</ul></div>'
        f"{conditions_html}"
        + more("Pains, gains and costs, in detail", "every claim with its number", grid)
    )


def phases_block(phases) -> str:
    steps = "".join(
        f'<div class="fstep"><div class="fn">{index + 1}</div><h3>{esc(phase.get("name"))}</h3>'
        f'<p>{esc(phase.get("goal"))}</p><p class="ph-gate"><b>Gate</b> {esc(phase.get("gate"))}</p></div>'
        for index, phase in enumerate(phases or [])
        if isinstance(phase, dict)
    )
    return f'<div class="p1-flow">{steps}</div>'


def progress_block(ws: str) -> str:
    return f'<div id="plan-progress" class="plan-progress" data-plan-progress data-ws="{esc(ws)}"></div>'


def decisions_block(decisions, review: str) -> str:
    rows = []
    for item in decisions or []:
        if not isinstance(item, dict):
            continue
        alternatives = "".join(
            f"<li><b>{esc(alt.get('option'))}</b>: {esc(alt.get('why_not'))}</li>"
            for alt in item.get("alternatives") or []
            if isinstance(alt, dict)
        )
        evidence = "; ".join(esc(entry) for entry in (item.get("evidence") or [])[:4])
        reversible = "reversible" if item.get("reversible") else "one-way"
        rows.append(
            f'<div class="dec-c"><div class="dec-h"><span class="id">{esc(item.get("id"))}</span>'
            f'<span class="pill s-exists">decided</span><span class="pill p-pass">{reversible}</span></div>'
            f'<p class="dec-q">{esc(item.get("question"))}</p><p class="dec-d">{esc(item.get("decision"))}</p>'
            f'<p class="why">{esc(item.get("why"))}</p>'
            f"<details><summary>Alternatives and evidence</summary><ul>{alternatives}</ul>"
            f'<p class="cap">Evidence: {evidence}</p><p class="cap">Risk: {esc(item.get("risk"))}</p></details></div>'
        )
    return f'<p class="sub">{esc(review)}</p><div class="dec-grid">{"".join(rows)}</div>'


def agent_block(agent: dict) -> str:
    agent = agent or {}
    requirements = "".join(
        f'<div class="tcard"><div class="tname">{esc(item.get("n"))} · {esc(item.get("title"))}</div>'
        f'<div class="tgrain" style="margin-bottom:4px">{esc(item.get("mechanism"))}</div>'
        f'<div class="cap" style="margin:0">Check: <span class="mono">{esc(item.get("verify"))}</span></div></div>'
        for item in agent.get("requirements") or []
        if isinstance(item, dict)
    )
    return (
        f'<div class="box" style="border-color:var(--accent)"><h3>Litmus: the agent-friendly exit criterion</h3>'
        f'<p class="dtext" style="font-size:15px;margin:8px 0"><b>{esc(agent.get("litmus"))}</b></p>'
        f'<p class="why">Proved by: {esc(agent.get("litmus_check"))}</p></div>'
        f'<div class="tgrid">{requirements}</div>'
    )


def _piece(value, fragments: dict) -> str:
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str) and item.startswith("<"):
                parts.append(item)
            else:
                parts.append(fragments.get(item, ""))
        return "".join(parts)
    return "" if value is None else str(value)


def build(ws: str, data: dict, fragments: dict, layout: dict) -> str:
    narrative = data.get("narrative") if isinstance(data.get("narrative"), dict) else {}
    anchors = [
        ("approvals", "Approvals"),
        ("why", "Why"),
        ("end", "End state"),
        ("plan", "Plan"),
        ("progress", "Progress"),
        ("decided", "Decided"),
        ("agent", "Agent-friendly"),
        ("ref", "Reference"),
    ]
    jump = '<nav class="jump" aria-label="On this page">' + "".join(
        f'<a href="#{ws.lower()}-{anchor}">{label}</a>' for anchor, label in anchors
    ) + "</nav>"
    header = fragments.get("__header") or "<header><p class=\"thesis\"></p></header>"
    thesis = esc(narrative.get("thesis"))
    if re.search(r'<p class="thesis">.*?</p>', header, re.S):
        header = re.sub(r'<p class="thesis">.*?</p>', f'<p class="thesis">{thesis}</p>', header, count=1, flags=re.S)
    else:
        header = f'<header><p class="thesis">{thesis}</p></header>'
    header = re.sub(r'<div class="figs">.*</header>', "</header>", header, count=1, flags=re.S)
    prefix = ws.lower()
    ref_body = "".join(
        more(title, hint, fragments.get(title, ""))
        for title, hint in (layout.get("ref_sections") or [])
        if isinstance(title, str)
    )
    body = header
    body += sec(
        f"{prefix}-approvals",
        "Approvals",
        "Owner decisions",
        f'<div data-plan-approvals data-ws="{esc(ws)}"></div>',
    )
    body += jump
    body += sec(
        f"{prefix}-why",
        "Why",
        layout.get("why_title") or "Why",
        _piece(layout.get("why_visual"), fragments) + why_block(data.get("motivation") or {}),
    )
    body += sec(
        f"{prefix}-end",
        "End state",
        layout.get("end_title") or "End state",
        _piece(layout.get("end_body"), fragments),
        lead=esc(narrative.get("end_state")),
    )
    body += sec(
        f"{prefix}-plan",
        "Plan",
        layout.get("plan_title") or "Plan",
        phases_block(narrative.get("phases")) + _piece(layout.get("plan_extra"), fragments),
    )
    body += sec(
        f"{prefix}-progress",
        "Progress",
        f"Where {ws} is",
        progress_block(ws),
        lead="The map reads the live plan. Green is done, magenta is waiting on a prod step, amber is an open PR, dashed is not started, and dotted is deferred.",
    )
    body += sec(
        f"{prefix}-decided",
        "Decided",
        "Decisions, made by review",
        decisions_block(data.get("decisions") or [], layout.get("review_note") or ""),
    )
    body += sec(
        f"{prefix}-agent",
        "Agent-friendly",
        layout.get("agent_title") or "Agent-friendly",
        _piece(layout.get("agent_visual"), fragments) + agent_block(data.get("agent_friendly") or {}),
    )
    body += sec(f"{prefix}-ref", "Reference", "Details", ref_body)
    return f'<div id="panel-{prefix}" role="tabpanel">\n{body}</div>\n'


def render(bundle_dir: Path) -> str:
    """Write page.html for one bundle directory and return the HTML."""
    bundle_dir = Path(bundle_dir)
    plan_path = bundle_dir / "plan.json"
    layout_path = bundle_dir / "layout.json"
    if not plan_path.is_file():
        raise RenderError(f"missing {plan_path.name}")
    if not layout_path.is_file():
        raise RenderError(f"missing {layout_path.name}")
    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderError("plan.json or layout.json is not readable JSON") from exc
    if not isinstance(data, dict) or not isinstance(layout, dict):
        raise RenderError("plan.json and layout.json must be JSON objects")
    visuals = bundle_dir / "visuals.html"
    fragments = sections(visuals) if visuals.is_file() else {"__header": ""}
    bundle_path = bundle_dir / "bundle.json"
    ws = bundle_dir.name
    if bundle_path.is_file():
        try:
            meta = json.loads(bundle_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        if isinstance(meta, dict) and isinstance(meta.get("id"), str) and meta["id"]:
            ws = meta["id"]
    page = build(ws, data, fragments, layout)
    target = bundle_dir / "page.html"
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(page, encoding="utf-8")
    tmp.replace(target)
    return page
