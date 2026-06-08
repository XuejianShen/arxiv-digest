#!/usr/bin/env python3
"""radar.py — the missed-citation radar.

The digest's signature feature. For each (relevant) new paper D, it asks: does D cite a
*sibling* of one of your papers P — a contemporary, same-topic paper — while NOT citing P
itself? If so, that's a place you were arguably under-cited, and it's surfaced.

Getting D's reference list is the crux, and brand-new arXiv papers are NOT yet resolved in
ADS's citation graph (a day or more of lag). So radar gathers evidence from TWO sources and
unions them:
  1. ADS resolved references — a set of cited bibcodes (precise, but often empty same-day).
  2. The arXiv source bibliography — fetched from arxiv.org/e-print/<id>, the .bbl/.bib text,
     searched for each sibling's arXiv id / DOI / title (works on the day of posting).

Design bias: PRECISION over recall. A false "you were snubbed!" is worse than a quiet miss.
So (a) the check for whether *your* paper P is cited is generous (bibcode OR arXiv id OR DOI
OR title match) — we only call it a miss when P is confidently absent; and (b) we never
declare a miss for a paper whose references we couldn't read at all (reported as "skipped").

Run radar on the RELEVANT subset of the day's papers (e.g. the top ~30 after ranking), not
the whole firehose — that bounds the arXiv-source fetches and is the only subset you care about.

Output (under <out-dir>/radar/<date>/):
  alerts.json   {alerts: [...], skipped: [...], meta: {...}}

USAGE
  python3 radar.py --daily <relevant_papers.json> --out-dir <data-dir>
  python3 radar.py --daily <papers.json> --out-dir <data-dir> --max 30 --date 2026-06-06
"""
import argparse
import datetime
import gzip
import io
import json
import os
import re
import sys
import tarfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C   # noqa: E402
import ads as A       # noqa: E402

ARXIV_SRC = "https://arxiv.org/e-print/"


# --------------------------------------------------------------------------------------
# Citation evidence
# --------------------------------------------------------------------------------------
def ads_refs(token, arxiv_id, cache, verbose=False):
    """Return (bibcode_of_D_or_None, [referenced bibcodes]) from ADS, if it has the paper yet."""
    if not token:
        return None, []
    params = {"q": f"identifier:{arxiv_id}", "fl": "bibcode,reference,identifier,title",
              "rows": 1, "sort": "date desc"}
    try:
        _, recs, _ = A.run_query(token, params, cache, verbose=verbose)
    except (C.ApiError, C.AuthError) as e:
        sys.stderr.write(f"[radar] ADS lookup failed for {arxiv_id}: {repr(e)[:120]}\n")
        return None, []
    if not recs:
        return None, []
    return recs[0].get("bibcode"), (recs[0].get("reference") or [])


def _extract_bib_text(data, max_bytes=600000):
    """Pull the bibliography text out of an arXiv e-print payload (tar.gz, .gz, or raw tex)."""
    texts = []
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
        for m in tf.getmembers():
            if m.isfile() and m.name.lower().endswith((".bbl", ".bib", ".tex")):
                try:
                    texts.append((m.name.lower(), tf.extractfile(m).read().decode("utf-8", "replace")))
                except Exception:
                    continue
        tf.close()
    except (tarfile.TarError, OSError, EOFError):
        for decode in (lambda d: gzip.decompress(d).decode("utf-8", "replace"),
                       lambda d: d.decode("utf-8", "replace")):
            try:
                texts.append(("main.tex", decode(data)))
                break
            except (OSError, EOFError, UnicodeError):
                continue
    if not texts:
        return ""
    bbl = [c for n, c in texts if n.endswith(".bbl")]
    if bbl:
        return "\n".join(bbl)[:max_bytes]
    bib = [c for n, c in texts if n.endswith(".bib")]
    if bib:
        return "\n".join(bib)[:max_bytes]
    out = []
    for _, c in texts:
        i = c.lower().find("\\begin{thebibliography}")
        if i != -1:
            j = c.lower().find("\\end{thebibliography}", i)
            out.append(c[i:(j if j != -1 else len(c))])
    return ("\n".join(out) if out else "\n".join(c for _, c in texts))[:max_bytes]


def arxiv_refblob(arxiv_id, cache, verbose=False):
    key = C.cache_key("arxiv.refblob", arxiv_id)
    hit = cache.get(key)
    if hit is not None:
        return hit.get("blob", "")
    blob = ""
    try:
        _, _, data = C.http_request(ARXIV_SRC + arxiv_id, verbose=verbose)
        blob = _extract_bib_text(data)
    except Exception as e:
        sys.stderr.write(f"[radar] e-print fetch failed for {arxiv_id}: {repr(e)[:120]}\n")
    cache.set(key, {"blob": blob})
    return blob


def _norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def cited_how(paper, ref_bibcodes, blob_lower, blob_norm):
    """How (if at all) `paper` appears in D's references. Returns a method string or None."""
    b = paper.get("bibcode")
    if b and b in ref_bibcodes:
        return "bibcode"
    ax = paper.get("arxiv_id")
    if ax:
        core = str(ax).lower().split("v")[0]
        if core and len(core) >= 8 and core in blob_lower:
            return "arxiv_id"
    doi = paper.get("doi")
    if doi and str(doi).lower() in blob_lower:
        return "doi"
    t = _norm_title(paper.get("title"))
    if t and len(t) >= 30 and t in blob_norm:
        return "title"
    return None


def _slim(rec, extra=None):
    out = {k: rec.get(k) for k in ("bibcode", "arxiv_id", "doi", "title", "first_author", "year", "pdf_url")}
    if extra:
        out.update(extra)
    return out


def main():
    p = argparse.ArgumentParser(description="Missed-citation radar over the day's relevant new papers.")
    p.add_argument("--daily", required=True, help="JSON of the day's (relevant) new papers")
    p.add_argument("--out-dir", default="./arxiv-digest-data")
    p.add_argument("--profile-dir", help="dir holding profile/siblings.json + papers.json (default <out-dir>/profile)")
    p.add_argument("--date", help="label for the output dir (default: today)")
    p.add_argument("--max", type=int, default=40, help="cap daily papers processed (cost guard)")
    p.add_argument("--relevance", help="JSON {arxiv_id: 0..1}; restrict to daily papers at/above --rel-floor "
                                       "(run the radar on the RELEVANT subset, not the whole firehose)")
    p.add_argument("--rel-floor", type=float, default=0.3, help="min relevance to include when --relevance given")
    p.add_argument("--no-ads", action="store_true", help="skip ADS resolved references")
    p.add_argument("--no-arxiv-source", action="store_true", help="skip arXiv .bbl fetching")
    p.add_argument("--cache-dir")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    out_dir = os.path.abspath(args.out_dir)
    prof_dir = args.profile_dir or os.path.join(out_dir, "profile")
    sib = json.load(open(os.path.join(prof_dir, "siblings.json")))["siblings"]
    upapers = {r["bibcode"]: r for r in C.load_records(os.path.join(prof_dir, "papers.json"))}
    daily = C.load_records(args.daily)
    if args.relevance:
        rel = json.load(open(args.relevance))
        before = len(daily)
        daily = [d for d in daily if float(rel.get(d.get("arxiv_id"), 0)) >= args.rel_floor]
        sys.stderr.write(f"[radar] relevance filter: kept {len(daily)}/{before} daily papers >= {args.rel_floor}\n")
    daily = daily[:args.max]

    cache = C.Cache(args.cache_dir or os.path.join(out_dir, ".cache"))
    token = None if args.no_ads else C.resolve_token()
    verbose = not args.quiet

    # Only user papers that actually have siblings can ever fire an alert.
    active = {b: s for b, s in sib.items() if s}
    sys.stderr.write(f"[radar] {len(daily)} daily papers x {len(active)} of your papers (those with siblings)\n")

    alerts, skipped = [], []
    for i, D in enumerate(daily, 1):
        ax = D.get("arxiv_id")
        if not ax:
            skipped.append(_slim(D, {"reason": "no arXiv id"}))
            continue
        dbib, refs = ads_refs(token, ax, cache, verbose=False)
        refset = set(refs)
        blob = "" if args.no_arxiv_source else arxiv_refblob(ax, cache, verbose=False)
        blob_lower, blob_norm = blob.lower(), _norm_title(blob)
        sources = []
        if refset:
            sources.append(f"ads({len(refset)})")
        if blob.strip():
            sources.append("arxiv_bbl")
        if not refset and not blob.strip():
            skipped.append(_slim(D, {"reason": "no reference data (ADS lag + no arXiv source)"}))
            continue

        for P_bib, sibs in active.items():
            P = upapers.get(P_bib, {"bibcode": P_bib})
            cited = [_slim(S, {"matched_by": how})
                     for S in sibs if (how := cited_how(S, refset, blob_lower, blob_norm))]
            if not cited:
                continue
            if cited_how(P, refset, blob_lower, blob_norm):
                continue  # your paper IS cited here — not a miss
            alerts.append({"your_paper": _slim(P), "daily_paper": _slim(D),
                           "cited_siblings": cited, "evidence": "+".join(sources)})
        if verbose and (i % 10 == 0 or i == len(daily)):
            sys.stderr.write(f"[radar]   {i}/{len(daily)} processed, {len(alerts)} alerts so far\n")

    ref_date = args.date or datetime.date.today().isoformat()
    rdir = os.path.join(out_dir, "radar", ref_date)
    os.makedirs(rdir, exist_ok=True)
    out_path = os.path.join(rdir, "alerts.json")
    with open(out_path, "w") as f:
        json.dump({"meta": {"date": ref_date, "n_daily": len(daily), "n_skipped": len(skipped),
                            "ads": not args.no_ads, "arxiv_source": not args.no_arxiv_source},
                   "alerts": alerts, "skipped": skipped}, f, indent=1)

    print(f"[radar] {len(alerts)} missed-citation alert(s) across {len(daily)} papers "
          f"({len(skipped)} had no readable references) -> {out_path}")
    for a in alerts[:12]:
        sib_titles = ", ".join((s.get("title") or "")[:40] for s in a["cited_siblings"][:2])
        print(f"  • '{(a['daily_paper'].get('title') or '')[:55]}' cites your sibling [{sib_titles}] "
              f"but not YOUR '{(a['your_paper'].get('title') or '')[:45]}'")


if __name__ == "__main__":
    main()
