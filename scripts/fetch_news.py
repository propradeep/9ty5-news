#!/usr/bin/env python3
"""Server-side news fetcher for 9ty5 News.

Runs in GitHub Actions (or locally) and writes data/news.json, which the page
reads instead of calling third-party CORS proxies from the browser.

Sources (config/sources.json):
  feeds    -> plain RSS/Atom (optional per-feed "filter" regex for general feeds)
  bluesky  -> official accounts via Bluesky's native RSS (free, stable)
  youtube  -> channel uploads via YouTube's native RSS (free, stable)
  plus new anime episodes (AniList public API) and new manga chapters (MangaUpdates public API).

Every source reports a status so failures are visible instead of silent. If a
source fails, its items from the previous run are kept and marked stale.
"""
import json
import os
import re
import sys
import time
from urllib.parse import quote_plus
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "sources.json"
OUT = ROOT / "data" / "news.json"

UA_BROWSER = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 9ty5-news-bot"
)
PER_FEED = int(os.environ.get("PER_FEED", "12"))
TIMEOUT = 20


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]*>", " ", s)
    s = re.sub(r"&[a-zA-Z#0-9]+;", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clip(s, n):
    s = strip_html(s)
    return s if len(s) <= n else s[:n].rstrip() + "…"


def entry_date(e):
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc).isoformat(timespec="seconds")
    return ""


def entry_thumb(e):
    for key in ("media_thumbnail", "media_content"):
        for m in e.get(key, []) or []:
            if m.get("url"):
                return m["url"]
    for enc in e.get("enclosures", []) or []:
        if re.search(r"\.(jpe?g|png|webp|gif)", enc.get("href", ""), re.I):
            return enc["href"]
    html = ""
    if e.get("content"):
        html = e["content"][0].get("value", "")
    html = html or e.get("summary", "")
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', html or "", re.I)
    return m.group(1) if m else None


def parse_feed(url, headers=None):
    r = requests.get(url, headers=headers or {"User-Agent": UA_BROWSER}, timeout=TIMEOUT)
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"unparseable feed ({type(parsed.bozo_exception).__name__})")
    return parsed.entries


def to_item(e, source, cat, typ, **extra):
    item = {
        "title": clip(e.get("title", ""), 200),
        "link": e.get("link") or e.get("id") or "",
        "summary": clip(e.get("summary", "") or "", 240),
        "pubDate": entry_date(e),
        "source": source,
        "cat": cat,
        "type": typ,
        "thumb": entry_thumb(e),
    }
    item.update(extra)
    return item


# ── previous output (for stale fallback) ─────────────────────────────────────
def load_previous():
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── RSS ──────────────────────────────────────────────────────────────────────
def fetch_rss_source(f):
    sid = f"rss:{f['name']}"
    try:
        entries = parse_feed(f["url"])
        items = [to_item(e, f["name"], f["cat"], "rss") for e in entries[:60]]
        items = [i for i in items if i["title"] and i["link"]]
        if f.get("filter"):  # optional keyword filter for general feeds, e.g. "anime|manga"
            rx = re.compile(f["filter"], re.I)
            items = [i for i in items if rx.search(i["title"] + " " + i["summary"])]
        items = items[:PER_FEED]
        # A filtered feed with no matching posts right now is healthy, not broken.
        status = "ok" if (items or f.get("filter")) else "empty"
        return {"id": sid, "type": "rss", "name": f["name"], "status": status, "count": len(items)}, items
    except Exception as ex:  # noqa: BLE001 - report every failure
        return {"id": sid, "type": "rss", "name": f["name"], "status": "error", "count": 0, "error": str(ex)[:160]}, []


# ── Official accounts: Bluesky + YouTube (free native RSS) and X (via a bridge) ─
def fetch_official(cfg):
    """Returns (statuses, items). All entries are type 'official' so the page can
    show them in one tab; each item's source says where it came from."""
    jobs = []  # (id, display name, cat, feed url, kind)
    for b in cfg.get("bluesky", []):
        jobs.append((f"bsky:{b['handle']}", b.get("name") or f"@{b['handle']}", b["cat"],
                     f"https://bsky.app/profile/{b['handle']}/rss", "bluesky"))
    for y in cfg.get("youtube", []):
        jobs.append((f"yt:{y['channel_id']}", y.get("name") or y["channel_id"], y["cat"],
                     f"https://www.youtube.com/feeds/videos.xml?channel_id={y['channel_id']}", "youtube"))
    def one(job):
        sid, name, cat, url, kind = job
        try:
            entries = parse_feed(url)[:PER_FEED]
            its = []
            for e in entries:
                it = to_item(e, name, cat, "official", kind=kind)
                it["title"] = clip(e.get("title") or e.get("summary", ""), 280)
                if it["title"] and it["link"]:
                    its.append(it)
            return {"id": sid, "type": "official", "name": name, "status": "ok" if its else "empty",
                    "count": len(its)}, its
        except Exception as ex:  # noqa: BLE001
            return {"id": sid, "type": "official", "name": name, "status": "error", "count": 0,
                    "error": str(ex)[:160]}, []

    statuses, items = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for st, its in pool.map(one, jobs):
            statuses.append(st)
            items.extend(its)
    return statuses, items


# ── New anime episodes: AniList's free public GraphQL API (no key needed) ────
ANILIST_QUERY = """
query ($from: Int, $to: Int) {
  Page(perPage: 50) {
    airingSchedules(airingAt_greater: $from, airingAt_lesser: $to, sort: TIME_DESC) {
      airingAt
      episode
      media { siteUrl isAdult countryOfOrigin title { romaji english } coverImage { medium } }
    }
  }
}
"""


def fetch_releases():
    sid, name = "anilist:airing", "AniList Airing"
    try:
        now = int(time.time())
        r = requests.post(
            "https://graphql.anilist.co",
            json={"query": ANILIST_QUERY, "variables": {"from": now - 24 * 3600, "to": now}},
            headers={"User-Agent": UA_BROWSER, "Accept": "application/json"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json()["data"]["Page"]["airingSchedules"]
        items = []
        for row in rows:
            m = row["media"]
            if m.get("isAdult"):
                continue
            t = m["title"].get("english") or m["title"].get("romaji") or ""
            items.append({
                "title": f"{t} — Episode {row['episode']} aired",
                "link": m["siteUrl"],
                "summary": "",
                "pubDate": datetime.fromtimestamp(row["airingAt"], tz=timezone.utc).isoformat(timespec="seconds"),
                "source": name, "cat": "anime", "type": "releases",
                "thumb": (m.get("coverImage") or {}).get("medium"),
            })
        st = {"id": sid, "type": "releases", "name": name, "status": "ok" if items else "empty", "count": len(items)}
        return [st], items
    except Exception as ex:  # noqa: BLE001
        return [{"id": sid, "type": "releases", "name": name, "status": "error", "count": 0,
                 "error": str(ex)[:160]}], []


# ── New manga chapters: MangaUpdates' public releases API (official publishers only) ──
def fetch_chapters(cfg):
    """Recent releases from MangaUpdates, kept only when the releasing group matches the
    official-publisher allowlist in config/sources.json -> chapters_official_groups.
    Links go to a MangaUpdates series search."""
    sid, name = "mangaupdates:releases", "MangaUpdates (official)"
    pages = int(os.environ.get("CHAPTER_PAGES", "8"))
    allow = [re.compile(r"\b" + re.escape(g) + r"\b", re.I) for g in cfg.get("chapters_official_groups", [])]
    try:
        merged, seen_groups, total = {}, {}, 0
        for page in range(1, pages + 1):
            r = requests.get("https://api.mangaupdates.com/v1/releases/days",
                             params={"page": page, "perpage": 100},
                             headers={"User-Agent": UA_BROWSER, "Accept": "application/json"}, timeout=TIMEOUT)
            r.raise_for_status()
            rows = r.json().get("results", [])
            if not rows:
                break
            total += len(rows)
            for row in rows:
                rec = row.get("record", {})
                title, ch, vol = (rec.get("title") or "").strip(), rec.get("chapter"), rec.get("volume")
                if not title or not (ch or vol):
                    continue
                groups = [g.get("name") for g in rec.get("groups", []) if g.get("name")]
                official = [g for g in groups if any(rx.search(g) for rx in allow)]
                for g in groups:
                    seen_groups[g] = seen_groups.get(g, 0) + 1
                if allow and not official:
                    continue
                key = (title, ch, vol)
                ts = (rec.get("time_added") or {}).get("timestamp") or 0
                if key in merged:
                    merged[key]["groups"] |= set(official or groups)
                    merged[key]["ts"] = max(merged[key]["ts"], ts)
                else:
                    merged[key] = {"groups": set(official or groups), "ts": ts, "date": rec.get("release_date")}
            time.sleep(0.5)
        items = []
        for (title, ch, vol), v in sorted(merged.items(), key=lambda kv: kv[1]["ts"], reverse=True)[:80]:
            label = " ".join(x for x in [f"Vol. {vol}" if vol else "", f"Ch. {ch}" if ch else ""] if x)
            groups = ", ".join(sorted(v["groups"]))
            items.append({
                "title": f"{title} — {label}",
                "link": "https://www.mangaupdates.com/series?search=" + quote_plus(title),
                "summary": f"Official release: {groups}" if groups else "",
                "pubDate": datetime.fromtimestamp(v["ts"], tz=timezone.utc).isoformat(timespec="seconds") if v["ts"] else (v["date"] or ""),
                "source": name, "cat": "manga", "type": "chapters", "thumb": None,
            })
        st = {"id": sid, "type": "chapters", "name": name, "status": "ok" if items else "empty", "count": len(items),
              "scanned": total,
              # most common groups that did NOT match the allowlist, to help tune the list
              "top_unmatched": [g for g, _ in sorted(seen_groups.items(), key=lambda kv: -kv[1])
                                if not any(rx.search(g) for rx in allow)][:25]}
        return [st], items
    except Exception as ex:  # noqa: BLE001
        return [{"id": sid, "type": "chapters", "name": name, "status": "error", "count": 0,
                 "error": str(ex)[:160]}], []


# ── stale fallback ───────────────────────────────────────────────────────────
def keep_stale(statuses, new_items, old_items, key_fn):
    """For sources that errored, carry over their previous items, flagged stale."""
    bad = {s["name"] for s in statuses if s["status"] in ("error", "empty")}
    if not bad:
        return new_items
    carried = []
    for it in old_items:
        if key_fn(it) in bad:
            it = dict(it)
            it["stale"] = True
            carried.append(it)
    for s in statuses:
        if s["name"] in bad and any(key_fn(c) == s["name"] for c in carried):
            s["stale"] = True
    return new_items + carried


def newest_first(items):
    return sorted(items, key=lambda i: i.get("pubDate") or "", reverse=True)


def main():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    prev = load_previous()
    statuses = []

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch_rss_source, cfg["feeds"]))
    rss_items = []
    rss_status = []
    for st, its in results:
        rss_status.append(st)
        rss_items.extend(its)
    rss_items = keep_stale(rss_status, rss_items, prev.get("rss", []), lambda i: i["source"])
    statuses += rss_status

    official_status, official_items = fetch_official(cfg)
    official_items = keep_stale(official_status, official_items, prev.get("official", []), lambda i: i["source"])
    statuses += official_status

    releases_status, releases_items = fetch_releases()
    releases_items = keep_stale(releases_status, releases_items, prev.get("releases", []), lambda i: i["source"])
    statuses += releases_status

    chapters_status, chapters_items = fetch_chapters(cfg)
    chapters_items = keep_stale(chapters_status, chapters_items, prev.get("chapters", []), lambda i: i["source"])
    statuses += chapters_status

    out = {
        "generated_at": now_iso(),
        "sources": statuses,
        "rss": newest_first(rss_items),
        "official": newest_first(official_items),
        "releases": newest_first(releases_items),
        "chapters": newest_first(chapters_items),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    summary = {}
    for s in statuses:
        summary.setdefault(s["type"], {}).setdefault(s["status"], 0)
        summary[s["type"]][s["status"]] += 1
    print(json.dumps(summary), file=sys.stderr)
    # Fail the run only if nothing at all was fetched (so a real outage is visible).
    if not (out["rss"] or out["official"] or out["releases"] or out["chapters"]):
        sys.exit("No items fetched from any source")


if __name__ == "__main__":
    main()
