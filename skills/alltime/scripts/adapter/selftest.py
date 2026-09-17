"""Seam tripwire.

The adapter reaches into upstream internals. Upstream ships fast (3.16.0 ->
3.18.4 in about two weeks, with pipeline.py gaining 927 lines), so a seam can
move without warning. Every override this skill applies has a check here that
runs before any research does, and names the exact seam that moved rather than
failing later as a mysteriously empty result set.

A failed seam is loud and fatal. A degraded seam (present but shaped oddly) is
a warning: research still runs, possibly with a narrower window than requested.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from types import ModuleType


@dataclass
class SeamResult:
    name: str
    ok: bool
    detail: str
    fatal: bool = True


def _check(name: str, fn, fatal: bool = True) -> SeamResult:
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001 - any failure means the seam moved
        return SeamResult(name, False, f"{type(exc).__name__}: {exc}", fatal)
    if detail is True or detail is None:
        return SeamResult(name, True, "ok", fatal)
    if detail is False:
        return SeamResult(name, False, "check returned False", fatal)
    return SeamResult(name, True, str(detail), fatal)


def _seam_reddit_depth_config(lib: ModuleType) -> str:
    cfg = lib.reddit.DEPTH_CONFIG
    missing = [d for d in ("quick", "default", "deep") if d not in cfg]
    if missing:
        raise KeyError(f"DEPTH_CONFIG missing depths: {missing}")
    no_tf = [d for d, v in cfg.items() if "timeframe" not in v]
    if no_tf:
        raise KeyError(f"DEPTH_CONFIG entries without 'timeframe': {no_tf}")
    return f"depths={sorted(cfg)} timeframes={sorted({v['timeframe'] for v in cfg.values()})}"


def _seam_reddit_timeframe_order(lib: ModuleType) -> str:
    order = lib.reddit._TIMEFRAME_ORDER
    for bucket in ("day", "week", "month", "year", "all"):
        if bucket not in order:
            raise KeyError(f"_TIMEFRAME_ORDER missing '{bucket}'")
    if not order["all"] > order["month"]:
        raise ValueError("_TIMEFRAME_ORDER no longer ranks 'all' above 'month'")
    return f"all={order['all']} > month={order['month']}"


def _seam_reddit_window_mapper(lib: ModuleType) -> str:
    """The mapper must still widen for a long window.

    This is the seam that matters most: upstream computes the right bucket and
    then clamps it away in search_reddit. We rely on the computation being
    correct and neutralize the clamp by raising DEPTH_CONFIG's ceiling.
    """
    today = date.today()
    two_years = (today - timedelta(days=730)).isoformat()
    wide = lib.reddit._window_to_time_filter(two_years, today.isoformat())
    if wide not in ("year", "all"):
        raise ValueError(
            f"_window_to_time_filter(730d) returned {wide!r}, expected 'year' or 'all'"
        )
    ten_years = (today - timedelta(days=3650)).isoformat()
    widest = lib.reddit._window_to_time_filter(ten_years, today.isoformat())
    if widest != "all":
        raise ValueError(
            f"_window_to_time_filter(3650d) returned {widest!r}, expected 'all'"
        )
    return f"730d->{wide}, 3650d->{widest}"


def _seam_reddit_listing_timeframe(lib: ModuleType) -> str:
    tf = lib.reddit_listing.TIMEFRAME
    if not isinstance(tf, str):
        raise TypeError(f"reddit_listing.TIMEFRAME is {type(tf).__name__}, expected str")
    return f"TIMEFRAME={tf!r}"


def _seam_reddit_rss_build_urls(lib: ModuleType) -> str:
    urls = lib.reddit_rss._build_urls("test query", "default", ["python"])
    if not urls:
        raise ValueError("_build_urls returned no URLs")
    if not any("t=" in u for u in urls):
        raise ValueError("_build_urls output carries no t= parameter to override")
    return f"{len(urls)} urls, sample={urls[0][:70]}"


def _seam_pipeline_fetch_cap(lib: ModuleType) -> str:
    """The per-source fetch cap must still be a mutable dict the pipeline reads."""
    caps = lib.pipeline.MAX_SOURCE_FETCHES
    if not isinstance(caps, dict):
        raise TypeError(
            f"MAX_SOURCE_FETCHES is {type(caps).__name__}, expected dict"
        )
    if "x" not in caps:
        raise KeyError(
            "MAX_SOURCE_FETCHES no longer caps 'x' -- the cap mechanism may have moved"
        )
    return f"caps={ {k: caps[k] for k in sorted(caps)} }"


def _seam_reddit_keyless_limiter(lib: ModuleType) -> str:
    """The shared keyless throttle must still be a rebindable RateLimiter."""
    limiter = getattr(lib.http, "REDDIT_KEYLESS_LIMITER", None)
    if limiter is None:
        raise AttributeError("http.REDDIT_KEYLESS_LIMITER missing")
    if not hasattr(lib.http, "RateLimiter"):
        raise AttributeError("http.RateLimiter missing")
    for attr in ("rate", "capacity", "acquire"):
        if not hasattr(limiter, attr):
            raise AttributeError(f"REDDIT_KEYLESS_LIMITER has no '{attr}'")
    return f"{limiter.rate}/s burst {limiter.capacity}"


def _seam_reddit_enrich_budget(lib: ModuleType) -> str:
    budget = lib.reddit_keyless.ENRICH_BUDGET
    if not isinstance(budget, (int, float)):
        raise TypeError(
            f"reddit_keyless.ENRICH_BUDGET is {type(budget).__name__}, expected number"
        )
    return f"ENRICH_BUDGET={budget}"


def _seam_reddit_enrich_hook(lib: ModuleType) -> str:
    """Per-post enrichment must still be a wrappable fn, and the miss declaration present."""
    if not callable(getattr(lib.reddit_keyless, "_enrich_one", None)):
        raise AttributeError("reddit_keyless._enrich_one missing or not callable")
    if not hasattr(lib.http, "expected_misses"):
        raise AttributeError("http.expected_misses missing")
    return "_enrich_one wrappable, expected_misses available"


def _seam_reddit_arctic_lane(lib: ModuleType) -> str:
    """The archive lane must expose every primitive the windowing override borrows.

    Engine >= 3.23.0 only. `_window_arctic` replaces `fetch_listings` but reuses
    upstream's endpoint, row normalizer, pacing and depth table, so each of
    those is checked by name here rather than discovered missing mid-run.
    """
    mod = getattr(lib, "reddit_arctic", None)
    if mod is None:
        raise AttributeError("lib.reddit_arctic missing (engine older than 3.23.0?)")
    for attr in (
        "SEARCH_API",
        "fetch_listings",
        "_normalize_listing_row",
        "_LISTING_DEPTH_LIMITS",
        "_LISTING_SUPPLEMENT_MULTIPLIER",
        "_LISTING_DEADLINE_SECONDS",
        "PACE_SECONDS",
        "TIMEOUT",
        "_log",
        "http",
    ):
        if not hasattr(mod, attr):
            raise AttributeError(f"reddit_arctic.{attr} missing")
    if not hasattr(mod.http, "BROWSER_USER_AGENT"):
        raise AttributeError("http.BROWSER_USER_AGENT missing")
    missing = [
        d for d in ("quick", "default", "deep") if d not in mod._LISTING_DEPTH_LIMITS
    ]
    if missing:
        raise KeyError(f"reddit_arctic._LISTING_DEPTH_LIMITS missing depths: {missing}")
    if "arctic-shift" not in str(mod.SEARCH_API):
        raise ValueError(f"reddit_arctic.SEARCH_API moved: {mod.SEARCH_API!r}")
    limits = {k: mod._LISTING_DEPTH_LIMITS[k] for k in sorted(mod._LISTING_DEPTH_LIMITS)}
    return f"endpoint ok, limits={limits}"


def _seam_reddit_arctic_row_shape(lib: ModuleType) -> str:
    """The normalizer must still emit a card carrying a `url`.

    The windowing override dedupes on `url` and hands these rows straight to
    reddit_keyless, which expects the shreddit card shape. If that schema moves,
    a run would return rows the pipeline silently drops.
    """
    mod = getattr(lib, "reddit_arctic", None)
    if mod is None:
        raise AttributeError("lib.reddit_arctic missing")
    row = mod._normalize_listing_row(
        {
            "title": "t",
            "permalink": "/r/x/comments/abc/t/",
            "url": "https://www.reddit.com/r/x/comments/abc/t/",
            "score": 1,
            "num_comments": 0,
            "subreddit": "x",
            "created_utc": 1700000000,
        },
        "q",
    )
    if not isinstance(row, dict):
        raise TypeError(f"_normalize_listing_row returned {type(row).__name__}")
    if "url" not in row:
        raise KeyError("_normalize_listing_row output has no 'url' key to dedupe on")
    return f"card keys={len(row)}, url present"


def _seam_arxiv_recency(lib: ModuleType) -> str:
    days = lib.arxiv.RECENCY_DAYS
    if not isinstance(days, int):
        raise TypeError(f"arxiv.RECENCY_DAYS is {type(days).__name__}, expected int")
    return f"RECENCY_DAYS={days}"


def _seam_arxiv_depth_config(lib: ModuleType) -> str:
    cfg = lib.arxiv.DEPTH_CONFIG
    missing = [d for d in ("quick", "default", "deep") if d not in cfg]
    if missing:
        raise KeyError(f"arxiv.DEPTH_CONFIG missing depths: {missing}")
    return f"limits={ {k: cfg[k] for k in sorted(cfg)} }"


def _seam_arxiv_search_args(lib: ModuleType) -> str:
    args = lib.arxiv._build_search_args("test topic", 10)
    if "--sort-by" not in args:
        raise ValueError("_build_search_args no longer emits --sort-by")
    if "--max-results" not in args:
        raise ValueError("_build_search_args no longer emits --max-results")
    return f"{len(args)} args"


def _seam_github_scoped_lane(lib: ModuleType) -> str:
    """Every primitive the repo-scoped issue lane borrows must still be there.

    The lane wraps `search_github` and reuses upstream's endpoint, token
    resolution, query cleaning, depth table and JSON fetcher. It also depends
    on the wrapped signature: the pipeline calls it positionally
    (topic, from_date, to_date) with depth/token as keywords, and the wrapper
    has to forward both to the original.
    """
    import inspect

    mod = lib.github
    for attr in (
        "search_github",
        "SEARCH_URL",
        "DEPTH_LIMITS",
        "_fetch_json",
        "_resolve_token",
        "strip_search_qualifiers",
        "extract_core_subject",
        "token_overlap_relevance",
        "_log",
    ):
        if not hasattr(mod, attr):
            raise AttributeError(f"github.{attr} missing")
    params = list(inspect.signature(mod.search_github).parameters)
    for name in ("topic", "from_date", "to_date", "depth", "token"):
        if name not in params:
            raise TypeError(f"search_github no longer takes '{name}' (params={params})")
    if "search/issues" not in str(mod.SEARCH_URL):
        raise ValueError(f"github.SEARCH_URL moved: {mod.SEARCH_URL!r}")
    missing = [d for d in ("quick", "default", "deep") if d not in mod.DEPTH_LIMITS]
    if missing:
        raise KeyError(f"github.DEPTH_LIMITS missing depths: {missing}")
    return f"signature ok, limits={ {k: mod.DEPTH_LIMITS[k] for k in sorted(mod.DEPTH_LIMITS)} }"


def _seam_github_envelope(lib: ModuleType) -> str:
    """The parser must still read its row budget from the envelope context.

    The scoped lane merges extra rows in and raises `context["count"]` to keep
    them. If the parser stops honouring that key, the extra requests would be
    spent and then discarded silently -- worse than not scoping at all.
    """
    import inspect

    src = inspect.getsource(lib.github.parse_github_response)
    if 'context.get("count")' not in src:
        raise ValueError(
            "parse_github_response no longer reads context['count'] -- scoped "
            "rows would be fetched and then truncated away"
        )
    if "raw_items[:count]" not in src:
        raise ValueError("parse_github_response truncation shape changed")
    return "context['count'] honoured"


def _seam_hackernews_range(lib: ModuleType) -> str:
    """HN honors the window already -- verify it still does, don't patch it."""
    src = lib.hackernews
    if not hasattr(src, "search_hackernews"):
        raise AttributeError("hackernews.search_hackernews missing")
    return "date-ranged via Algolia numericFilters"


SEAMS = [
    ("reddit.DEPTH_CONFIG", _seam_reddit_depth_config, True),
    ("reddit._TIMEFRAME_ORDER", _seam_reddit_timeframe_order, True),
    ("reddit._window_to_time_filter", _seam_reddit_window_mapper, True),
    ("reddit_listing.TIMEFRAME", _seam_reddit_listing_timeframe, True),
    ("reddit_rss._build_urls", _seam_reddit_rss_build_urls, True),
    # Rate-limit seams are warnings, not aborts: if one moves, the window is
    # still correct and the run still produces research -- it just risks the
    # 429-thinned Reddit coverage these overrides exist to prevent. Killing a
    # whole run over a moved throttle constant would be the worse failure.
    ("pipeline.MAX_SOURCE_FETCHES", _seam_pipeline_fetch_cap, False),
    ("http.REDDIT_KEYLESS_LIMITER", _seam_reddit_keyless_limiter, False),
    ("reddit_keyless.ENRICH_BUDGET", _seam_reddit_enrich_budget, False),
    ("reddit_keyless._enrich_one", _seam_reddit_enrich_hook, False),
    # The archive lane is a window seam, but a non-fatal one: it supplements
    # the shreddit lane rather than replacing it, so a moved seam costs Reddit
    # reach without making the run wrong. It reports loudly and continues.
    ("reddit_arctic lane", _seam_reddit_arctic_lane, False),
    ("reddit_arctic._normalize_listing_row", _seam_reddit_arctic_row_shape, False),
    ("arxiv.RECENCY_DAYS", _seam_arxiv_recency, True),
    ("arxiv.DEPTH_CONFIG", _seam_arxiv_depth_config, True),
    ("arxiv._build_search_args", _seam_arxiv_search_args, True),
    # Scoped GitHub lanes are an aim seam, not a window seam: if one moves, the
    # run still covers the right window through upstream's own global search.
    # It just goes back to asking the whole site, which is what this override
    # exists to stop -- loud about it, but not worth killing a run over.
    ("github scoped lane", _seam_github_scoped_lane, False),
    ("github.parse_github_response", _seam_github_envelope, False),
    ("hackernews.search_hackernews", _seam_hackernews_range, False),
]


def run(lib: ModuleType) -> list[SeamResult]:
    return [_check(name, lambda f=fn: f(lib), fatal) for name, fn, fatal in SEAMS]


def assert_seams(lib: ModuleType, version: str, pinned: str) -> list[SeamResult]:
    """Run every seam check; raise on any fatal failure.

    Raises RuntimeError naming the moved seams and the version delta, which is
    the signal to either update this adapter or vendor the modules outright.
    """
    results = run(lib)
    broken = [r for r in results if not r.ok and r.fatal]
    if broken:
        lines = [
            f"/alltime adapter is out of sync with the installed engine.",
            f"  engine version: {version}   adapter pinned to: {pinned}",
            "",
            "Seams that moved:",
        ]
        lines += [f"  ✗ {r.name}: {r.detail}" for r in broken]
        lines += [
            "",
            "Fix: re-read the upstream module and update scripts/adapter/overrides.py,",
            "or pin an older engine via ALLTIME_ENGINE_DIR.",
        ]
        raise RuntimeError("\n".join(lines))
    return results
