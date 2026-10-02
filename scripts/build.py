#!/usr/bin/env python3
"""Build the dynamic GitHub profile.

Fetches live data from the GitHub API, fills profile.template.svg, and
regenerates README.md. Only Python stdlib is required.

    python scripts/build.py --dry-run          preview without writing
    python scripts/build.py --offline          render from cached values only
    python scripts/build.py                    full build (SVG + README)
    python scripts/build.py --sync-readme      only refresh README.md

Values come from config.json; every key is optional and documented below.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

REST = "https://api.github.com"
GRAPHQL = "https://api.github.com/graphql"
USER_AGENT = "ai-chetan-profile-builder"

REQUEST_ATTEMPTS = 4
BACKOFF_SECONDS = (2, 5, 12)
RETRYABLE_STATUS = frozenset({403, 408, 425, 429, 500, 502, 503, 504})
TRANSIENT_MARKERS = ("rate limit", "secondary rate", "timeout", "timed out", "temporarily", "abuse")

# GitHub rejects contributionsCollection ranges longer than a year, so the
# range is split into contiguous chunks (one request per year).
CONTRIBUTION_WINDOW_DAYS = 364

RELATIVE_UNITS = (
    ("year", 365 * 86400),
    ("month", 30 * 86400),
    ("week", 7 * 86400),
    ("day", 86400),
    ("hour", 3600),
    ("minute", 60),
)

WIDE = set("MWmw@%&")
NARROW = set("iljtfrI.,:;'!| ")

CONTRIBUTIONS_QUERY = """
query ($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays {
            contributionCount
          }
        }
      }
    }
  }
}
"""

STATUS_MAP = {
    "archived": "Archived",
    "active": "Active",
    "stale": "Stale",
    "wip": "Work in progress",
}
STALE_AFTER_DAYS = 90

# Per-card text origin (matches the template's existing tspan x values).
ORIGIN_X = {"1": 63, "2": 301, "3": 537, "4": 777}
CARD_TEXT_WIDTH = 190.0


class BuildError(RuntimeError):
    pass


class RetryableError(BuildError):
    pass


# --------------------------------------------------------------------------
# GitHub API helpers
# --------------------------------------------------------------------------

def with_retries(task, label):
    for attempt in range(1, REQUEST_ATTEMPTS + 1):
        try:
            return task()
        except RetryableError as error:
            if attempt == REQUEST_ATTEMPTS:
                raise BuildError(f"{label}: {error}") from error
            delay = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
            print(f"  {label}: attempt {attempt} failed ({error}); retrying in {delay}s", file=sys.stderr)
            time.sleep(delay)


def request_json(url, token, payload=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = " ".join(error.read().decode("utf-8", "replace")[:200].split())
        message = f"HTTP {error.code}: {detail}"
        if error.code in RETRYABLE_STATUS:
            raise RetryableError(message) from error
        raise BuildError(message) from error
    except urllib.error.URLError as error:
        raise RetryableError(f"unreachable: {error.reason}") from error


def api_get(path, token):
    return with_retries(lambda: request_json(REST + path, token), path.split("?")[0])


def describe_graphql_errors(errors):
    """Turn a GraphQL error list into a short, readable summary."""
    parts = []
    for error in errors[:3]:
        extensions = error.get("extensions") or {}
        code = extensions.get("code") or "error"
        message = " ".join(str(error.get("message", "")).split())[:120]
        if code == "undefinedField":
            path = ".".join(str(item) for item in error.get("path", []))
            parts.append(f"{code}: field {path} no longer exists on the schema ({message})")
        else:
            parts.append(f"{code}: {message}")
    return " | ".join(parts) or "unknown GraphQL error"


def api_graphql(query, variables, token):
    def call():
        payload = request_json(GRAPHQL, token, {"query": query, "variables": variables})
        errors = payload.get("errors")
        if errors:
            message = describe_graphql_errors(errors)
            if any(marker in message.lower() for marker in TRANSIENT_MARKERS):
                raise RetryableError(message)
            raise BuildError(message)
        return payload["data"]

    return with_retries(call, "graphql")


def contribution_windows(start, end):
    cursor = start
    while cursor <= end:
        window_end = min(cursor + dt.timedelta(days=CONTRIBUTION_WINDOW_DAYS - 1), end)
        yield cursor, window_end
        cursor = window_end + dt.timedelta(days=1)


def fetch_contributions(login, since, today, token):
    """Sum contributions across one-year windows.

    `totalContributions` lives on `contributionsCollection.contributionCalendar`,
    not on `contributionsCollection` (the latter was removed from the schema).

    The calendar's own total is authoritative for the requested range. Summing
    the per-day counts is only a fallback: the first/last calendar week is
    partial, so the days can spill outside the window and would be double
    counted across adjacent windows.
    """
    total = 0
    windows = list(contribution_windows(since, today))
    for window_start, window_end in windows:
        data = api_graphql(
            CONTRIBUTIONS_QUERY,
            {
                "login": login,
                "from": f"{window_start.isoformat()}T00:00:00Z",
                "to": f"{window_end.isoformat()}T23:59:59Z",
            },
            token,
        )
        user = data.get("user")
        if not user:
            raise BuildError(f"no user returned for {login}")
        calendar = user["contributionsCollection"]["contributionCalendar"]
        reported = calendar.get("totalContributions")
        counted = sum(
            day["contributionCount"]
            for week in calendar.get("weeks", [])
            for day in week["contributionDays"]
        )
        if isinstance(reported, int):
            total += reported
            if reported != counted:
                print(
                    f"  contributions {window_start}..{window_end}: calendar total {reported}"
                    f" vs summed days {counted} (expected: partial edge weeks); using calendar total",
                    file=sys.stderr,
                )
        else:
            total += counted
    return total, windows


def fetch_owned_repos(login, token):
    repos = []
    page = 1
    while True:
        if token:
            batch = api_get(f"/user/repos?affiliation=owner&per_page=100&page={page}&sort=full_name", token)
        else:
            batch = api_get(f"/users/{login}/repos?per_page=100&page={page}&sort=full_name", token)
        if not isinstance(batch, list) or not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return repos


def fetch_repo(slug, token):
    return api_get("/repos/" + slug, token)


def fetch_languages(slug, token):
    try:
        data = api_get(f"/repos/{slug}/languages", token)
        return sorted(data, key=data.get, reverse=True)
    except BuildError as error:
        print(f"  languages for {slug}: {error}; falling back to repo field", file=sys.stderr)
        return []


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------

def parse_timestamp(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def relative_time(moment, now):
    seconds = int((now - moment).total_seconds())
    if seconds < 60:
        return "just now"
    for name, size in RELATIVE_UNITS:
        if seconds >= size:
            amount = seconds // size
            return f"{amount} {name}{'' if amount == 1 else 's'} ago"
    return "just now"


def format_badge(previous, value):
    """Zero-pad to the template's placeholder width (03 stays 03 -> 048)."""
    text = f"{value:,}"
    if previous.isdigit() and previous.startswith("0") and len(previous) > 1 and "," not in text:
        return text.zfill(len(previous))
    return text


def format_stars(value):
    if value < 1000:
        return str(value)
    if value < 10000:
        return f"{value / 1000:.1f}k".replace(".0k", "k")
    return f"{round(value / 1000)}k"


def format_range(start):
    return start.strftime("%b %Y") + " – Present"


def format_views(value):
    """Exact count with comma grouping -- a view counter should not be rounded."""
    return f"{value:,}"


def fetch_profile_views(url):
    """Pull the number out of a shields-style badge SVG.

    GitHub serves profile.svg through <img>, and browsers refuse to load any
    external resource from inside an SVG used as an image. A live badge therefore
    cannot be embedded; the count has to be read here and baked into the file.
    The badge carries a drop-shadowed label and value, so the last <text> holding
    digits is the value.
    """
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "image/svg+xml,text/xml,*/*",
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            badge = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        message = f"HTTP {error.code}"
        if error.code in RETRYABLE_STATUS:
            raise RetryableError(message) from error
        raise BuildError(message) from error
    except urllib.error.URLError as error:
        raise RetryableError(f"unreachable: {error.reason}") from error

    candidates = re.findall(r">([^<>]*\d[^<>]*)</text>", badge)
    if not candidates:
        raise BuildError("badge carried no numeric text")
    digits = re.sub(r"[^\d]", "", candidates[-1])
    if not digits:
        raise BuildError(f"could not read a count from {candidates[-1]!r}")
    return int(digits)


def escape_xml(value):
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text_width(value, font_size):
    em = 0.0
    for character in value:
        if character in WIDE:
            em += 0.9
        elif character in NARROW:
            em += 0.31
        elif character.isdigit():
            em += 0.56
        elif character in ",.":
            em += 0.28
        elif character.isupper():
            em += 0.66
        else:
            em += 0.53
    return em * font_size


# --------------------------------------------------------------------------
# SVG surgery helpers
# --------------------------------------------------------------------------

def attribute(attrs, name, default):
    match = re.search(r"\b" + re.escape(name) + r'="([^"]*)"', attrs)
    return match.group(1) if match else default


def replace_attribute(attrs, name, value):
    pattern = re.compile(r'(\b' + re.escape(name) + r'=)"([^"]*)"')
    if pattern.search(attrs):
        return pattern.sub(lambda found: found.group(1) + '"' + value + '"', attrs, count=1)
    return attrs + f' {name}="{value}"'


def single_match(svg, pattern, description):
    found = list(pattern.finditer(svg))
    if len(found) != 1:
        raise BuildError(f"expected exactly one {description}, found {len(found)}")
    return found[0]


def text_match(svg, node_id):
    pattern = re.compile(r'<text\b([^>]*\bid="' + re.escape(node_id) + r'"[^>]*)>([^<]*)</text>')
    return single_match(svg, pattern, f'<text id="{node_id}">')


def rect_match(svg, box_id):
    pattern = re.compile(r'<rect\b([^>]*\bid="' + re.escape(box_id) + r'"[^>]*?)/?>')
    return single_match(svg, pattern, f'<rect id="{box_id}">')


def any_match(svg, node_id):
    pattern = re.compile(r'<(\w+)\b([^>]*\bid="' + re.escape(node_id) + r'"[^>]*?)(/?)>')
    return single_match(svg, pattern, f'element id="{node_id}"')


def resize_box(svg, box_id, previous, value, font_size):
    match = rect_match(svg, box_id)
    attrs = match.group(1)
    current = float(attribute(attrs, "width", "0"))
    updated = max(34.0, round(current + text_width(value, font_size) - text_width(previous, font_size), 1))
    attrs = replace_attribute(attrs, "width", f"{updated:g}")
    return svg[: match.start(1)] + attrs + svg[match.end(1) :]


def set_text(svg, node_id, value, box_id=None):
    match = text_match(svg, node_id)
    previous = match.group(2)
    if value == previous:
        return svg
    font_size = float(attribute(match.group(1), "font-size", "27"))
    escaped = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    svg = svg[: match.start(2)] + escaped + svg[match.end(2) :]
    if box_id:
        svg = resize_box(svg, box_id, previous, value, font_size)
    return svg


def shift_node(svg, node_id, delta):
    match = any_match(svg, node_id)
    attrs = replace_attribute(match.group(2), "x", f"{float(attribute(match.group(2), 'x', '0')) + delta:g}")
    return svg[: match.start(2)] + attrs + svg[match.end(2) :]


def keep_gap(svg, left_box_id, right_box_id, gap=24.0):
    """Keep `gap` px between two box rects by shifting the right one."""
    left = rect_match(svg, left_box_id).group(1)
    right = rect_match(svg, right_box_id).group(1)
    edge = float(attribute(left, "x", "0")) + float(attribute(left, "width", "0"))
    overflow = round(edge + gap - float(attribute(right, "x", "0")), 1)
    if overflow <= 0:
        return svg
    pair = right_box_id[: -len("-box")] if right_box_id.endswith("-box") else right_box_id
    svg = shift_node(svg, right_box_id, overflow)
    return shift_node(svg, pair, overflow)


def set_attribute_on_id(svg, node_id, name, value):
    match = any_match(svg, node_id)
    attrs = replace_attribute(match.group(2), name, escape_xml(value))
    return svg[: match.start(2)] + attrs + svg[match.end(2) :]


def wrap_lines(text, font_size, max_width, max_lines):
    """Greedy word-wrap using the same width model as resize_box."""
    lines = []
    current = ""
    truncated = False
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and text_width(candidate, font_size) > max_width:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                truncated = True
                break
        else:
            current = candidate
    if not truncated and current:
        lines.append(current)
    if truncated and lines:
        last = lines[-1]
        while last and text_width(last + "…", font_size) > max_width:
            last = last.rsplit(" ", 1)[0]
        lines[-1] = last.rstrip() + "…"
    return lines


def set_description(svg, node_id, text, x, line_height=15.0, max_width=194.0, max_lines=3, font_size=12.5):
    """Replace the tspans of a multi-line description <text> element."""
    match = single_match(
        svg,
        re.compile(r'(<text\b[^>]*\bid="' + re.escape(node_id) + r'"[^>]*>)(.*?)(</text>)', re.DOTALL),
        f'<text id="{node_id}">',
    )
    lines = wrap_lines(text, font_size, max_width, max_lines) or ["No description provided."]
    tspans = "".join(
        f'<tspan x="{x:g}" dy="{"0" if index == 0 else f"{line_height:g}"}">{escape_xml(line)}</tspan>'
        for index, line in enumerate(lines)
    )
    return svg[: match.start(2)] + tspans + svg[match.end(2) :]


def validate(svg, expected_ids):
    ET.fromstring(svg)
    ids = re.findall(r'(?<![\w.-])id="([^"]+)"', svg)
    duplicates = sorted(name for name, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise BuildError("duplicate ids in output: " + ", ".join(duplicates))
    missing = [node_id for node_id in expected_ids if node_id not in ids]
    if missing:
        raise BuildError("missing ids in output: " + ", ".join(missing))


# --------------------------------------------------------------------------
# README generation (clickable slice mosaic, same layout as the original)
# --------------------------------------------------------------------------

SVG_WIDTH = 1024
README_SCALE = 99.0  # img widths are percentages of the container; full row = 99%

# Slice geometry mirrors profile.template.svg:
#   row 1: hero (section 1)            y 0-342
#   row 2: social icon strip           y 342-430  (icon slices around the
#   row 3: tech stack + stats          y 430-1260  four social circles)
#   rows 4-7: contact list rows        y 1260-1358
#   rows 8-9: footer                   y 1358-1510
#
# The rows tile contiguously from 0 to the SVG's 1510 viewBox height. Rows 8 and
# 9 must sum to 152 (90 + 62): the artwork is 1510 units tall, so anything less
# leaves the bottom of the background image sliced off at the bottom of the
# profile, and leaves a seam above the footer where the band is never drawn.
#
# "P/L/E/D" are link keys resolved from config.socials at build time; None
# renders an unlinked slice.
#
# Contact rows link only the "Link" affordance (text at x=869 plus the chevron
# ending at x=959), mirroring how the social row is cut tight around each icon.
# The wide empty margins either side of the panel (contour spans x 553-990) are
# left unlinked instead of falling back to the portfolio URL.
#
# Do not try to dodge the line-box strut by making the contact rows taller
# instead. The artwork leaves only ~11 units of slack between them, so moving a
# boundary far enough to clear GitHub's ~24px strut would cut a row's own
# artwork. generate_readme zeroes the strut on a wrapper div instead.
CONTACT_LINK_SLICE = (866, 96)
MOSAIC_ROWS = (
    (0, 342, ((0, 1024, "P"),)),
    (342, 88, (
        (0, 78, "P"), (78, 64, "L"), (142, 164, "P"), (306, 60, "E"),
        (366, 187, "P"), (553, 66, "P"), (619, 165, "P"), (784, 64, "D"),
        (848, 176, "P"),
    )),
    (430, 830, ((0, 1024, "P"),)),
    (1260, 24, ((0, 866, None), (866, 96, "D"), (962, 62, None))),
    (1284, 24, ((0, 866, None), (866, 96, "L"), (962, 62, None))),
    (1308, 24, ((0, 866, None), (866, 96, "E"), (962, 62, None))),
    (1332, 26, ((0, 866, None), (866, 96, "P"), (962, 62, None))),
    (1358, 90, ((0, 1024, "P"),)),
    (1448, 62, ((0, 1024, "P"),)),
)


def generate_readme(cfg, profile_url):
    socials = {s.get("id"): s for s in cfg.get("socials", []) if s.get("id")}
    fallback = cfg.get("site", {}).get("portfolio_url", "")

    def link_for(key):
        if key in ("L", "E", "D", "P"):
            social_id = {"L": "linkedin", "E": "email", "D": "discord", "P": "portfolio"}[key]
            return socials.get(social_id, {}).get("url", fallback)
        return fallback

    def alt_for(key):
        social_id = {"L": "linkedin", "E": "email", "D": "discord", "P": "portfolio"}.get(key)
        return socials.get(social_id, {}).get("label", "") if social_id else ""

    lines = ["<!-- AUTO-GENERATED by scripts/build.py -- edit config.json, not this file -->"]
    # Every mosaic row is its own line box, and a line box is never shorter than
    # its block's font strut (GitHub renders READMEs at 16px/1.5, so ~24px). The
    # four contact rows are only 24-26 svg units tall, which renders to ~13px once
    # the viewport drops below ~1060px -- below the strut, the leftover shows
    # through as a white bar across the row and the two panels get sliced. Zeroing
    # the strut removes it. `font-size` is a second line of defence in case a
    # renderer honours one property and not the other; there is no text in here
    # for it to affect.
    lines.append('<div style="line-height:0;font-size:0">')
    for y, height, slices in MOSAIC_ROWS:
        row = []
        for x, width, key in slices:
            alt = alt_for(key)
            pct = f"{width / SVG_WIDTH * README_SCALE:.10g}".rstrip("0").rstrip(".")
            src = f"{escape_html_attr(profile_url)}#svgView(viewBox({x},{y},{width},{height}))"
            img = f'<img src="{src}" width="{pct}%" alt="{escape_html_attr(alt)}" align="top" />'
            if key is None:
                row.append(img)
                continue
            url = escape_html_attr(link_for(key))
            row.append(f'<a href="{url}" target="_blank" rel="noopener noreferrer">{img}</a>')
        lines.append("".join(row) + "<br>")
    lines.append("</div>")

    return "\n".join(lines) + "\n"


def escape_html_attr(value):
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------
# Build orchestration
# --------------------------------------------------------------------------

def build_updates(cfg, token, now, warnings):
    """Collect every dynamic value; unavailable ones degrade to warnings."""
    gh = cfg.get("github", {})
    login = gh["user"]
    stats = cfg.get("stats", {})

    def note(warning):
        warnings.append(warning)

    # ---- profile stats ------------------------------------------------
    repos = None
    totals = None
    contributions = None

    if token:
        try:
            repos = fetch_owned_repos(login, token)
        except BuildError as error:
            note(f"repository list: {error}")

        if repos is not None:
            include_forks = stats.get("count_forks_as_repositories", True)
            include_forks_in_stars = stats.get("count_forks_in_stars", False)
            owned = repos if include_forks else [r for r in repos if not r["fork"]]
            original = repos if include_forks_in_stars else [r for r in repos if not r["fork"]]
            totals = {
                "repositories": len(owned),
                "stars": sum(r["stargazers_count"] for r in original),
                "forks": sum(1 for r in repos if r["fork"]),
            }

        since_text = stats.get("contributions_since")
        if since_text:
            since = dt.date.fromisoformat(since_text)
            try:
                contributions, _windows = fetch_contributions(login, since, now.date(), token)
            except BuildError as error:
                note(f"contributions: {error}")
        else:
            note("stats.contributions_since not set; skipping contributions total")
    else:
        note("no token: set STATS_PAT to fetch live values; keeping template values")

    # ---- profile visits ------------------------------------------------
    views = None
    views_cfg = cfg.get("views") or {}
    if views_cfg.get("enabled", True) and views_cfg.get("source"):
        try:
            views = with_retries(
                lambda: fetch_profile_views(views_cfg["source"]), "profile views"
            )
        except BuildError as error:
            note(f"profile views: {error}")
        else:
            # The badge serves a valid "0" for an unknown user, and it resets
            # counter-side, so a real profile reading 0 is almost always an
            # upstream hiccup rather than a genuine drop to zero.
            if views == 0:
                note("profile views: source reported 0; publishing it anyway")
    else:
        note("views.enabled is false; keeping the template's view count")

    return {
        "repos": repos,
        "totals": totals,
        "contributions": contributions,
        "views": views,
    }


def apply_svg(svg, cfg, data, now, warnings, token=""):
    """Write every dynamic value into the SVG and return it."""
    updates = data.get("totals") or {}
    contributions = data.get("contributions")
    stats_cfg = cfg.get("stats", {})

    # ---- stat badges ---------------------------------------------------
    def put(node_id, value, box_id=None):
        try:
            return set_text(svg, node_id, value, box_id)
        except BuildError as error:
            warnings.append(str(error))
            return svg

    if "repositories" in updates:
        svg = put(
            "stat-repositories",
            format_badge(text_match(svg, "stat-repositories").group(2), updates["repositories"]),
            "stat-repositories-box",
        )
    if "stars" in updates:
        svg = put(
            "stat-stars",
            format_badge(text_match(svg, "stat-stars").group(2), updates["stars"]),
            "stat-stars-box",
        )
    if "forks" in updates:
        svg = put(
            "stat-forks",
            format_badge(text_match(svg, "stat-forks").group(2), updates["forks"]),
            "stat-forks-box",
        )
    if contributions is not None:
        svg = put(
            "stat-contributions",
            format_badge(text_match(svg, "stat-contributions").group(2), contributions),
            "stat-contributions-box",
        )

    since_text = stats_cfg.get("contributions_since")
    if since_text:
        svg = put("stat-contributions-range", format_range(dt.date.fromisoformat(since_text)))

    svg = keep_gap(svg, "stat-contributions-box", "stat-repositories-box")
    svg = keep_gap(svg, "stat-stars-box", "stat-forks-box")

    # ---- project cards ---------------------------------------------------
    projects = cfg.get("projects", [])
    stale_days = int(stats_cfg.get("stale_after_days", STALE_AFTER_DAYS))

    for project in projects:
        card = project["card"]
        slug = project["repo"]
        overrides = project.get("override", {})
        label = f"card {card} ({slug})"

        try:
            repo = fetch_repo(slug, token)
        except BuildError as error:
            warnings.append(f"{label}: {error}")
            continue

        # link href (both href and xlink:href)
        url = overrides.get("url", f"https://github.com/{slug}")
        svg = set_attribute_on_id(svg, f"project-{card}-link", "href", url)
        svg = set_attribute_on_id(svg, f"project-{card}-link", "xlink:href", url)
        # aria label
        title = overrides.get("title") or repo.get("name") or slug.split("/")[-1]
        svg = set_attribute_on_id(svg, f"project-{card}-link", "aria-label", f"Open project: {title}")

        # title
        svg = put(f"project-{card}-title", title)

        # description (greedy word-wrap into the card's 3 tspan lines)
        description = overrides.get("description") or repo.get("description") or "No description provided."
        try:
            svg = set_description(svg, f"project-{card}-description", description, x=ORIGIN_X[str(card)], max_width=CARD_TEXT_WIDTH)
        except BuildError as error:
            warnings.append(f"{label} description: {error}")

        # status pill: override wins, else archived / stale / active heuristics
        if overrides.get("status"):
            status_key = str(overrides["status"]).lower()
            status_text = STATUS_MAP.get(status_key, str(overrides["status"]))
        elif repo.get("archived"):
            status_text = "Archived"
        elif (now - parse_timestamp(repo["pushed_at"])).days > stale_days:
            status_text = "Stale"
        else:
            status_text = "Active"

        try:
            svg = set_text(svg, f"project-{card}-status", status_text)
            box = rect_match(svg, f"project-{card}-status-box")
            attrs = box.group(1)
            box_x = float(attribute(attrs, "x", "0"))
            width = max(40.0, round(18 + text_width(status_text, 9.5), 1))
            svg = svg[: box.start(1)] + replace_attribute(attrs, "width", f"{width:g}") + svg[box.end(1) :]
            status_match = text_match(svg, f"project-{card}-status")
            svg = svg[: status_match.start(1)] + replace_attribute(
                status_match.group(1), "x", f"{box_x + width / 2:g}"
            ) + svg[status_match.end(1) :]
        except BuildError as error:
            warnings.append(f"{label} status: {error}")

        # tech tags: overrides win, else top languages from the repo
        tags = overrides.get("tags") or fetch_languages(slug, token)[:3]
        tag_x = {1: 67, 2: 302, 3: 538, 4: 778}[card]
        if not tags:
            warnings.append(f"{label}: no tags resolved")
        else:
            x = tag_x
            pieces = []
            for tag in tags[:3]:
                w = round(14 + text_width(tag, 9.5), 1)
                cx = x + w / 2
                pieces.append(
                    f'<rect x="{x:g}" y="246" width="{w:g}" height="20" rx="4" fill="url(#tag-surface)" stroke="#0a5a80" stroke-width=".6"/>'
                    f'<text class="tag-text" x="{cx:g}" y="259.5">{escape_xml(tag)}</text>'
                )
                x += w + 5
            pattern = re.compile(
                r'<g id="project-' + str(card) + r'-technologies">.*?</g>', re.DOTALL
            )
            if not pattern.search(svg):
                warnings.append(f"{label}: technologies group missing")
            else:
                svg = pattern.sub(lambda _m: f'<g id="project-{card}-technologies">' + "".join(pieces) + "</g>", svg, count=1)

        # last updated + stars
        pushed_at = parse_timestamp(repo["pushed_at"])
        svg = put(f"project-{card}-updated", relative_time(pushed_at, now))
        svg = put(f"project-{card}-star-count", format_stars(repo["stargazers_count"]))

    # ---- social links ------------------------------------------------
    for social in cfg.get("socials", []):
        sid = social.get("id")
        url = social.get("url")
        if not sid or not url:
            continue
        try:
            svg = set_attribute_on_id(svg, f"social-{sid}", "href", url)
            svg = set_attribute_on_id(svg, f"social-{sid}", "xlink:href", url)
        except BuildError as error:
            warnings.append(f"social-{sid}: {error}")

    # ---- tech stack links ---------------------------------------------
    for tech in cfg.get("tech", []):
        tid = tech.get("id")
        url = tech.get("url")
        if not tid or not url:
            continue
        try:
            svg = set_attribute_on_id(svg, f"tech-{tid}", "href", url)
            svg = set_attribute_on_id(svg, f"tech-{tid}", "xlink:href", url)
        except BuildError as error:
            warnings.append(f"tech-{tid}: {error}")

    # ---- profile visit counter -----------------------------------------
    views = data.get("views")
    views_cfg = cfg.get("views") or {}
    if views_cfg.get("label"):
        svg = put("views-label", escape_xml(views_cfg["label"]))
    if views is not None:
        svg = put("views-count", format_views(views))

    return svg


def generate_svg(template, cfg, data, now, warnings, token=""):
    svg = apply_svg(template, cfg, data, now, warnings, token)
    expected = [
        "stat-contributions", "stat-repositories", "stat-stars", "stat-forks",
        "stat-contributions-range",
        "stat-contributions-box", "stat-repositories-box", "stat-stars-box", "stat-forks-box",
        "views-count",
    ]
    for project in cfg.get("projects", []):
        c = project["card"]
        expected += [
            f"project-{c}-title", f"project-{c}-description", f"project-{c}-status",
            f"project-{c}-updated", f"project-{c}-star-count", f"project-{c}-link",
        ]
    for social in cfg.get("socials", []):
        if social.get("id"):
            expected.append(f"social-{social['id']}")
    for tech in cfg.get("tech", []):
        if tech.get("id"):
            expected.append(f"tech-{tech['id']}")
    validate(svg, expected)
    return svg


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Fill live GitHub values into the profile SVG + README.")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--template", default="profile.template.svg")
    parser.add_argument("--out", default="profile.svg")
    parser.add_argument("--readme", default="README.md", help="also regenerate README.md (default)")
    parser.add_argument("--no-readme", action="store_true", help="skip README generation")
    parser.add_argument("--token", default=os.environ.get("STATS_PAT") or os.environ.get("GH_TOKEN") or "")
    parser.add_argument("--now", default="", help="ISO-8601 UTC timestamp override (for tests)")
    parser.add_argument("--dry-run", action="store_true", help="report values without writing files")
    parser.add_argument("--offline", action="store_true", help="render template as-is (no network)")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero if any primary stat could not be fetched",
    )
    arguments = parser.parse_args(argv)

    cfg = json.loads(Path(arguments.config).read_text(encoding="utf-8"))
    with open(arguments.template, encoding="utf-8", newline="") as handle:
        template = handle.read()
    now = parse_timestamp(arguments.now) if arguments.now else dt.datetime.now(dt.timezone.utc)

    warnings = []
    if arguments.offline:
        data = {"repos": None, "totals": None, "contributions": None}
        if not arguments.dry_run:
            Path(arguments.out).write_text(template, encoding="utf-8", newline="\n")
            print(f"offline mode: wrote template copy to {arguments.out}")
            return 0
        print("offline + dry-run: nothing to do")
        return 0

    data = build_updates(cfg, arguments.token, now, warnings)
    svg = generate_svg(template, cfg, data, now, warnings, arguments.token)

    # ---- report ------------------------------------------------------
    print()
    print(f"stats: repos={data['totals'] and data['totals']['repositories']} "
          f"stars={data['totals'] and data['totals']['stars']} "
          f"forks={data['totals'] and data['totals']['forks']} "
          f"contributions={data['contributions']}")
    print(f"views: {data['views']}")
    if warnings:
        print()
        for warning in warnings:
            print("warning: " + warning)
    write_step_summary(data, warnings)

    if arguments.strict:
        missing = []
        if data.get("totals") is None:
            missing.append("repository stats")
        if data.get("contributions") is None and cfg.get("stats", {}).get("contributions_since"):
            missing.append("contributions")
        if missing:
            print(
                "\nerror: strict mode, could not fetch " + ", ".join(missing) + "; refusing to publish",
                file=sys.stderr,
            )
            return 2

    if arguments.dry_run:
        print("\ndry run: nothing written")
        return 0

    Path(arguments.out).write_text(svg, encoding="utf-8", newline="\n")
    print(f"wrote {arguments.out} ({len(svg.encode('utf-8')):,} bytes)")

    if not arguments.no_readme:
        profile_url = cfg.get("site", {}).get(
            "profile_svg_url",
            f"https://raw.githubusercontent.com/{cfg['github']['user']}/{cfg['github']['user']}/live/profile.svg",
        )
        readme = generate_readme(cfg, profile_url)
        Path(arguments.readme).write_text(readme, encoding="utf-8", newline="\n")
        print(f"wrote {arguments.readme}")

    return 0


def write_step_summary(data, warnings):
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    totals = data.get("totals") or {}
    lines = [
        "## Live profile build",
        "",
        "| value | live |",
        "| --- | ---: |",
        f"| repositories | {totals.get('repositories', '-')} |",
        f"| stars | {totals.get('stars', '-')} |",
        f"| forks | {totals.get('forks', '-')} |",
        f"| contributions | {data.get('contributions', '-')} |",
        f"| profile views | {data.get('views', '-')} |",
    ]
    if warnings:
        lines += ["", "### Warnings", ""] + [f"- {warning}" for warning in warnings]
    try:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as error:
        print(f"could not write step summary: {error}", file=sys.stderr)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildError as failure:
        print(f"error: {failure}", file=sys.stderr)
        sys.exit(1)
