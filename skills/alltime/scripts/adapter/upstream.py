"""Locate and import the installed /last30days engine.

This skill is an *adapter*, not a fork. It borrows last30days' source-access
layer (the part that gets past Reddit's shreddit anti-bot wall, the arxiv-pp-cli
wrapper, the relevance/rerank/dedupe stack) and overrides only the places where
that engine hardcodes a 30-day horizon.

Resolution order deliberately prefers the versioned plugin cache over the
marketplaces git clone: Claude Code auto-restores the clone to origin/main on
session start, so it can lag the cache by one or more releases. This is the
same trap last30days' own SKILL.md STEP 0 defends against.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

# The upstream release this adapter was written and seam-tested against.
# Mismatch is a warning, not a failure -- selftest.py is what actually decides
# whether the seams still hold.
PINNED_VERSION = "3.23.0"

# `lib/__init__.py` does not re-export submodules, and the engine's own import
# line only pulls the ones its CLI path needs. The source modules this adapter
# patches are loaded lazily by the pipeline, so they must be imported explicitly
# or `lib.reddit_rss` and friends are simply absent.
REQUIRED_SUBMODULES = (
    "reddit",
    "reddit_listing",
    "reddit_rss",
    "reddit_keyless",
    # The arctic-shift archive lane, added in engine 3.23.0. It is what stopped
    # Reddit 429ing out of long-window runs, and it ships recency-only, so the
    # windowing override needs it present by name.
    "reddit_arctic",
    "arxiv",
    "hackernews",
    # Not source modules: `pipeline` owns the per-source fetch cap and `http`
    # owns the shared keyless-Reddit throttle, both of which the rate-limit
    # overrides retune. Imported here for the same reason as the rest -- an
    # attribute that was never imported is simply absent from `lib`.
    "pipeline",
    "http",
)


class UpstreamNotFound(RuntimeError):
    """No usable last30days install on this machine."""


def _version_key(path: Path) -> tuple:
    """Sort key for version directory names, newest last."""
    parts = []
    for chunk in path.name.split("."):
        parts.append(int(chunk) if chunk.isdigit() else -1)
    return tuple(parts)


def _candidate_roots() -> list[Path]:
    """Every directory that might hold a last30days `scripts/` tree.

    Ordered best-first. Two cache layouts ship in the wild -- nested
    (`{version}/skills/last30days/scripts`) and flat (`{version}/scripts`) --
    so both shapes are probed for each versioned directory.
    """
    roots: list[Path] = []
    home = Path.home()

    override = os.environ.get("ALLTIME_ENGINE_DIR")
    if override:
        roots.append(Path(override).expanduser())

    cache = home / ".claude/plugins/cache/last30days-skill/last30days"
    if cache.is_dir():
        versions = [p for p in cache.iterdir() if p.is_dir()]
        for vdir in sorted(versions, key=_version_key, reverse=True):
            roots.append(vdir / "skills/last30days/scripts")
            roots.append(vdir / "scripts")

    # Non-Claude-Code installs, then the stale-prone git clone as a last resort.
    roots.append(home / ".agents/skills/last30days/scripts")
    roots.append(home / ".codex/skills/last30days/scripts")
    roots.append(
        home / ".claude/plugins/marketplaces/last30days-skill/skills/last30days/scripts"
    )
    return roots


def find_engine_dir() -> Path:
    """Return the `scripts/` directory of the best available install."""
    tried: list[str] = []
    for root in _candidate_roots():
        tried.append(str(root))
        if (root / "last30days.py").is_file() and (root / "lib").is_dir():
            return root
    raise UpstreamNotFound(
        "No /last30days engine found. Install it with:\n"
        "  claude plugin marketplace add mvanhorn/last30days-skill\n"
        "  claude plugin install last30days@last30days-skill\n"
        "Or point ALLTIME_ENGINE_DIR at an existing scripts/ directory.\n"
        "Looked in:\n  " + "\n  ".join(tried)
    )


def installed_version(engine_dir: Path) -> str:
    """Best-effort version of the resolved install.

    The versioned cache encodes it in a path component; other layouts may not
    carry it at all, hence "unknown" rather than an exception.
    """
    for parent in engine_dir.parents:
        name = parent.name
        if name and name[0].isdigit() and all(
            c.isdigit() or c == "." for c in name
        ):
            return name
    return "unknown"


def load_engine() -> tuple[ModuleType, ModuleType, Path, str]:
    """Import the engine and its `lib` package.

    Returns ``(last30days_module, lib_package, engine_dir, version)``.

    `last30days.py` inserts its own directory on `sys.path` at import time and
    then does `from lib import ...`, so `sys.path` has to be primed before the
    module executes or that import fails.
    """
    engine_dir = find_engine_dir()
    version = installed_version(engine_dir)

    if str(engine_dir) not in sys.path:
        sys.path.insert(0, str(engine_dir))

    lib = importlib.import_module("lib")
    for name in REQUIRED_SUBMODULES:
        try:
            importlib.import_module(f"lib.{name}")
        except ImportError as exc:
            raise UpstreamNotFound(
                f"Engine at {engine_dir} is missing lib.{name} ({exc}). "
                "This install looks incomplete or is a much older release."
            ) from exc

    # Load by explicit path: `last30days` is a plausible name collision, and we
    # want the file next to the lib we just imported, not whatever else answers.
    spec = importlib.util.spec_from_file_location(
        "_alltime_engine", engine_dir / "last30days.py"
    )
    if spec is None or spec.loader is None:
        raise UpstreamNotFound(f"Cannot load {engine_dir / 'last30days.py'}")
    engine = importlib.util.module_from_spec(spec)
    sys.modules["_alltime_engine"] = engine
    spec.loader.exec_module(engine)

    return engine, lib, engine_dir, version
