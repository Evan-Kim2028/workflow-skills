"""Extract a workstream task map from a saved plan-artifact HTML file.

Usage: python3 pull_plan_from_artifact.py <saved_artifact.html> <out_plan.json>

The script reads the window.__ws1Dag definition (T titles, G groups, E edges, COLS) and the
status lists passed to window.__ws1Dag(S). It writes a plan file the dashboard can serve.
"""
import json
import re
import sys
from collections import Counter


def main(src, out):
    h = open(src, encoding="utf-8").read()
    fn = h[h.index("window.__ws1Dag = function"):]
    titles = json.loads(re.search(r"var T = (\{.*?\});", fn, re.S).group(1))
    groups_src = re.search(r"var G = \[(.*?)\];", fn, re.S).group(1)
    groups = [{"id": m[0], "col": int(m[1]), "title": m[2], "keys": m[3].split()}
              for m in re.findall(r'\{id:"([^"]+)", col:(\d), t:"([^"]+)", k:"([^"]+)"\}', groups_src)]
    edges = json.loads(re.search(r"var E = (\[.*?\]);", fn, re.S).group(1))
    cols = json.loads(re.search(r"var COLS = (\[.*?\]);", fn, re.S).group(1))
    call = h.index("window.__ws1Dag(S)")
    status = {}
    for keys, value in re.findall(r'"([0-9a-z ]+)"\.split\(" "\)\.forEach\(function \(k\) \{ S\[k\] = "(\w+)"', h[call - 1500:call]):
        for k in keys.split():
            status[k] = value
    plan = {"ws": "WS1",
            "dag": {"cols": cols, "edges": edges,
                    "groups": [{"id": g["id"], "col": g["col"], "title": g["title"],
                                "tasks": [{"id": k, "title": titles.get(k, ""), "status": status.get(k, "todo")} for k in g["keys"]]}
                               for g in groups]},
            "narrative": {"phases": []},
            "_source": "artifact WS1 tab built-in task state"}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=1)
    counts = Counter(t["status"] for g in plan["dag"]["groups"] for t in g["tasks"])
    print(f"{len(groups)} groups, {sum(counts.values())} tasks, {dict(counts)} -> {out}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
