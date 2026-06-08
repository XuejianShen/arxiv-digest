#!/usr/bin/env python3
"""rank.py — transparent, multi-signal ranking + bucketing of candidate papers.

The skill must NOT just take the top ADS hits. This script merges every candidate record gathered
during the search (search hits + citations + references, from ads.py and/or fallback.py), de-dups
them, and scores each paper by combining several signals, then sorts them into the buckets the
final report needs. Every component score is written into the output JSON so the ranking is fully
auditable — nothing is a black box.

THE SIGNALS (each normalized to [0,1] across the candidate pool, log-scaled where noted)
  C   citation impact        log(1+citation_count),     normalized   — raw influence
  A   age-normalized impact  log(1+citations_per_year), normalized   — surfaces RECENT high-impact work
  Nz  author-normalized      log(1+citation_count_norm),normalized   — ADS's per-author citations (dampens
                                                                        huge-collaboration inflation)
  X   citation-graph degree  (# anchor papers that cite it)/(#anchors) — "repeatedly cited as foundational"
                                                                        and "connected to multiple branches"
  G   relevance gate [0,1]   supplied by the model via --relevance, else a keyword-overlap proxy
  Rcy recency boost          exp(-(age-1)/tau)                        — used only in the "recent" lane

COMPOSITE SCORES
  foundational_score = G * (wC*C + wA*A + wX*X + wNz*Nz)      # G is a GATE: irrelevant-but-cited -> ~0
  recent_score       = G * (0.5*Rcy + 0.3*A + 0.2*C)         # rewards new + relevant + rising

Why a multiplicative relevance gate? Because the single most common failure mode is a hugely cited
paper that is only adjacent to the topic (a generic instrument paper, a stats method, matplotlib).
Multiplying by relevance sends those toward zero no matter how high their citation count.

BUCKETS (mutually exclusive, in priority order)
  rejected      relevance < rel_threshold, OR infrastructure (matplotlib/numpy/...) — with a reason
  review        is_review                                   (sorted by citation impact)
  recent        age <= recent_years and relevant            (sorted by recent_score)
  foundational  the rest, relevant                          (sorted by foundational_score)
  peripheral    relevance in [rel_threshold, peripheral_threshold) — relevant but not core

A brand-new field (e.g. JWST "little red dots") can have papers that are BOTH foundational and
recent; such overlap is expected and noted, not an error. The model makes the final call — this
script produces the defensible first-pass ordering and the evidence behind it.

USAGE
  python3 rank.py raw/*.json --topic "little red dots supermassive black hole growth" \
      --anchor-refs raw/anchor_refs.json --relevance work/relevance.json \
      --out ranked/candidates.json
"""

import argparse
import glob
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _common as C  # noqa: E402

STOPWORDS = set("a an the of in on at to and or for with from by as is are be this that these those "
                "high low at z and/or via using toward towards into over under between within".split())


# --------------------------------------------------------------------------------------
# Relevance proxy (used only when the model does not supply per-paper relevance)
# --------------------------------------------------------------------------------------
def topic_terms(topic, extra_terms):
    terms = set()
    for t in re.split(r"[^a-z0-9]+", (topic or "").lower()):
        if len(t) > 2 and t not in STOPWORDS:
            terms.add(t)
    for t in (extra_terms or []):
        t = t.strip().lower()
        if t:
            terms.add(t)
    return terms


def relevance_proxy(rec, terms):
    """Crude bag-of-words overlap of topic terms with the title (weighted) + abstract + keywords.
    Returns [0,1]. This is a FALLBACK; real relevance should come from the model reading abstracts."""
    if not terms:
        return 0.5
    title = (rec.get("title") or "").lower()
    abs = (rec.get("abstract") or "").lower()
    kw = " ".join(rec.get("keyword") or []).lower()
    hits_title = sum(1 for t in terms if t in title)
    hits_other = sum(1 for t in terms if (t in abs or t in kw))
    # title hits count double; cap contribution per term
    score = (2 * hits_title + hits_other) / (2 * len(terms))
    return max(0.0, min(1.0, score))


# --------------------------------------------------------------------------------------
# Connectivity from anchor reference lists
# --------------------------------------------------------------------------------------
def connectivity_map(anchor_refs):
    """anchor_refs: {anchor_bibcode: [cited bibcodes]}. Returns {bibcode: fraction_of_anchors_citing_it}."""
    if not anchor_refs:
        return {}, 0
    n = len(anchor_refs)
    count = {}
    for refs in anchor_refs.values():
        for b in set(refs or []):
            count[b] = count.get(b, 0) + 1
    return {b: c / n for b, c in count.items()}, n


# --------------------------------------------------------------------------------------
# Normalization helpers
# --------------------------------------------------------------------------------------
def lognorm(values):
    """Return a function mapping a raw value to log(1+v)/log(1+max). Robust to all-zero/empty."""
    vmax = max([v for v in values if v is not None], default=0)
    denom = math.log1p(vmax) if vmax > 0 else 0.0
    if denom == 0:
        return lambda v: 0.0
    return lambda v: (math.log1p(v) / denom) if v else 0.0


def load_all(paths):
    records = []
    for pat in paths:
        for path in sorted(glob.glob(pat)) or [pat]:
            if os.path.exists(path):
                records.extend(C.load_records(path))
    return records


def match_relevance(rec, rel_map):
    for k in ("bibcode", "arxiv_id", "doi"):
        v = rec.get(k)
        if v and str(v) in rel_map:
            return float(rel_map[str(v)])
    rid = C.record_id(rec)
    if rid in rel_map:
        return float(rel_map[rid])
    return None


def main():
    p = argparse.ArgumentParser(description="Multi-signal ranking + bucketing for astro-lit-review.")
    p.add_argument("inputs", nargs="+", help="record JSON files / globs (search + citations + references)")
    p.add_argument("--topic", default="", help="topic string (for the relevance proxy)")
    p.add_argument("--terms", help="comma-separated extra relevance terms/synonyms")
    p.add_argument("--anchor-refs", help="JSON {anchor_bibcode:[cited bibcodes]} for the connectivity signal")
    p.add_argument("--relevance", help="JSON {id: 0..1} of MODEL-judged relevance (overrides the proxy)")
    p.add_argument("--out", help="write the fully-scored, ranked records here")
    p.add_argument("--recent-years", type=int, default=4, help="age (yr) defining the 'recent' lane")
    p.add_argument("--rel-threshold", type=float, default=0.18, help="below this relevance -> rejected")
    p.add_argument("--peripheral-threshold", type=float, default=0.42, help="below this -> peripheral")
    p.add_argument("--tau", type=float, default=3.0, help="recency decay timescale (yr)")
    p.add_argument("--w-cite", type=float, default=0.40)
    p.add_argument("--w-age", type=float, default=0.30)
    p.add_argument("--w-conn", type=float, default=0.20)
    p.add_argument("--w-norm", type=float, default=0.10)
    p.add_argument("--show", type=int, default=12, help="rows to print per bucket")
    args = p.parse_args()

    records = C.dedupe(load_all(args.inputs))
    if not records:
        sys.exit("rank: no records found in inputs.")

    rel_map = json.load(open(args.relevance)) if args.relevance else {}
    anchor_refs = json.load(open(args.anchor_refs)) if args.anchor_refs else {}
    conn, n_anchors = connectivity_map(anchor_refs)
    terms = topic_terms(args.topic, (args.terms or "").split(",") if args.terms else [])

    cur = C.current_year()
    cite_norm = lognorm([r.get("citation_count") for r in records])
    cpy_norm = lognorm([r.get("citations_per_year") for r in records])
    nz_norm = lognorm([r.get("citation_count_norm") for r in records])
    rel_source = "model" if rel_map else "proxy"

    for r in records:
        C_ = cite_norm(r.get("citation_count"))
        A_ = cpy_norm(r.get("citations_per_year"))
        Nz = nz_norm(r.get("citation_count_norm"))
        X_ = conn.get(r.get("bibcode"), 0.0)
        g_model = match_relevance(r, rel_map)
        if g_model is not None:
            g, this_src = g_model, "model"
        else:
            # First-pass proxy. Being cited by the field's OWN anchor papers is strong relevance
            # evidence — a foundational method/diagnostic paper (BPT diagram, Hα BH masses, dust law)
            # rarely carries the topic phrase in its title and may have no abstract fetched, so the
            # bare text proxy underrates it. Let connectivity lift it. Infrastructure is excluded
            # separately, so this never rescues matplotlib.
            # Cap the connectivity lift below 1.0 so a paper with genuine TEXT relevance can still
            # outrank one that is merely well-connected (generic stats/photo-z/extraction code). The
            # model-relevance pass is what ultimately orders these correctly.
            g = max(relevance_proxy(r, terms), min(0.9, 1.2 * X_))
            this_src = "proxy+conn"
        age = max(1, cur - int(r["year"]) + 1) if r.get("year") else 99
        rcy = math.exp(-(age - 1) / args.tau)
        found = g * (args.w_cite * C_ + args.w_age * A_ + args.w_conn * X_ + args.w_norm * Nz)
        recent = g * (0.5 * rcy + 0.3 * A_ + 0.2 * C_)
        r["scores"] = {"relevance": round(g, 3), "rel_source": this_src,
                       "C_cite": round(C_, 3), "A_age": round(A_, 3),
                       "Nz_authornorm": round(Nz, 3), "X_connectivity": round(X_, 3),
                       "recency": round(rcy, 3),
                       "foundational_score": round(found, 4), "recent_score": round(recent, 4)}
        # bucket assignment
        if r.get("is_infrastructure"):
            r["bucket"], r["reject_reason"] = "rejected", "software/infrastructure paper (not a science contribution)"
        elif g < args.rel_threshold:
            r["bucket"], r["reject_reason"] = "rejected", f"low topical relevance ({g:.2f} < {args.rel_threshold})"
        elif r.get("is_review"):
            r["bucket"] = "review"
        elif age <= args.recent_years:
            r["bucket"] = "recent"
        elif g < args.peripheral_threshold:
            r["bucket"] = "peripheral"
        else:
            r["bucket"] = "foundational"

    buckets = {b: [] for b in ("foundational", "review", "recent", "peripheral", "rejected")}
    for r in records:
        buckets[r["bucket"]].append(r)
    buckets["foundational"].sort(key=lambda r: r["scores"]["foundational_score"], reverse=True)
    buckets["review"].sort(key=lambda r: (r.get("citation_count") or 0), reverse=True)
    buckets["recent"].sort(key=lambda r: r["scores"]["recent_score"], reverse=True)
    buckets["peripheral"].sort(key=lambda r: r["scores"]["foundational_score"], reverse=True)
    buckets["rejected"].sort(key=lambda r: (r.get("citation_count") or 0), reverse=True)

    # ---- report to stdout ----
    print(f"# ranked {len(records)} unique papers | relevance source: {rel_source}"
          + (f" | connectivity from {n_anchors} anchors" if n_anchors else " | no connectivity (no --anchor-refs)"))
    print(f"# weights: cite={args.w_cite} age={args.w_age} conn={args.w_conn} authornorm={args.w_norm}"
          f" | recent<= {args.recent_years}yr | rel_threshold={args.rel_threshold}")
    for b in ("foundational", "review", "recent", "peripheral", "rejected"):
        rows = buckets[b]
        print(f"\n===== {b.upper()}  ({len(rows)}) =====")
        key = "recent_score" if b == "recent" else "foundational_score"
        cols = [
            ("year", lambda r: str(r.get("year") or "?"), 4),
            ("score", lambda r, k=key: f"{r['scores'][k]:.3f}", 5),
            ("rel", lambda r: f"{r['scores']['relevance']:.2f}", 4),
            ("cite", lambda r: C._fmt(r.get("citation_count")), 5),
            ("c/yr", lambda r: C._fmt(r.get("citations_per_year")), 5),
            ("X", lambda r: f"{r['scores']['X_connectivity']:.2f}", 4),
            ("author", C.short_authors, 15),
            ("title", lambda r: r.get("title") or "", 52),
        ]
        if b == "rejected":
            cols.append(("why", lambda r: r.get("reject_reason", ""), 28))
        C.print_table(rows, cols=cols, limit=args.show)

    if args.out:
        C.save_records(args.out, records, meta={
            "topic": args.topic, "relevance_source": rel_source, "n_anchors": n_anchors,
            "weights": {"cite": args.w_cite, "age": args.w_age, "conn": args.w_conn, "authornorm": args.w_norm},
            "buckets": {b: len(v) for b, v in buckets.items()}})
        sys.stderr.write(f"[rank] wrote {len(records)} scored records -> {args.out}\n")


if __name__ == "__main__":
    main()
