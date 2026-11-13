"""One place for every network request: timeouts, retries and rate-limit waits."""
import time
import warnings

# The Python that ships with macOS prints a scary-looking but harmless SSL warning.
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

import requests  # noqa: E402

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Status codes worth waiting on and retrying.
RETRY_STATUSES = {429, 500, 502, 503, 504, 520, 522, 524}
MAX_WAIT = 60  # never sleep longer than this on a single retry


class SourceError(Exception):
    """A source failed in a way that retrying won't fix (blocked, gone, bad query)."""


class Unsupported(Exception):
    """A source can't do this kind of search at all."""


def _wait_seconds(resp, fallback):
    for header in ("Retry-After", "X-RateLimit-Reset"):
        value = resp.headers.get(header)
        if value:
            try:
                return min(max(float(value), 1.0), MAX_WAIT)
            except ValueError:
                pass
    return min(fallback, MAX_WAIT)


def get(url, params=None, *, retries=4, timeout=30, log=None):
    """GET a URL and return the response. Retries rate limits and server errors."""
    delay = 3.0
    last_problem = "unknown error"
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                url, params=params, timeout=timeout, headers={"User-Agent": USER_AGENT}
            )
        except requests.exceptions.ProxyError:
            raise SourceError(
                f"{_host(url)} is blocked by a proxy/firewall on this network "
                "(common on office Wi-Fi/VPN; try another network or ask IT to allow it)"
            )
        except requests.exceptions.SSLError:
            raise SourceError(
                f"secure connection to {_host(url)} failed (often a company network inspecting traffic)"
            )
        except requests.Timeout:
            last_problem = "timed out"
        except requests.ConnectionError:
            last_problem = "couldn't connect (no internet, or the site is down)"
        except requests.RequestException as e:
            last_problem = f"network error ({e.__class__.__name__})"
        else:
            if resp.status_code == 200:
                return resp
            if resp.status_code in RETRY_STATUSES:
                last_problem = f"HTTP {resp.status_code}"
                if attempt < retries:
                    wait = _wait_seconds(resp, delay)
                    if log:
                        log(f"  {last_problem} from {_host(url)}, waiting {wait:.0f}s and retrying")
                    time.sleep(wait)
                    delay *= 2
                    continue
                break
            # 401/403/404 etc: retrying won't help.
            detail = _error_text(resp)
            raise SourceError(f"HTTP {resp.status_code} from {_host(url)}{detail}")
        if attempt < retries:
            if log:
                log(f"  {last_problem}, retrying in {delay:.0f}s")
            time.sleep(delay)
            delay *= 2
    raise SourceError(f"{_host(url)} kept failing ({last_problem})")


def get_json(url, params=None, **kw):
    resp = get(url, params, **kw)
    try:
        data = resp.json()
    except ValueError:
        raise SourceError(f"{_host(url)} returned something that isn't JSON (probably a block page)")
    if isinstance(data, dict) and data.get("error") and not data.get("data"):
        raise SourceError(f"{_host(url)} said: {data['error']}")
    return data


def _host(url):
    return url.split("/")[2] if "://" in url else url


def _error_text(resp):
    try:
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            return f": {data['error']}"
    except ValueError:
        pass
    return ""
