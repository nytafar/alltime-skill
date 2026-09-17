"""Platform catalogue and availability probing.

The skill asks the user which platforms to search before every run, so the
choices offered have to reflect what this machine can actually reach right now
-- offering YouTube on a box without yt-dlp just produces a silent empty source.

`window_note` records whether a platform honors an arbitrary lookback, and how.
That distinction is the whole point of this skill, so it is data, not prose in
a doc that drifts.
"""

from __future__ import annotations

from dataclasses import dataclass
from shutil import which
from types import ModuleType


@dataclass
class Platform:
    key: str            # value passed to the engine's --search
    label: str          # shown in the picker
    default_on: bool
    blurb: str          # what it contributes
    window_note: str    # how it handles a long window
    available: bool = True
    reason: str = ""    # why not, when unavailable


# Ordered as presented to the user. Reddit and X are the defaults.
CATALOGUE: tuple[dict, ...] = (
    dict(
        key="reddit",
        label="Reddit",
        default_on=True,
        blurb="Threads and top comments with real upvote counts. No API key needed.",
        window_note="patched: t= bucket raised from month to the requested window",
    ),
    dict(
        key="x",
        label="X / Twitter",
        default_on=True,
        blurb="Expert threads and hot takes. Needs browser session or API creds.",
        window_note="native: builds since:/until: from the requested window",
    ),
    dict(
        key="arxiv",
        label="arXiv",
        default_on=False,
        blurb="Research papers behind the topic. No API key needed.",
        window_note="patched: 365-day cutoff lifted, 20 -> 100 results at --deep",
    ),
    dict(
        key="hackernews",
        label="Hacker News",
        default_on=False,
        blurb="Developer consensus, points and comment counts.",
        window_note="native: full archive via Algolia date filters",
    ),
    dict(
        key="github",
        label="GitHub",
        default_on=False,
        blurb="Issues, discussions, releases, repo activity.",
        window_note="native: date-ranged",
    ),
    dict(
        key="youtube",
        label="YouTube",
        default_on=False,
        blurb="Long-form talks and full transcripts. Slowest source per item.",
        window_note="native: soft filter >= from_date, no ceiling",
    ),
)


def _probe(entry: dict, config: dict, lib: ModuleType) -> tuple[bool, str]:
    """Can this platform actually return data on this machine?"""
    key = entry["key"]

    if key == "reddit":
        return True, ""  # keyless lanes always work

    if key == "arxiv":
        if which("arxiv-pp-cli"):
            return True, ""
        return False, "arxiv-pp-cli not on PATH"

    if key == "youtube":
        if which("yt-dlp"):
            return True, ""
        if config.get("SCRAPECREATORS_API_KEY"):
            return True, ""
        return False, "yt-dlp not on PATH and no SCRAPECREATORS_API_KEY"

    if key == "x":
        try:
            if lib.env.get_x_source(config, local_only=True):
                return True, ""
            if lib.env.x_pending_browser_auth(config):
                return True, ""
        except Exception as exc:  # noqa: BLE001 - probing must never abort a run
            return False, f"probe failed: {type(exc).__name__}"
        return False, "no X credentials (AUTH_TOKEN/CT0, xurl, or browser session)"

    if key in ("hackernews", "github"):
        return True, ""  # both reachable unauthenticated

    return True, ""


def detect(config: dict, lib: ModuleType) -> list[Platform]:
    """Build the catalogue with live availability filled in."""
    out: list[Platform] = []
    for entry in CATALOGUE:
        available, reason = _probe(entry, config, lib)
        out.append(Platform(**entry, available=available, reason=reason))
    return out


def default_selection(platforms: list[Platform]) -> list[str]:
    """Keys that are both default-on and actually reachable."""
    return [p.key for p in platforms if p.default_on and p.available]


def render(platforms: list[Platform]) -> str:
    """Human/agent-readable availability table for --list-sources."""
    width = max(len(p.label) for p in platforms)
    lines = [
        "Platforms for /alltime  (* = on by default)",
        "",
        f"  {'':1} {'PLATFORM'.ljust(width)}  {'STATUS'.ljust(11)}  WINDOW",
    ]
    for p in platforms:
        mark = "*" if p.default_on else " "
        status = "available" if p.available else "unavailable"
        lines.append(f"  {mark} {p.label.ljust(width)}  {status.ljust(11)}  {p.window_note}")
        lines.append(f"    {''.ljust(width)}  {p.blurb}")
        if not p.available:
            lines.append(f"    {''.ljust(width)}  -> {p.reason}")
    lines.append("")
    lines.append(
        "default --search: " + ",".join(default_selection(platforms))
    )
    return "\n".join(lines)
