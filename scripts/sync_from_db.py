#!/usr/bin/env python3
"""Rebuild the Markdown archive from a dump of the dashboard database.

Use when an execution could not push (its container is gone) or to resync the
repo with the dashboard. Idempotent: rewrites files, never appends twice.

Usage: python3 scripts/sync_from_db.py <dump_dir>
<dump_dir> holds ArtifactData list exports (out_dir) of: runs, news, radar, kb, posts, insights
"""
import json, pathlib, sys, subprocess, tempfile
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(dump, coll):
    out = []
    for f in sorted((dump / coll).glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        d = d.get("data", d) if "data" in d and isinstance(d.get("data"), dict) else d
        d.setdefault("id", f.stem)
        out.append(d)
    return out


def main():
    dump = pathlib.Path(sys.argv[1])
    runs, news, radar, kb, posts, ins = (load(dump, c) for c in ("runs", "news", "radar", "kb", "posts", "insights"))
    by = lambda items: (lambda g: [g[i.get("runId", "")].append(i) for i in items] and g)(defaultdict(list))
    n_by, r_by, p_by = by(news), by(radar), by(posts)
    tmp = pathlib.Path(tempfile.mkdtemp())
    written = []
    for r in runs:
        rid = r["id"]
        run_meta = {k: v for k, v in r.items() if k not in ("excluded",)}
        doc = {"run": run_meta, "focus": [], "news": n_by.get(rid, []), "radar": r_by.get(rid, []),
               "excluded": r.get("excluded", []), "kb": [], "posts": p_by.get(rid, []), "insights": []}
        p = ROOT / "data" / "runs" / f"{rid}.json"
        if not p.exists():  # keep the richer original if the run saved it
            json.dump(doc, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        t = tmp / f"{rid}.json"
        json.dump(doc, open(t, "w", encoding="utf-8"), ensure_ascii=False)
        subprocess.run([sys.executable, str(ROOT / "scripts" / "render.py"), str(t)], check=True)
        written.append(rid)
    # Knowledge base: one file per entry, changelog rebuilt from scratch
    (ROOT / "knowledge" / "changelog.md").unlink(missing_ok=True)
    kb_doc = {"run": {"id": "sync-kb", "window": {"from": "", "to": ""}}, "kb": kb}
    t = tmp / "kb.json"; json.dump(kb_doc, open(t, "w", encoding="utf-8"), ensure_ascii=False)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "render.py"), str(t)], check=True)
    (ROOT / "daily" / "sync-kb.md").unlink(missing_ok=True)
    # Insights: rebuilt from the database (status reflects the user's decisions)
    lines = ["# Insights & Opportunities\n\nStato aggiornato dalla dashboard.\n"]
    for d in sorted({i.get("date", "") for i in ins}, reverse=True):
        lines.append(f"\n## {d or 'senza data'}\n")
        lines += [f"- **{i['title']}** ({i.get('kind')}, {i.get('status')}). {i.get('body', '')}\n" for i in ins if i.get("date", "") == d]
    (ROOT / "insights" / "proposals.md").write_text("".join(lines), encoding="utf-8")
    print("runs:", ", ".join(written), "| kb:", len(kb), "| posts:", len(posts), "| insights:", len(ins))


if __name__ == "__main__":
    main()
