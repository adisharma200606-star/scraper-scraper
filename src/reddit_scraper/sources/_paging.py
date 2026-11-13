"""Shared paging for the two archive APIs (Arctic Shift and PullPush).

Both return results newest-first and let you ask for "everything before time X".
So we fetch a page, then ask again for items before the oldest one we got,
until we run out or hit the limit.
"""
import time

from ..http import get_json


def walk_backwards(url, params, *, max_items, page_size, size_param, sleep, log, patient=True):
    """patient=False: give up on the first "slow down" instead of waiting (for quick attempts)."""
    seen = set()
    until = params.pop("before", None)
    count = 0
    while count < max_items:
        p = dict(params)
        p[size_param] = page_size
        if until is not None:
            p["before"] = until
        if patient:
            rows = (get_json(url, p, log=log) or {}).get("data") or []
        else:
            rows = (get_json(url, p, log=log, retry_slowdowns=False) or {}).get("data") or []
        fresh = [r for r in rows if r.get("id") and r["id"] not in seen]
        if not fresh:
            return
        for row in fresh:
            seen.add(row["id"])
            yield row
            count += 1
            if count >= max_items:
                return
        if len(rows) < page_size:
            return
        # +1 so items sharing the oldest second aren't skipped (duplicates are filtered above).
        until = min(int(float(r["created_utc"])) for r in rows) + 1
        time.sleep(sleep)
