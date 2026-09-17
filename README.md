# alltime-skill

`/alltime` — deep research across Reddit, X, arXiv, Hacker News, GitHub and YouTube **over years, not weeks**.

It is an adapter on top of [`/last30days`](https://github.com/mvanhorn/last30days-skill): it imports that engine and lifts the places where a ~30-day horizon is hardcoded, so the same source-access machinery can answer "what has this community concluded?" instead of "what happened this month?".

## What it does differently

| | `/last30days` | `/alltime` |
|---|---|---|
| Question | what happened recently? | what has been concluded? |
| Window | 30 days, hard | 730 days default, `--all-time` available |
| Ranked by | engagement + recency | relevance + engagement |
| Platforms | 8+, engine picks | 6, **the user picks, every run** |

Concretely:

- **Window overrides.** Reddit's timeframe buckets, the arctic-shift archive lane, and arXiv's `RECENCY_DAYS` / result caps are patched so a multi-year request actually fetches a multi-year candidate pool instead of collapsing to `month`.
- **A date-sliced archive lane.** Upstream accepts a `timeframe` for arctic-shift and never reads it, so the lane returns the newest N posts regardless of window. `/alltime` replaces it with a version that slices the window and fetches each slice.
- **Mandatory platform choice.** The skill probes what this machine can reach, then asks — no silent platform selection, and unreachable sources are dropped with a reason rather than returning an empty section that reads as "nobody discussed this".
- **Seam checks.** The adapter reaches into upstream internals and upstream ships fast, so every override has a startup check that names the exact seam if it moves. Window seams abort the run; rate-limit and archive seams only warn.

## What it borrows

Everything hard. The `/last30days` engine keeps owning source access: getting past Reddit's shreddit anti-bot wall without an API key, the `arxiv-pp-cli` wrapper, the X / Hacker News / GitHub / YouTube lanes, and the relevance / rerank / dedupe / fusion stack. No new scrapers are added here — reach comes from removing ceilings.

That engine is MIT-licensed, by Matt Van Horn. This repo vendors none of its code; it imports the installed copy at runtime and monkey-patches named seams.

## Install

Requires `/last30days` to be installed first:

```bash
claude plugin install last30days@last30days-skill
npx skills add nytafar/alltime-skill
```

Or copy `skills/alltime/` into your agent's skills directory by hand.

Also needs Python 3.12+. Reddit, Hacker News and GitHub need no credentials; arXiv needs `arxiv-pp-cli`, YouTube needs `yt-dlp`, X needs a browser session or API creds.

## Use

```
/alltime speculative decoding failure modes
/alltime sourdough hydration --all-time
```

The skill asks which platforms to search, writes a 2–4 subquery plan, then runs:

```bash
python3 skills/alltime/scripts/alltime.py "<topic>" --search reddit,arxiv --days 730
python3 skills/alltime/scripts/alltime.py --list-sources   # what this machine can reach
python3 skills/alltime/scripts/alltime.py --explain        # resolved engine + seam checks
```

Useful flags: `--all-time`, `--deep` / `--quick`, `--subreddits a,b`, `--arxiv-loose`, `--emit json|md|compact`. Unrecognized flags pass straight through to the engine, so everything in `/last30days --help` still works. `ALLTIME_ENGINE_DIR` pins a specific engine install.

See [`skills/alltime/SKILL.md`](skills/alltime/SKILL.md) for the full flag table, the per-platform window handling, and the honest limits (Reddit's coarse `t=` buckets, flattened recency scoring over long windows, and the archive lane sampling the window rather than sweeping it).

## License

MIT. `/last30days` is MIT, © Matt Van Horn.
