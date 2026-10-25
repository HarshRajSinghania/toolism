"""Contributor leaderboard, published weekly to a pinned issue.

The point rules are written for contributors in CONTRIBUTING.md ("Points and levels"). Only
merged or maintainer-confirmed work scores, and points depend on the issue's label, not on the
size of a change, so splitting or padding PRs earns nothing.

    python3 .github/scripts/leaderboard.py             # print the leaderboard (needs a token)
    python3 .github/scripts/leaderboard.py --publish   # update the issue and the README card
    python3 .github/scripts/leaderboard.py --publish --celebrate 123   # ...and congratulate PR #123

Run by .github/workflows/leaderboard.yml with GITHUB_TOKEN and GITHUB_REPOSITORY set. The
scoring and rendering functions are pure and tested in test_leaderboard.py
(`uv run pytest .github/scripts`). The script itself uses the standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

GOOD_FIRST_ISSUE = "good first issue"
HELP_WANTED = "help wanted"
BUG = "bug"
MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

POINTS = {
    "help_wanted_pr": 30,
    "good_first_issue_pr": 10,
    "other_pr": 5,
    "first_pr_bonus": 5,
    "review": 3,
    "bug": 5,
}
MIN_REVIEW_CHARS = 10  # a review must say something, not just click "Approve"
REVIEW_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "COMMENTED"})
BONUS_LABEL = re.compile(r"^points:\s*\+(\d+)$", re.IGNORECASE)
LINK = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.IGNORECASE)
MARKER = "<!-- toolism-leaderboard -->"
CELEBRATE_MARKER = "<!-- toolism-leaderboard:celebrate -->"
TITLE = "🏆 Contributor leaderboard"
BOARD_BRANCH = "leaderboard"  # unprotected branch holding only the README card
BOARD_FILE = "leaderboard.svg"
PR_KINDS = frozenset({"help_wanted_pr", "good_first_issue_pr", "other_pr"})
KIND_LABELS = {
    "help_wanted_pr": "help wanted issue",
    "good_first_issue_pr": "good first issue",
    "other_pr": "merged PR",
    "first_pr_bonus": "first merged PR",
    "bonus": "maintainer bonus",
}

# (emoji and name, merged PRs needed, reviews needed), highest first.
LEVELS = (
    ("🏅 Core", 10, 10),
    ("⭐ Regular", 5, 0),
    ("🔧 Contributor", 3, 0),
    ("🌱 Newcomer", 1, 0),
)
SUPPORTER = "👋 Supporter"  # points from reviews or bug reports, no merged PR yet


# ---------------------------------------------------------------------- scoring


@dataclass(frozen=True)
class Contribution:
    login: str
    kind: str
    points: int
    when: datetime
    ref: int  # PR or issue number


@dataclass
class Standing:
    login: str
    points: int = 0
    merged_prs: int = 0
    reviews: int = 0
    level: str = ""


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def linked_issues(body: str | None) -> list[int]:
    """Issue numbers a PR body says it closes ("Closes #12", "fixes #3", ...)."""
    return sorted({int(n) for n in LINK.findall(body or "")})


def _is_bot(user: dict[str, Any] | None) -> bool:
    return not user or user.get("type") == "Bot" or user.get("login", "").endswith("[bot]")


def _labels(item: dict[str, Any]) -> set[str]:
    return {label["name"] if isinstance(label, dict) else label for label in item.get("labels", [])}


def maintainers(prs: Iterable[dict[str, Any]], reviews: Iterable[dict[str, Any]]) -> set[str]:
    """Logins GitHub reports as owner, member or collaborator on any PR or review."""
    found = set()
    for item in [*prs, *reviews]:
        user = item.get("user") or {}
        if item.get("author_association") in MAINTAINER_ASSOCIATIONS and user.get("login"):
            found.add(user["login"])
    return found


def score_prs(
    prs: Iterable[dict[str, Any]], issue_labels: dict[int, set[str]]
) -> list[Contribution]:
    """Points for merged PRs, by the label of the issue they close, plus bonuses."""
    merged = sorted(
        (pr for pr in prs if pr.get("merged_at") and not _is_bot(pr.get("user"))),
        key=lambda pr: (pr["merged_at"], pr["number"]),
    )
    out: list[Contribution] = []
    seen: set[str] = set()
    for pr in merged:
        login, when = pr["user"]["login"], parse_time(pr["merged_at"])
        labels: set[str] = set()
        for number in linked_issues(pr.get("body")):
            labels |= issue_labels.get(number, set())
        if HELP_WANTED in labels:
            kind = "help_wanted_pr"
        elif GOOD_FIRST_ISSUE in labels:
            kind = "good_first_issue_pr"
        else:
            kind = "other_pr"
        out.append(Contribution(login, kind, POINTS[kind], when, pr["number"]))
        if login not in seen:
            seen.add(login)
            out.append(
                Contribution(login, "first_pr_bonus", POINTS["first_pr_bonus"], when, pr["number"])
            )
        for label in _labels(pr):
            match = BONUS_LABEL.match(label.strip())
            if match:
                out.append(Contribution(login, "bonus", int(match.group(1)), when, pr["number"]))
    return out


def score_reviews(reviews: Iterable[dict[str, Any]]) -> list[Contribution]:
    """One review credit per reviewer per PR, for a written review of someone else's PR.

    Each review dict carries the API fields plus ``pr_number`` and ``pr_author``.
    """
    best: dict[tuple[str, int], datetime] = {}
    for review in reviews:
        user = review.get("user")
        if _is_bot(user) or review.get("state") not in REVIEW_STATES:
            continue
        login = user["login"]
        if (
            login == review["pr_author"]
            or len((review.get("body") or "").strip()) < MIN_REVIEW_CHARS
        ):
            continue
        when = parse_time(review["submitted_at"])
        key = (login, review["pr_number"])
        if key not in best or when < best[key]:
            best[key] = when
    return [
        Contribution(login, "review", POINTS["review"], when, number)
        for (login, number), when in sorted(best.items())
    ]


def score_bugs(issues: Iterable[dict[str, Any]]) -> list[Contribution]:
    """Bug reports a maintainer confirmed: labelled ``bug`` and closed as completed."""
    out = []
    for issue in issues:
        if "pull_request" in issue or _is_bot(issue.get("user")):
            continue
        if BUG not in _labels(issue) or issue.get("state_reason") != "completed":
            continue
        if not issue.get("closed_at"):
            continue
        out.append(
            Contribution(
                issue["user"]["login"],
                "bug",
                POINTS["bug"],
                parse_time(issue["closed_at"]),
                issue["number"],
            )
        )
    return out


def level(merged_prs: int, reviews: int, points: int) -> str:
    for name, prs_needed, reviews_needed in LEVELS:
        if merged_prs >= prs_needed and reviews >= reviews_needed:
            return name
    return SUPPORTER if points else ""


def standings(
    contributions: Iterable[Contribution], *, since: datetime | None = None
) -> list[Standing]:
    """Totals per person, highest points first. Levels always use all-time counts."""
    items = list(contributions)
    rows: dict[str, Standing] = {}
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])  # all-time prs, reviews, points
    for c in items:
        prs, reviews, points = totals[c.login]
        totals[c.login] = [
            prs + c.kind.endswith("_pr"),
            reviews + (c.kind == "review"),
            points + c.points,
        ]
        if since is not None and c.when < since:
            continue
        row = rows.setdefault(c.login, Standing(c.login))
        row.points += c.points
    for login, row in rows.items():
        prs, reviews, points = totals[login]
        row.merged_prs, row.reviews = prs, reviews
        row.level = level(prs, reviews, points)
    return sorted(rows.values(), key=lambda r: (-r.points, r.login.lower()))


def newcomers(contributions: Iterable[Contribution], since: datetime) -> list[Contribution]:
    """First merged PRs since ``since``, oldest first."""
    return sorted(
        (c for c in contributions if c.kind == "first_pr_bonus" and c.when >= since),
        key=lambda c: c.when,
    )


# -------------------------------------------------------------------- rendering


def _ranked(rows: list[Standing]) -> Iterator[tuple[int, Standing]]:
    rank, previous = 0, None
    for position, row in enumerate(rows, start=1):
        if row.points != previous:
            rank, previous = position, row.points
        yield rank, row


def _table(rows: list[Standing], repo: str, *, ranked: bool = True) -> list[str]:
    if not rows:
        return ["_Nobody yet: be the first!_"]
    head = (
        "| Rank | Contributor | Level | Points | PRs | Reviews |"
        if ranked
        else ("| Contributor | Level | Points | PRs | Reviews |")
    )
    lines = [
        head,
        "| ---: | --- | --- | ---: | ---: | ---: |"
        if ranked
        else "| --- | --- | ---: | ---: | ---: |",
    ]
    for rank, row in _ranked(rows):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, str(rank)) if ranked else ""
        who = (
            f'<img src="https://github.com/{row.login}.png?size=40" width="20" alt=""> '
            f"[@{row.login}](https://github.com/{repo}/pulls?q=is%3Apr+is%3Amerged+author%3A{row.login})"
        )
        cells = [who, row.level or "—", str(row.points), str(row.merged_prs), str(row.reviews)]
        lines.append("| " + " | ".join(([medal] if ranked else []) + cells) + " |")
    return lines


def render(
    contributions: list[Contribution],
    *,
    repo: str,
    now: datetime,
    maintainer_logins: set[str],
) -> str:
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    everyone = standings(contributions)
    month = standings(contributions, since=month_start)
    fresh = [
        c
        for c in newcomers(contributions, now - timedelta(days=7))
        if c.login not in maintainer_logins
    ]
    guide = f"https://github.com/{repo}/blob/master/CONTRIBUTING.md#points-and-levels"
    lines = [
        f"# {TITLE}",
        "",
        f"Updated {now:%Y-%m-%d}. Points come from merged PRs, written reviews and confirmed bug "
        f"reports; see [Points and levels]({guide}). Updated on every merged PR and every Monday.",
        "",
        f"## This month ({now:%B %Y})",
        "",
        *_table([r for r in month if r.login not in maintainer_logins], repo),
        "",
        "## All time",
        "",
        *_table([r for r in everyone if r.login not in maintainer_logins], repo),
        "",
        "## 🎉 First PR merged this week",
        "",
    ]
    lines += [f"- @{c.login} (#{c.ref})" for c in fresh] or [
        "_None this week. Yours could be next!_"
    ]
    team = [r for r in everyone if r.login in maintainer_logins]
    if team:
        lines += ["", "## Maintainers (not ranked)", "", *_table(team, repo, ranked=False)]
    lines += ["", MARKER, ""]
    return "\n".join(lines)


def _xml(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def render_svg(
    contributions: list[Contribution],
    *,
    now: datetime,
    maintainer_logins: set[str],
    top: int = 10,
) -> str:
    """A small card with the top contributors, for the README (which can't embed live text).

    Shows this month's ranking, or all-time when nobody has scored this month yet.
    """
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = [
        r for r in standings(contributions, since=month_start) if r.login not in maintainer_logins
    ]
    heading = f"Top contributors \u2014 {now:%B %Y}"
    if not rows:
        rows = [r for r in standings(contributions) if r.login not in maintainer_logins]
        heading = "Top contributors \u2014 all time"
    rows = rows[:top]
    width, row_height, top_pad = 480, 30, 64
    height = top_pad + max(len(rows), 1) * row_height + 30
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{_xml(heading)}">',
        "<style>text{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,"
        "sans-serif;fill:#1f2328}.muted{fill:#59636e}.points{font-weight:600}</style>",
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="10" '
        'fill="#ffffff" stroke="#d1d9e0"/>',
        f'<text x="20" y="34" font-size="18" font-weight="600">\U0001f3c6 {_xml(heading)}</text>',
    ]
    if not rows:
        out.append(
            f'<text x="20" y="{top_pad + 20}" font-size="14" class="muted">'
            "Nobody yet: be the first!</text>"
        )
    for index, (rank, row) in enumerate(_ranked(rows)):
        y = top_pad + index * row_height + 20
        medal = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}.get(rank, f"{rank}.")
        out += [
            f'<text x="20" y="{y}" font-size="15">{medal}</text>',
            f'<text x="60" y="{y}" font-size="15">@{_xml(row.login)}</text>',
            f'<text x="270" y="{y}" font-size="13" class="muted">{_xml(row.level)}</text>',
            f'<text x="{width - 20}" y="{y}" font-size="15" text-anchor="end" class="points">'
            f"{row.points} pts</text>",
        ]
    out += [
        f'<text x="20" y="{height - 12}" font-size="11" class="muted">'
        f"Updated {now:%Y-%m-%d} \u00b7 points for merged PRs, reviews and bug reports</text>",
        "</svg>",
        "",
    ]
    return "\n".join(out)


def _next_level(merged_prs: int, reviews: int) -> str | None:
    """What it takes to reach the next level, or None at the top."""
    for name, prs_needed, reviews_needed in reversed(LEVELS):
        if merged_prs < prs_needed or reviews < reviews_needed:
            missing = []
            if merged_prs < prs_needed:
                n = prs_needed - merged_prs
                missing.append(f"{n} more merged PR{'s' if n != 1 else ''}")
            if reviews < reviews_needed:
                n = reviews_needed - reviews
                missing.append(f"{n} more review{'s' if n != 1 else ''}")
            return f"{name} is {' and '.join(missing)} away"
    return None


def celebration(
    contributions: list[Contribution],
    *,
    pr_number: int,
    author: str,
    now: datetime,
    maintainer_logins: set[str],
    board_url: str,
) -> str | None:
    """A comment for a just-merged PR: points earned, rank and level. None when nothing scored."""
    lines: list[str] = []
    earned = [
        c
        for c in contributions
        if c.ref == pr_number and c.login == author and c.kind in KIND_LABELS
    ]
    if earned and author not in maintainer_logins:
        total = sum(c.points for c in earned)
        parts = ", ".join(f"{KIND_LABELS[c.kind]} +{c.points}" for c in earned)
        # Standings as of this merge, so a re-run or a later merge can't change the story.
        merged_at = max(c.when for c in earned)
        history = [c for c in contributions if c.when <= merged_at]
        others = [c for c in history if c not in earned]
        before = {r.login: r for r in standings(others)}.get(author)
        after = {r.login: r for r in standings(history)}[author]
        month_start = merged_at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month = [
            r for r in standings(history, since=month_start) if r.login not in maintainer_logins
        ]
        rank = next((rank for rank, row in _ranked(month) if row.login == author), None)
        lines.append(
            f"🎉 Merged, thank you @{author}! This PR earned **+{total} points** ({parts})."
        )
        lines.append("")
        standing = f"You're **#{rank}** this month" if rank else "You're on the board"
        lines.append(f"{standing} with {after.points} points in total, level **{after.level}**.")
        if before is None or before.level != after.level:
            lines.append(f"⬆️ **Level up: {after.level}!**")
        upcoming = _next_level(after.merged_prs, after.reviews)
        if upcoming:
            lines.append(f"Next: {upcoming}.")
    reviewers = sorted(
        c.login
        for c in contributions
        if c.ref == pr_number and c.kind == "review" and c.login not in maintainer_logins
    )
    if reviewers:
        if lines:
            lines.append("")
        thanks = ", ".join(f"@{login}" for login in reviewers)
        lines.append(f"👀 Thanks for reviewing, {thanks}: +{POINTS['review']} points each.")
    if not lines:
        return None
    lines += ["", f"See the [🏆 contributor leaderboard]({board_url}).", CELEBRATE_MARKER, ""]
    return "\n".join(lines)


# --------------------------------------------------------------------- GitHub I/O


class GitHub:
    def __init__(self, repo: str, token: str) -> None:
        self.repo, self.token = repo, token

    def _request(
        self, method: str, url: str, data: dict[str, Any] | None = None
    ) -> tuple[Any, str]:
        if not url.startswith("https://"):
            url = f"https://api.github.com/repos/{self.repo}/{url}"
        request = urllib.request.Request(
            url,
            method=method,
            data=None if data is None else json.dumps(data).encode(),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "toolism-leaderboard",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response), response.headers.get("Link", "")

    def _exists(self, path: str) -> Any:
        try:
            return self._request("GET", path)[0]
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise

    def paginate(self, path: str) -> list[Any]:
        items: list[Any] = []
        url: str | None = path + ("&" if "?" in path else "?") + "per_page=100"
        while url:
            page, link = self._request("GET", url)
            items.extend(page)
            match = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = match.group(1) if match else None
        return items

    def collect(self) -> tuple[list[Contribution], set[str]]:
        prs = self.paginate("pulls?state=all")
        issues = self.paginate("issues?state=all")
        issue_labels = {i["number"]: _labels(i) for i in issues if "pull_request" not in i}
        reviews = []
        for pr in prs:
            if _is_bot(pr.get("user")):
                continue
            for review in self.paginate(f"pulls/{pr['number']}/reviews"):
                review["pr_number"], review["pr_author"] = pr["number"], pr["user"]["login"]
                reviews.append(review)
        contributions = [
            *score_prs(prs, issue_labels),
            *score_reviews(reviews),
            *score_bugs(issues),
        ]
        return contributions, maintainers(prs, reviews)

    def publish(self, body: str) -> str:
        for issue in self.paginate("issues?state=open&creator=github-actions%5Bbot%5D"):
            if MARKER in (issue.get("body") or ""):
                updated, _ = self._request("PATCH", f"issues/{issue['number']}", {"body": body})
                return updated["html_url"]
        created, _ = self._request("POST", "issues", {"title": TITLE, "body": body})
        return created["html_url"]

    def publish_svg(self, svg: str) -> None:
        """Replace the leaderboard branch with a single commit holding the README card.

        The branch only ever contains that one file, so it never carries code and never needs
        branch protection. Each update is a fresh root commit (force-moved ref).
        """
        tree, _ = self._request(
            "POST",
            "git/trees",
            {"tree": [{"path": BOARD_FILE, "mode": "100644", "type": "blob", "content": svg}]},
        )
        commit, _ = self._request(
            "POST",
            "git/commits",
            {"message": "Update leaderboard card", "tree": tree["sha"], "parents": []},
        )
        ref = f"heads/{BOARD_BRANCH}"
        if self._exists(f"git/ref/{ref}") is None:
            self._request("POST", "git/refs", {"ref": f"refs/{ref}", "sha": commit["sha"]})
        else:
            self._request("PATCH", f"git/refs/{ref}", {"sha": commit["sha"], "force": True})

    def comment_once(self, number: int, body: str, marker: str) -> bool:
        """Comment on an issue or PR unless a comment carrying ``marker`` is already there."""
        for existing in self.paginate(f"issues/{number}/comments"):
            if marker in (existing.get("body") or ""):
                return False
        self._request("POST", f"issues/{number}/comments", {"body": body})
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the toolism contributor leaderboard.")
    parser.add_argument(
        "--publish", action="store_true", help="update the issue and the README card"
    )
    parser.add_argument(
        "--celebrate", type=int, metavar="PR", help="comment on this just-merged PR"
    )
    args = parser.parse_args(argv)
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        print("error: GITHUB_REPOSITORY and GITHUB_TOKEN must be set", file=sys.stderr)
        return 2
    github = GitHub(repo, token)
    contributions, team = github.collect()
    now = datetime.now(timezone.utc)
    body = render(contributions, repo=repo, now=now, maintainer_logins=team)
    if not args.publish and args.celebrate is None:
        print(body)
        return 0
    board_url = f"https://github.com/{repo}/issues?q=is%3Aissue+%22{TITLE.split(' ', 1)[1]}%22"
    if args.publish:
        board_url = github.publish(body)
        github.publish_svg(render_svg(contributions, now=now, maintainer_logins=team))
        print(board_url)
    if args.celebrate is not None:
        pr, _ = github._request("GET", f"pulls/{args.celebrate}")
        if pr.get("merged_at"):
            message = celebration(
                contributions,
                pr_number=args.celebrate,
                author=pr["user"]["login"],
                now=now,
                maintainer_logins=team,
                board_url=board_url,
            )
            if message:
                github.comment_once(args.celebrate, message, CELEBRATE_MARKER)
    return 0


if __name__ == "__main__":
    sys.exit(main())
