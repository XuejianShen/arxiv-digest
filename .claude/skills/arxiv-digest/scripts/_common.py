#!/usr/bin/env python3
"""_common.py — shared plumbing for the astro-lit-review scripts (ads.py, fallback.py, rank.py).

Why this module exists: ads.py (NASA ADS / SciX), fallback.py (arXiv + Semantic Scholar), and
rank.py all need the SAME things — a single normalized "paper" record schema so a ranked list looks
identical no matter which source produced it, a small HTTP client that is polite about rate limits,
an on-disk cache so re-running a research session does not re-hit the APIs, and a compact table
printer. Keeping them here means the three entry-point scripts stay short and behave consistently.

Standard library ONLY (urllib, json, hashlib, ...). No pip install required — the scripts must run
in any environment that has python3. (The `requests` package is intentionally NOT used.)

The normalized paper record (the lingua franca between scripts) looks like:

    {
      "source": "ads" | "arxiv" | "s2",
      "bibcode": "2024ApJ...963..129M" | null,   # ADS identifier (primary key when present)
      "arxiv_id": "2306.05448" | null,
      "doi": "10.3847/1538-4357/ad2345" | null,
      "title": "Little Red Dots: ...",
      "first_author": "Matthee, Jorryt",
      "author_count": 28,
      "year": 2024,
      "pubdate": "2024-03-00",
      "pub": "The Astrophysical Journal",
      "bibstem": "ApJ",
      "doctype": "article",
      "is_refereed": true,
      "is_review": false,            # heuristic: review venue OR title says "review"
      "is_infrastructure": false,    # matplotlib/numpy/astropy/... — drop from FOUNDATIONAL ranking
      "openaccess": true,
      "arxiv_url": "https://arxiv.org/abs/2306.05448" | null,
      "pdf_url": "https://arxiv.org/pdf/2306.05448" | null,   # legal full text when available
      "ads_url": "https://ui.adsabs.harvard.edu/abs/2024ApJ...963..129M" | null,
      "citation_count": 738 | null,
      "citation_count_norm": 26.36 | null,   # ADS author-normalized citations
      "read_count": 880 | null,              # ADS 90-day reads (a recency-of-attention signal)
      "citations_per_year": 369.0 | null,    # DERIVED age-normalized impact
      "abstract": "..." | null
    }
"""

import datetime
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request


# --------------------------------------------------------------------------------------
# Token resolution
# --------------------------------------------------------------------------------------
# A SciX token (scixplorer.org) and a classic ADS token are the same credential and both
# authenticate against api.adsabs.harvard.edu with `Authorization: Bearer <token>`.
TOKEN_ENV_VARS = ("NASA_ADS_API_TOKEN", "ADS_DEV_KEY", "ADS_API_TOKEN", "SCIX_API_TOKEN")


def resolve_token(token_file=None):
    """Return the ADS/SciX token from --token-file, then the env vars, then ~/.ads/dev_key.

    Returns None if nothing is found (callers decide whether that is fatal or a cue to fall back)."""
    if token_file and os.path.exists(token_file):
        return open(token_file).read().strip()
    for var in TOKEN_ENV_VARS:
        val = os.environ.get(var)
        if val and val.strip():
            return val.strip()
    classic = os.path.expanduser("~/.ads/dev_key")
    if os.path.exists(classic):
        return open(classic).read().strip()
    return None


class AuthError(RuntimeError):
    pass


class ApiError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------
# Polite HTTP with rate-limit + backoff handling
# --------------------------------------------------------------------------------------
def http_request(url, headers=None, data=None, method=None, timeout=40, max_retries=4,
                 verbose=True):
    """Make one HTTP request, returning (status, response_headers_dict, body_bytes).

    Handles the failure modes these public APIs actually exhibit:
      - 429 Too Many Requests  -> honor Retry-After / X-RateLimit-Reset, sleep (capped), retry.
      - 5xx server errors      -> exponential backoff, retry.
      - 401 / 403              -> raise AuthError immediately (a bad/missing token; retrying is futile).
      - other 4xx             -> raise ApiError with the server's message (usually a query syntax error).
    """
    headers = dict(headers or {})
    headers.setdefault("User-Agent", "astro-lit-review/1.0 (Claude Code skill; mailto:adshelp@cfa.harvard.edu)")
    attempt = 0
    while True:
        attempt += 1
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, hdrs, resp.read()
        except urllib.error.HTTPError as e:
            hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
            body = e.read() if hasattr(e, "read") else b""
            if e.code in (401, 403):
                raise AuthError(f"HTTP {e.code}: token missing/invalid or unauthorized. "
                                f"Body: {body[:300].decode('utf-8', 'replace')}")
            if e.code == 429 and attempt <= max_retries:
                wait = _retry_after_seconds(hdrs, default=20)
                if verbose:
                    sys.stderr.write(f"[rate-limit] 429; sleeping {wait}s (attempt {attempt}/{max_retries})\n")
                time.sleep(wait)
                continue
            if 500 <= e.code < 600 and attempt <= max_retries:
                wait = min(2 ** attempt, 30)
                if verbose:
                    sys.stderr.write(f"[server {e.code}] backoff {wait}s (attempt {attempt}/{max_retries})\n")
                time.sleep(wait)
                continue
            raise ApiError(f"HTTP {e.code} for {url}\n{body[:500].decode('utf-8', 'replace')}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            # A socket *read* timeout surfaces as a bare TimeoutError (not a URLError), and arXiv in
            # particular drops slow connections under load — so retry these with backoff too.
            reason = getattr(e, "reason", e)
            if attempt <= max_retries:
                wait = min(2 ** attempt, 30)
                if verbose:
                    sys.stderr.write(f"[network] {reason}; retry in {wait}s (attempt {attempt}/{max_retries})\n")
                time.sleep(wait)
                continue
            raise ApiError(f"Network error contacting {url}: {reason}")


def _retry_after_seconds(hdrs, default=20):
    ra = hdrs.get("retry-after")
    if ra:
        try:
            return min(int(float(ra)) + 1, 90)
        except ValueError:
            pass
    reset = hdrs.get("x-ratelimit-reset")
    if reset:
        try:
            delta = int(reset) - int(time.time())
            if 0 < delta < 90:
                return delta + 1
        except ValueError:
            pass
    return default


def get_json(url, headers=None, timeout=40, verbose=True):
    status, hdrs, body = http_request(url, headers=headers, timeout=timeout, verbose=verbose)
    return json.loads(body.decode("utf-8", "replace")), hdrs


def post_json(url, payload, headers=None, timeout=60, verbose=True):
    headers = dict(headers or {})
    headers["Content-Type"] = "application/json"
    data = json.dumps(payload).encode("utf-8")
    status, hdrs, body = http_request(url, headers=headers, data=data, method="POST",
                                      timeout=timeout, verbose=verbose)
    return json.loads(body.decode("utf-8", "replace")), hdrs


# --------------------------------------------------------------------------------------
# On-disk cache (keyed by a hash of the request)
# --------------------------------------------------------------------------------------
class Cache:
    """A trivial JSON file cache. Repeated identical requests during one research session (or
    across re-runs within the TTL) come back from disk instead of re-hitting the API. This is what
    lets the skill be re-run cheaply and stay under rate limits."""

    def __init__(self, cache_dir, ttl_days=7, enabled=True):
        self.dir = cache_dir
        self.ttl = ttl_days * 86400
        self.enabled = enabled
        if enabled and cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    def _path(self, key):
        return os.path.join(self.dir, key + ".json")

    def get(self, key):
        if not self.enabled:
            return None
        p = self._path(key)
        if not os.path.exists(p):
            return None
        if self.ttl and (time.time() - os.path.getmtime(p)) > self.ttl:
            return None
        try:
            with open(p) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def set(self, key, value):
        if not self.enabled or not self.dir:
            return
        try:
            with open(self._path(key), "w") as f:
                json.dump(value, f)
        except OSError:
            pass


def cache_key(*parts):
    raw = "\x1f".join(json.dumps(p, sort_keys=True, default=str) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


# --------------------------------------------------------------------------------------
# Record normalization + derived signals
# --------------------------------------------------------------------------------------
# Premier astronomy/physics REVIEW venues. A paper here is almost always a review article — the
# kind you bucket separately from primary research.
REVIEW_BIBSTEMS = {
    "ARA&A", "A&ARv", "NewAR", "PhR", "LRR", "LRSP", "LRCA", "SSRv", "RvMP", "PrPNP", "RPPh",
}
# Software / infrastructure papers. Hugely cited, never "foundational" to a SCIENCE topic — they
# pollute citation-sorted reference lists (matplotlib alone has >20k citations). Flagged so the
# ranker can drop them from the foundational lane. Kept deliberately TIGHT: method papers such as
# emcee or GALFIT are real science contributions and are NOT listed here.
INFRA_TITLE_MARKERS = (
    "matplotlib", "numpy array", "array programming with numpy", "the numpy array",
    "scipy 1.0", "scipy: ", "astropy project", "astropy package", "astropy community",
    "ipython", "jupyter", "pandas", "scikit-learn", "the python", "python package for",
)


def current_year():
    return datetime.date.today().year


def _clean_str(s):
    if s is None:
        return None
    if isinstance(s, list):
        s = s[0] if s else None
    if s is None:
        return None
    return html.unescape(re.sub(r"\s+", " ", str(s)).strip())


def derive(rec):
    """Fill the derived fields (citations_per_year, is_review, is_infrastructure, urls) on a record
    that already has the raw fields set. Idempotent."""
    yr = rec.get("year")
    cc = rec.get("citation_count")
    if yr and cc is not None:
        age = max(1, current_year() - int(yr) + 1)
        rec["citations_per_year"] = round(cc / age, 2)
    else:
        rec.setdefault("citations_per_year", None)

    bib = rec.get("bibstem") or ""
    title = (rec.get("title") or "").lower()
    rec["is_review"] = bool(
        bib in REVIEW_BIBSTEMS
        or rec.get("doctype") in ("review",)
        or title.startswith("a review")
        or " review of " in title
        or title.startswith("review of")
        or "annual review" in (rec.get("pub") or "").lower()
    )
    rec["is_infrastructure"] = bool(
        rec.get("doctype") == "software"
        or any(m in title for m in INFRA_TITLE_MARKERS)
    )

    if rec.get("arxiv_id"):
        rec.setdefault("arxiv_url", f"https://arxiv.org/abs/{rec['arxiv_id']}")
        rec.setdefault("pdf_url", f"https://arxiv.org/pdf/{rec['arxiv_id']}")
    if rec.get("bibcode"):
        rec.setdefault("ads_url", f"https://ui.adsabs.harvard.edu/abs/{rec['bibcode']}")
    return rec


def record_id(rec):
    """A stable de-dup key: prefer bibcode, then arXiv id, then DOI, then a slug of the title."""
    for k in ("bibcode", "arxiv_id", "doi"):
        if rec.get(k):
            return f"{k}:{str(rec[k]).lower()}"
    title = (rec.get("title") or "").lower()
    return "title:" + re.sub(r"[^a-z0-9]+", "", title)[:60]


def dedupe(records):
    """Merge records that refer to the same paper (e.g. an arXiv eprint + its published version, or
    the same paper from two sources). Keep the most information-rich version, preferring non-null
    citation data and an ADS bibcode."""
    by_id = {}
    # First pass: group by the strongest available identifier.
    for r in records:
        rid = record_id(r)
        if rid not in by_id:
            by_id[rid] = dict(r)
        else:
            _merge_into(by_id[rid], r)
    # Second pass: catch eprint/published splits that have different ids but the same arXiv id/DOI.
    merged = list(by_id.values())
    for key in ("arxiv_id", "doi"):
        seen = {}
        out = []
        for r in merged:
            v = r.get(key)
            v = str(v).lower() if v else None
            if v and v in seen:
                _merge_into(seen[v], r)
            else:
                out.append(r)
                if v:
                    seen[v] = r
        merged = out
    return merged


def _merge_into(base, other):
    # Prefer ADS (it has citation data); fill any missing field from the other record.
    if other.get("source") == "ads" and base.get("source") != "ads":
        for k, v in other.items():
            if v not in (None, "", []):
                base[k] = v
        return
    for k, v in other.items():
        if base.get(k) in (None, "", []) and v not in (None, "", []):
            base[k] = v


# --------------------------------------------------------------------------------------
# I/O helpers
# --------------------------------------------------------------------------------------
def load_records(path):
    """Load a list of records from a JSON file. Accepts either a bare list or {"records": [...]}."""
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, dict):
        return data.get("records") or data.get("docs") or []
    return data


def save_records(path, records, meta=None):
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump({"meta": meta or {}, "count": len(records), "records": records}, f, indent=1)


def short_authors(rec):
    fa = rec.get("first_author") or ""
    last = fa.split(",")[0].strip() if fa else "?"
    n = rec.get("author_count") or 0
    return f"{last} +{max(0, n-1)}" if n and n > 1 else last


def print_table(records, cols=None, limit=None, score_key=None):
    """Compact fixed-width table to stdout. cols is a list of (header, fn(rec)->str, width)."""
    if limit:
        records = records[:limit]
    if cols is None:
        cols = [
            ("#", lambda r, i=[0]: str(_counter(i)), 3),
            ("year", lambda r: str(r.get("year") or "?"), 4),
            ("author", short_authors, 16),
            ("cites", lambda r: _fmt(r.get("citation_count")), 6),
            ("c/yr", lambda r: _fmt(r.get("citations_per_year")), 6),
            ("typ", lambda r: ("REV" if r.get("is_review") else ("INFRA" if r.get("is_infrastructure") else r.get("doctype", "")[:5])), 5),
            ("title", lambda r: r.get("title") or "", 64),
        ]
        if score_key:
            cols.insert(1, ("score", lambda r: f"{r.get(score_key, 0):.3f}", 6))
    header = "  ".join(h.ljust(w)[:w] for h, _, w in cols)
    print(header)
    print("-" * len(header))
    for idx, r in enumerate(records, 1):
        cells = []
        for h, fn, w in cols:
            try:
                val = fn(r) if h != "#" else str(idx)
            except Exception:
                val = ""
            cells.append(str(val).ljust(w)[:w])
        print("  ".join(cells))


def _counter(box):
    box[0] += 1
    return box[0]


def _fmt(v):
    return "-" if v is None else (f"{v:.0f}" if isinstance(v, float) else str(v))
