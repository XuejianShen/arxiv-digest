# arxiv-digest

A personalized **daily arXiv digest** that runs as a Claude Code scheduled **routine** (cloud, ~8 AM)
and delivers to Slack. It reads the day's new astro-ph papers, ranks them against the user's actual
published work, deep-reads the top few (full text) with a "why this matters to you" note, one-lines
the rest, runs a **missed-citation radar**, and posts the result to the user's Slack DM.

This repo is **self-contained** so a cloud routine can clone it and run with no local-machine access.

## Layout

```
.claude/skills/arxiv-digest/   the skill (auto-loaded as a project skill in the clone)
  SKILL.md                     the workflow
  scripts/                     stdlib-only Python (ADS/SciX + arXiv clients, corpus/fetch/rank/radar)
  references/                  output format + config docs
data/
  config.json                  categories, keywords, top_n, sibling window, Slack target
  profile/                     PREBUILT: the user's papers + sibling sets (rides in the repo;
                               the cloud sandbox has no persistent storage)
```

The transient API cache and per-day `daily/`/`radar/` outputs are `.gitignore`d (regenerated each run).

## Running it (locally, to test)

```bash
export ADS_API_TOKEN=...                       # NASA ADS / SciX token
cd <repo>
# in Claude Code: "run my arxiv digest, data dir ./data, post to my Slack DM"
# or drive the scripts directly:
python3 .claude/skills/arxiv-digest/scripts/fetch_daily.py --config data/config.json --out-dir data
```

## The routine

See **[ROUTINE.md](ROUTINE.md)** for the exact schedule, instruction prompt, environment secret,
Slack connector, and network domains to set when creating the scheduled routine.

## Refreshing the profile

The `profile/` rides in the repo. Rebuild it when the user publishes new papers (or every few weeks):

```bash
python3 .claude/skills/arxiv-digest/scripts/corpus.py --config data/config.json --out-dir data
git add data/profile && git commit -m "refresh profile" && git push
```
