#!/usr/bin/env python3
"""corpus.py — build the user's research profile from their ADS / SciX library.

This is the FOUNDATION of the daily arXiv digest. It reads every paper in the user's
public ADS/SciX library, fetches titles/abstracts/metadata, and for each paper finds a
"sibling set": topically similar papers from around the same time (ADS `similar()`
operator), with the user's OWN papers removed. The profile powers two things:

  1. Relevance ranking — the day's new arXiv papers are scored against what the user
     actually works on (their abstracts + keyword watchlist), not a guess.
  2. The "missed-citation radar" — if a new paper cites a *sibling* of the user's paper
     P but does NOT cite P, that's surfaced (the user is arguably under-cited there).

Why siblings instead of "papers similar to me, period": the radar only fires usefully
when a new paper had a clear opportunity to cite the user — i.e. it cited a *contemporary*
paper on the same topic. The time window (`--sibling-window`, default ±1 yr, measured by
arXiv APPEARANCE date — not publication year, which lags the preprint) encodes that.

Optionally, sibling mining is restricted to the user's LEAD-author papers: with
`author_name` + `sibling_max_author_rank` set (config or CLI), only papers where the user
appears within the first N authors get a sibling set — so middle-author collaboration
papers never set radar expectations. Relevance ranking still uses ALL library papers;
only the radar is narrowed.

Outputs (under <out-dir>/profile/):
  papers.json    normalized records for every library paper (incl. abstracts)
  siblings.json  {user_bibcode: [sibling records within the time window]}
  summary.md     human-readable overview of the profile

Reuses ads.py / _common.py (the NASA ADS / SciX client) from this same scripts dir, so
it inherits token resolution, on-disk caching, and polite rate-limit handling for free.

USAGE
  python3 corpus.py --config <config.json> --out-dir <data-dir>
  python3 corpus.py --library <url-or-id> --out-dir <data-dir> --sibling-window 2
  python3 corpus.py --config <config.json> --out-dir <data-dir> --max-papers 5   # quick test
"""
import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C   # noqa: E402
import ads as A       # noqa: E402  (reuse the ADS client's bigquery + run_query helpers)

ADS_BASE = "https://api.adsabs.harvard.edu/v1"
# Library papers want abstracts + keywords (for the relevance profile) and the full author
# list (for the lead-author restriction on sibling mining) on top of the usual metadata.
LIB_FIELDS = (A.DEFAULT_FIELDS + ",abstract,keyword,author")


def parse_library_id(s):
    """Accept a full scixplorer/ADS library URL or a bare library id."""
    s = (s or "").strip()
    m = re.search(r"/(?:public-libraries|libraries|user/libraries)/([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1)
    return s.rstrip("/").split("/")[-1]


def arxiv_appearance_index(rec):
    """Month index (year*12 + month) of a paper's FIRST arXiv appearance, derived from its arXiv id.

    "Within ±1 year of appearance date on arXiv" must use the arXiv posting date, not the
    publication year (which can lag the preprint by a year or two through refereeing). The arXiv id
    encodes the posting month: new-style `YYMM.NNNNN` (e.g. 2306.05448 -> 2023-06) and old-style
    `archive/YYMMNNN` (e.g. astro-ph/0309134 -> 2003-09). Falls back to mid-publication-year when no
    arXiv id is present; None if neither is available."""
    ax = rec.get("arxiv_id")
    if ax:
        m = re.match(r"(\d{2})(\d{2})\.\d", str(ax))            # new style YYMM.NNNNN
        if m and 1 <= int(m.group(2)) <= 12:
            return (2000 + int(m.group(1))) * 12 + (int(m.group(2)) - 1)
        m = re.search(r"/(\d{2})(\d{2})\d{3}", str(ax))         # old style archive/YYMMNNN
        if m and 1 <= int(m.group(2)) <= 12:
            yy = int(m.group(1))
            return ((1900 + yy) if yy >= 91 else (2000 + yy)) * 12 + (int(m.group(2)) - 1)
    y = rec.get("year")
    return (int(y) * 12 + 5) if y else None


def _name_key(s):
    """Normalize an author name to 'lastname f' (last name + first initial, ascii-folded),
    so "Shen, Xuejian" == "Shen, X." == "shen, xuejian"."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    last, _, first = s.partition(",")
    last = re.sub(r"[^a-z]+", " ", last.lower()).strip()
    first = first.strip().lower()
    return (last + " " + first[:1]).strip()


def author_rank(rec, name_key):
    """1-based position of the user in rec's author list, or None if absent."""
    for i, a in enumerate(rec.get("author") or [], 1):
        if _name_key(a) == name_key:
            return i
    return None


def fetch_library_bibcodes(token, lib_id, cache, refresh=False, verbose=True):
    """Page through the biblib API and return (bibcodes, library_name, total_count)."""
    out, start, rows = [], 0, 100
    name, total = None, None
    while True:
        params = {"rows": rows, "start": start}
        key = C.cache_key("biblib", lib_id, params)
        data = None if refresh else cache.get(key)
        if data is None:
            url = ADS_BASE + "/biblib/libraries/" + lib_id + "?" + urllib.parse.urlencode(params)
            data, _ = C.get_json(url, headers={"Authorization": "Bearer " + token}, verbose=verbose)
            cache.set(key, data)
        docs = data.get("documents") or []
        meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        name = name or meta.get("name")
        if meta.get("num_documents") is not None:
            total = meta.get("num_documents")
        out.extend(docs)
        start += rows
        if not docs or (total is not None and len(out) >= total):
            break
    seen, uniq = set(), []
    for b in out:
        if b and b not in seen:
            seen.add(b)
            uniq.append(b)
    return uniq, name, total


def fetch_siblings(token, paper, cache, rows, window_years, user_bibcodes, refresh=False, verbose=True):
    """ADS similar() for one paper A, kept only if a candidate B appeared on arXiv within ±window_years
    of A's arXiv appearance — and stripped of the user's own work (a "miss" must be someone else's B)."""
    b = paper.get("bibcode")
    if not b:
        return []
    pa = arxiv_appearance_index(paper)
    window_months = window_years * 12
    params = {"q": f"similar(bibcode:{b})", "fl": A.DEFAULT_FIELDS, "rows": rows, "sort": "score desc"}
    try:
        _, recs, _ = A.run_query(token, params, cache, refresh=refresh, verbose=verbose)
    except (C.ApiError, C.AuthError) as e:
        sys.stderr.write(f"[siblings] similar({b}) failed: {e}\n")
        return []
    sibs = []
    for r in recs:
        rb = r.get("bibcode")
        if not rb or rb == b or rb in user_bibcodes:
            continue  # skip self and the user's own papers — a "miss" must be someone else's paper
        ba = arxiv_appearance_index(r)
        if pa is not None and ba is not None and abs(ba - pa) > window_months:
            continue  # B must have appeared on arXiv within ±window_years of A
        sibs.append(r)
    return sibs


def write_summary(path, lib_name, papers, siblings, cfg, author_name=None, max_rank=0, not_mined=frozenset()):
    papers_sorted = sorted(papers, key=lambda r: (r.get("pubdate") or str(r.get("year") or "0")), reverse=True)
    n_sib = sum(len(v) for v in siblings.values())
    lines = [f"# Research profile — {lib_name or 'library'}", ""]
    lines.append(f"- Papers in library: **{len(papers)}**")
    lines.append(f"- Papers with a sibling set: **{sum(1 for v in siblings.values() if v)}**")
    lines.append(f"- Total sibling papers tracked (for the missed-citation radar): **{n_sib}**")
    if max_rank:
        lines.append(f"- Radar restricted to lead-author papers: **{author_name}** within the top **{max_rank}** authors "
                     f"({len(papers) - len(not_mined)} mined, {len(not_mined)} excluded)")
    if cfg.get("keywords"):
        lines.append(f"- Keyword watchlist: {', '.join(cfg['keywords'])}")
    lines += ["", "## Papers (most recent first)", ""]
    for r in papers_sorted:
        yr = r.get("year") or "?"
        nsib = len(siblings.get(r.get("bibcode"), []))
        title = r.get("title") or "(untitled)"
        tag = "not mined — author rank" if r.get("bibcode") in not_mined else f"{nsib} siblings"
        lines.append(f"- **{yr}** · {C.short_authors(r)} · {title}  _( {tag} )_")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    p = argparse.ArgumentParser(description="Build the user's research profile from an ADS/SciX library.")
    p.add_argument("--config", help="config.json (provides ads_library_id, sibling params, keywords)")
    p.add_argument("--library", help="ADS/SciX library URL or id (overrides config)")
    p.add_argument("--out-dir", default="./arxiv-digest-data", help="where to write profile/ and the cache")
    p.add_argument("--sibling-rows", type=int, help="similar() rows fetched per paper (default 25)")
    p.add_argument("--sibling-window", type=int, help="keep B if it appeared on arXiv within +/- this many years of A (default 1)")
    p.add_argument("--author-name", help='your name as ADS writes it, "Last, First" (overrides config author_name)')
    p.add_argument("--max-author-rank", type=int, help="mine siblings ONLY for papers where you are within the first N authors "
                                                       "(0 = all papers; overrides config sibling_max_author_rank)")
    p.add_argument("--max-papers", type=int, help="only process the first N library papers (for quick tests)")
    p.add_argument("--cache-dir", help="cache dir (default <out-dir>/.cache)")
    p.add_argument("--refresh", action="store_true", help="ignore cache and re-fetch")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    cfg = json.load(open(args.config)) if args.config else {}
    lib = args.library or cfg.get("ads_library_id")
    if not lib:
        sys.exit("corpus: need a library (use --library or set ads_library_id in --config).")
    sib_rows = args.sibling_rows or cfg.get("sibling_rows", 25)
    sib_window = args.sibling_window if args.sibling_window is not None else cfg.get("sibling_window_years", 1)
    author_name = args.author_name or cfg.get("author_name")
    max_rank = args.max_author_rank if args.max_author_rank is not None else int(cfg.get("sibling_max_author_rank") or 0)
    if max_rank and not author_name:
        sys.exit("corpus: sibling_max_author_rank is set but author_name is not (add it to config.json).")

    token = C.resolve_token()
    if not token:
        sys.exit("corpus: no ADS/SciX token (set NASA_ADS_API_TOKEN / SCIX_API_TOKEN).")

    out_dir = os.path.abspath(args.out_dir)
    prof_dir = os.path.join(out_dir, "profile")
    os.makedirs(prof_dir, exist_ok=True)
    cache = C.Cache(args.cache_dir or os.path.join(out_dir, ".cache"))
    verbose = not args.quiet
    lib_id = parse_library_id(lib)

    sys.stderr.write(f"[corpus] fetching library {lib_id} ...\n")
    bibcodes, lib_name, total = fetch_library_bibcodes(token, lib_id, cache, refresh=args.refresh, verbose=verbose)
    sys.stderr.write(f"[corpus] '{lib_name}': {len(bibcodes)} bibcodes (reported total {total}).\n")
    if not bibcodes:
        sys.exit("corpus: library returned no documents (is the id right / library public?).")

    user_bibcodes = set(bibcodes)
    sys.stderr.write("[corpus] fetching metadata + abstracts ...\n")
    papers = A._bigquery(token, bibcodes, LIB_FIELDS, cache, refresh=args.refresh, verbose=verbose)
    by_bib = {r.get("bibcode"): r for r in papers}
    # preserve library order; keep only papers we got metadata for
    papers = [by_bib[b] for b in bibcodes if b in by_bib]

    todo = papers[:args.max_papers] if args.max_papers else papers
    not_mined = set()
    if max_rank:
        nk = _name_key(author_name)
        not_mined = {p["bibcode"] for p in todo if (author_rank(p, nk) or 99) > max_rank}
        sys.stderr.write(f"[corpus] lead-author restriction: '{author_name}' is within the top {max_rank} authors on "
                         f"{len(todo) - len(not_mined)}/{len(todo)} papers; the rest get NO sibling set (radar won't fire for them).\n")
    sys.stderr.write(f"[corpus] finding siblings for {len(todo) - len(not_mined)} papers (window +/-{sib_window} yr, {sib_rows} rows each) ...\n")
    siblings = {}
    for i, paper in enumerate(todo, 1):
        sibs = [] if paper["bibcode"] in not_mined else fetch_siblings(
            token, paper, cache, sib_rows, sib_window, user_bibcodes,
            refresh=args.refresh, verbose=False)
        siblings[paper["bibcode"]] = sibs
        if verbose and (i % 10 == 0 or i == len(todo)):
            sys.stderr.write(f"[corpus]   {i}/{len(todo)} done\n")

    C.save_records(os.path.join(prof_dir, "papers.json"), papers,
                   meta={"library_id": lib_id, "library_name": lib_name, "count": len(papers)})
    with open(os.path.join(prof_dir, "siblings.json"), "w") as f:
        json.dump({"meta": {"library_id": lib_id, "window_years": sib_window, "rows": sib_rows,
                            "author_name": author_name, "max_author_rank": max_rank},
                   "siblings": {k: v for k, v in siblings.items()}}, f, indent=1)
    write_summary(os.path.join(prof_dir, "summary.md"), lib_name, papers, siblings, cfg,
                  author_name=author_name, max_rank=max_rank, not_mined=not_mined)

    n_sib = sum(len(v) for v in siblings.values())
    extra = f" ({len(not_mined)} papers excluded by the author-rank restriction)" if not_mined else ""
    print(f"[corpus] DONE: {len(papers)} papers, {n_sib} sibling papers across {sum(1 for v in siblings.values() if v)} "
          f"papers with siblings{extra}.\n  profile -> {prof_dir}")


if __name__ == "__main__":
    main()
