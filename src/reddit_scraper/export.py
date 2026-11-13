"""Save results as CSV (opens in Excel / Google Sheets) or JSON."""
import csv
import json
import os
from datetime import datetime
from pathlib import Path

from .models import COMMENT_FIELDS, POST_FIELDS

OUTPUT_DIR = Path(os.environ.get("RSCRAPE_OUTPUT", "output"))
SHEETS_CELL_LIMIT = 49000  # Google Sheets rejects cells over 50,000 characters


def default_path(kind, ext):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return OUTPUT_DIR / f"{stamp}_{kind}.{ext}"


def write_csv(items, fields, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Excel shows emojis and non-English text correctly
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for item in items:
            row = {k: item.get(k) for k in fields}
            for k, v in row.items():
                if isinstance(v, str) and len(v) > SHEETS_CELL_LIMIT:
                    row[k] = v[:SHEETS_CELL_LIMIT] + " [truncated]"
            w.writerow(row)
    return path


def nest(posts, comments):
    """Attach each post's comments under it (for JSON output)."""
    by_post = {}
    for c in comments:
        by_post.setdefault(c.get("post_id"), []).append(c)
    return [dict(p, comments=by_post.get(p["id"], [])) for p in posts]


def write_json(data, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def save(posts=None, comments=None, out=None, fmt="csv"):
    """Write results and return the list of files created.

    CSV: one file for posts, one for comments.
    JSON: one file, with comments nested under their posts when both exist.
    """
    posts, comments = posts or [], comments or []
    files = []
    if fmt in ("json", "both"):
        if posts:
            data = {"posts": nest(posts, comments)} if comments else {"posts": posts}
        else:
            data = {"comments": comments}
        target = Path(out).with_suffix(".json") if out else default_path("posts" if posts else "comments", "json")
        files.append(write_json(data, target))
    if fmt in ("csv", "both"):
        if posts:
            target = Path(out).with_suffix(".csv") if out else default_path("posts", "csv")
            files.append(write_csv(posts, POST_FIELDS, target))
        if comments:
            if out and not posts:
                target = Path(out).with_suffix(".csv")
            elif out:
                target = Path(out).with_name(Path(out).stem + "_comments.csv")
            else:
                target = default_path("comments", "csv")
            files.append(write_csv(comments, COMMENT_FIELDS, target))
    return files
