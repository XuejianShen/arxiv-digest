# config.json schema

The single source of truth for the digest, so future tuning needs no code changes. Lives in the
data directory (`$ARXIV_DIGEST_DIR/config.json`). A `config.example.json` ships with the skill.

| key | type | meaning |
|---|---|---|
| `ads_library_id` | string | ADS/SciX library id (or full URL) holding the user's papers. The corpus is built from this. |
| `arxiv_categories` | list | arXiv categories to monitor, e.g. `["astro-ph.GA","astro-ph.CO","astro-ph.HE"]`. |
| `keywords` | list | Always-surface topic phrases (the watchlist), e.g. `"self-interacting dark matter"`. Used for triage terms and to never-bury a hit. |
| `authors_watchlist` | list | Author names to always surface regardless of topic, `"Last, F"` form. |
| `top_n_deep_read` | int | How many papers get a full-text deep read (default 10). |
| `sibling_window_years` | int | A sibling B must have **appeared on arXiv** within ±this many years of paper A's arXiv appearance (default 1; measured from the arXiv id YYMM, not publication year). |
| `sibling_rows` | int | How many `similar()` candidates to pull per paper before the window filter (default 25). |
| `lookback_days` | int | Keep arXiv papers submitted within this many days (default 2; covers the announce delay). |
| `slack_target` | string | `"self-dm"` for a DM to the user, or a channel like `"#arxiv"`. |
| `slack_user_id` | string | (optional, auto-filled) resolved Slack id for the self-DM, cached after first lookup. |

Edit freely; the scripts and workflow read these at run time. `corpus.py` uses
`ads_library_id` + the sibling params; `fetch_daily.py` uses `arxiv_categories` + `lookback_days`;
ranking uses `keywords` + `authors_watchlist`; delivery uses `slack_target`.

To change fields/topics later: edit `arxiv_categories` / `keywords`, and (if the library changed)
re-run `corpus.py` to rebuild the profile.
