---
name: arxiv-digest
description: >-
  Produce the user's personalized DAILY arXiv digest. It fetches the day's new astro-ph
  submissions in their fields, ranks them by relevance to the user's ACTUAL published work
  (read from their ADS/SciX library) plus a configurable keyword/author watchlist,
  deep-reads the top ~10 papers (full text) each with a "why this matters to you" note,
  one-line-summarizes the rest, runs a "missed-citation radar" (flags new papers that cite a
  contemporary sibling of one of the user's own papers but do NOT cite the user), and
  delivers the result to Slack. Use WHENEVER the user wants their new-papers feed for the day
  — e.g. "run my arxiv digest", "what's new on arXiv today", "today's papers in my field",
  "give me my morning digest", "anything new worth reading today", "did anyone miss-cite or
  scoop me". This is ALSO the skill the user's scheduled ~8 a.m. cron invokes each morning.
  Do NOT trigger for: a deep literature review / "state of the field" on a TOPIC (use
  astro-lit-review); reviewing the user's own manuscript (manuscript-review); or a one-off
  single-paper lookup.
---

# Daily arXiv digest

Turn the day's flood of new arXiv papers into a short, personalized briefing: the handful that
actually matter to *this* researcher, read in depth, plus a radar for places they were
under-cited. The output is a Slack message they can read over coffee — substance over volume.

The guiding principle: **be the sharp postdoc who skims every new astro-ph paper before the
PI wakes up and flags the five that matter, having actually read them.** Relevance is judged
against the user's *real* body of work (their ADS library), not vibes. Every paper named is a
real arXiv id with a real link; every "why it matters" is grounded in the paper's actual
content, not its title.

## How it fits together

```
config.json ──> corpus.py ──> profile/   (your papers + their "sibling" sets; built once, refreshed weekly)
                                  │
fetch_daily.py ──> daily/    ────┤
                                  ├──> [you rank relevance] ──> top-N deep read + the rest one-line
                                  └──> radar.py ──> radar/   (missed-citation alerts)
                                                       │
                                              assemble digest ──> Slack DM
```

Scripts live in `scripts/` and are standard-library-only (no pip), so they run in a headless
cron. They reuse the proven NASA ADS / SciX + arXiv client from `astro-lit-review` (token
handling, caching, rate-limit backoff). Read `references/output-format.md` for the exact digest
layout and the Slack delivery mechanics, and `references/config.md` for the config schema.

## Setup (once, and to refresh)

The data directory holds `config.json`, the `profile/`, each day's `daily/` + `radar/`, and the
API cache. Resolve it from `$ARXIV_DIGEST_DIR` if set, else the directory containing the
`config.json` you were pointed at. All script paths below assume:

```
SKILL=<this skill dir>/scripts
DATA=<the data dir>            # contains config.json
```

- **Token.** ADS/SciX token is read from the environment (`NASA_ADS_API_TOKEN` / `SCIX_API_TOKEN`
  / `ADS_API_TOKEN` / `ADS_DEV_KEY`). Verify: `python3 $SKILL/ads.py check`. No token → the digest can still fetch
  and summarize via arXiv, but the radar and ADS metadata are disabled; say so in the output.
- **Profile.** Needs `profile/papers.json` + `profile/siblings.json`. Build/refresh with:
  ```
  python3 $SKILL/corpus.py --config $DATA/config.json --out-dir $DATA
  ```
  This reads the user's ADS library, fetches their papers' abstracts, and computes each paper's
  sibling set: same-topic papers (ADS `similar()`) that appeared on arXiv within ±1 yr of the
  paper's own arXiv appearance (config `sibling_window_years`; measured from the arXiv id, not the
  publication year). If `sibling_max_author_rank` is set, only papers where the user
  (`author_name`) is within the top N authors get a sibling set — the radar never fires for
  middle-author collaboration papers. It's cached; re-running is cheap. **Refresh weekly** (or
  when the user publishes) so new papers get a sibling set. If
  `profile/` is missing when a digest is requested, build it first.

## The morning workflow

### Phase 0 — Frame the run
Note the date (default: today). Load `config.json` (categories, keywords, authors watchlist,
`top_n_deep_read`, `lookback_days`, sibling params, `slack_target`). Confirm the profile exists
(build it if not) and the token works.

### Phase 1 — Fetch the day's new papers
```
python3 $SKILL/fetch_daily.py --config $DATA/config.json --out-dir $DATA
```
Writes `daily/<date>/papers.json` (new arXiv records in the configured categories + recency
window, cross-listings de-duped). On a quiet day this may be a few dozen; on a Monday, 150+.

### Phase 2 — Rank relevance to THIS researcher (the judgment step)
This is where the digest earns its keep. Two signals, combined by you:
1. **First-pass triage (cheap).** Run the keyword proxy to order the pool before you read:
   ```
   python3 $SKILL/rank.py $DATA/daily/<date>/papers.json \
     --terms "<comma-joined config keywords>" --topic "<short phrase for the user's field>" \
     --out $DATA/daily/<date>/ranked.json
   ```
   (Citation signals are ~0 for brand-new papers, so this is essentially relevance triage.)
2. **Real relevance (you).** Read the **title + abstract** of the top candidates (and of every
   paper hitting the keyword/author watchlist, regardless of proxy rank — never let triage bury a
   watchlist hit). Judge each against the user's actual work in `profile/papers.json`: is this in
   their lane, does it bear on their methods/objects/claims? Write `daily/<date>/relevance.json`
   = `{arxiv_id: 0.0–1.0}`. Deep-read **up to `top_n_deep_read`** (default 10) — a *ceiling, not a
   quota*: stop at the relevance floor (~0.3). If only 4 papers clear the bar, deep-read 4 and don't
   pad with a weak 5th; the other above-floor papers get one-liners. A short, high-signal digest on a
   thin day is the right outcome — never pad with adjacent-field papers to hit the number.

### Phase 3 — Missed-citation radar
Run the radar, restricting it to the RELEVANT papers via your `relevance.json` (it filters
internally — no separate file to build; this bounds the arXiv-source fetches AND removes false
positives from irrelevant papers):
```
python3 $SKILL/radar.py --daily $DATA/daily/<date>/papers.json \
  --relevance $DATA/daily/<date>/relevance.json --rel-floor 0.3 --out-dir $DATA --date <date>
```
**What an alert means** (the definition): for one of the user's papers *A* (only lead-author
papers — those with the user within the top `sibling_max_author_rank` authors — when that config
is set), a *sibling* *B* is a same-topic paper that appeared on arXiv within ±1 yr of *A*'s arXiv
appearance; an alert fires when a new daily paper *C* cites *B* but **not** *A*. The script gives
raw leads — **you vet them** into:
- **Hard flags** (surface prominently): the sibling is genuinely *A*'s sub-topic and a citation to
  *A* would be expected — a defensible "you were under-cited."
- **Soft flags** (surface, but label as borderline): plausible, but the topical link is loose or the
  probe/regime differs. Let the user judge.
- **Drop**: same-collaboration artifacts, theses, and cases where *A* is a different probe/regime
  from the citing context. Bias to precision — a wrong "you were snubbed" is worse than a quiet miss.
If `alerts.json` reports `n_skipped` > 0, those papers had no readable references (rare — the arXiv
`.bbl` path usually works same-day); note it, and they re-check cleanly once ADS catches up.

### Phase 4 — Deep-read the top papers
For each paper in the deep set, read the **actual paper**, not just the abstract:
```
curl -sL https://arxiv.org/pdf/<arxiv_id> -o /tmp/<arxiv_id>.pdf   # then Read the PDF
```
(Or fetch the arXiv HTML; a PDF Read may need an explicit page range — the result is usually in the
first pages, so don't burn effort on robustness appendices.) Extract: the actual result/claim and
the evidence for it, the method, the numbers that matter, and the caveats. Then write an
**abstract-style summary** and a specific **"why this matters to you"** that connects it to the
user's work (a method they use, an object/regime they study, a claim of theirs it supports or
challenges). **Adaptive depth:** match length to relevance — a core-lane paper (squarely the user's
topic/method) earns the full 4–6 sentences; an adjacent one gets 2–3 plus a one-line tie. If a key
cited paper is load-bearing, skim its abstract too — but don't rabbit-hole. Ground every claim in
the text; if you're unsure, say so rather than inflate.

### Phase 5 — One-liners for the rest
For the remaining relevant papers, one crisp sentence each: what they did and the one reason it's
on the user's radar. Group by sub-theme if there are many (see `references/output-format.md`).

### Phase 6 — Assemble and deliver to Slack
Build the digest per `references/output-format.md` (headline counts → radar alerts → deep reads →
one-liners → caveats), then deliver to the configured `slack_target` (default: a DM to the user).
Slack mechanics, length handling, and the self-DM lookup are in `references/output-format.md`.
After posting, print a one-line terminal summary (date, #new, #deep, #one-liners, #radar alerts,
where it was posted, any caveats).

## Scaling & degradation
- **Volume.** Quiet day (<40 new): read all abstracts directly, skip the proxy. Busy day (150+):
  lean on the proxy triage, deep-read exactly `top_n_deep_read`, one-line a capped ~25, and say how
  many relevant papers were rolled up.
- **No ADS token** → fetch + summarize from arXiv only; radar and citation context off; flag it.
- **ADS lag on references** → radar is best-effort same-day; report coverage, don't invent misses.
- **Empty/thin day** (weekend, holiday) → a 2–3 line "nothing major in your areas today" is a fine,
  honest digest. Don't manufacture relevance.
- **Profile stale/missing** → build it first (Phase 0); note if it was just rebuilt.

## Cardinal rules
1. **Relevance is judged against the user's real corpus**, not the paper's self-description. A
   hype-y title in an adjacent field is not relevant; a dry paper that bears directly on their
   methods is.
2. **Ground every "why it matters" in the paper's content** you actually read. No inflation, no
   inventing results. An honest "abstract only; full text not reached" beats a confident guess.
3. **The radar biases to precision.** Only surface a missed-citation alert you'd defend to the
   user's face. False "you were snubbed" is worse than a quiet miss.
4. **Short and high-signal wins.** The user reads this every morning; respect their time.
