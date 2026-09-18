---
name: alltime
description: Deep research across Reddit, X, arXiv, Hacker News, GitHub and YouTube over years, not weeks. Use when the user wants what a community or the literature has concluded about a topic over time — "what do people think about X", "research X properly", "what does the literature say", "deep dive on X", "find the best threads on X". For what happened *recently*, use /last30days instead.
argument-hint: 'alltime retrieval augmented generation failure modes | alltime sourdough hydration --all-time'
allowed-tools: Bash, Read, Write, AskUserQuestion, WebSearch
user-invocable: true
---

# /alltime

Deep, time-unbounded research across six platforms, with the user choosing
which ones to search on every run.

**Choosing between this and `/last30days`:**

| | `/last30days` | `/alltime` |
|---|---|---|
| Question | "what happened recently?" | "what has been concluded?" |
| Window | 30 days, hard | 2 years default, `--all-time` available |
| Ranked by | engagement + recency | relevance + engagement |
| Platforms | 8+, engine picks | 6, **user picks** |

If the user's question is about *news, reactions, or the current state of
play*, hand off to `/last30days` — it is better at that, and this skill is not
trying to replace it.

# STEP 1 — ASK WHICH PLATFORMS (MANDATORY, BEFORE ANY RESEARCH)

Never pick platforms silently. Run the availability probe first, then ask.

```bash
python3 "$SKILL_DIR/scripts/alltime.py" --list-sources
```

Then call **AskUserQuestion** with `multiSelect: true`, offering **only the
platforms the probe reported as available**. Pre-select Reddit and X by putting
them first and marking them `(default)`. Use the probe's own blurbs as the
option descriptions.

| Platform | On by default | Contributes |
|---|---|---|
| **Reddit** | **yes** | Threads and top comments with real upvote counts. No API key. |
| **X / Twitter** | **yes** | Expert threads and hot takes. |
| arXiv | no | The papers behind the topic. No API key. |
| Hacker News | no | Developer consensus, points, comment counts. |
| GitHub | no | Issues, discussions, releases, repo activity. |
| YouTube | no | Long-form talks and full transcripts. Slowest per item. |
| Web (Exa) | no | Open-web pages and articles. Needs `EXA_API_KEY`. Paid per search. |

Skip the question **only** when the user already named the platforms in their
request ("search reddit and arxiv for…") or passed `--search` explicitly. If
the user declines to choose, use the default set.

Pass the answer through as `--search reddit,arxiv,…`. Unreachable platforms are
dropped with a reason on stderr rather than silently returning nothing — an
empty source reads as "nobody discussed this", which is a different claim from
"we could not look".

# STEP 2 — WRITE THE PLAN

The engine will tell you this on stderr, and it means it. Without `--plan` it
falls back to a deterministic single-subquery plan — the headless/cron path,
and noticeably thinner. **You are the LLM. Write the plan.**

Decompose into 2–4 subqueries covering distinct facets, set `freshness_mode` to
`evergreen_ok` (this is not a news query), and list only the platforms the user
actually chose:

```json
{
  "intent": "concept",
  "freshness_mode": "evergreen_ok",
  "cluster_mode": "none",
  "subqueries": [
    {
      "label": "primary",
      "search_query": "speculative decoding",
      "ranking_query": "How does speculative decoding work and when does it help?",
      "sources": ["reddit", "arxiv"],
      "weight": 1.0
    },
    {
      "label": "failure-modes",
      "search_query": "speculative decoding overhead regression",
      "ranking_query": "When does speculative decoding fail to deliver a speedup?",
      "sources": ["reddit", "hackernews"],
      "weight": 0.8
    }
  ]
}
```

Pass with `--plan '<json>'` or `--plan /path/to/plan.json`. Resolve concrete
subreddits when you know them — `--subreddits LocalLLaMA,MachineLearning`
materially outperforms letting the engine discover them over long windows.

# STEP 3 — RUN

```bash
python3 "$SKILL_DIR/scripts/alltime.py" "<topic>" --search <chosen> [flags]
```

| Flag | Meaning |
|---|---|
| `--search a,b` | Platforms. Defaults to the reachable default-on set (`reddit,x`). `all` lets the engine pick. |
| `--list-sources` | What this machine can reach, and how each handles the window. |
| `--days N` | Lookback. Default **730** (two years). |
| `--all-time` | No lower bound. Reddit `t=all`, no arXiv cutoff. |
| `--deep` / `--quick` | Recall vs latency. `--deep` gets 100 arXiv papers, 40 videos. |
| `--arxiv-loose` | AND the topic's terms instead of requiring the exact phrase. Use for multi-word topics no paper states verbatim. |
| `--arxiv-sort` | `relevance` (default), `lastUpdatedDate`, `submittedDate`. |
| `--explain` | Resolved engine, seam checks, applied overrides, chosen platforms. Researches nothing. |
| `--gh-repos N` | Repos to scope GitHub's issue search to. Default **4**; `0` restores upstream's global-only search. |
| `--gh-per-repo N` | Rows per scoped repo lane. Default 8/12/20 by depth. |
| `--gh-scope a/b,c/d` | Pin the scoped repos instead of resolving them. |
| `--emit json\|md\|compact` | Passed through to the engine. |

Unrecognized flags pass straight through, so everything in `/last30days --help`
still works (`--subreddits`, `--max-results`, `--store`, `--save-dir`, …).

## How it works

This is an **adapter**, not a fork. It imports the installed `/last30days`
engine and overrides only the places that hardcode a ~30-day horizon. That
engine keeps owning the hard parts: getting past Reddit's shreddit anti-bot
wall without an API key, the `arxiv-pp-cli` wrapper, and the
relevance/rerank/dedupe/fusion stack.

| Platform | Window handling |
|---|---|
| Reddit — search & listing lanes | **patched** — `DEPTH_CONFIG[*].timeframe` raised so upstream's clamp resolves to the requested bucket instead of collapsing to `month`; `reddit_listing.TIMEFRAME` and `reddit_rss._build_urls` widened too |
| Reddit — arctic-shift archive lane | **patched** (engine ≥ 3.23.0) — upstream accepts a `timeframe` and never reads it, so the lane returns the newest N posts regardless of window. Replaced with a date-bounded version that slices the window and fetches each slice |
| arXiv | **patched** — `RECENCY_DAYS` 365 → requested window; results 5/10/20 → 15/40/100 |
| X | native — builds `since:`/`until:` from the window |
| Hacker News | native — full archive via Algolia `numericFilters` |
| GitHub | window native; **aim patched** — see below |
| YouTube | native — soft filter `>= from_date`, no ceiling |
| Web (Exa) | native — `startPublishedDate`/`endPublishedDate` from the window |

### GitHub: scoped instead of unbounded

GitHub's window was never the problem — `created:>{from}` honours whatever it
is given. The problem is aim. Upstream asks `/search/issues` for
`"<topic> created:>{from}"` across all of GitHub, reaction-sorted; over 30 days
that is a reasonable net, but over 730 the corpus is ~24x larger with the same
query, so reaction-sorting surfaces whatever was loudest *anywhere*. Measured
on `speculative decoding` over three years: "Rewrite Bun in Rust" (6,161
reactions) ranked fourth.

So `/alltime` resolves the topic's repos from GitHub's own repo index and adds
one `repo:owner/name <topic> is:issue created:{from}..{to}` lane per repo:

1. one `/search/repositories` call per subquery's core subject, cached
2. drop forks and archived, demote awesome-lists and paper lists, keep only
   repos with **≥100 stars and ≥3 open issues**, rank by topic overlap then stars
3. one scoped issue search per surviving repo, merged into upstream's envelope

Everything downstream — the parser, the date filter, comment enrichment,
rerank, dedupe — is untouched. Measured on the same query: 30 items → 70, the
new ones from `deepseek-ai/DeepSpec`, `z-lab/dflash`, `NVIDIA/Model-Optimizer`
and `youssofal/MTPLX`, none of which the global lane reached.

Two behaviours are worth knowing:

- **It abstains when there is no landscape.** `sourdough hydration` returns 21
  repos, all 0-star with no issues; `retrieval augmented generation failure
  modes` returns six of the same. Below two survivors the lane logs why and
  leaves the global search alone, because four lanes of nothing is worse than
  one wide net.
- **The merge interleaves, it does not re-sort.** `parse_github_response`
  weights arrival position (0.6) above topical content (0.4), so concatenating
  and reaction-sorting would bury every scoped row — they are precise but quiet
  — beneath the loud off-topic ones they exist to displace.

Needs a token (`GITHUB_TOKEN` or `gh auth login`). Without one the lane is
skipped: the anonymous tier allows ~10 searches a minute, and spending them on
scoping would starve the search that already works.

## Seam checks

The adapter reaches into upstream internals, and upstream ships fast (3.16.0 →
3.23.0 in about three months, with pipeline.py gaining a thousand lines). Every
override has a startup check that names the exact seam if it moves, rather than
silently returning a narrow window.

```bash
python3 "$SKILL_DIR/scripts/alltime.py" --explain
```

A failed seam **aborts** the run with the moved seam named. That is the signal
to update `scripts/adapter/overrides.py` — or, if it recurs, to vendor the
modules outright. `ALLTIME_ENGINE_DIR` pins a specific engine install.

Pinned to and seam-tested against engine **3.24.0**: seventeen seams, eight
fatal. The window seams abort. The four rate-limit seams, the two archive-lane
seams, the two GitHub-scoping seams and the Hacker News check only warn — a moved seam there costs Reddit
reach or throttling, not window correctness, and killing a run over it would
be the worse failure.

## Honest limits

- **Reddit's `t=` buckets are coarse.** No "two years" bucket exists, so a
  730-day request fetches `t=all` and the engine's date filter trims the tail.
  Correct results, but ranking sees an all-time candidate pool first.
- **Long windows flatten recency scoring.** `recency_score` is a linear ramp
  over the window; across years it is near-uniform, so ranking is effectively
  relevance + engagement. Right trade for research, but a 2019 and a 2026
  thread of equal quality are not ordered by age.
- **Reddit relevance degrades on `t=all`** for generic topics — bigger
  candidate pool, unchanged query. Pass `--subreddits` for anything ambiguous.
- **The archive lane samples the window, it does not sweep it.** arctic-shift
  orders within a date bound, so each slice returns posts clustered at its
  newest edge. Contiguous even slices make those edges evenly spaced — a
  730-day run samples four points, all-time samples eight — but between two
  sample points the lane sees nothing. More reach than upstream's single
  newest-N grab; still a sample, not a census.
- **The archive lane has no sort lanes.** Upstream's own note: arctic-shift is
  recency-only, with no top/hot/new. Slicing widens *when* it looks, not *how*
  it ranks. Engagement ordering still comes from the shreddit lane and from
  the engine's own scoring.
- **GitHub scoping is only as good as the repo index.** It ranks by stars
  inside a topic-overlap bucket, and `token_overlap_relevance` saturates near
  1.0 for most repo descriptions — so in practice stars carry the ranking and
  the quality floor does the discriminating. A field whose centre of gravity is
  a repo that never names the topic in its description will be missed; pass
  `--gh-scope` when you already know the repos.
- **Scoped lanes query issues only.** PR traffic inside one repo is mostly
  dependabot and merge chatter, and dropping it halves the request count. The
  global lane still covers PRs site-wide.
- **arXiv is relevance-sorted, not exhaustive.** 100 papers at `--deep` is a
  strong sample, not a systematic review.
- **YouTube is slow.** Roughly 25s added per run; transcripts cost more.
- **No new integrations.** Every platform here is one `/last30days` already
  wires up. Reach comes from removing ceilings, not from new scrapers. `web`
  is the engine's own `grounding` source on its `exa` backend — surfaced here,
  not added.

## Requirements

- `/last30days` installed (`claude plugin install last30days@last30days-skill`)
- Python 3.12+
- Reddit, Hacker News and GitHub need nothing. arXiv needs `arxiv-pp-cli`;
  YouTube needs `yt-dlp` (or an SC key); X needs browser session or API creds.
  `--list-sources` reports what is actually reachable.
- **Web (Exa)** needs `EXA_API_KEY` — the environment, or
  `~/.config/last30days/.env` where the engine already keeps its other keys.
  Exa is billed per search, so it stays off by default.
