#!/usr/bin/env python3
"""Render a run file (data/runs/<id>.json) into the Markdown archive and
into db-ready documents for the dashboard.

Usage: python3 scripts/render.py data/runs/2026-11-11-r1.json [--db-out DIR]

Markdown written:
  daily/<date>-<run>.md            Daily Intelligence of the run
  knowledge/entries/<YYYY>/<MM>/<id>.md   one file per KB entry (overwritten = updated)
  knowledge/changelog.md           appended with state changes ("superata", updates)
  linkedin/drafts/<id>.md          post drafts (status lives in the dashboard db)
  insights/proposals.md            appended with new proposals
  focus/<id>.md                    created only if missing (dossiers are edited by hand/runs)

--db-out writes one JSON per document as <dir>/<collection>/<doc_id>.json,
ready for an ArtifactData batch (collections: focus, news, radar, kb, posts,
insights, runs).
"""
import json, os, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
TIER = {"breaking": "Breaking News", "alta": "Alta rilevanza", "media": "Media rilevanza", "monitor": "Da monitorare"}
STATE = {"valida": "Attualmente valida", "storica": "Storica", "superata": "Superata",
         "ipotesi": "Ipotesi / previsione", "da_verificare": "Da verificare"}


def w(path, text, mode="w"):
    path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8") as f:
        f.write(text)


def news_md(n, focus_names):
    links = ", ".join(focus_names.get(x, x) for x in n.get("links", [])) or "—"
    extra = "".join(f"\n  - {u}" for u in n.get("extraUrls", []))
    return f"""### {n['title']}
- **Pubblicazione:** {n['publishedAt']} ({n['tz']}). Verifica orario: {n['timeVerified']}
- **Fonte:** {n['source']}
- **Link:** {n['url']}{extra}
- **Focus:** {", ".join(focus_names.get(x, x) for x in n['focus'])}

**Sintesi (fatti).** {n['summary']}

**Perché è importante.** {n['why']}

**Impatto potenziale.** {n['impact']}

**Il mio punto di vista (interpretazione).** {n['view']}

**Collegamenti con altri Focus.** {links}

_Nota sui fatti:_ {n.get('facts', '—')}
"""


def main():
    run_path = pathlib.Path(sys.argv[1])
    db_out = None
    if "--db-out" in sys.argv:
        db_out = pathlib.Path(sys.argv[sys.argv.index("--db-out") + 1])
    d = json.load(open(run_path, encoding="utf-8"))
    run = d["run"]
    date = run["id"][:10]
    focus_names = {f["id"]: f["name"] for f in d.get("focus", [])}
    idx = ROOT / "focus" / "_index.json"
    if idx.exists():
        focus_names.update({f["id"]: f["name"] for f in json.load(open(idx, encoding="utf-8"))})

    # Daily Intelligence
    parts = [f"# Daily Intelligence — {date} ({run['id']})\n",
             f"Finestra: {run['window']['from']} → {run['window']['to']} (UTC)\n",
             f"> {run.get('notes', '')}\n"]
    for t in ["breaking", "alta", "media", "monitor"]:
        items = [n for n in d.get("news", []) if n["tier"] == t]
        if items:
            parts.append(f"\n## {TIER[t]}\n")
            parts += [news_md(n, focus_names) for n in items]
    if not d.get("news"):
        parts.append("\n**Nessuna notizia rilevante nelle ultime 12 ore.**\n")
    if d.get("radar"):
        parts.append("\n## Radar 12–36 ore (fuori finestra, solo contesto)\n")
        parts += [f"- **{r['title']}** — {r['source']}, {r['publishedAt']}. {r.get('note', '')} {r['url']}\n" for r in d["radar"]]
    if d.get("excluded"):
        parts.append("\n## Escluse e perché\n")
        parts += [f"- {e['title']}: {e['reason']}\n" for e in d["excluded"]]
    w(f"daily/{run['id']}.md", "\n".join(parts))

    # Knowledge base
    for k in d.get("kb", []):
        y, m = k["acquiredAt"][:4], k["acquiredAt"][5:7]
        w(f"knowledge/entries/{y}/{m}/{k['id']}.md", f"""---
id: {k['id']}
focus: {k['focus']}
state: {k['state']}
relevance: {k['relevance']}
published: "{k['publishedAt']}"
acquired: "{k['acquiredAt']}"
source: "{k['source']}"
url: {k['url']}
links: {json.dumps(k.get('linksTo', []))}
---
# {k['title']}

**Stato:** {STATE.get(k['state'], k['state'])}

{k['summary']}

**Concetti chiave:** {", ".join(k.get('concepts', []))}

**Aggiornamenti:** {k.get('updates') or '—'}
""")
        if k.get("updates") or k["state"] == "superata":
            w("knowledge/changelog.md", f"- {k['acquiredAt']} · `{k['id']}` · {STATE.get(k['state'])}: {k.get('updates') or k['title']}\n", "a")

    # LinkedIn drafts
    for p in d.get("posts", []):
        w(f"linkedin/drafts/{p['id']}.md", f"""---
id: {p['id']}
status: {p['status']}
lang: {p['lang']}
type: {p['type']}
focus: {json.dumps(p['focus'])}
source: {p['sourceUrl']}
---
# {p['hook']}

- **Argomento:** {p['topic']}
- **Perché interessa al pubblico:** {p['audience']}
- **Messaggio principale:** {p['message']}
- **Tesi:** {p['thesis']}
- **Domanda finale:** {p['question']}
- **Fonte:** {p['sourceName']} — {p['sourceUrl']}

## Post completo

{p['body']}
""")

    # Insights
    if d.get("insights"):
        w("insights/proposals.md", f"\n## {date}\n" + "".join(
            f"- **{i['title']}** ({i['kind']}, {i['status']}). {i['body']}\n" for i in d["insights"]), "a")

    # Focus dossiers (create if missing)
    for f in d.get("focus", []):
        fp = ROOT / "focus" / f"{f['id']}.md"
        if not fp.exists():
            w(f"focus/{f['id']}.md", f"# {f['name']}\n\nTipo: {f['type']} · Priorità: {f['priority']}\n\n{f['note']}\n\nParole chiave: {', '.join(f['keywords'])}\n")

    if db_out:
        def dump(coll, doc_id, data):
            p = db_out / coll / f"{doc_id}.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False)
        for f in d.get("focus", []):
            dump("focus", f["id"], {**f, "createdAt": run["startedAt"]})
        for n in d.get("news", []):
            dump("news", n["id"], {**n, "date": date})
        for i, r in enumerate(d.get("radar", [])):
            dump("radar", f"{run['id']}-{i}", {**r, "runId": run["id"], "date": date})
        for k in d.get("kb", []):
            dump("kb", k["id"], k)
        for p in d.get("posts", []):
            dump("posts", p["id"], {**p, "date": date})
        for i in d.get("insights", []):
            dump("insights", i["id"], {**i, "date": date})
        dump("runs", run["id"], {**run, "counts": {"news": len(d.get("news", [])), "radar": len(d.get("radar", [])),
             "kb": len(d.get("kb", [])), "posts": len(d.get("posts", []))}, "excluded": d.get("excluded", [])})


if __name__ == "__main__":
    main()
