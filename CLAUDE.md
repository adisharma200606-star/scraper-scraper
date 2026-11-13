# About this project

## Why it exists

A personal tool for Reddit research: find posts and comments on a topic, read what people
actually say, and get it into a spreadsheet or straight into a Claude Code chat. It replaces
manual browsing and copy-pasting.

Hard requirements:
- **No login, no API keys, no Reddit account.** Every source is public and unauthenticated.
- **Reliable.** It tries several sources and never crashes because one is down; it says clearly
  what failed and why.
- **Simple to run** for a non-developer: one setup script, then a web page (`rscrape ui`),
  the command line, or plain-English requests in Claude Code.

This repo is a clean base. It gets cloned and customised for each project, so keep it generic:
no company names, client names or project-specific content in this repo.

## Built on a Mac, also used on Windows

This was written and tested on **macOS** (Apple Silicon, zsh, Python 3.15 from Homebrew;
it also supports the Python 3.9 that ships with macOS). The same code runs on **Windows**;
only the setup and the commands differ.

| | Mac | Windows |
|---|---|---|
| One-time setup | `bash setup.sh` | `setup.bat` (double-click, or `.\setup.bat` in PowerShell) |
| Run without activating | `.venv/bin/rscrape ui` | `.venv\Scripts\rscrape ui` |
| Activate the venv | `source .venv/bin/activate` | `.venv\Scripts\activate` (PowerShell may block this; use the line above instead) |
| Python command | `python3` | `py` or `python` |
| Shortcut so plain `rscrape` works anywhere | `setup.sh` offers to add an alias to `~/.zshrc` | not set up; use `.venv\Scripts\rscrape` |

Notes for Windows:
- Install Python from python.org with **"Add python.exe to PATH"** ticked. Avoid the Microsoft
  Store "python" stub.
- `.gitattributes` keeps `setup.bat` on CRLF line endings and `setup.sh` on LF. Don't change that.
- If `rscrape check` says a source is "blocked by a proxy/firewall", the office network is
  blocking it. The hosts needed are: `arctic-shift.photon-reddit.com`, `api.pullpush.io`,
  `www.reddit.com`.
- Code paths use `pathlib`, so nothing is Mac-specific. Results go to `output/` in the project
  folder on both systems.

## How it works

```
src/reddit_scraper/
  cli.py            rscrape posts / comments / check / ui
  core.py           tries sources in order, falls back, removes duplicates, enforces date ranges
  http.py           every network request: retries (429 + all 5xx), rate-limit waits, block detection
  models.py         Query, the common post/comment shape, date parsing, local keyword matcher
  export.py         CSV (utf-8-sig, for Excel/Sheets) and JSON; saves to <project>/output/
  sources/          one module per source, all exposing posts(), comments(), comments_for_post(), ping()
  web/server.py     tiny standard-library web server, 127.0.0.1 only, streams results as NDJSON
  web/index.html    the whole web page (light/dark), no build step, no external files
tests/              offline tests; every network call is faked
.claude/skills/reddit-scrape/   how Claude Code should use rscrape
```

The only runtime dependency is `requests`.

## What we learned about the sources (tested November 2026)

- **Arctic Shift** is the main source and the most dependable.
  - Plain browsing (subreddit + date range, no keywords) is fast and reliable.
  - Its keyword search (`query`, `title`, `selftext`) is **unreliable**: it often returns
    `HTTP 422 "Timeout. Maybe slow down a bit"` within a second, depending on how busy their
    server is. This is on their side and retrying doesn't help. So when keyword search fails,
    the code browses the subreddit and filters locally (`models.keyword_matcher`, capped by
    `--scan-max`). That "scanning and filtering locally" log line is expected, not an error.
  - Keyword search **requires a subreddit or author**. Searching all of Reddit by keyword is
    refused by design.
  - Its subreddit search only matches name prefixes, so it's useless for finding subreddits by topic.
  - Scores for posts younger than ~36 hours may still show 0 or 1.
- **PullPush** is often down (502/526). It's retried, then rested for 2 minutes.
- **Reddit JSON** (`www.reddit.com/....json`) is blocked (403) on many networks, including the
  home network this was built on, whatever user agent is used. After the first 403 it's skipped
  for 10 minutes.
- **Reddit RSS** works on that same network. It gives recent posts with text, and comment
  threads (no scores). Its per-subreddit search feed returned 404, so keyword filtering on RSS
  is done locally on the normal feed.

## Idea not built yet: finding subreddits automatically

Often the right subreddit isn't known in advance. Tested from the Mac and working:
- `https://www.reddit.com/subreddits/search.rss?q=<keywords>` lists matching communities.
- `https://www.reddit.com/search.rss?q=<keywords>&sort=new` searches all of Reddit.

Plan: when the subreddit is left empty, discover the top few subreddits with the first feed,
then run the normal Arctic Shift search in each. Optionally add results from the second feed.
In Claude Code you can also just pick likely subreddits yourself and run `rscrape` on each.

## Working on this repo

- Run tests: `pip install -e ".[dev]"`, then `pytest`. Tests must stay offline (fake every request).
- Keep it beginner-friendly: plain-English errors, comments that explain why, no new
  dependencies unless really needed.
- Never commit `output/`, `.venv/` or `.env`.
- The owner is learning to code: explain changes simply and say honestly what was and wasn't tested.
