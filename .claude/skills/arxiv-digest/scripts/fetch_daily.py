#!/usr/bin/env python3
"""fetch_daily.py — pull the day's new arXiv submissions for the configured categories.

The discovery source for the daily digest. arXiv (not ADS) is the right source for "what
posted today" because ADS indexes new eprints with a lag of a day or more. This wraps the
arXiv API (via fallback.py), pulls the most recent submissions across the configured
categories, de-duplicates cross-listings, and keeps only papers in the recency window.

Note on arXiv timing: papers submitted before the daily deadline are announced the next
business day, so an 8 a.m. digest typically wants a 1-2 day `lookback` to catch the latest
announced batch (weekends bunch up). Tune `lookback_days` in config; dedup against prior
runs can be layered on later.

Output (under <out-dir>/daily/<date>/):
  papers.json   normalized arXiv records (arxiv_id, title, authors, abstract, pdf_url)

USAGE
  python3 fetch_daily.py --config config.json --out-dir <data-dir>
  python3 fetch_daily.py --config config.json --out-dir <data-dir> --date 2026-06-06 --lookback-days 3
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C       # noqa: E402
import fallback as F      # noqa: E402  (reuse the arXiv client)


def window_bounds(anchor, lookback_days):
    """Inclusive [lo, hi] YYYY-MM-DD bounds covering `lookback_days` ending at the anchor date."""
    lo = (anchor - datetime.timedelta(days=max(0, lookback_days - 1))).isoformat()
    return lo, anchor.isoformat()


def main():
    p = argparse.ArgumentParser(description="Fetch the day's new arXiv papers for the configured categories.")
    p.add_argument("--config", help="config.json (arxiv_categories, lookback_days)")
    p.add_argument("--out-dir", default="./arxiv-digest-data")
    p.add_argument("--categories", help="comma-separated arXiv categories (overrides config)")
    p.add_argument("--date", help="reference date YYYY-MM-DD (default: today)")
    p.add_argument("--lookback-days", type=int, help="keep papers submitted within this many days (default 2)")
    p.add_argument("--max", type=int, default=500, help="max recent papers to pull before date-filtering")
    p.add_argument("--cache-dir")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    cfg = json.load(open(args.config)) if args.config else {}
    cats = (args.categories.split(",") if args.categories else cfg.get("arxiv_categories")
            or ["astro-ph.GA", "astro-ph.CO"])
    cats = [c.strip() for c in cats if c.strip()]
    lookback = args.lookback_days if args.lookback_days is not None else cfg.get("lookback_days", 1)
    out_dir = os.path.abspath(args.out_dir)
    cache = C.Cache(args.cache_dir or os.path.join(out_dir, ".cache"))
    verbose = not args.quiet

    query = " OR ".join(f"cat:{c}" for c in cats)
    sys.stderr.write(f"[daily] arXiv query: {query}\n")
    _, records = F.arxiv_search(query, args.max, "date", cache, refresh=args.refresh, verbose=verbose)
    fetched_dates = sorted((r.get("pubdate") or "")[:10] for r in records if r.get("pubdate"))

    # Anchor the window to the FRESHEST available arXiv date, not wall-clock "today": arXiv has no
    # weekend submissions, so on a Monday "today" holds no papers while Friday's batch is the real
    # new set. An explicit --date overrides (for reproducing a historical day).
    if args.date:
        anchor = datetime.date.fromisoformat(args.date)
    elif fetched_dates:
        anchor = datetime.date.fromisoformat(fetched_dates[-1])
    else:
        anchor = datetime.date.today()
    lo, hi = window_bounds(anchor, lookback)
    sys.stderr.write(f"[daily] anchor {hi} (freshest available), keeping submitted in [{lo} .. {hi}], "
                     f"lookback {lookback}d\n")

    day_dir = os.path.join(out_dir, "daily", anchor.isoformat())
    os.makedirs(day_dir, exist_ok=True)

    # Keep only the recency window, then de-dup cross-listed papers (same arXiv id in 2 categories).
    kept = [r for r in records if lo <= (r.get("pubdate") or "")[:10] <= hi]
    kept = C.dedupe(kept)
    kept.sort(key=lambda r: (r.get("pubdate") or ""), reverse=True)

    out_path = os.path.join(day_dir, "papers.json")
    C.save_records(out_path, kept, meta={"categories": cats, "anchor": anchor.isoformat(),
                                         "window": [lo, hi], "n_fetched": len(records)})
    print(f"[daily] {len(kept)} new papers for {anchor.isoformat()} (window {lo}..{hi}, "
          f"of {len(records)} recent fetched) across {len(cats)} categories -> {out_path}")
    if kept and verbose:
        C.print_table(kept, limit=12)
    # Truncation guard: if we hit --max AND the window's lower bound reaches past the oldest paper we
    # fetched, a busy span may be cut off — raise --max.
    if len(records) >= args.max and fetched_dates and lo < fetched_dates[0]:
        sys.stderr.write(f"[daily] WARNING: hit --max={args.max} and window start {lo} predates oldest "
                         f"fetched {fetched_dates[0]}; raise --max for a busy multi-day window.\n")


if __name__ == "__main__":
    main()
