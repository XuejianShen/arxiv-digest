# Digest layout & Slack delivery

## The digest structure

Assemble in this order — most actionable first, so a glance at the top of the Slack message is
already useful:

```
:newspaper: arXiv digest · <Weekday>, <Date>
<N> new in {astro-ph.GA, astro-ph.CO, ...} · <D> worth a close read · <R> citation-radar flags

━━ :radar: Missed-citation radar ━━            ← only if there are vetted alerts
• <New paper, first author + short title> cites <sibling> (your contemporary on <topic>)
  but not your *<your paper short title>* (<year>). → <arxiv link>
  _soft flag:_ <a borderline lead, clearly labelled — loose topical link or different probe>

━━ :mag: Worth reading closely ━━               ← the deep set (top_n_deep_read)
*1. <Title>*  — <First Author> et al. (<arxiv id>)  <link>
<3–5 sentence abstract-style summary: the actual result + evidence + key number + caveat.>
*Why this matters to you:* <specific tie to the user's work — a method, object, regime, or claim.>

*2. <Title>* ...

━━ :page_facing_up: Also on your radar ━━        ← one-liners, optionally grouped by sub-theme
*High-z galaxies* — <Author> (<id>): <one sentence>. <link>
*SIDM* — <Author> (<id>): <one sentence>. <link>

━━ notes ━━                                      ← only if relevant
<caveats: ADS reference lag meant radar skipped K papers; no token; thin day; etc.>
```

Deep summaries are **adaptive**: ~4–6 sentences for a core-lane paper, ~2–3 for an adjacent one.
Lead each with the *result*, not the setup. Links are `https://arxiv.org/abs/<id>`. Use the user's
own short titles for their papers in radar lines so they recognize them instantly.

## Slack mechanics

The Slack tools are deferred — load them first:
`ToolSearch "select:slack_send_message,slack_search_users,slack_create_canvas"`.

**Resolve the self-DM target** (config `slack_target: "self-dm"`): find the user's Slack id via
`slack_search_users` (try their email `xuejian@mit.edu`, else name), then send to that id — Slack
opens the direct message with themselves. If `slack_target` is a channel name (e.g. `#arxiv`),
send there instead. Cache the resolved user id back into config (`slack_user_id`) so later runs skip
the lookup.

**Canvas availability.** Canvas needs a PAID Slack workspace. If config `slack_use_canvas` is `false`
(or unset on a free workspace — `slack_create_canvas` returns `not_supported_free_team`), skip the
Canvas attempt and deliver as threaded messages directly.

**Length.** Slack messages cap around 4000–5000 characters; a full digest with 10 deep reads will
exceed that. Two good options:
- **Canvas (preferred for the full digest):** `slack_create_canvas` titled `arXiv digest · <date>`
  with the entire briefing in markdown, then `slack_send_message` a short DM — the headline counts +
  the radar flags + the deep-read *titles* — linking to the canvas. Best reading experience, no
  truncation.
- **Threaded messages:** send the headline + radar + deep set as the parent message (trimming if
  needed), then post the one-liners as a threaded reply. Simpler, no canvas.

Pick based on what's available; if canvas creation fails, fall back to threaded messages. Use Slack
*mrkdwn* (`*bold*`, `_italic_`, `<url|text>`) — not full Markdown — in messages.

**Idempotency for the scheduled run.** Before posting, you may check the target for an existing
"arXiv digest · <date>" to avoid double-posting if the cron fires twice. If found, update/skip
rather than duplicate.
```
