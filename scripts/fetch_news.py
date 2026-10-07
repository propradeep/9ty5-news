#!/usr/bin/env python3
"""Server-side news fetcher for 9ty5 News.

Runs in GitHub Actions (or locally) and writes data/news.json, which the page
reads instead of calling third-party CORS proxies from the browser.

Sources (config/sources.json):
  feeds        -> plain RSS/Atom
  subreddits   -> Reddit (official OAuth API if REDDIT_CLIENT_ID/SECRET are set,
                  otherwise public .rss which Reddit often blocks from CI IPs)
  bluesky      -> official accounts via Bluesky's native RSS (free, stable)
  youtube      -> channel uploads via YouTube's native RSS (free, stable)
  (releases)   -> new anime episodes from AniList's public airing schedule (last 24h)
  x_accounts   -> X/Twitter via an RSS bridge, only if one is configured (no native RSS exists):
                    X_FEED_TEMPLATE  e.g. https://your-rsshub.example.com/twitter/user/{handle}
                    x_feed_overrides in sources.json  {"handle": "https://rss.app/feeds/xxxx.xml"}

Every source reports a status so failures are visible instead of silent. If a
source fails, its items from the previous run are kept and marked stale.
"""
import json
import os
import re
import sys
import time
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
REDDIT_UA = os.environ.get("REDDIT_USER_AGENT", "linux:9ty5-news:1.0 (by /u/9ty5news)")
PER_FEED = int(os.environ.get("PER_FEED", "12"))
PER_SUB = int(os.environ.get("PER_SUB", "8"))
REDDIT_SORTS = [s for s in os.environ.get("REDDIT_SORTS", "hot,new,top,rising").split(",") if s]
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


# ── Reddit ───────────────────────────────────────────────────────────────────
def reddit_token():
    cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not (cid and secret):
        return None
    r = requests.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(cid, secret),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": REDDIT_UA},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def reddit_api_posts(sub, sort, token):
    params = {"limit": PER_SUB, "raw_json": 1}
    if sort == "top":
        params["t"] = "day"
    r = requests.get(
        f"https://oauth.reddit.com/r/{sub}/{sort}",
        params=params,
        headers={"Authorization": f"bearer {token}", "User-Agent": REDDIT_UA},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for c in r.json()["data"]["children"]:
        d = c["data"]
        if d.get("stickied"):
            continue
        thumb = d.get("thumbnail") if str(d.get("thumbnail", "")).startswith("http") else None
        out.append({
            "title": clip(d.get("title", ""), 200),
            "link": "https://www.reddit.com" + d.get("permalink", ""),
            "summary": clip(d.get("selftext", ""), 240),
            "pubDate": datetime.fromtimestamp(d["created_utc"], tz=timezone.utc).isoformat(timespec="seconds"),
            "source": f"r/{sub}", "sub": sub, "type": "reddit",
            "score": d.get("score"), "comments": d.get("num_comments"),
            "flair": d.get("link_flair_text") or "", "thumb": thumb,
        })
    return out


def reddit_rss_posts(sub, sort):
    path = f"{sort}.rss" + ("?t=day" if sort == "top" else "")
    entries = parse_feed(f"https://www.reddit.com/r/{sub}/{path}", headers={"User-Agent": REDDIT_UA})[:PER_SUB]
    return [{
        "title": clip(e.get("title", ""), 200), "link": e.get("link", ""),
        "summary": clip(e.get("summary", ""), 240), "pubDate": entry_date(e),
        "source": f"r/{sub}", "sub": sub, "type": "reddit",
        "score": None, "comments": None, "flair": "", "thumb": entry_thumb(e),
    } for e in entries]


def fetch_reddit(subs):
    statuses, by_sort = [], {s: [] for s in REDDIT_SORTS}
    try:
        token = reddit_token()
    except Exception as ex:  # noqa: BLE001
        print(f"Reddit OAuth failed, falling back to public RSS: {ex}", file=sys.stderr)
        token = None
    mode = "oauth" if token else "public-rss"
    print(f"Reddit mode: {mode}", file=sys.stderr)
    delay = 0.7 if token else 2.0  # stay under rate limits
    # Without credentials Reddit usually blocks CI servers. Use one sort and give up
    # quickly after repeated failures so the run still finishes and saves RSS/X data.
    sorts = REDDIT_SORTS if token else REDDIT_SORTS[:1]
    consecutive_fail, gave_up = 0, False
    for s in subs:
        if gave_up:
            statuses.append({"id": f"reddit:{s['sub']}", "type": "reddit", "name": f"r/{s['sub']}",
                             "status": "skipped", "count": 0,
                             "error": "Reddit is blocking this server. Add REDDIT_CLIENT_ID and "
                                      "REDDIT_CLIENT_SECRET repo secrets (reddit.com/prefs/apps)."})
            continue
        errors, got = [], 0
        for sort in sorts:
            try:
                posts = reddit_api_posts(s["sub"], sort, token) if token else reddit_rss_posts(s["sub"], sort)
                for p in posts:
                    p["cat"] = s["cat"]
                by_sort[sort].extend(posts)
                got += len(posts)
                consecutive_fail = 0
            except Exception as ex:  # noqa: BLE001
                errors.append(f"{sort}: {str(ex)[:90]}")
                consecutive_fail += 1
                if not token and consecutive_fail >= 5:
                    gave_up = True
                    break
            time.sleep(delay)
        st = "ok" if got and not errors else ("error" if not got else "partial")
        entry = {"id": f"reddit:{s['sub']}", "type": "reddit", "name": f"r/{s['sub']}", "status": st, "count": got}
        if errors:
            entry["error"] = "; ".join(errors[:2])[:200]
        statuses.append(entry)
    return statuses, by_sort, mode


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
    # X has no native RSS: only fetched when a bridge is configured, otherwise left out
    template = os.environ.get("X_FEED_TEMPLATE", "").strip()
    overrides = cfg.get("x_feed_overrides", {})
    for a in cfg.get("x_accounts", []):
        url = overrides.get(a["handle"]) or (template.format(handle=a["handle"]) if template else "")
        if url:
            jobs.append((f"x:{a['handle']}", f"@{a['handle']} (X)", a["cat"], url, "x"))

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

    reddit_status, reddit_by_sort, reddit_mode = fetch_reddit(cfg["subreddits"])
    old_reddit = prev.get("reddit", {})
    for sort in REDDIT_SORTS:
        reddit_by_sort[sort] = keep_stale(
            [dict(s) for s in reddit_status], reddit_by_sort[sort], old_reddit.get(sort, []), lambda i: i["source"]
        )
    statuses += reddit_status

    official_status, official_items = fetch_official(cfg)
    official_items = keep_stale(official_status, official_items, prev.get("official", []), lambda i: i["source"])
    statuses += official_status

    releases_status, releases_items = fetch_releases()
    releases_items = keep_stale(releases_status, releases_items, prev.get("releases", []), lambda i: i["source"])
    statuses += releases_status

    out = {
        "generated_at": now_iso(),
        "reddit_mode": reddit_mode,
        "sources": statuses,
        "rss": newest_first(rss_items),
        "reddit": {s: (newest_first(v) if s == "new" else v) for s, v in reddit_by_sort.items()},
        "official": newest_first(official_items),
        "releases": newest_first(releases_items),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    summary = {}
    for s in statuses:
        summary.setdefault(s["type"], {}).setdefault(s["status"], 0)
        summary[s["type"]][s["status"]] += 1
    print(json.dumps(summary), file=sys.stderr)
    # Fail the run only if nothing at all was fetched (so a real outage is visible).
    if not (out["rss"] or any(out["reddit"].values()) or out["official"]):
        sys.exit("No items fetched from any source")


if __name__ == "__main__":
    main()
