# scraper-scraper

A Reddit scraper that needs no login, no API keys and no Reddit account. Give it a subreddit, keywords, an author or a date range, and it collects posts and comments for you as CSV (for Excel / Google Sheets) or JSON.

It can be used three ways: a simple web page, the command line, or by asking Claude Code in plain English.

## Features

- Posts and comments, filtered by subreddit, keywords, author and date range
- Separate date range for comments
- Comments for specific posts (paste the links), or for the top N posts of a scrape
- **Auto mode:** tries each source in turn, so if one is down or blocked the next one takes over, picking up where the last one stopped
- Retries and waits on rate limits by itself
- Removes duplicates
- Saves to CSV or JSON, or copies straight into Google Sheets
- Light and dark mode web page that runs only on your own computer
- One dependency (`requests`)

## The four no-login scrapers

| Scraper | What it does | Good for | Limits |
|---|---|---|---|
| **Arctic Shift** | Free archive of Reddit going back to 2005 | Any date range, big pulls, keyword search, comments | Scores for posts under ~36h old may still show 0 or 1. Run by one volunteer, so it can go down |
| **PullPush** | Second free archive (successor to Pushshift) | Backup when Arctic Shift is down | Slower; often throws server errors (handled with retries) |
| **Reddit JSON** | Reddit's own public `.json` pages | Live, up-to-date scores and full comment threads | Only the latest ~1000 items; strict rate limits; sometimes blocks VPN/cloud IPs |
| **Reddit RSS** | Reddit's public RSS feeds | Last-resort "what's new" when everything else is blocked | Newest posts only; no scores, comment counts or post text |

Auto mode tries them in that order.

## Setup

Needs Python 3.9+ and Git. Clone the repo, then run the setup script once:

- **Mac/Linux:** `bash setup.sh`
- **Windows:** double-click `setup.bat` (or run `setup.bat` in a terminal)

The script creates a private Python environment in `.venv`, updates pip and installs the scraper.

To use `rscrape`, activate the environment in each new terminal:
- Mac/Linux: `source .venv/bin/activate`
- Windows: `.venv\Scripts\activate`

Or skip activating and call it directly: `.venv/bin/rscrape` (Mac/Linux) or `.venv\Scripts\rscrape` (Windows).

## Use

**Web page:** `rscrape ui` opens it in your browser.

**Command line:**
```
rscrape posts -s smallbusiness -q "refund policy" --since 2025-01-01 --until 2025-03-31 --max 500
rscrape posts -s smallbusiness --since 7d --comments 20   # plus comments on the 20 busiest posts
rscrape comments --post https://www.reddit.com/r/x/comments/abc123/title/
rscrape comments -s ecommerce -q shipping --since 30d
rscrape check                                            # which sources are working right now
```
Results are saved in the `output/` folder. Run `rscrape posts --help` to see every option.

**Claude Code:** open this folder in Claude Code and ask, e.g. *"get me the top posts from r/smallbusiness about refunds this month, with comments, and summarise the main complaints"*.
