#!/usr/bin/env python3
"""Collect news candidates with exact publication times from RSS feeds.

Sources:
  1. Google News RSS searches built from Focus keywords and from source domains
     (covers FT, Reuters, Bloomberg, AP headlines with exact pubDate).
  2. Direct RSS feeds listed in config/feeds.json and in the `rss` field of
     sources exported from the dashboard database.

Usage:
  python3 scripts/collect.py --focus focus.json --sources sources.json \
      --start 2026-11-12T05:40:00Z --out candidates.json [--hours 12] [--radar 36]

focus.json / sources.json: lists of documents as stored in the dashboard db
(an ArtifactData list with out_dir produces one file per doc: pass the folder
instead of a file and the script reads every *.json in it).

Output: {"window":..., "stats":..., "items":[{title, source, url, publishedAt (UTC ISO),
         ageHours, bucket: "window"|"radar", origin, query/feed, focus:[...]}]}
sorted newest first, deduplicated by normalized title.
"""
import argparse, json, re, sys, time, html, pathlib, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (compatible; IntelligenceHub/1.0; personal news digest)"
EDITIONS = [("en-US", "US", "US:en"), ("en-GB", "GB", "GB:en"), ("it", "IT", "IT:it")]


def load_docs(p):
    if not p:
        return []
    p = pathlib.Path(p)
    if p.is_dir():
        out = []
        for f in sorted(p.rglob("*.json")):
            d = json.load(open(f, encoding="utf-8"))
            out.append(d.get("data", d) if isinstance(d, dict) else d)
        return out
    d = json.load(open(p, encoding="utf-8"))
    return d if isinstance(d, list) else d.get("items", [])


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def text(el, *names):
    for n in names:
        for child in el:
            tag = child.tag.split("}")[-1]
            if tag == n:
                if n == "link" and child.get("href"):
                    return child.get("href")
                if child.text:
                    return child.text.strip()
    return ""


def parse_feed(raw):
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raw = re.sub(rb"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", b"&amp;", raw)
        root = ET.fromstring(raw)
    items = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        title = html.unescape(text(el, "title"))
        link = text(el, "link")
        date = parse_date(text(el, "pubDate", "published", "updated", "date"))
        src = text(el, "source")
        items.append({"title": title, "url": link, "date": date, "feedSource": src})
    return items


def norm(t):
    t = re.sub(r"\s+-\s+[^-]{2,60}$", "", t)  # drop " - Publisher" suffix (Google News)
    return re.sub(r"[^a-z0-9àèéìòù]+", " ", t.lower()).strip()[:90]


def gnews_url(q, ed, when="1d"):
    hl, gl, ceid = ed
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": f"{q} when:{when}", "hl": hl, "gl": gl, "ceid": ceid})


TIER1 = ["financial times", "reuters", "bloomberg", "wsj", "wall street journal", "associated press", "ap news", "cnbc", "cnn",
         "il sole 24 ore", "milano finanza", "mf ", "ansa", "teleborsa", "borsa italiana", "corriere", "repubblica", "la stampa",
         "business of fashion", "wwd", "vogue business", "jing daily", "fashionnetwork", "nikkei", "south china morning post", "caixin",
         "les echos", "handelsblatt", "le figaro", "pitchbook", "pe hub", "private equity international", "private debt investor",
         "creditflux", "ifr", "globalcapital", "debtwire", "mergermarket", "financial news", "barron", "the economist", "axios",
         "fortune", "marketscreener", "yahoo finance", "khaleej times", "the national", "pr newswire", "business wire", "globenewswire",
         "federal reserve", "european central bank", "bce", "ecb", "s&p global", "moody", "fitch", "we wealth", "milanofinanza", "il foglio",
         "radiocor", "affaritaliani", "fashion law", "drapers", "reuters breakingviews", "the information", "techcrunch", "semafor", "politico"]
LEX = r"acqui|merger|takeover|buyout|stake|\bbid\b|tender offer|\bipo\b|listing|flotation|bond|notes offering|\bloan|refinanc|debt|credit|" \
      r"private equity|\bfund(s|raise|raising)?\b|valuation|\bdeal|\bopa\b|\bops\b|opas|fusion|cession|quota|prestit|rifinanz|" \
      r"rating|downgrade|upgrade|rate (hike|cut)|interest rate|\btass|\bfed\b|\becb\b|\bbce\b|\bboj\b|inflation|inflazion|yield|rendiment|spread|" \
      r"luxury|lusso|lvmh|kering|richemont|herm[eè]s|prada|moncler|armani|zegna|ferragamo|tod's|capri|tapestry|burberry|chanel|gucci|" \
      r"essilor|swatch|cartier|dior|valentino|versace|balenciaga|golden goose|brunello|cucinelli|maison|fashion|moda|" \
      r"\bai\b|data cent|nvidia|anthropic|openai|compute|chip|banca|banche|bank|insur|assicur|m&a|private credit|direct lending|" \
      r"earnings|results|risultati|guidance|profit|utile|ricavi|revenue|sales|vendite|regulat|antitrust|golden power|consob|sec\b|csrd|ai act"
LEXRE = re.compile(LEX, re.I)


def score(rec, focus_kw):
    pub = (rec["source"] or "").lower()
    t = rec["title"]
    s = 0
    tier1 = any(x in pub for x in TIER1)
    if tier1: s += 3
    hits = len(set(m.group(0).lower() for m in LEXRE.finditer(t)))
    s += min(hits, 3) * 2
    tl = t.lower()
    if any(k in tl for k in focus_kw): s += 3
    distinct = len(set(m.split(" [")[0] for m in rec["matched"]))
    s += min(distinct - 1, 3)
    if not tier1 and hits == 0: s -= 5
    if rec["origin"] == "rss": s += 1
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--focus"); ap.add_argument("--sources")
    ap.add_argument("--start", default=datetime.now(timezone.utc).isoformat())
    ap.add_argument("--hours", type=float, default=12); ap.add_argument("--radar", type=float, default=36)
    ap.add_argument("--out", required=True); ap.add_argument("--extra", nargs="*", default=[], help="extra Google News queries")
    ap.add_argument("--top", type=int, default=180); ap.add_argument("--min-score", type=int, default=5)
    a = ap.parse_args()
    start = parse_date(a.start)
    w_from, r_from = start - timedelta(hours=a.hours), start - timedelta(hours=a.radar)

    focus = [f for f in load_docs(a.focus) if f.get("status", "attivo") != "in_pausa"]
    sources = [s for s in load_docs(a.sources) if s.get("status", "attiva") != "in_pausa"]
    feeds_cfg = json.load(open(ROOT / "config" / "feeds.json", encoding="utf-8"))

    jobs = []  # (url, origin, label, focus_ids)
    # 1. Google News by Focus keywords (each keyword alone + combined with the focus name), 3 editions
    for f in focus:
        kws = [k for k in f.get("keywords", []) if k][:8] or [f["name"]]
        eds = EDITIONS if f.get("priority") == "alta" else EDITIONS[:1] + EDITIONS[2:]
        for k in kws:
            for ed in eds:
                jobs.append((gnews_url(f'"{k}"' if " " in k else k, ed), "google_news", f"{k} [{ed[1]}]", [f["id"]]))
    # 2. Google News by source domain (catches headlines from sites without RSS or blocked to the crawler)
    for s in sources:
        if s.get("category") in ("Istituzionale", "Consulenza e report"):
            continue
        for d in s.get("domains", []):
            jobs.append((gnews_url(f"site:{d}", EDITIONS[2] if d.endswith(".it") else EDITIONS[0]), "google_news_site", d, s.get("focus", [])))
    for q in feeds_cfg.get("google_news_queries", []) + a.extra:
        for ed in EDITIONS:
            jobs.append((gnews_url(q, ed), "google_news", f"{q} [{ed[1]}]", []))
    # 3. Direct RSS feeds
    for fd in feeds_cfg.get("feeds", []) + [{"name": s["name"], "url": u, "focus": s.get("focus", [])} for s in sources for u in s.get("rss", [])]:
        jobs.append((fd["url"], "rss", fd["name"], fd.get("focus", [])))

    seen_urls = set(); jobs = [j for j in jobs if not (j[0] in seen_urls or seen_urls.add(j[0]))]

    def run(job):
        url, origin, label, fids = job
        for attempt in range(2):
            try:
                return job, parse_feed(fetch(url)), None
            except Exception as e:
                err = str(e)[:120]
                time.sleep(1.5)
        return job, [], err

    stats = {"requests": len(jobs), "ok": 0, "failed": [], "raw_items": 0}
    by_key = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for (url, origin, label, fids), items, err in ex.map(run, jobs):
            if err:
                stats["failed"].append({"label": label, "error": err}); continue
            stats["ok"] += 1; stats["raw_items"] += len(items)
            for it in items:
                if not it["date"] or it["date"] < r_from or it["date"] > start + timedelta(minutes=30):
                    continue
                title = it["title"]; publisher = it["feedSource"]
                m = re.match(r"^(.*)\s+-\s+([^-]{2,60})$", title)
                if origin.startswith("google_news") and m:
                    title, publisher = m.group(1), publisher or m.group(2)
                key = norm(title)
                if not key:
                    continue
                rec = by_key.get(key)
                if rec is None:
                    rec = by_key[key] = {"title": title, "source": publisher or label, "url": it["url"],
                                         "publishedAt": it["date"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                                         "ageHours": round((start - it["date"]).total_seconds() / 3600, 1),
                                         "bucket": "window" if it["date"] >= w_from else "radar",
                                         "origin": origin, "matched": [label], "focus": list(fids)}
                else:
                    if label not in rec["matched"]:
                        rec["matched"].append(label)
                    for x in fids:
                        if x not in rec["focus"]:
                            rec["focus"].append(x)
                    if origin == "rss" and rec["origin"] != "rss":  # prefer direct publisher link
                        rec.update(url=it["url"], origin="rss", source=label)
    focus_kw = sorted({k.lower() for f in focus for k in f.get("keywords", []) if len(k) > 6} | {f["name"].lower() for f in focus})
    for r in by_key.values():
        r["score"] = score(r, focus_kw)
    items = sorted(by_key.values(), key=lambda r: r["publishedAt"], reverse=True)
    stats["window_items"] = sum(1 for i in items if i["bucket"] == "window")
    stats["radar_items"] = sum(1 for i in items if i["bucket"] == "radar")
    out = {"window": {"from": w_from.strftime("%Y-%m-%dT%H:%M:%SZ"), "to": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "radarFrom": r_from.strftime("%Y-%m-%dT%H:%M:%SZ")}, "stats": stats, "items": items}
    json.dump(out, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    # Shortlist for analysis: best-scored window items (with a per-Focus floor) + best radar items
    win = sorted([i for i in items if i["bucket"] == "window" and i["score"] >= a.min_score], key=lambda r: -r["score"])
    keep = win[:a.top]
    for f in focus:
        extra = [i for i in win if f["id"] in i["focus"] and i not in keep][:max(0, 8 - sum(1 for i in keep if f["id"] in i["focus"]))]
        keep += extra
    radar = sorted([i for i in items if i["bucket"] == "radar" and i["score"] >= a.min_score + 2], key=lambda r: -r["score"])[:a.top // 3]
    short = {**out, "items": sorted(keep, key=lambda r: r["publishedAt"], reverse=True), "radar": radar}
    short["stats"] = {**stats, "shortlist_window": len(keep), "shortlist_radar": len(radar)}
    sp = a.out.replace(".json", "") + "_shortlist.json"
    json.dump(short, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("shortlist:", sp, len(keep), "window +", len(radar), "radar")
    print(json.dumps({k: v for k, v in stats.items() if k != "failed"}), f"failed={len(stats['failed'])}")
    for f in stats["failed"][:15]:
        print("  FAIL", f["label"], f["error"])


if __name__ == "__main__":
    main()
