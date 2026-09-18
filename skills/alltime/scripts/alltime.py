#!/usr/bin/env python3
"""/alltime -- deep, time-unbounded research over Reddit and arXiv.

An adapter over the installed /last30days engine. It reuses that engine's
source-access layer (keyless Reddit that gets past the shreddit anti-bot wall,
the arxiv-pp-cli wrapper, relevance/rerank/dedupe/fusion) and removes the
~30-day ceilings that engine hardcodes for its own use case.

  /last30days   what people said recently, ranked by engagement
  /alltime what the literature and the threads say, over years

Core sources are Reddit and arXiv. Hacker News, GitHub and X are optional and
already honor an arbitrary window upstream, so they need no patching.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Named `adapter`, not `lib`: the engine does a bare `from lib import ...`, so a
# local package called `lib` on sys.path shadows its own and breaks the import.
from adapter import overrides, selftest, sources, upstream  # noqa: E402

DEFAULT_DAYS = 730  # two years -- wide enough for real discourse, not archaeology

# arXiv result counts per depth. Upstream caps at 5/10/20; a literature pass
# needs materially more, and the API serves it in one request.
ARXIV_LIMITS = {"quick": 15, "default": 40, "deep": 100}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="alltime",
        description=(
            "Deep research across Reddit and arXiv, without a 30-day ceiling. "
            "Unrecognized flags pass through to the underlying last30days engine."
        ),
        epilog=(
            "Examples:\n"
            "  alltime.py 'retrieval augmented generation failure modes'\n"
            "  alltime.py 'sourdough hydration' --days 3650 --deep\n"
            "  alltime.py 'speculative decoding' --all-time --arxiv-loose\n"
            "  alltime.py --explain\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("topic", nargs="*", help="Research topic")

    window = p.add_mutually_exclusive_group()
    window.add_argument(
        "--days",
        type=int,
        default=None,
        help=f"Lookback window in days (default: {DEFAULT_DAYS}, i.e. two years)",
    )
    window.add_argument(
        "--all-time",
        action="store_true",
        help="No lower bound: Reddit t=all, no arXiv recency cutoff",
    )

    p.add_argument(
        "--search",
        default=None,
        help=(
            "Comma-separated platforms. Defaults to the reachable default-on set "
            "(reddit,x). Also available: arxiv, hackernews, github, youtube, web "
            "(Exa). Use 'all' to let the engine pick. Run --list-sources to see "
            "what this machine can actually reach."
        ),
    )
    p.add_argument(
        "--list-sources",
        action="store_true",
        help="Report which platforms are reachable here, and how each handles the window",
    )
    p.add_argument("--deep", action="store_true", help="Higher-recall profile")
    p.add_argument("--quick", action="store_true", help="Lower-latency profile")

    p.add_argument(
        "--arxiv-limit",
        type=int,
        default=None,
        help="Override arXiv results for the active depth (default 15/40/100)",
    )
    p.add_argument(
        "--arxiv-sort",
        default="relevance",
        choices=["relevance", "lastUpdatedDate", "submittedDate"],
        help="arXiv sort field (default: relevance -- date sorts go topic-blind)",
    )
    p.add_argument(
        "--arxiv-loose",
        action="store_true",
        help=(
            "AND the topic's terms instead of requiring the exact phrase. "
            "Widens recall on multi-word topics no paper states verbatim."
        ),
    )

    p.add_argument(
        "--gh-repos",
        type=int,
        default=None,
        help=(
            "Repos to scope GitHub issue search to, resolved from GitHub's repo "
            "index (default 4; 0 disables and leaves upstream's global search)"
        ),
    )
    p.add_argument(
        "--gh-per-repo",
        type=int,
        default=None,
        help="Rows per scoped repo lane (default 8/12/20 by depth)",
    )
    p.add_argument(
        "--gh-scope",
        default=None,
        help=(
            "Pin the scoped repos instead of resolving them, "
            "e.g. --gh-scope ggml-org/llama.cpp,vllm-project/vllm"
        ),
    )

    p.add_argument(
        "--explain",
        action="store_true",
        help="Report the resolved engine, seam checks and window overrides, then exit",
    )
    p.add_argument(
        "--selftest",
        action="store_true",
        help="Run seam checks against the installed engine and exit",
    )
    return p


def _resolve_depth(args: argparse.Namespace) -> str:
    if args.deep:
        return "deep"
    if args.quick:
        return "quick"
    return "default"


def _resolve_platforms(requested: str | None, platforms: list) -> list[str]:
    """Turn --search into a concrete platform list, warning about dead picks.

    An explicitly requested but unreachable platform is dropped with a reason
    rather than silently contributing zero items -- an empty source in the
    output otherwise reads as "nothing was said about this", which is a
    materially different claim from "we couldn't look".
    """
    known = {p.key: p for p in platforms}

    if requested is None:
        return sources.default_selection(platforms)
    if requested.strip().lower() == "all":
        return []  # let the engine decide

    out: list[str] = []
    for raw in requested.split(","):
        key = raw.strip().lower()
        if not key:
            continue
        p = known.get(key)
        if p is None:
            # Unknown to this skill's catalogue, but the engine may still
            # support it (tiktok, polymarket, ...). Pass it through untouched.
            out.append(key)
            continue
        if not p.available:
            sys.stderr.write(
                f"[alltime] skipping {p.label}: {p.reason}\n"
            )
            continue
        out.append(key)

    if not out:
        sys.stderr.write(
            "[alltime] no requested platform is reachable; "
            "falling back to defaults\n"
        )
        return sources.default_selection(platforms)
    return out


def _print_report(version, engine_dir, results, log, days, bucket, selected) -> None:
    print(f"engine   : {version}  ({engine_dir})")
    print(f"pinned   : {upstream.PINNED_VERSION}")
    window = "all-time" if days is None else f"{days} days"
    print(f"window   : {window}  -> reddit t={bucket}")
    print(f"platforms: {','.join(selected) if selected else '(engine default)'}")
    print()
    print("seam checks:")
    for r in results:
        mark = "ok  " if r.ok else ("FAIL" if r.fatal else "warn")
        print(f"  [{mark}] {r.name}: {r.detail}")
    print()
    if len(log):
        print(f"overrides applied ({len(log)}):")
        for entry in log:
            print(f"  - {entry}")
    else:
        print("overrides applied: none (engine already matches the requested window)")


def main() -> int:
    parser = build_parser()
    args, passthrough = parser.parse_known_args()

    try:
        engine, lib, engine_dir, version = upstream.load_engine()
    except upstream.UpstreamNotFound as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    if version != upstream.PINNED_VERSION and version != "unknown":
        sys.stderr.write(
            f"[alltime] engine {version} differs from pinned "
            f"{upstream.PINNED_VERSION}; verifying seams\n"
        )

    config = lib.env.get_config()
    platforms = sources.detect(config, lib)

    if args.list_sources:
        print(sources.render(platforms))
        return 0

    selected = _resolve_platforms(args.search, platforms)

    try:
        results = selftest.assert_seams(lib, version, upstream.PINNED_VERSION)
    except RuntimeError as exc:
        sys.stderr.write(f"{exc}\n")
        return 3

    days = None if args.all_time else (args.days if args.days is not None else DEFAULT_DAYS)
    if days is not None and days < 1:
        sys.stderr.write("--days must be >= 1\n")
        return 2

    depth = _resolve_depth(args)
    limits = dict(ARXIV_LIMITS)
    if args.arxiv_limit is not None:
        limits[depth] = args.arxiv_limit

    pinned_repos = [
        r.strip() for r in (args.gh_scope or "").split(",")
        if r.strip() and "/" in r.strip()
    ] or None

    log = overrides.apply(
        lib,
        days=days,
        arxiv_limits=limits,
        arxiv_sort=args.arxiv_sort,
        arxiv_loose=args.arxiv_loose,
        depth=depth,
        github_scope_repos=(
            overrides.GITHUB_SCOPE_REPOS if args.gh_repos is None else args.gh_repos
        ),
        github_per_repo=args.gh_per_repo,
        github_repos=pinned_repos,
    )
    bucket = "all" if days is None else overrides.days_to_reddit_bucket(days)

    if args.selftest or args.explain:
        _print_report(version, engine_dir, results, log, days, bucket, selected)
        return 0

    topic = " ".join(args.topic).strip()
    if not topic:
        parser.print_help()
        return 2

    # The engine reads sys.argv directly (parse_known_args inside its main()),
    # so hand it a constructed argv rather than calling a lower-level entry
    # point -- that keeps every downstream flag and default intact.
    engine_argv = [
        "last30days",
        topic,
        "--days",
        # All-time still needs a concrete lower bound; 1926 predates every
        # source here, and Reddit is already unbounded via t=all.
        str(36500 if days is None else days),
    ]
    if selected:
        engine_argv += ["--search", ",".join(selected)]
    if args.deep:
        engine_argv.append("--deep")
    if args.quick:
        engine_argv.append("--quick")
    engine_argv += passthrough

    sys.stderr.write(
        f"[alltime] engine {version} | window "
        f"{'all-time' if days is None else str(days) + 'd'} | reddit t={bucket} | "
        f"platforms {','.join(selected) if selected else 'engine-default'} | "
        f"{len(log)} overrides\n"
    )

    argv_backup = sys.argv
    sys.argv = engine_argv
    try:
        return engine.main()
    finally:
        sys.argv = argv_backup


if __name__ == "__main__":
    raise SystemExit(main())
