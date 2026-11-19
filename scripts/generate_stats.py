#!/usr/bin/env python3
"""
Generate self-hosted SVG cards for the GitHub profile README.

Outputs (in ./assets):
  - commits-by-language-{dark,light}.svg  : languages in your recent projects, by code size
  - stats-{dark,light}.svg                : headline numbers (commits, contributions, PRs, repos)

The language card measures code size (bytes per language, as reported by GitHub) in the
repositories you committed to during the last WINDOW_DAYS days. Commit counts undervalue
projects built in a few large commits; code size shows what you actually wrote recently.
Commit counts per language are still saved in stats.json for reference.

Why self-hosted? Public stats services (github-readme-stats on Vercel, etc.) are often
rate-limited or down, and they can't see private repositories. This script runs inside a
GitHub Action with your own token, so private work is counted too — only aggregate numbers
are published, never repo names or code.

Token: set a repository secret named GH_STATS_TOKEN with a fine-grained or classic PAT that
can read your repositories (classic: `repo` + `read:user`; fine-grained: "All repositories",
Metadata + Contents = read-only). The Action's built-in GITHUB_TOKEN can't be used here:
it belongs to a bot, not to you, so "your commits" can't be resolved with it.

No third-party dependencies — standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

API = "https://api.github.com/graphql"

# ---- Configuration -----------------------------------------------------------

TOP_N = 7  # languages shown; the rest are grouped into "Other"

# The language card looks at repos you committed to in the last WINDOW_DAYS days.
# Set to 0 to use every repo you ever committed to. The stats card is always all-time.
WINDOW_DAYS = 365

# Merge related languages into one bar.
LANGUAGE_ALIASES = {
    "Blade": "PHP",
    "Hack": "PHP",
    "Jupyter Notebook": "Python",
    "TSX": "TypeScript",
}

# Left out of the code-size card: markup, styling and build files, which are often large
# (generated pages, bundled CSS) without saying much about programming skills.
EXCLUDE_LANGUAGES = {
    "HTML", "CSS", "SCSS", "Sass", "Less", "Stylus",
    "Dockerfile", "Makefile", "Procfile", "Batchfile", "Nix", "Smarty", "Twig",
}

# Repositories to ignore (owner/name), e.g. tutorials or vendored code.
EXCLUDE_REPOS: set[str] = set()

# Bar colours (GitHub linguist colours, tuned for contrast on dark and light backgrounds).
COLORS = {
    "Go": "#00ADD8",
    "PHP": "#777BB4",
    "Python": "#3572A5",
    "JavaScript": "#E3C800",
    "TypeScript": "#3178C6",
    "Vue": "#41B883",
    "Ruby": "#CC342D",
    "Kotlin": "#A97BFF",
    "Java": "#B07219",
    "C#": "#178600",
    "Rust": "#DEA584",
    "Lua": "#5A6FD6",
    "Shell": "#89E051",
    "PLpgSQL": "#336790",
    "Other": "#8B949E",
}

THEMES = {
    "dark": {
        "bg": "#0d1117",
        "border": "#30363d",
        "title": "#e6edf3",
        "text": "#c9d1d9",
        "muted": "#8b949e",
        "track": "#21262d",
        "accent": "#00ADD8",
    },
    "light": {
        "bg": "#ffffff",
        "border": "#d0d7de",
        "title": "#1f2328",
        "text": "#1f2328",
        "muted": "#656d76",
        "track": "#eaeef2",
        "accent": "#0086a8",
    },
}

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"

# ---- GitHub API --------------------------------------------------------------


def gql(token: str, query: str, variables: dict | None = None) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "profile-stats-generator",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.load(resp)
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"], indent=2))
    return payload["data"]


VIEWER_Q = """
query {
  viewer {
    login id createdAt
    pullRequests { totalCount }
    contributionsCollection {
      contributionCalendar { totalContributions }
    }
  }
}
"""

REPO_FIELDS = """
  pageInfo { hasNextPage endCursor }
  nodes {
    nameWithOwner isFork
    primaryLanguage { name }
    languages(first: 20, orderBy: {field: SIZE, direction: DESC}) {
      edges { size node { name } }
    }
    defaultBranchRef {
      target {
        ... on Commit {
          all: history(author: {id: $uid}) { totalCount }
          recent: history(author: {id: $uid}, since: $since) { totalCount }
        }
      }
    }
  }
"""

OWN_REPOS_Q = f"""
query($uid: ID!, $since: GitTimestamp!, $cursor: String) {{
  viewer {{
    repositories(first: 20, after: $cursor,
                 ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {{
      {REPO_FIELDS}
    }}
  }}
}}
"""

CONTRIB_REPOS_Q = f"""
query($uid: ID!, $since: GitTimestamp!, $cursor: String) {{
  viewer {{
    repositoriesContributedTo(first: 20, after: $cursor, includeUserRepositories: true,
                              contributionTypes: [COMMIT, PULL_REQUEST]) {{
      {REPO_FIELDS}
    }}
  }}
}}
"""


def paged_repos(token: str, query: str, key: str, uid: str, since: str) -> list[dict]:
    out, cursor = [], None
    while True:
        data = gql(token, query, {"uid": uid, "since": since, "cursor": cursor})["viewer"][key]
        out.extend(n for n in data["nodes"] if n)
        if not data["pageInfo"]["hasNextPage"]:
            return out
        cursor = data["pageInfo"]["endCursor"]


def _alias(lang: str) -> str:
    return LANGUAGE_ALIASES.get(lang, lang)


def collect(token: str) -> dict:
    viewer = gql(token, VIEWER_Q)["viewer"]
    uid = viewer["id"]
    now = dt.datetime.now(dt.timezone.utc)
    since_dt = now - dt.timedelta(days=WINDOW_DAYS) if WINDOW_DAYS else dt.datetime(2008, 1, 1, tzinfo=dt.timezone.utc)
    since = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    repos: dict[str, dict] = {}
    for q, key in ((OWN_REPOS_Q, "repositories"), (CONTRIB_REPOS_Q, "repositoriesContributedTo")):
        for r in paged_repos(token, q, key, uid, since):
            repos[r["nameWithOwner"]] = r

    bytes_recent: dict[str, int] = defaultdict(int)  # code size in repos committed to recently
    bytes_all: dict[str, int] = defaultdict(int)  # code size in every repo committed to
    commits_recent: dict[str, int] = defaultdict(int)
    commits_all: dict[str, int] = defaultdict(int)
    total_commits = 0
    repos_with_commits = 0

    for name, r in repos.items():
        if name in EXCLUDE_REPOS or r["isFork"]:
            continue
        target = (r.get("defaultBranchRef") or {}).get("target") or {}
        count = (target.get("all") or {}).get("totalCount", 0)
        recent = (target.get("recent") or {}).get("totalCount", 0)
        if not count:
            continue
        total_commits += count
        repos_with_commits += 1

        primary = _alias((r.get("primaryLanguage") or {}).get("name") or "Other")
        commits_all[primary] += count
        if recent:
            commits_recent[primary] += recent

        for edge in (r.get("languages") or {}).get("edges") or []:
            lang = edge["node"]["name"]
            if lang in EXCLUDE_LANGUAGES:
                continue
            lang = _alias(lang)
            bytes_all[lang] += edge["size"]
            if recent:
                bytes_recent[lang] += edge["size"]

    created = dt.datetime.fromisoformat(viewer["createdAt"].replace("Z", "+00:00"))
    use_recent = bool(bytes_recent)  # no recent activity → fall back to all-time
    return {
        "login": viewer["login"],
        "since": created.year,
        "language_window_days": WINDOW_DAYS if use_recent else 0,
        "code_bytes_by_language": dict(bytes_recent if use_recent else bytes_all),
        "commits_by_language": dict(commits_recent),
        "commits_by_language_all_time": dict(commits_all),
        "total_commits": total_commits,
        "repos": repos_with_commits,
        "pull_requests": viewer["pullRequests"]["totalCount"],
        "contributions_last_year": viewer["contributionsCollection"]["contributionCalendar"][
            "totalContributions"
        ],
    }


# ---- SVG rendering -----------------------------------------------------------


def _fmt(n: int) -> str:
    return f"{n:,}"


def _card(width: int, height: int, theme: dict, title: str, body: str, footer: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">
  <title>{html.escape(title)}</title>
  <style>
    text {{ font-family: {FONT}; }}
    .title {{ font-size: 15px; font-weight: 600; fill: {theme['title']}; }}
    .label {{ font-size: 12.5px; font-weight: 500; fill: {theme['text']}; }}
    .value {{ font-size: 12.5px; font-weight: 600; fill: {theme['text']}; font-variant-numeric: tabular-nums; }}
    .muted {{ font-size: 11px; fill: {theme['muted']}; }}
    .big {{ font-size: 24px; font-weight: 700; fill: {theme['title']}; font-variant-numeric: tabular-nums; }}
    .bar {{ animation: grow 0.9s ease-out both; transform-origin: left; transform-box: fill-box; }}
    @keyframes grow {{ from {{ transform: scaleX(0); }} to {{ transform: scaleX(1); }} }}
    @media (prefers-reduced-motion: reduce) {{ .bar {{ animation: none; }} }}
  </style>
  <rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="10" fill="{theme['bg']}" stroke="{theme['border']}"/>
  <text x="24" y="36" class="title">{html.escape(title)}</text>
{body}
  <text x="24" y="{height - 16}" class="muted">{html.escape(footer)}</text>
</svg>
"""


def render_languages(data: dict, theme: dict) -> str:
    items = sorted(data["code_bytes_by_language"].items(), key=lambda kv: kv[1], reverse=True)
    if len(items) > TOP_N:
        rest = sum(v for _, v in items[TOP_N:])
        items = items[:TOP_N] + [("Other", rest)]
    total = sum(v for _, v in items) or 1
    peak = max((v for _, v in items), default=1)

    width, row_h, top = 495, 30, 60
    bar_x = 24 + 92
    bar_max = width - bar_x - 80
    rows = []
    for i, (lang, value) in enumerate(items):
        y = top + i * row_h
        w = max(4, round(bar_max * value / peak))
        color = COLORS.get(lang, COLORS["Other"])
        pct = 100 * value / total
        delay = f"animation-delay: {i * 0.08:.2f}s"
        rows.append(
            f'  <text x="24" y="{y + 13}" class="label">{html.escape(lang)}</text>\n'
            f'  <rect x="{bar_x}" y="{y + 2}" width="{bar_max}" height="14" rx="4" fill="{theme["track"]}"/>\n'
            f'  <rect x="{bar_x}" y="{y + 2}" width="{w}" height="14" rx="4" fill="{color}" class="bar" style="{delay}"/>\n'
            f'  <text x="{width - 24}" y="{y + 13}" class="value" text-anchor="end">{pct:.1f}%</text>'
        )
    height = top + len(items) * row_h + 34
    window = data.get("language_window_days", 0)
    today = f"{dt.date.today():%b %Y}"
    if window:
        period = "last 12 months" if window == 365 else f"last {window} days"
        title = f"Languages in my projects · {period}"
        footer = f"Code size in repos I committed to in the {period} · updated {today}"
    else:
        title = "Languages in my projects"
        footer = f"Code size in repos I committed to · updated {today}"
    return _card(width, height, theme, title, "\n".join(rows), footer)


def render_stats(data: dict, theme: dict) -> str:
    width, height = 495, 180
    metrics = [
        (_fmt(data["total_commits"]), "commits"),
        (_fmt(data["contributions_last_year"]), "contributions (12 mo)"),
        (_fmt(data["pull_requests"]), "pull requests"),
        (_fmt(data["repos"]), "repositories"),
    ]
    col_w = (width - 48) / 2
    cells = []
    for i, (value, label) in enumerate(metrics):
        x = 24 + (i % 2) * col_w
        y = 80 + (i // 2) * 54
        cells.append(
            f'  <rect x="{x}" y="{y - 22}" width="3" height="30" rx="1.5" fill="{theme["accent"]}"/>\n'
            f'  <text x="{x + 14}" y="{y - 2}" class="big">{value}</text>\n'
            f'  <text x="{x + 14}" y="{y + 12}" class="muted">{html.escape(label)}</text>'
        )
    footer = f"Public + private work · on GitHub since {data['since']} · updated {dt.date.today():%b %Y}"
    return _card(width, height + 10, theme, "GitHub activity", "\n".join(cells), footer)


def write_all(data: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, theme in THEMES.items():
        # File name kept from the first version so the README doesn't need to change.
        (out_dir / f"commits-by-language-{name}.svg").write_text(render_languages(data, theme))
        (out_dir / f"stats-{name}.svg").write_text(render_stats(data, theme))
    (out_dir / "stats.json").write_text(json.dumps(data, indent=2) + "\n")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="assets", help="output directory (default: assets)")
    p.add_argument("--from-json", help="render from a saved stats.json instead of calling the API")
    args = p.parse_args()

    if args.from_json:
        data = json.loads(Path(args.from_json).read_text())
    else:
        token = os.environ.get("GH_STATS_TOKEN")
        if not token:
            print(
                "GH_STATS_TOKEN is not set. Create a personal access token and add it as a "
                "repository secret named GH_STATS_TOKEN (see the docstring at the top of this file).",
                file=sys.stderr,
            )
            return 1
        data = collect(token)

    write_all(data, Path(args.out))
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
