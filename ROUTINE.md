# Scheduled routine — setup values

Create a Claude Code **routine** (cloud scheduled agent) with the settings below.

| Field | Value |
|---|---|
| **Repository** | `XuejianShen/arxiv-digest` (branch `main`) |
| **Schedule** | Daily at **08:00 America/New_York** |
| **Environment secret** | `ADS_API_TOKEN` = *(your NASA ADS / SciX token)* |
| **Connector** | **Slack** (so the run can post to your DM) |
| **Network access** | Allow `export.arxiv.org`, `arxiv.org`, `api.adsabs.harvard.edu`, `api.scixplorer.org`, `api.semanticscholar.org` — or set network to **Full** |

## Instruction prompt (paste verbatim)

```
Produce my personalized daily arXiv digest for today and post it to my Slack DM.

Follow the skill at .claude/skills/arxiv-digest/SKILL.md. Use data directory ./data — it already
contains config.json and a PREBUILT profile/ (my papers + sibling sets); do NOT rebuild the profile
(skip corpus.py). Run the pipeline: fetch the day's new papers (fetch_daily.py), rank relevance
against my profile, run the missed-citation radar on the relevance-filtered subset
(radar.py --relevance ... --rel-floor 0.3), deep-read up to top_n_deep_read papers in full text with
a specific "why this matters to you", one-line the rest, then deliver to my Slack DM
(user id U05V3F9EAQ5) as threaded messages — my Slack workspace is free-tier, so do NOT use a Canvas.

Be honest on thin days (a short digest is fine; never pad). Ground every claim in the papers you
actually read; mark anything you couldn't verify. If no papers cleared the relevance floor, post a
2-line "nothing in your lanes today" note.
```

## Notes
- The profile is prebuilt and committed, so each daily run is fast (no ~15-min `similar()` rebuild).
- The cloud sandbox is wiped between runs; that's fine — `fetch_daily`/`radar` re-fetch from arXiv/ADS
  each morning, and the cache is not needed across days.
- **Timezone**: arXiv has no weekend submissions, so a Mon/holiday run anchors to the freshest day
  available (Fri) automatically — that's expected, not a bug.
- **Profile refresh**: re-run `corpus.py` and push when you publish (see README). A monthly nudge is
  plenty unless you're publishing in bursts.
