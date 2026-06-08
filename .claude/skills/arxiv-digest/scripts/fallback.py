#!/usr/bin/env python3
"""fallback.py — tokenless literature search for when no NASA ADS / SciX token is available.

ADS is the primary source (scripts/ads.py). When no token is configured, this script keeps the
skill usable by combining two free, key-less APIs:

  * arXiv API  (export.arxiv.org/api/query) — excellent for DISCOVERY and abstracts in astro/physics,
    but it has NO citation counts.
  * Semantic Scholar Graph API (api.semanticscholar.org) — supplies citation_count,
    influentialCitationCount, and the citation/reference graph, which arXiv lacks.

It emits the SAME normalized record schema as ads.py, so rank.py treats the output identically.

LIMITATIONS to be honest about in the report:
  * Coverage and citation counts are less complete/authoritative than ADS.
  * Semantic Scholar without an API key is sharply rate-limited (HTTP 429 is common). Set
    S2_API_KEY in the environment to raise the limit. This script backs off politely either way.
  * arXiv only indexes preprints, so published-only or pre-2000 literature is under-represented.

SUBCOMMANDS
  search    --topic '<words>'    arXiv search (--q for a raw arXiv query). --enrich adds S2 citations.
  enrich    --from records.json  Add S2 citation_count/influentialCitationCount to existing records.
  citations --arxiv <id>         Papers citing this one (Semantic Scholar).
  references --arxiv <id>        Papers this one cites (Semantic Scholar).
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

ARXIV_API = "https://export.arxiv.org/api/query"
S2_BASE = "https://api.semanticscholar.org/graph/v1"
NS = {"a": "http://www.w3.org/2005/Atom",
      "arxiv": "http://arxiv.org/schemas/atom",
      "opensearch": "http://a9.com/-/spec/opensearch/1.1/"}


# --------------------------------------------------------------------------------------
# arXiv
# --------------------------------------------------------------------------------------
def arxiv_search(query, rows, sort, cache, refresh=False, verbose=True):
    sort_map = {"relevance": "relevance", "date": "submittedDate",
                "submittedDate": "submittedDate", "lastUpdated": "lastUpdatedDate"}
    params = {"search_query": query, "start": 0, "max_results": rows,
              "sortBy": sort_map.get(sort, "relevance"), "sortOrder": "descending"}
    key = C.cache_key("arxiv.search", params)
    if not refresh:
        hit = cache.get(key)
        if hit is not None:
            return hit["total"], hit["records"]
    url = ARXIV_API + "?" + urllib.parse.urlencode(params)
    _, _, body = C.http_request(url, verbose=verbose)
    total, records = _parse_arxiv(body)
    cache.set(key, {"total": total, "records": records})
    return total, records


def _parse_arxiv(xml_bytes):
    root = ET.fromstring(xml_bytes)
    total = int(root.findtext("opensearch:totalResults", default="0", namespaces=NS) or 0)
    records = []
    for e in root.findall("a:entry", NS):
        idurl = (e.findtext("a:id", default="", namespaces=NS) or "").strip()
        axid = idurl.rsplit("/abs/", 1)[-1]
        axid = axid.split("v")[0] if axid and axid[0].isdigit() else axid  # strip version on new-style
        authors = [a.findtext("a:name", namespaces=NS) for a in e.findall("a:author", NS)]
        published = (e.findtext("a:published", default="", namespaces=NS) or "")
        prim = e.find("arxiv:primary_category", NS)
        pdf = None
        for ln in e.findall("a:link", NS):
            if ln.get("title") == "pdf":
                pdf = ln.get("href")
        rec = {
            "source": "arxiv",
            "bibcode": None,
            "arxiv_id": axid or None,
            "doi": e.findtext("arxiv:doi", namespaces=NS),
            "title": C._clean_str(e.findtext("a:title", namespaces=NS)),
            "first_author": authors[0] if authors else None,
            "author_count": len(authors),
            "year": int(published[:4]) if published[:4].isdigit() else None,
            "pubdate": published[:10] or None,
            "pub": "arXiv (preprint)",
            "bibstem": "arXiv",
            "arxiv_category": (prim.get("term") if prim is not None else None),
            "doctype": "eprint",
            "citation_count": None,
            "read_count": None,
            "is_refereed": False,
            "openaccess": True,
            "abstract": C._clean_str(e.findtext("a:summary", namespaces=NS)),
            "pdf_url": pdf,
        }
        records.append(C.derive(rec))
    return total, records


# --------------------------------------------------------------------------------------
# Semantic Scholar
# --------------------------------------------------------------------------------------
def _s2_headers():
    h = {}
    if os.environ.get("S2_API_KEY"):
        h["x-api-key"] = os.environ["S2_API_KEY"]
    return h


S2_FIELDS = "title,year,citationCount,influentialCitationCount,externalIds,venue,publicationTypes,authors"


def s2_enrich(records, cache, refresh=False, verbose=True):
    """Add citation data from S2 to records that have an arXiv id or DOI, via the batch endpoint."""
    ids, idx = [], []
    for i, r in enumerate(records):
        if r.get("arxiv_id"):
            ids.append("ARXIV:" + r["arxiv_id"]); idx.append(i)
        elif r.get("doi"):
            ids.append("DOI:" + r["doi"]); idx.append(i)
    for chunk_start in range(0, len(ids), 100):
        chunk = ids[chunk_start:chunk_start + 100]
        cidx = idx[chunk_start:chunk_start + 100]
        key = C.cache_key("s2.batch", chunk, S2_FIELDS)
        data = None if refresh else cache.get(key)
        if data is None:
            url = S2_BASE + "/paper/batch?" + urllib.parse.urlencode({"fields": S2_FIELDS})
            try:
                data, _ = C.post_json(url, {"ids": chunk}, headers=_s2_headers(), verbose=verbose)
                cache.set(key, data)
                time.sleep(1.0)  # be polite to the keyless endpoint
            except (C.ApiError, C.AuthError) as e:
                sys.stderr.write(f"[s2] enrich failed for a batch: {e}\n")
                continue
        for r_i, item in zip(cidx, data):
            if item:
                _apply_s2(records[r_i], item)
    return records


def _apply_s2(rec, item):
    if item.get("citationCount") is not None:
        rec["citation_count"] = item["citationCount"]
    if item.get("influentialCitationCount") is not None:
        rec["influential_citations"] = item["influentialCitationCount"]
    if item.get("venue") and not rec.get("pub"):
        rec["pub"] = item["venue"]
    if "Review" in (item.get("publicationTypes") or []):
        rec["is_review"] = True
    C.derive(rec)


def _s2_record_from_node(node):
    ext = node.get("externalIds") or {}
    authors = node.get("authors") or []
    rec = {
        "source": "s2",
        "bibcode": None,
        "arxiv_id": ext.get("ArXiv"),
        "doi": ext.get("DOI"),
        "title": C._clean_str(node.get("title")),
        "first_author": (authors[0]["name"] if authors else None),
        "author_count": len(authors) or None,
        "year": node.get("year"),
        "pub": node.get("venue"),
        "bibstem": None,
        "doctype": "review" if "Review" in (node.get("publicationTypes") or []) else "article",
        "citation_count": node.get("citationCount"),
        "influential_citations": node.get("influentialCitationCount"),
        "is_refereed": True,
        "openaccess": bool(ext.get("ArXiv")),
        "abstract": None,
    }
    return C.derive(rec)


def s2_graph(arxiv_id, direction, rows, cache, refresh=False, verbose=True):
    """direction='citations' or 'references'."""
    fields = "citationCount,influentialCitationCount,title,year,externalIds,venue,publicationTypes,authors"
    sub = "citingPaper" if direction == "citations" else "citedPaper"
    key = C.cache_key("s2.graph", arxiv_id, direction, rows, fields)
    data = None if refresh else cache.get(key)
    if data is None:
        url = (f"{S2_BASE}/paper/ARXIV:{arxiv_id}/{direction}?"
               + urllib.parse.urlencode({"fields": fields, "limit": min(rows, 1000)}))
        data, _ = C.get_json(url, headers=_s2_headers(), verbose=verbose)
        cache.set(key, data)
    return [_s2_record_from_node(it[sub]) for it in data.get("data", []) if it.get(sub)]


# --------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------
def cmd_search(args, cache):
    q = args.q or (f'abs:"{args.topic}"' if args.topic else None)
    if not q:
        sys.exit("search: provide --topic '<words>' or --q '<raw arXiv query>'")
    total, records = arxiv_search(q, args.rows, args.sort, cache, refresh=args.refresh, verbose=not args.quiet)
    if args.enrich:
        records = s2_enrich(records, cache, refresh=args.refresh, verbose=not args.quiet)
    _emit(args, records, num=total, label=f"arXiv {q!r}" + (" +S2" if args.enrich else ""))


def cmd_enrich(args, cache):
    records = C.load_records(args.from_file)
    records = s2_enrich(records, cache, refresh=args.refresh, verbose=not args.quiet)
    _emit(args, records, num=len(records), label=f"S2-enriched {args.from_file}")


def cmd_citations(args, cache):
    records = s2_graph(args.arxiv, "citations", args.rows, cache, refresh=args.refresh, verbose=not args.quiet)
    _emit(args, records, num=len(records), label=f"S2 citations(arXiv:{args.arxiv})")


def cmd_references(args, cache):
    records = s2_graph(args.arxiv, "references", args.rows, cache, refresh=args.refresh, verbose=not args.quiet)
    _emit(args, records, num=len(records), label=f"S2 references(arXiv:{args.arxiv})")


def _emit(args, records, num=None, label=""):
    if args.out:
        C.save_records(args.out, records, meta={"query": label, "numFound": num})
        sys.stderr.write(f"[fallback] {label}: {len(records)} records -> {args.out}\n")
    if args.json:
        print(json.dumps(records, indent=1))
    else:
        print(f"# {label}  |  {len(records)} records"
              + (f" (of ~{num} on arXiv)" if num else ""))
        C.print_table(records)
        if any(r.get("citation_count") is None for r in records) and not args.enrich:
            sys.stderr.write("[note] no citation counts (arXiv only). Add --enrich for Semantic Scholar counts.\n")


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cache-dir", default=os.environ.get("ALR_CACHE_DIR", "./.alr_cache"))
    common.add_argument("--ttl-days", type=int, default=7)
    common.add_argument("--no-cache", action="store_true")
    common.add_argument("--refresh", action="store_true")
    common.add_argument("--json", action="store_true")
    common.add_argument("--out")
    common.add_argument("--quiet", action="store_true")

    p = argparse.ArgumentParser(description="Tokenless fallback search (arXiv + Semantic Scholar).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("search", parents=[common])
    sp.add_argument("--topic"); sp.add_argument("--q")
    sp.add_argument("--rows", type=int, default=30)
    sp.add_argument("--sort", default="relevance", help="relevance | date")
    sp.add_argument("--enrich", action="store_true", help="add Semantic Scholar citation counts")
    sp = sub.add_parser("enrich", parents=[common]); sp.add_argument("--from", dest="from_file", required=True)
    sp = sub.add_parser("citations", parents=[common]); sp.add_argument("--arxiv", required=True); sp.add_argument("--rows", type=int, default=100)
    sp = sub.add_parser("references", parents=[common]); sp.add_argument("--arxiv", required=True); sp.add_argument("--rows", type=int, default=200)
    args = p.parse_args()
    cache = C.Cache(args.cache_dir, ttl_days=args.ttl_days, enabled=not args.no_cache)
    try:
        {"search": cmd_search, "enrich": cmd_enrich,
         "citations": cmd_citations, "references": cmd_references}[args.cmd](args, cache)
    except C.ApiError as e:
        sys.exit(f"API ERROR: {e}")


if __name__ == "__main__":
    main()
