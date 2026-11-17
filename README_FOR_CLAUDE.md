# Read me first (for Claude)

This file is loaded automatically via `CLAUDE.md`. It tells you what this repo is for, how to
use the scraper well, and where it's heading. `CLAUDE.md` covers how the code works; the
skill in `.claude/skills/reddit-scrape/SKILL.md` covers running `rscrape` step by step.

## What this is for

`rscrape` is a no-login Reddit research tool. It is the first stage of a bigger workflow:

1. **Research** (works today): pull posts and comments from the right subreddits.
2. **Analysis** (partly today): what people talk about, what they ask, which tone works, what
   gets removed. Today that's the per-subreddit "what works here" line plus you reading the
   results. Planned: a local Ollama model doing the bulk analysis (see Roadmap).
3. **Writing** (you): drafting posts and replies, using writing skills the owner adds separately.
4. **Posting** (a human): the owner reviews each draft and posts it from their own account.

**Never build anything that submits to Reddit automatically.** Reddit bans automated and
inauthentic posting, and AutoModerator removes most of it anyway (in one sample, 57 of 100
posts in a big subreddit). Automate everything up to a ready-to-post draft with a one-click,
prefilled "submit" link; the human presses Post.

The owner is learning to code: use plain English, keep changes small, and say honestly what
was and wasn't tested.

## Using the scraper well

First time on a machine or network: run the setup script (see `CLAUDE.md`), then
`rscrape check`. Office networks often block sources; the hosts needed are
`arctic-shift.photon-reddit.com` (the important one), `api.pullpush.io` and `www.reddit.com`.

For every request, follow the skill. The rules that matter most:

- **Pick the subreddits yourself.** Choose 4-8 real, active ones covering the angles of the
  request and pass them with `-s "a, b, c"`. Leaving `-s` empty makes the scraper ask
  reddit.com, which often refuses home and office connections for 10-15 minutes.
- **Use the fewest, most distinctive keywords.** Every word must appear in a post:
  `trump` finds far more than `donald trump`. Use `OR` for alternatives.
- **Fit the dates to the topic:** about a year for rare topics, 30 days for busy ones.
  Rare keyword + huge subreddit + long range is slow (about 6 minutes per 10,000 posts).
- **Read the verdict at the end of every run** and tell the owner which it was:
  - `YOUR SEARCH:` (exit 1): the scraper worked; change the subreddits, dates or keywords.
    Follow any "Did you mean r/X" suggestion.
  - `SCRAPER PROBLEM:` (exit 3): sources failed; the empty result means nothing. Retry later.
  - `PARTLY DONE:` (exit 0): results, but there may be more.
  - `check your input:` (exit 2): something typed was invalid.
- **Removed posts are left out by default.** Every row has a `status` column. When the goal is
  learning what works or finding examples for writing, add `--until 5d` so every post has been
  rechecked and is known to be "live". Use `--include-removed` only to study what gets removed.
- **Reading results yourself:** `--out -` prints JSON. For big pulls, save
  `--format json --out output/<name>.json` and read the file. Files go to `output/`.
- **Don't fire many runs back to back.** Reddit blocks bursts, and Arctic Shift slows down.

Example:
```
rscrape posts -s "VoiceAgents, AI_Agents, LocalLLaMA" -q sarvam --since 365d --max 200
rscrape posts -s "smallbusiness, Entrepreneur" --since 30d --until 5d --max 500 --format json --out output/biz.json
rscrape comments --post https://www.reddit.com/r/x/comments/abc123/title/
```

## Where the sources stand (November 2026)

- **Arctic Shift**: the main source. Browsing is fast and reliable. Its own keyword search is
  usually refused, so the scraper checks posts one by one instead (this is expected).
- **PullPush**: says it no longer offers free scraping to automated tools. Don't build on it.
- **Reddit JSON**: often blocked on home and office networks.
- **Reddit RSS**: works on most networks, but newest posts only and no scores.

Details and dates are in `CLAUDE.md` under "What we learned about the sources".

## Roadmap: room to grow

None of this exists yet. It's the planned direction, so design changes to leave room for it.

- **`rscrape analyze` with Ollama.** Send scraped posts to a local Ollama model
  (`http://localhost:11434`, called with `requests`, so no new dependency) and save a short
  brief per subreddit: main themes, recurring questions, the tone that does well, how live
  posts differ from removed ones (use `--include-removed`), and threads worth replying to.
  You (Claude) then write from that brief. Check that Ollama is installed and a model is
  pulled before relying on it, and fail with a clear message if not.
- **n8n automation.** A scheduled flow: scrape, then analyse with Ollama, then draft with
  Claude, then a review queue (Google Sheet or Slack) where each draft has a prefilled Reddit
  submit link (check Reddit's current submit-URL format when building it). n8n can run
  `rscrape` with its Execute Command node on the same machine. The web server only accepts
  requests from its own page (Origin checks), so if n8n needs an HTTP endpoint, add a separate,
  deliberate one rather than loosening those checks.
- **Other connectors and APIs:** Google Sheets export, Slack or email notifications, and
  other research sources, as long as they need no login or paid key for the core features.

Rules for any addition:
- keep the core no-login
- keep the source fallbacks and the "your search vs scraper problem" verdicts working
- add offline tests for everything (fake every request)
- add new dependencies only when really needed
- keep this repo generic: no company or client names; project-specific setup belongs in each
  project's own copy
