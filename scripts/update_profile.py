from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

REST = "https://api.github.com"
GRAPHQL = "https://api.github.com/graphql"
USER_AGENT = "ai-chetan-profile-live-stats"
BOX_SUFFIX = "-box"
MIN_BADGE_GAP = 24
MIN_BOX_WIDTH = 34
BADGE_FONT_SIZE = "27"
ROWS = (("stat-contributions", "stat-repositories"), ("stat-stars", "stat-forks"), ("stat-views", "stat-clones"))
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
      totalContributions
    }
  }
}
"""


class LiveStatsError(RuntimeError):
    pass


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
        detail = error.read().decode("utf-8", "replace")[:200].replace("\n", " ")
        raise LiveStatsError(f"HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise LiveStatsError(f"unreachable: {error.reason}") from error


def api_get(path, token):
    return request_json(REST + path, token)


def api_graphql(query, variables, token):
    payload = request_json(GRAPHQL, token, {"query": query, "variables": variables})
    if payload.get("errors"):
        raise LiveStatsError(json.dumps(payload["errors"])[:200])
    return payload["data"]


def next_month(date):
    if date.month == 12:
        return dt.date(date.year + 1, 1, 1)
    return dt.date(date.year, date.month + 1, 1)


def month_windows(start, end):
    cursor = start.replace(day=1)
    while cursor <= end:
        following = next_month(cursor)
        yield cursor, min(following - dt.timedelta(days=1), end)
        cursor = following


def fetch_contributions(login, since, today, token):
    total = 0
    for window_start, window_end in month_windows(since, today):
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
            raise LiveStatsError(f"no user returned for {login}")
        total += user["contributionsCollection"]["totalContributions"]
    return total


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


def fetch_project(slug, token):
    data = api_get("/repos/" + slug, token)
    return data["stargazers_count"], parse_timestamp(data["pushed_at"])


def fetch_traffic(slug, token):
    views = api_get(f"/repos/{slug}/traffic/views", token)
    clones = api_get(f"/repos/{slug}/traffic/clones", token)
    return views["count"], clones["count"]


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
        raise LiveStatsError(f"expected exactly one {description}, found {len(found)}")
    return found[0]


def text_match(svg, node_id):
    pattern = re.compile(r'<text\b([^>]*\bid="' + re.escape(node_id) + r'"[^>]*)>([^<]*)</text>')
    return single_match(svg, pattern, f'<text id="{node_id}">')


def rect_match(svg, box_id):
    pattern = re.compile(r'<rect\b([^>]*\bid="' + re.escape(box_id) + r'"[^>]*?)/?>')
    return single_match(svg, pattern, f'<rect id="{box_id}">')


def resize_box(svg, box_id, previous, value, font_size):
    match = rect_match(svg, box_id)
    attrs = match.group(1)
    current = float(attribute(attrs, "width", "0"))
    updated = max(MIN_BOX_WIDTH, round(current + text_width(value, font_size) - text_width(previous, font_size), 1))
    attrs = replace_attribute(attrs, "width", f"{updated:g}")
    return svg[: match.start(1)] + attrs + svg[match.end(1) :]


def set_text(svg, node_id, value, box_id=None):
    match = text_match(svg, node_id)
    previous = match.group(2)
    if value == previous:
        return svg
    font_size = float(attribute(match.group(1), "font-size", BADGE_FONT_SIZE))
    escaped = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    svg = svg[: match.start(2)] + escaped + svg[match.end(2) :]
    if box_id:
        svg = resize_box(svg, box_id, previous, value, font_size)
    return svg


def shift_node(svg, node_id, delta):
    for description, matcher in (("<rect>", rect_match), ("<text>", text_match)):
        try:
            match = matcher(svg, node_id)
        except LiveStatsError:
            continue
        attrs = replace_attribute(match.group(1), "x", f"{float(attribute(match.group(1), 'x', '0')) + delta:g}")
        return svg[: match.start(1)] + attrs + svg[match.end(1) :]
    raise LiveStatsError(f'node "{node_id}" not found')


def keep_gap(svg, left_box_id, right_box_id):
    left = rect_match(svg, left_box_id).group(1)
    right = rect_match(svg, right_box_id).group(1)
    edge = float(attribute(left, "x", "0")) + float(attribute(left, "width", "0"))
    overflow = round(edge + MIN_BADGE_GAP - float(attribute(right, "x", "0")), 1)
    if overflow <= 0:
        return svg
    right_id = right_box_id[: -len(BOX_SUFFIX)]
    svg = shift_node(svg, right_box_id, overflow)
    return shift_node(svg, right_id, overflow)


def validate(svg, expected_ids):
    ET.fromstring(svg)
    ids = re.findall(r'\bid="([^"]+)"', svg)
    duplicates = sorted(name for name, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise LiveStatsError("duplicate ids in output: " + ", ".join(duplicates))
    missing = [node_id for node_id in expected_ids if node_id not in ids]
    if missing:
        raise LiveStatsError("missing ids in output: " + ", ".join(missing))


def guarded(label, task, warnings):
    try:
        return task()
    except LiveStatsError as error:
        warnings.append(f"{label}: {error}")
        return None


def build_updates(config, token, now, warnings):
    github = config["github"]
    login = github["user"]
    updates = []

    def entry(node_id, label, value, numeric=True, box_id=None, status="updated"):
        return {
            "node_id": node_id,
            "box_id": box_id,
            "label": label,
            "value": value,
            "numeric": numeric,
            "rendered": "",
            "previous": "",
            "status": status,
        }

    def add(node_id, label, value, numeric=True, box_id=None):
        updates.append(entry(node_id, label, value, numeric, node_id + BOX_SUFFIX if box_id is None else box_id))

    def skip(node_id, label, numeric=True, box_id=None):
        updates.append(entry(node_id, label, None, numeric, box_id, "unavailable"))

    if token:
        repos = guarded("repository list", lambda: fetch_owned_repos(login, token), warnings)
        if repos is None:
            for node_id, label in (
                ("stat-repositories", "repositories"),
                ("stat-stars", "stars"),
                ("stat-forks", "forks"),
            ):
                skip(node_id, label)
        else:
            counted = repos if github["count_forks_in_stars_and_forks"] else [item for item in repos if not item["fork"]]
            owned = repos if github["count_forks_as_repositories"] else [item for item in repos if not item["fork"]]
            add("stat-repositories", "repositories", len(owned))
            add("stat-stars", "stars", sum(item["stargazers_count"] for item in counted))
            add("stat-forks", "forks", sum(item["forks_count"] for item in counted))

        since = dt.date.fromisoformat(github["contributions_since"])
        total = guarded("contributions", lambda: fetch_contributions(login, since, now.date(), token), warnings)
        if total is not None:
            add("stat-contributions", "contributions", total)

        traffic_repo = github.get("traffic_repo")
        traffic = guarded("traffic", lambda: fetch_traffic(traffic_repo, token), warnings) if traffic_repo else None
        if traffic is not None:
            add("stat-views", "repo views (14d)", traffic[0])
            add("stat-clones", "repo clones (14d)", traffic[1])
    else:
        for node_id, label in (
            ("stat-contributions", "contributions"),
            ("stat-repositories", "repositories"),
            ("stat-stars", "stars"),
            ("stat-forks", "forks"),
            ("stat-views", "repo views (14d)"),
            ("stat-clones", "repo clones (14d)"),
        ):
            skip(node_id, label)
        warnings.append(
            "no token supplied: private repositories, contributions and traffic need STATS_PAT, template values kept"
        )

    for project in config["projects"]:
        card = project["card"]
        label = f"card {card} ({project['repo']})"
        stats = guarded(label, lambda slug=project["repo"]: fetch_project(slug, token), warnings)
        if stats is None:
            skip(f"project-{card}-star-count", f"{label} stars")
            skip(f"project-{card}-updated", f"{label} updated", numeric=False)
            continue
        stars, pushed_at = stats
        add(f"project-{card}-star-count", f"{label} stars", format_stars(stars), numeric=False, box_id="")
        add(f"project-{card}-updated", f"{label} updated", relative_time(pushed_at, now), numeric=False, box_id="")

    return updates


def apply_updates(template, updates):
    svg = template
    for update in updates:
        if update["value"] is None:
            continue
        previous = text_match(svg, update["node_id"]).group(2)
        value = format_badge(previous, update["value"]) if update["numeric"] else str(update["value"])
        update["previous"] = previous
        update["rendered"] = value
        if value == previous:
            update["status"] = "unchanged"
            continue
        svg = set_text(svg, update["node_id"], value, update["box_id"])
    for left, right in ROWS:
        svg = keep_gap(svg, left + BOX_SUFFIX, right + BOX_SUFFIX)
    return svg


def report(updates, warnings):
    print("value".ljust(46) + "template".rjust(12) + "live".rjust(12) + "  status")
    print("-" * 84)
    for update in updates:
        rendered = update["rendered"] if update["status"] != "unavailable" else "-"
        print(
            update["label"].ljust(46)
            + update["previous"].rjust(12)
            + rendered.rjust(12)
            + "  " + update["status"]
        )
    if warnings:
        print()
        for warning in warnings:
            print("warning: " + warning)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fill live GitHub values into the profile SVG template.")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--template", default="profile.template.svg")
    parser.add_argument("--out", default="profile.svg")
    parser.add_argument("--token", default=os.environ.get("STATS_PAT") or os.environ.get("GH_TOKEN") or "")
    parser.add_argument("--now", default="", help="ISO-8601 UTC timestamp, overrides the current time")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(argv)

    config = json.loads(Path(arguments.config).read_text(encoding="utf-8"))
    with open(arguments.template, encoding="utf-8", newline="") as handle:
        template = handle.read()
    now = parse_timestamp(arguments.now) if arguments.now else dt.datetime.now(dt.timezone.utc)

    warnings = []
    updates = build_updates(config, arguments.token, now, warnings)
    svg = apply_updates(template, updates)
    validate(svg, [update["node_id"] for update in updates if update["value"] is not None])
    report(updates, warnings)

    if arguments.dry_run:
        print("\ndry run, nothing written")
        return 0
    with open(arguments.out, "w", encoding="utf-8", newline="") as handle:
        handle.write(svg)
    print(f"\nwrote {arguments.out} ({len(svg.encode('utf-8'))} bytes)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LiveStatsError as failure:
        print(f"error: {failure}", file=sys.stderr)
        sys.exit(1)
