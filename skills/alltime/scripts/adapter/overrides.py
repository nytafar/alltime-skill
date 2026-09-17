"""The seam patches.

Most of this exists because the upstream engine hardcodes a ~30-day horizon in a
handful of spots. Each override is deliberately the smallest intervention that
removes a ceiling, so upstream keeps owning the hard parts (anti-bot evasion,
ranking, dedupe) and this file owns only "how far back".

Two overrides are not about the window at all: `_cap_reddit_fetches` and
`_throttle_reddit_keyless` retune request volume, because the multi-subquery
plans this skill runs overwhelm a Reddit throttle sized for a 30-day brief.

Engine 3.23.0 added the arctic-shift archive as a second keyless Reddit lane,
which is why Reddit stopped 429ing out of runs. It arrives recency-only --
upstream's own docstring calls that a KNOWN LIMITATION -- so `_window_arctic`
is the override that makes the new lane honour the requested window instead of
returning the newest N posts regardless of it.

What needs no patch, because it already honors the requested window:
  hackernews  Algolia `numericFilters: created_at_i>X,created_at_i<Y`
  github      date-ranged natively
  bird_x      `search_x` builds `since:{from_date}`
  xquik       builds `since:{from_date} until:{to_date}`

(The `timedelta(days=30)` in bird_x.py and xquik.py is inside `probe_works()`,
an auth health check -- not the research window. Do not "fix" it.)
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from types import ModuleType

# Reddit's `t=` buckets, widest last.
REDDIT_BUCKETS = ("hour", "day", "week", "month", "year", "all")

# arXiv recency cutoff used for an all-time run. The upstream parser compares
# `age_days > RECENCY_DAYS`, so this just has to outlive arXiv itself (1991).
ARXIV_NO_CUTOFF = 100_000

# --- Reddit request volume -------------------------------------------------
#
# Upstream sizes its keyless-Reddit throttle for a 30-day brief: one subquery,
# a handful of feeds. Two things this skill does multiply that fan-out, and
# neither is a window seam, so both are corrected here rather than tolerated:
#
#   * the LLM planner emits 2-3 subqueries, and `MAX_SOURCE_FETCHES` caps every
#     capped source EXCEPT reddit -- so the whole keyless pipeline (~16
#     reddit.com requests at default depth) runs once per subquery, concurrently
#   * `REDDIT_KEYLESS_LIMITER` refills at 5/s = 300/min sustained, far above
#     what keyless reddit.com tolerates, so it effectively never engages
#
# The result was `HTTP 429: Too Many Requests` partway through a run, reported
# honestly as partial coverage but leaving Reddit thin. Both values below are
# *reductions* in aggressiveness using mechanisms upstream already ships.

# Uses upstream's own `MAX_SOURCE_FETCHES` mechanism (which already caps x at 2)
# to fetch Reddit ONCE per run rather than once per subquery.
#
# One, not two, because Reddit ignores subquery differentiation: pipeline.py
# resolves `reddit_query = raw_topic or subquery.search_query`, so every
# subquery issues a byte-identical Reddit query. Measured on a 4-subquery plan:
# the two allowed fetches raced the *same* `/search.rss?q=...` URL in the same
# instant -- one 200, one 429, then the retry 429'd too. The duplicate cannot
# add coverage (same query, and downstream dedupe would collapse it anyway); it
# can only collide with itself. Every other reddit.com endpoint in that run
# returned 200, including ten ~800KB shreddit listings back to back.
REDDIT_FETCH_CAP = 1

# Sustained requests/sec against keyless reddit.com, shared across the RSS,
# listing and shreddit lanes. 1.0/s = 60/min, a 5x cut from upstream's 300/min.
# Override with ALLTIME_REDDIT_RATE to tune without editing this file.
REDDIT_RATE_PER_SEC = 1.0
REDDIT_BURST = 3

# Comment enrichment runs under a wall-clock budget, so slowing the limiter
# would silently cost comments -- the highest signal-per-token content the
# engine produces. Widen the budget to match the slower rate.
#
# Engine 3.23.0 raised its own default to 45. That is sized for one 30-day
# discovery pass; a multi-year window surfaces more distinct threads worth
# enriching, and comments are where this skill's evidence actually lives.
REDDIT_ENRICH_BUDGET = 120

# --- arctic-shift archive lane (engine >= 3.23.0) --------------------------
#
# `reddit_arctic.fetch_listings` accepts a `timeframe` argument and never reads
# it. Every call issues `?subreddit={sub}&limit={n}&sort=desc` with no date
# bound, so it returns the newest N posts in the subreddit -- measured: an
# unbounded 5-row probe of r/ClaudeAI came back with five posts all from the
# same day. For a 30-day brief that is the right answer. For a two-year or
# all-time run it is a hard recency ceiling on the lane that now carries most
# of Reddit's coverage.
#
# The endpoint does support bounds: `after=` and `before=` (ISO dates) both
# bind correctly, verified against the live API. So the fix is not a new
# scraper -- it is passing the window the caller already asked for, and
# slicing it so the archive is sampled across the whole span rather than
# piled onto whichever end `sort=desc` lands on.

# Reddit's public launch. The floor for an all-time run; nothing predates it.
REDDIT_EPOCH = "2005-06-23"

# One slice per this many days, clamped. 730d -> 4 slices, all-time -> 8.
ARCTIC_DAYS_PER_SLICE = 180
ARCTIC_MIN_SLICES = 1
ARCTIC_MAX_SLICES = 8

# Total posts per subreddit across ALL slices, as a multiple of the volume
# upstream would have fetched for the same call.
#
# 1, not 2, and this was measured rather than reasoned. The arctic lane sends
# no query to the API -- `query` is used only for local relevance scoring, and
# the endpoint 422s ("Timeout. Maybe slow down a bit") whenever full-text
# search is combined with subreddit and date bounds, reproducibly. So every
# extra slice returns unfiltered subreddit listing, and raising the multiplier
# to 2 grew the candidate pool from ~300 to 612 and pushed a 1,197-point
# orchestration thread out of the final set in favour of a support question.
# The relevance floor is what pays for extra volume, and on this lane volume
# is indiscriminate.
#
# So: same total volume upstream asks for, redistributed across the window.
# Date spread is bought with request count, not with pool size.
ARCTIC_RECALL_MULTIPLIER = 1
ARCTIC_MIN_LIMIT_PER_SLICE = 5

# Upstream's deadline (45s) assumes one request per subreddit. Slicing
# multiplies that, so the deadline scales with it rather than truncating the
# oldest slices -- which would silently reintroduce the recency bias.
ARCTIC_DEADLINE_BASE = 45
ARCTIC_DEADLINE_PER_SLICE = 20
ARCTIC_DEADLINE_MAX = 240


@dataclass
class OverrideLog:
    """What was actually changed, for --explain and for honest reporting."""

    entries: list[str] = field(default_factory=list)

    def add(self, seam: str, before: object, after: object) -> None:
        self.entries.append(f"{seam}: {before!r} -> {after!r}")

    def __iter__(self):
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


def days_to_reddit_bucket(days: int) -> str:
    """Smallest Reddit bucket covering `days`.

    Mirrors upstream's `_days_to_reddit_bucket` rather than calling it, so a
    change there surfaces as a seam-check failure instead of silently altering
    this skill's window.
    """
    covered = days + 1
    if covered <= 1:
        return "day"
    if covered <= 7:
        return "week"
    if covered <= 31:
        return "month"
    if covered <= 366:
        return "year"
    return "all"


def _raise_reddit_ceiling(lib: ModuleType, bucket: str, log: OverrideLog) -> None:
    """Neutralize the clamp in `search_reddit` by raising the depth ceiling.

    Upstream computes the correct bucket from the requested date range and then
    keeps `min(window_bucket, depth_bucket)` -- see reddit.py:

        timeframe = _window_tf if _TIMEFRAME_ORDER[_window_tf] <= _TIMEFRAME_ORDER[_depth_tf] else _depth_tf

    Since every depth caps at "month", any request wider than ~31 days collapses
    back to a month. Raising the depth entry to the requested bucket makes the
    existing min() resolve to the window bucket, so upstream's own (correct)
    date-range logic decides the window. No function is replaced.
    """
    cfg = lib.reddit.DEPTH_CONFIG
    for depth, entry in cfg.items():
        before = entry.get("timeframe")
        if before == bucket:
            continue
        entry["timeframe"] = bucket
        log.add(f"reddit.DEPTH_CONFIG[{depth}].timeframe", before, bucket)


def _retarget_defaults(fn, old: str, new: str) -> bool:
    """Rewrite `old` -> `new` in a function's default arguments.

    `reddit_listing` binds `timeframe: str = TIMEFRAME` at definition time, so
    rebinding the module constant alone changes nothing for existing callers.
    """
    changed = False
    if fn.__defaults__:
        updated = tuple(new if d == old else d for d in fn.__defaults__)
        if updated != fn.__defaults__:
            fn.__defaults__ = updated
            changed = True
    if getattr(fn, "__kwdefaults__", None):
        updated_kw = {
            k: (new if v == old else v) for k, v in fn.__kwdefaults__.items()
        }
        if updated_kw != fn.__kwdefaults__:
            fn.__kwdefaults__ = updated_kw
            changed = True
    return changed


def _widen_reddit_listing(lib: ModuleType, bucket: str, log: OverrideLog) -> None:
    """Point the keyless shreddit listing lane at the requested bucket."""
    mod = lib.reddit_listing
    old = mod.TIMEFRAME
    if old == bucket:
        return
    mod.TIMEFRAME = bucket
    log.add("reddit_listing.TIMEFRAME", old, bucket)

    retargeted = []
    for name, obj in vars(mod).items():
        if inspect.isfunction(obj) and _retarget_defaults(obj, old, bucket):
            retargeted.append(name)
    if retargeted:
        log.add(
            "reddit_listing defaults", f"{len(retargeted)} fns @ {old}", bucket
        )


_T_PARAM = re.compile(r"([?&])t=[a-z]+")


def _widen_reddit_rss(lib: ModuleType, bucket: str, log: OverrideLog) -> None:
    """Rewrite `t=` on the keyless RSS lane's URLs.

    `_build_urls` bakes `t=month` into three f-string templates, so there is no
    constant to rebind. Wrapping the function and rewriting its output keeps
    this robust against upstream adding or reordering feed URLs.
    """
    mod = lib.reddit_rss
    original = mod._build_urls
    if getattr(original, "_alltime_wrapped", False):
        return

    def _build_urls_windowed(*args, **kwargs):
        return [_T_PARAM.sub(rf"\1t={bucket}", u) for u in original(*args, **kwargs)]

    _build_urls_windowed._alltime_wrapped = True  # type: ignore[attr-defined]
    _build_urls_windowed.__name__ = original.__name__
    _build_urls_windowed.__doc__ = original.__doc__
    mod._build_urls = _build_urls_windowed
    log.add("reddit_rss._build_urls t=", "month", bucket)


def _cap_reddit_fetches(lib: ModuleType, log: OverrideLog) -> None:
    """Stop Reddit re-running its whole keyless pipeline for every subquery.

    `pipeline.MAX_SOURCE_FETCHES` already caps x/jobs/linkedin/stocktwits/
    trustpilot; reddit is simply absent from the dict, so a 3-subquery plan
    fires three concurrent Reddit pipelines. Adding the key uses upstream's own
    cap mechanism -- no new control flow.
    """
    caps = lib.pipeline.MAX_SOURCE_FETCHES
    before = caps.get("reddit")
    if before == REDDIT_FETCH_CAP:
        return
    caps["reddit"] = REDDIT_FETCH_CAP
    log.add("pipeline.MAX_SOURCE_FETCHES[reddit]", before, REDDIT_FETCH_CAP)


def _throttle_reddit_keyless(lib: ModuleType, log: OverrideLog) -> None:
    """Slow the shared keyless-Reddit token bucket to a survivable rate.

    `reddit_keyless_get_text` looks `REDDIT_KEYLESS_LIMITER` up in the http
    module namespace at call time, so rebinding the attribute retunes every
    lane (RSS, listing, shreddit) at once -- same class, smaller numbers.
    """
    import os

    try:
        rate = float(os.environ.get("ALLTIME_REDDIT_RATE") or REDDIT_RATE_PER_SEC)
    except ValueError:
        rate = REDDIT_RATE_PER_SEC
    if rate <= 0:
        rate = REDDIT_RATE_PER_SEC

    limiter = lib.http.REDDIT_KEYLESS_LIMITER
    before = f"{limiter.rate}/s burst {limiter.capacity}"
    # Engine 3.23.0 adopted 1.0/s itself, so this is now usually a no-op. Left
    # in place because it is the seam that protects the run if upstream ever
    # raises the rate again, and because ALLTIME_REDDIT_RATE tunes it down.
    if limiter.rate <= rate:
        return  # upstream already at or below the target; leave it alone
    lib.http.REDDIT_KEYLESS_LIMITER = lib.http.RateLimiter(
        rate_per_sec=rate, burst=REDDIT_BURST
    )
    log.add(
        "http.REDDIT_KEYLESS_LIMITER", before, f"{rate}/s burst {REDDIT_BURST}"
    )


def _widen_reddit_enrich_budget(lib: ModuleType, log: OverrideLog) -> None:
    """Widen the comment-enrichment wall-clock budget.

    Separate from `_throttle_reddit_keyless` on purpose. It used to live inside
    it, after an early `return` taken when upstream's limiter was already at or
    below the target rate. Engine 3.23.0 adopted that rate, so from 3.23.0 the
    early return always fired and the budget bump silently stopped applying --
    the skill reported 12 overrides while believing it had applied 13. The two
    concerns are independent: one is how fast we may ask, the other is how long
    we keep asking. They no longer share a control path.
    """
    budget = lib.reddit_keyless.ENRICH_BUDGET
    if budget < REDDIT_ENRICH_BUDGET:
        lib.reddit_keyless.ENRICH_BUDGET = REDDIT_ENRICH_BUDGET
        log.add("reddit_keyless.ENRICH_BUDGET", budget, REDDIT_ENRICH_BUDGET)


def _quiet_reddit_enrichment_misses(lib: ModuleType, log: OverrideLog) -> None:
    """Stop one post's failed comment fetch from condemning the whole source.

    Comment enrichment is best-effort and per-post: `_enrich_one` already
    swallows every exception and keeps the post. But the failure *sink* is
    context-local and blind to that, so a single refused thread is promoted to
    a source-level verdict -- and because `_FAILURE_SPECIFICITY` ranks
    AUTH_FAILED above everything, a lone 401 on one of N posts prints
    "Reddit partial ... HTTP 401: Unauthorized (run doctor for fixes)" over a
    run whose other 15 requests all returned 200. That sends the reader hunting
    for a credential problem that cannot exist: the keyless lanes send no
    credentials at all, so 401/403/429 there is only ever Reddit declining that
    particular thread. Upstream encodes the same reasoning in doctor.py's
    `_PROBE_BLOCKED_STATUSES = {"reddit": frozenset({403, 429})}`.

    `expected_misses` is upstream's own declaration for exactly this -- it drops
    the listed statuses from the sink instead of suppressing the request. Scoped
    to per-post enrichment only, so a discovery-lane failure still surfaces.
    """
    mod = lib.reddit_keyless
    original = mod._enrich_one
    if getattr(original, "_alltime_wrapped", False):
        return

    def _enrich_one_quiet(post):
        with lib.http.expected_misses(401, 403, 429):
            return original(post)

    _enrich_one_quiet._alltime_wrapped = True  # type: ignore[attr-defined]
    _enrich_one_quiet.__name__ = original.__name__
    _enrich_one_quiet.__doc__ = original.__doc__
    mod._enrich_one = _enrich_one_quiet
    log.add("reddit_keyless._enrich_one", "failures escalate", "401/403/429 expected")


def _arctic_slices(days: int | None) -> list[tuple[str, str]]:
    """Split the requested window into (after, before) ISO date pairs.

    Linear rather than geometric: the point of an all-time run is even reach
    across the span, not a recency-weighted sample. Upstream's ranking already
    handles which of the returned posts matter.
    """
    from datetime import date, timedelta

    today = date.today()
    if days is None:
        start = date.fromisoformat(REDDIT_EPOCH)
        span = (today - start).days
    else:
        span = max(days, 1)
        start = today - timedelta(days=span)

    n = max(ARCTIC_MIN_SLICES, min(ARCTIC_MAX_SLICES, round(span / ARCTIC_DAYS_PER_SLICE)))
    step = span / n
    bounds = []
    for i in range(n):
        lo = start + timedelta(days=step * i)
        hi = start + timedelta(days=step * (i + 1)) if i < n - 1 else today
        bounds.append((lo.isoformat(), hi.isoformat()))
    return bounds


def _window_arctic(lib: ModuleType, days: int | None, log: OverrideLog) -> None:
    """Make the arctic-shift archive lane span the requested window.

    Replaces `reddit_arctic.fetch_listings` rather than wrapping it, because
    the URL it builds has no date parameters to rewrite and no constant to
    rebind -- the endpoint is interpolated inline as
    ``f"{SEARCH_API}?subreddit={sub}&limit={n}&sort=desc"``.

    This is the most invasive override in the file, so it borrows every
    upstream primitive it can rather than reimplementing them: `SEARCH_API`,
    `_normalize_listing_row` (the shreddit-compatible card schema that lets
    reddit_keyless consume either backend), `http.get`, `http.BROWSER_USER_AGENT`,
    `PACE_SECONDS` and `TIMEOUT`. What it owns is the loop and the date bounds.
    Each of those names has a seam check, so an upstream rename aborts the run
    instead of silently degrading it.

    Degradation is bounded: on any failure the lane returns `[]`, exactly as
    upstream's does, and the shreddit lane it supplements is untouched.
    """
    mod = lib.reddit_arctic
    original = mod.fetch_listings
    if getattr(original, "_alltime_wrapped", False):
        return

    import time

    slices = _arctic_slices(days)
    deadline_total = min(
        ARCTIC_DEADLINE_MAX,
        ARCTIC_DEADLINE_BASE + ARCTIC_DEADLINE_PER_SLICE * len(slices),
    )

    def fetch_listings_windowed(
        subreddits,
        depth="default",
        query="",
        sorts=None,
        timeframe="month",
        limit=None,
    ):
        if not subreddits:
            return []
        base = limit or mod._LISTING_DEPTH_LIMITS.get(
            depth, mod._LISTING_DEPTH_LIMITS["default"]
        )
        # Mirror upstream's own multi-sort compensation so the totals match
        # call for call, then divide that total across the slices instead of
        # taking it all from the newest end of the archive.
        upstream_total = base * (
            mod._LISTING_SUPPLEMENT_MULTIPLIER if sorts and len(sorts) > 1 else 1
        )
        per_slice = max(
            ARCTIC_MIN_LIMIT_PER_SLICE,
            -(-upstream_total * ARCTIC_RECALL_MULTIPLIER // len(slices)),
        )

        out = []
        seen = set()
        deadline = time.time() + deadline_total
        issued = 0
        for sub in subreddits:
            sub = sub.removeprefix("r/").strip()
            if not sub or sub.lower() == "all":
                continue
            for after, before in slices:
                if time.time() >= deadline:
                    mod._log(
                        f"windowed listing deadline reached after {issued} requests; "
                        f"remaining slices skipped"
                    )
                    return out
                if issued:
                    time.sleep(mod.PACE_SECONDS)
                issued += 1
                # Uniform `sort=desc` within each slice, deliberately. Arctic
                # orders within the bound, so each slice returns posts clustered
                # at its newest edge -- and because the slices are contiguous and
                # evenly spaced, those edges are themselves evenly spaced across
                # the window. Alternating asc/desc was tried and is worse: it
                # makes adjacent slices converge on their shared boundary, which
                # collapsed a 4-point sample to 2.
                url = (
                    f"{mod.SEARCH_API}?subreddit={sub}&limit={per_slice}"
                    f"&sort=desc&after={after}&before={before}"
                )
                try:
                    data = mod.http.get(
                        url,
                        headers={"User-Agent": mod.http.BROWSER_USER_AGENT},
                        timeout=mod.TIMEOUT,
                        retries=1,
                    )
                except Exception as exc:  # network/non-200 -- degrade, never raise
                    mod._log(f"windowed listing failed r/{sub} {after}..{before}: {exc}")
                    continue
                rows = (data or {}).get("data")
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    post = mod._normalize_listing_row(row, query)
                    url_key = post.get("url")
                    if url_key and url_key not in seen:
                        seen.add(url_key)
                        out.append(post)
        return out

    fetch_listings_windowed._alltime_wrapped = True  # type: ignore[attr-defined]
    fetch_listings_windowed.__name__ = original.__name__
    fetch_listings_windowed.__doc__ = original.__doc__
    mod.fetch_listings = fetch_listings_windowed
    span = "all-time" if days is None else f"{days}d"
    log.add(
        "reddit_arctic.fetch_listings",
        "unbounded, sort=desc (newest N)",
        f"{len(slices)} slices over {span}",
    )

    # The deadline constant is read at call time inside upstream's own
    # fetch_listings too, so raise it for the fetch_scores path as well.
    before_deadline = mod._LISTING_DEADLINE_SECONDS
    if before_deadline < deadline_total:
        mod._LISTING_DEADLINE_SECONDS = deadline_total
        log.add(
            "reddit_arctic._LISTING_DEADLINE_SECONDS", before_deadline, deadline_total
        )


def _widen_arxiv(
    lib: ModuleType,
    days: int | None,
    limits: dict[str, int],
    sort_by: str,
    loose_query: bool,
    log: OverrideLog,
) -> None:
    """Lift arXiv's 365-day cutoff and 20-result ceiling.

    Upstream caps arXiv at RECENCY_DAYS=365 and 20 results even at --deep, and
    never paginates -- reasonable for a 30-day social brief, far too thin for a
    literature pass.
    """
    mod = lib.arxiv

    cutoff = ARXIV_NO_CUTOFF if days is None else max(days, 1)
    if mod.RECENCY_DAYS != cutoff:
        log.add("arxiv.RECENCY_DAYS", mod.RECENCY_DAYS, cutoff)
        mod.RECENCY_DAYS = cutoff

    for depth, limit in limits.items():
        before = mod.DEPTH_CONFIG.get(depth)
        if before != limit:
            mod.DEPTH_CONFIG[depth] = limit
            log.add(f"arxiv.DEPTH_CONFIG[{depth}]", before, limit)

    original_args = mod._build_search_args
    if getattr(original_args, "_alltime_wrapped", False):
        return

    def _build_search_args_deep(topic: str, limit: int, *, quoted: bool = True):
        phrase = mod._clean_phrase(topic)
        # Upstream quotes the phrase as a deliberate noise gate. Relaxing it to
        # AND-ed terms widens recall for multi-word research topics that no
        # paper states verbatim; still relevance-sorted, so it degrades to
        # ranking rather than to date-ordered noise. Upstream also retries with
        # quoted=False when the phrase match comes back empty -- honour that.
        if (loose_query or not quoted) and len(phrase.split()) > 1:
            query = " AND ".join(f"all:{t}" for t in phrase.split())
        else:
            query = f'all:"{phrase}"'
        return [
            mod.CLI_BIN,
            "query",
            "--search-query",
            query,
            "--sort-by",
            sort_by,
            "--max-results",
            str(limit),
            "--agent",
        ]

    _build_search_args_deep._alltime_wrapped = True  # type: ignore[attr-defined]
    mod._build_search_args = _build_search_args_deep
    # The wrapper always installs (it carries the limit/sort plumbing), but only
    # report it when it actually diverges from upstream's phrase+relevance query.
    if loose_query or sort_by != "relevance":
        log.add(
            "arxiv._build_search_args",
            "phrase/relevance",
            f"{'loose' if loose_query else 'phrase'}/{sort_by}",
        )


def apply(
    lib: ModuleType,
    *,
    days: int | None,
    arxiv_limits: dict[str, int],
    arxiv_sort: str = "relevance",
    arxiv_loose: bool = False,
) -> OverrideLog:
    """Apply every window override. `days=None` means all-time.

    Returns a log of what changed, so a run can report its real reach instead
    of asserting it.
    """
    log = OverrideLog()
    bucket = "all" if days is None else days_to_reddit_bucket(days)

    _raise_reddit_ceiling(lib, bucket, log)
    _widen_reddit_listing(lib, bucket, log)
    _widen_reddit_rss(lib, bucket, log)
    # Not window seams: a wider window costs no extra requests, but the wider
    # *plan* it arrives with does. These keep the lanes above from 429ing.
    _cap_reddit_fetches(lib, log)
    _throttle_reddit_keyless(lib, log)
    _widen_reddit_enrich_budget(lib, log)
    _quiet_reddit_enrichment_misses(lib, log)
    # The archive lane IS a window seam -- it just arrived after the rest.
    # Best-effort: the lane supplements shreddit, so losing it costs coverage
    # rather than correctness, and aborting a run over it would be worse.
    if hasattr(lib, "reddit_arctic"):
        try:
            _window_arctic(lib, days, log)
        except Exception as exc:  # noqa: BLE001
            log.add("reddit_arctic.fetch_listings", "windowing FAILED", str(exc)[:80])
    _widen_arxiv(lib, days, arxiv_limits, arxiv_sort, arxiv_loose, log)
    return log
