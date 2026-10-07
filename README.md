# 9ty5 News Aggregator

Mobile-friendly web app for anime, manga & gaming news.

## Setup on GitHub Pages (free hosting)

1. Create a new GitHub repo named `9ty5-news` (or any name)
2. Upload all files in this folder to the repo
3. Go to repo Settings → Pages → Source → Deploy from branch → main → / (root)
4. Your app will be live at: `https://YOUR_USERNAME.github.io/9ty5-news`

## Files
- `index.html` — main app
- `manifest.json` — PWA config (add to home screen)
- `sw.js` — service worker (offline support)
- `icon-192.png` / `icon-512.png` — add your own icons

## Sources
- 55 RSS feeds (anime, manga/comics, gaming)
- 40 Reddit communities
- Favorites saved locally in browser
- Read history tracked locally

## Usage
1. Open on phone browser
2. Tap "Add to Home Screen" for app-like experience
3. Tap Refresh to fetch latest news
4. Switch between RSS and Reddit tabs
5. Star articles to save them
6. Copy any article for Instagram/Facebook captioning

## How the data works

News is no longer fetched in the browser. A GitHub Actions workflow
(`.github/workflows/fetch-news.yml`, every 30 minutes) runs `scripts/fetch_news.py`,
which reads `config/sources.json` and writes `data/news.json`. The page only reads that file.

- **RSS**: add or remove feeds in `config/sources.json` → `feeds`.
- **Reddit**: subreddits live in `config/sources.json` → `subreddits`. Add repo secrets
  `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` (create a free "script" app at
  reddit.com/prefs/apps) for reliable results. Without them it falls back to public `.rss`,
  which Reddit often blocks from CI servers. "My Feed" filters this data, so any subreddit
  you list in Settings must also be in `config/sources.json`.
- **X / official accounts**: X has no native RSS. Set the repo *variable* `X_FEED_TEMPLATE`
  (e.g. `https://your-rsshub.example.com/twitter/user/{handle}`) or put per-account feed URLs
  (for example from RSS.app) in `config/sources.json` → `x_feed_overrides`.
- Each source reports `ok / empty / error / skipped` in `data/news.json`; the page shows a
  warning banner listing any problems. If a source fails, its previous posts are kept (marked "cached").
