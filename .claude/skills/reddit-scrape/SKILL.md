---
name: reddit-scrape
description: Scrape Reddit posts and comments (no login) with the rscrape CLI in this repo. Use whenever the user asks to find, pull, collect, research or analyse Reddit posts, comments, subreddits or users, or to put Reddit data into a sheet.
---

# Scraping Reddit with rscrape

This repo installs a CLI called `rscrape`. If `rscrape` isn't found, activate the venv first
(`source .venv/bin/activate`, or `.venv\Scripts\activate` on Windows) or install it with `pip install -e .`.

## Turn the request into a command

Posts:
```
rscrape posts [-s SUBREDDIT] [-q "KEYWORDS"] [-a AUTHOR] [--since DATE] [--until DATE]
              [--max N] [--sort new|old|top|comments] [--comments N|all] [--comments-max N]
              [--out FILE | --out -] [--format csv|json|both]
```
Comments:
```
rscrape comments --post URL_OR_ID [--post ...] [--max N]          # comments under specific posts
rscrape comments -s SUB [-q KEYWORDS] [-a AUTHOR] [--since] [--until] [--max N]
```
- Dates: `YYYY-MM-DD` or relative like `7d`, `12h`, `2w`. `--until` includes that whole day.
- Keywords support `"exact phrase"`, `OR` and `-exclude`.
- Comment keyword search needs a subreddit or author too.
- `--sort top` only sorts what was fetched, so raise `--max` (e.g. 1000+) to find the real top posts in a range.
- Leave `--source` on auto unless the user asks for a specific one.
- No subreddit given? Leave `-s` out: it finds the best subreddits itself (`--find N`, default 5).
  Or pass several: `-s "AI_Agents, SaaS"`. If you know the topic well, choosing the subreddits
  yourself usually beats auto-finding. `rscrape subs "keywords"` previews what it would pick.
- Keyword searches work best with a subreddit. If Arctic Shift's own keyword search times out, the
  scraper scans the subreddit and filters locally (up to `--scan-max`, default 10000 items checked);
  add a date range to make that faster.
- Reddit's JSON pages are blocked on many networks; RSS usually still works for recent posts and
  comment threads (no scores).

## Where the results go

- If you need to read the results yourself (summarise, analyse, answer questions): use `--out -`
  to get JSON on stdout. Progress messages go to stderr. For big pulls, save with
  `--format json --out output/<name>.json` and read the file instead of flooding the context.
- If the user wants a file or sheet: save CSV (the default, in `output/`) and tell them the path.
  Google Sheets: File > Import > Upload the CSV. Or use the web page's "Copy for Sheets" button.

## When it fails

Run `rscrape check` to see which sources are reachable. "blocked by a proxy/firewall" means the
network is blocking that site (common on office networks/VPNs). Tell the user which hosts need to be
allowed: arctic-shift.photon-reddit.com, api.pullpush.io, www.reddit.com, old.reddit.com.
Report what was actually found. Never make up posts or comments.
