#!/usr/bin/env python3
"""ads.py — NASA ADS / SciX client for the astro-lit-review skill.

This is the PRIMARY discovery tool. It wraps the ADS search API (https://api.adsabs.harvard.edu)
so the skill can search the literature, walk the citation graph (citations / references), fetch
metadata for a known set of papers, and export BibTeX — all with on-disk caching and polite
rate-limit handling, and all emitting the normalized record schema defined in _common.py so the
output feeds straight into rank.py.

A SciX token (from scixplorer.org) and a classic ADS token are interchangeable; both use
`Authorization: Bearer <token>`. The token is read from the environment (NASA_ADS_API_TOKEN and a
few aliases) or a --token-file. It is NEVER written to disk by this script.

SUBCOMMANDS
  check                          Validate the token and print the daily rate-limit budget.
  search   --q '<ADS query>'     Search. --sort, --fq (repeatable), --rows, --abstract, --refs.
  citations --bibcode <bibcode>  Papers that CITE this one (q=citations(bibcode:...)).
  references --bibcode <bibcode> Papers this one CITES (q=references(bibcode:...)).
  get      --bibcodes a,b,c      Fetch metadata for a known set (uses bigquery for large sets).
  bibtex   --bibcodes a,b,c      Export BibTeX (also accepts --from <records.json>).

EXAMPLES
  python3 ads.py check
  python3 ads.py search --q '"little red dots" AND abs:"black hole"' --sort 'citation_count desc' --rows 40 --out raw/lrd_core.json
  python3 ads.py search --q 'abs:"AGN feedback"' --fq 'bibstem:(ARA&A OR A&ARv OR PhR)' --sort 'citation_count desc'   # reviews only
  python3 ads.py references --bibcode 2024ApJ...963..129M --sort 'citation_count desc' --out raw/matthee_refs.json
  python3 ads.py citations  --bibcode 2024ApJ...963..129M --sort 'date desc' --rows 60
  python3 ads.py bibtex --from selected/core.json --out report/core.bib

See references/ads-api.md for the query language, the field list, and the second-order operators.
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

ADS_BASE = "https://api.adsabs.harvard.edu/v1"
DEFAULT_FIELDS = ("bibcode,title,first_author,author_count,year,pubdate,pub,bibstem,doctype,"
                  "citation_count,citation_count_norm,read_count,esources,property,doi,identifier")


# --------------------------------------------------------------------------------------
# ADS doc -> normalized record
# --------------------------------------------------------------------------------------
def _extract_arxiv(identifiers, dois):
    for ident in identifiers or []:
        s = str(ident)
        if s.lower().startswith("arxiv:"):
            return s.split(":", 1)[1]
    for d in (dois or []):
        m = re.search(r"arxiv\.(\d{4}\.\d{4,5})", str(d), re.I)
        if m:
            return m.group(1)
    for ident in identifiers or []:
        m = re.search(r"\b(\d{4}\.\d{4,5})\b", str(ident))
        if m:
            return m.group(1)
        m = re.search(r"\b((?:astro-ph|hep-ph|hep-th|gr-qc|astro-ph\.[A-Z]{2})/\d{7})\b", str(ident))
        if m:
            return m.group(1)
    return None


def normalize_ads(doc):
    props = doc.get("property", []) or []
    bibstem = doc.get("bibstem") or []
    rec = {
        "source": "ads",
        "bibcode": doc.get("bibcode"),
        "title": C._clean_str(doc.get("title")),
        "first_author": doc.get("first_author"),
        "author_count": doc.get("author_count"),
        "year": int(doc["year"]) if doc.get("year") else None,
        "pubdate": doc.get("pubdate"),
        "pub": doc.get("pub"),
        "bibstem": bibstem[0] if bibstem else None,
        "doctype": doc.get("doctype"),
        "citation_count": doc.get("citation_count"),
        "citation_count_norm": round(doc["citation_count_norm"], 2) if doc.get("citation_count_norm") is not None else None,
        "read_count": doc.get("read_count"),
        "doi": (doc.get("doi") or [None])[0],
        "arxiv_id": _extract_arxiv(doc.get("identifier"), doc.get("doi")),
        "is_refereed": "REFEREED" in props,
        "openaccess": ("OPENACCESS" in props) or ("EPRINT_OPENACCESS" in props),
        "abstract": C._clean_str(doc.get("abstract")) if doc.get("abstract") else None,
    }
    if doc.get("reference"):
        rec["reference"] = doc["reference"]          # list of bibcodes this paper cites
    if doc.get("keyword"):
        rec["keyword"] = doc["keyword"]
    return C.derive(rec)


# --------------------------------------------------------------------------------------
# Query execution (with cache)
# --------------------------------------------------------------------------------------
def _auth(token):
    return {"Authorization": "Bearer " + token}


def run_query(token, params, cache, refresh=False, verbose=True):
    """Execute one /search/query call. Returns (numFound, [records], rate_remaining)."""
    import urllib.parse
    key = C.cache_key("ads.search", params)
    if not refresh:
        hit = cache.get(key)
        if hit is not None:
            return hit["numFound"], hit["records"], hit.get("rate_remaining")
    url = ADS_BASE + "/search/query?" + urllib.parse.urlencode(params)
    data, hdrs = C.get_json(url, headers=_auth(token), verbose=verbose)
    docs = data.get("response", {}).get("docs", [])
    num = data.get("response", {}).get("numFound", len(docs))
    records = [normalize_ads(d) for d in docs]
    rate = hdrs.get("x-ratelimit-remaining")
    cache.set(key, {"numFound": num, "records": records, "rate_remaining": rate})
    return num, records, rate


def cmd_search(args, token, cache):
    fields = args.fields or DEFAULT_FIELDS
    if args.abstract and "abstract" not in fields:
        fields += ",abstract,keyword"
    if args.refs and "reference" not in fields:
        fields += ",reference"
    params = {"q": args.q, "fl": fields, "rows": args.rows, "start": args.start, "sort": args.sort}
    if args.fq:
        # urlencode handles a list value by repeating the key, which is what ADS wants for multiple fq.
        params_list = list(params.items()) + [("fq", f) for f in args.fq]
        import urllib.parse
        key = C.cache_key("ads.search", params_list)
        url = ADS_BASE + "/search/query?" + urllib.parse.urlencode(params_list)
        if args.refresh or cache.get(key) is None:
            data, hdrs = C.get_json(url, headers=_auth(token), verbose=not args.quiet)
            docs = data.get("response", {}).get("docs", [])
            num = data.get("response", {}).get("numFound", len(docs))
            records = [normalize_ads(d) for d in docs]
            cache.set(key, {"numFound": num, "records": records, "rate_remaining": hdrs.get("x-ratelimit-remaining")})
        else:
            hit = cache.get(key)
            num, records = hit["numFound"], hit["records"]
    else:
        num, records, _ = run_query(token, params, cache, refresh=args.refresh, verbose=not args.quiet)
    _emit(args, records, num=num, label=f"search q={args.q!r} sort={args.sort!r}")


def cmd_citations(args, token, cache):
    _second_order(args, token, cache, "citations")


def cmd_references(args, token, cache):
    _second_order(args, token, cache, "references")


def _second_order(args, token, cache, op):
    fields = args.fields or DEFAULT_FIELDS
    if args.abstract and "abstract" not in fields:
        fields += ",abstract"
    params = {"q": f"{op}(bibcode:{args.bibcode})", "fl": fields,
              "rows": args.rows, "start": args.start, "sort": args.sort}
    num, records, _ = run_query(token, params, cache, refresh=args.refresh, verbose=not args.quiet)
    _emit(args, records, num=num, label=f"{op}({args.bibcode}) sort={args.sort!r}")


def cmd_get(args, token, cache):
    """Fetch metadata for a known set of bibcodes (bigquery for large sets)."""
    bibcodes = _collect_bibcodes(args)
    if not bibcodes:
        sys.exit("get: no bibcodes (use --bibcodes a,b,c or --from records.json)")
    fields = args.fields or DEFAULT_FIELDS
    records = []
    if len(bibcodes) <= 25:
        q = "bibcode:(" + " OR ".join(bibcodes) + ")"
        _, records, _ = run_query(token, {"q": q, "fl": fields, "rows": len(bibcodes),
                                          "sort": "citation_count desc"}, cache,
                                  refresh=args.refresh, verbose=not args.quiet)
    else:
        records = _bigquery(token, bibcodes, fields, cache, refresh=args.refresh, verbose=not args.quiet)
    _emit(args, records, num=len(records), label=f"get {len(bibcodes)} bibcodes")


def _bigquery(token, bibcodes, fields, cache, refresh=False, verbose=True):
    import urllib.parse
    key = C.cache_key("ads.bigquery", sorted(bibcodes), fields)
    if not refresh:
        hit = cache.get(key)
        if hit is not None:
            return hit["records"]
    params = {"q": "*:*", "fl": fields, "rows": len(bibcodes), "sort": "citation_count desc"}
    url = ADS_BASE + "/search/bigquery?" + urllib.parse.urlencode(params)
    body = ("bibcode\n" + "\n".join(bibcodes)).encode("utf-8")
    headers = {**_auth(token), "Content-Type": "big-query/csv"}
    _, _, raw = C.http_request(url, headers=headers, data=body, method="POST", verbose=verbose)
    import json
    data = json.loads(raw.decode("utf-8", "replace"))
    records = [normalize_ads(d) for d in data.get("response", {}).get("docs", [])]
    cache.set(key, {"records": records})
    return records


def cmd_expand(args, token, cache):
    """Citation-graph expansion over MANY anchors at once — the recall workhorse.

    Given a set of anchor bibcodes (the field's reviews + foundational + top recent papers, e.g.
    --from selected/core.json), pull each one's references() and/or citations() and merge into one
    de-duplicated pool. This is what reaches the parent-mechanism roots and the expert-curated
    bibliographies that a topic-phrase search alone misses (see references/search-and-ranking.md)."""
    bibs = _collect_bibcodes(args)
    if not bibs:
        sys.exit("expand: no anchor bibcodes (use --from records.json or --bibcodes a,b,c)")
    if args.max_anchors and len(bibs) > args.max_anchors:
        sys.stderr.write(f"[expand] {len(bibs)} anchors given; using the first {args.max_anchors} "
                         f"(raise --max-anchors to go deeper). DROPPED {len(bibs)-args.max_anchors}.\n")
        bibs = bibs[:args.max_anchors]
    ops = {"references": ["references"], "citations": ["citations"],
           "both": ["references", "citations"]}[args.mode]
    fields = args.fields or DEFAULT_FIELDS
    if args.abstract and "abstract" not in fields:
        fields += ",abstract"
    # relevance ('score') is degenerate for an operator query with no free text; cite-sort by default.
    sort = "citation_count desc" if args.sort == "score desc" else args.sort
    seen = {}
    for b in bibs:
        for op in ops:
            try:
                _, recs, _ = run_query(token, {"q": f"{op}(bibcode:{b})", "fl": fields,
                                               "rows": args.per, "sort": sort},
                                       cache, refresh=args.refresh, verbose=not args.quiet)
            except C.ApiError as e:
                sys.stderr.write(f"[expand] {op}({b}) failed: {e}\n")
                continue
            for r in recs:
                if r.get("bibcode"):
                    seen[r["bibcode"]] = r
    records = list(seen.values())
    _emit(args, records, num=len(records),
          label=f"expand {args.mode} of {len(bibs)} anchors -> {len(records)} unique")


def cmd_bibtex(args, token, cache):
    bibcodes = _collect_bibcodes(args)
    if not bibcodes:
        sys.exit("bibtex: no bibcodes (use --bibcodes a,b,c or --from records.json)")
    key = C.cache_key("ads.bibtex", sorted(bibcodes))
    bib = None if args.refresh else cache.get(key)
    if bib is None:
        data, _ = C.post_json(ADS_BASE + "/export/bibtex", {"bibcode": bibcodes},
                              headers=_auth(token), verbose=not args.quiet)
        bib = data.get("export", "")
        cache.set(key, bib)
    elif isinstance(bib, dict):
        bib = bib.get("export", "")
    if args.out:
        with open(args.out, "w") as f:
            f.write(bib)
        sys.stderr.write(f"[bibtex] {len(bibcodes)} entries -> {args.out}\n")
    else:
        print(bib)


def cmd_check(args, token, cache):
    import urllib.parse
    url = ADS_BASE + "/search/query?" + urllib.parse.urlencode({"q": "*:*", "rows": 1, "fl": "bibcode"})
    try:
        _, hdrs, _ = C.http_request(url, headers=_auth(token), verbose=False)
    except C.AuthError as e:
        sys.exit(f"[FAIL] ADS token rejected: {e}")
    print("[OK] ADS/SciX token is valid.")
    print(f"  Daily limit ...... {hdrs.get('x-ratelimit-limit', '?')}")
    print(f"  Remaining today .. {hdrs.get('x-ratelimit-remaining', '?')}")
    reset = hdrs.get("x-ratelimit-reset")
    if reset:
        import datetime
        print(f"  Resets at ........ {datetime.datetime.fromtimestamp(int(reset))}")


# --------------------------------------------------------------------------------------
# Shared emit / arg plumbing
# --------------------------------------------------------------------------------------
def _collect_bibcodes(args):
    out = []
    if args.bibcodes:
        out += [b.strip() for b in args.bibcodes.split(",") if b.strip()]
    if getattr(args, "from_file", None):
        for r in C.load_records(args.from_file):
            if isinstance(r, str):
                out.append(r)
            elif r.get("bibcode"):
                out.append(r["bibcode"])
    # de-dup, preserve order
    seen, uniq = set(), []
    for b in out:
        if b not in seen:
            seen.add(b)
            uniq.append(b)
    return uniq


def _emit(args, records, num=None, label=""):
    if args.out:
        C.save_records(args.out, records, meta={"query": label, "numFound": num})
        sys.stderr.write(f"[ads] {label}: {len(records)} records (of {num} found) -> {args.out}\n")
    if args.json:
        import json
        print(json.dumps(records, indent=1))
    else:
        if num is not None:
            print(f"# {label}  |  {len(records)} shown of {num} found")
        C.print_table(records)


def build_parser():
    # Common options live on a parent parser so they can be given AFTER the subcommand, which is the
    # natural order (e.g. `ads.py search --q ... --out raw/x.json`). zsh does not word-split unquoted
    # vars, so each invocation should spell its flags out rather than bundling them in a shell var.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--token-file", help="file containing the ADS/SciX token (else read from env)")
    common.add_argument("--cache-dir", default=os.environ.get("ALR_CACHE_DIR", "./.alr_cache"))
    common.add_argument("--ttl-days", type=int, default=7)
    common.add_argument("--no-cache", action="store_true")
    common.add_argument("--refresh", action="store_true", help="ignore cache for this call, then refresh it")
    common.add_argument("--json", action="store_true", help="print full JSON records to stdout")
    common.add_argument("--out", help="save normalized records (or .bib) to this path")
    common.add_argument("--quiet", action="store_true")

    p = argparse.ArgumentParser(description="NASA ADS / SciX client for astro-lit-review.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_query(sp, need_q=False, need_bib=False, bibcodes=False):
        if need_q:
            sp.add_argument("--q", required=True, help="raw ADS query (see references/ads-api.md)")
        if need_bib:
            sp.add_argument("--bibcode", required=True)
        if bibcodes:
            sp.add_argument("--bibcodes", help="comma-separated bibcodes")
            sp.add_argument("--from", dest="from_file", help="records JSON to pull bibcodes from")
        sp.add_argument("--fields", help="override the ADS fl field list")
        sp.add_argument("--rows", type=int, default=30)
        sp.add_argument("--start", type=int, default=0)
        sp.add_argument("--sort", default="score desc",
                        help="ADS sort, e.g. 'citation_count desc', 'classic_factor desc', 'date desc'")
        sp.add_argument("--abstract", action="store_true", help="also fetch abstracts (+keywords)")

    sub.add_parser("check", parents=[common])
    sp = sub.add_parser("search", parents=[common]); add_query(sp, need_q=True)
    sp.add_argument("--fq", action="append", help="filter query (repeatable), e.g. property:refereed")
    sp.add_argument("--refs", action="store_true", help="also fetch each paper's reference list (large)")
    sp = sub.add_parser("citations", parents=[common]); add_query(sp, need_bib=True)
    sp = sub.add_parser("references", parents=[common]); add_query(sp, need_bib=True)
    sp = sub.add_parser("get", parents=[common]); add_query(sp, bibcodes=True)
    sp = sub.add_parser("bibtex", parents=[common]); add_query(sp, bibcodes=True)
    sp = sub.add_parser("expand", parents=[common]); add_query(sp, bibcodes=True)
    sp.add_argument("--mode", choices=["references", "citations", "both"], default="references",
                    help="references() = roots (recall of foundations); citations() = descendants (recent)")
    sp.add_argument("--per", type=int, default=60, help="rows fetched per anchor")
    sp.add_argument("--max-anchors", type=int, default=30, help="cap anchors processed (cost guard)")
    return p


def main():
    args = build_parser().parse_args()
    cache = C.Cache(args.cache_dir, ttl_days=args.ttl_days, enabled=not args.no_cache)
    token = C.resolve_token(args.token_file)
    if not token:
        sys.exit("ERROR: no ADS/SciX token found.\n"
                 "  Set one of: " + ", ".join(C.TOKEN_ENV_VARS) + "  (export NASA_ADS_API_TOKEN=...)\n"
                 "  Get a free token at https://ui.adsabs.harvard.edu/user/settings/token\n"
                 "  No token? Use scripts/fallback.py (arXiv + Semantic Scholar) instead.")
    handler = {"check": cmd_check, "search": cmd_search, "citations": cmd_citations,
               "references": cmd_references, "get": cmd_get, "bibtex": cmd_bibtex,
               "expand": cmd_expand}[args.cmd]
    try:
        handler(args, token, cache)
    except C.AuthError as e:
        sys.exit(f"AUTH ERROR: {e}\n  The token was rejected. Check NASA_ADS_API_TOKEN.")
    except C.ApiError as e:
        sys.exit(f"API ERROR: {e}")


if __name__ == "__main__":
    main()
