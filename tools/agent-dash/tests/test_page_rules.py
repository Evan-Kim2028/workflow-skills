"""Static rules for the dashboard pages: every chart has axes, every number is compact."""
import os
import subprocess

os.environ["AGENT_DASH_IGNORE_LOCAL"] = "1"
os.environ.setdefault("AGENT_DASH_WORKSTREAMS", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "bundles"))
import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
FORMATTERS = ("function fmtN", "function fmtUsd", "function fmtDur", "function fmtDate")


def pages():
    """Yield (path, script text) for every page script under static/."""
    for p in sorted(STATIC.glob("*.html")) + sorted(STATIC.glob("*.js")):
        text = p.read_text()
        if p.suffix == ".html":
            text = "\n".join(re.findall(r"<script>(.*?)</script>", text, re.S))
        if text.strip():
            yield p, text


def functions(src):
    """Split a script into top-level-ish function bodies by 'function name(' headers."""
    heads = [m for m in re.finditer(r"\n  function (\w+)\s*\(", src)]
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(src)
        yield m.group(1), src[m.start():end]


def inline_scripts():
    """The executable script of static/index.html (external src files excluded)."""
    text = (STATIC / "index.html").read_text()
    return "\n".join(re.findall(r"<script>(.*?)</script>", text, re.S))


class PageRules(unittest.TestCase):
    def test_every_chart_has_axes(self):
        for path, src in pages():
            for name, body in functions(src):
                if 'role: "img"' in body and "aria-label" in body and "data-diagram" not in body and name != "axes":
                    is_chart = "viewBox" in body and ("rect" in body or "path" in body)
                    if is_chart:
                        self.assertIn("axes(", body, f"{path.name}:{name} draws a chart without axes()")

    def test_numbers_are_compact(self):
        for path, src in pages():
            for name, body in functions(src):
                if any(body.lstrip("\n ").startswith(f) for f in FORMATTERS):
                    continue
                for bad in (".toFixed(", "toLocaleString()"):
                    self.assertNotIn(bad, body, f"{path.name}:{name} formats a number with {bad}; use fmtN/fmtUsd/fmtDur")

    def test_progress_comes_from_the_server(self):
        """Plan % done is computed once on the server. The pages read
        progress.pct/.done/.total; they never compute it from task counts."""
        script = inline_scripts()
        plan_js = (STATIC / "plan.js").read_text()
        for name, src in (("index.html", script), ("plan.js", plan_js)):
            hits = re.findall(r"Math\.round\s*\(\s*100\s*\*[^;]*?done", src, re.I)
            self.assertEqual(hits, [], f"{name} computes % done itself: {hits[:2]}")
            self.assertIn(".progress", src, f"{name} never reads the server progress object")

    def test_unknown_duration_is_a_dash(self):
        script = (
            "const fs=require('fs');"
            "const src=fs.readFileSync(process.argv[1],'utf8').replace('})(window);','})(global);');"
            "eval(src);"
            "const dash='\\u2013';"
            "if(AD.fmtDur(null)!==dash) process.exit(2);"
            "if(AD.fmtDur(undefined)!==dash) process.exit(3);"
            "if(AD.fmtDur(0)!=='0s') process.exit(4);"
            "if(AD.fmtDur(90)!=='1m 30s') process.exit(5);"
        )
        proc = subprocess.run(
            ["node", "-e", script, str(STATIC / "format.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)


if __name__ == "__main__":
    unittest.main()
