"""Tests for leaderboard.py. Run with: uv run pytest .github/scripts"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

import leaderboard as lb

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def user(login: str, kind: str = "User") -> dict[str, Any]:
    return {"login": login, "type": kind}


def pr(
    number: int,
    login: str,
    merged_at: str | None,
    body: str = "",
    labels: tuple[str, ...] = (),
    association: str = "CONTRIBUTOR",
    kind: str = "User",
) -> dict[str, Any]:
    return {
        "number": number,
        "user": user(login, kind),
        "merged_at": merged_at,
        "body": body,
        "labels": [{"name": name} for name in labels],
        "author_association": association,
    }


def review(
    pr_number: int,
    pr_author: str,
    login: str,
    body: str,
    when: str,
    state: str = "APPROVED",
    association: str = "NONE",
) -> dict[str, Any]:
    return {
        "pr_number": pr_number,
        "pr_author": pr_author,
        "user": user(login),
        "body": body,
        "state": state,
        "submitted_at": when,
        "author_association": association,
    }


ISSUES = {1: {"good first issue"}, 2: {"help wanted"}, 3: {"bug"}}


class TestPullRequestPoints:
    def kinds(self, prs: list[dict[str, Any]]) -> list[tuple[str, str, int]]:
        return [(c.login, c.kind, c.points) for c in lb.score_prs(prs, ISSUES)]

    def test_points_follow_the_linked_issue_label(self) -> None:
        got = self.kinds(
            [
                pr(10, "ann", "2026-10-01T00:00:00Z", "Closes #1"),
                pr(11, "ann", "2026-10-02T00:00:00Z", "Fixes #2"),
                pr(12, "ann", "2026-10-03T00:00:00Z", "a typo fix"),
            ]
        )
        assert got == [
            ("ann", "good_first_issue_pr", 10),
            ("ann", "first_pr_bonus", 5),
            ("ann", "help_wanted_pr", 30),
            ("ann", "other_pr", 5),
        ]

    def test_help_wanted_wins_when_a_pr_closes_both_kinds(self) -> None:
        got = self.kinds([pr(10, "ann", "2026-10-01T00:00:00Z", "Closes #1, closes #2")])
        assert ("ann", "help_wanted_pr", 30) in got

    def test_unmerged_and_bot_prs_score_nothing(self) -> None:
        got = self.kinds(
            [
                pr(10, "ann", None, "Closes #1"),
                pr(11, "dependabot[bot]", "2026-10-01T00:00:00Z", kind="Bot"),
            ]
        )
        assert got == []

    def test_first_pr_bonus_goes_to_the_earliest_merge_only(self) -> None:
        contributions = lb.score_prs(
            [
                pr(20, "bob", "2026-10-05T00:00:00Z"),
                pr(19, "bob", "2026-10-02T00:00:00Z"),
            ],
            ISSUES,
        )
        bonus = [c for c in contributions if c.kind == "first_pr_bonus"]
        assert [c.ref for c in bonus] == [19]

    def test_maintainer_bonus_label(self) -> None:
        got = self.kinds(
            [pr(10, "ann", "2026-10-01T00:00:00Z", labels=("points: +20", "points: lots"))]
        )
        assert ("ann", "bonus", 20) in got
        assert len([g for g in got if g[1] == "bonus"]) == 1


class TestReviewPoints:
    def test_one_credit_per_reviewer_per_pr_for_written_reviews_of_others(self) -> None:
        got = lb.score_reviews(
            [
                review(10, "ann", "bob", "Ran the tests, all pass.", "2026-10-02T00:00:00Z"),
                review(10, "ann", "bob", "Second look, still good.", "2026-10-03T00:00:00Z"),
                review(11, "ann", "bob", "", "2026-10-03T00:00:00Z"),  # bare approve
                review(12, "bob", "bob", "My own PR looks great!", "2026-10-03T00:00:00Z"),
                review(13, "ann", "carl", "Nice one, thanks.", "2026-10-04T00:00:00Z", "DISMISSED"),
            ]
        )
        assert [(c.login, c.ref, c.points) for c in got] == [("bob", 10, 3)]
        assert got[0].when == lb.parse_time("2026-10-02T00:00:00Z")


class TestBugPoints:
    def test_only_confirmed_bug_reports_count(self) -> None:
        base = {"user": user("dee"), "closed_at": "2026-10-04T00:00:00Z"}
        issues = [
            {**base, "number": 1, "labels": [{"name": "bug"}], "state_reason": "completed"},
            {**base, "number": 2, "labels": [{"name": "bug"}], "state_reason": "not_planned"},
            {**base, "number": 3, "labels": [], "state_reason": "completed"},
            {
                **base,
                "number": 4,
                "labels": [{"name": "bug"}],
                "state_reason": "completed",
                "pull_request": {},
            },
        ]
        assert [c.ref for c in lb.score_bugs(issues)] == [1]


class TestStandings:
    def test_levels_use_all_time_counts_and_month_filters_points(self) -> None:
        old = "2026-09-10T00:00:00Z"
        new = "2026-10-10T00:00:00Z"
        prs = [pr(n, "ann", old) for n in range(1, 5)] + [pr(5, "ann", new, "Closes #2")]
        contributions = lb.score_prs(prs, ISSUES)
        month = lb.standings(contributions, since=datetime(2026, 10, 1, tzinfo=timezone.utc))
        assert len(month) == 1
        assert month[0].points == 30  # only the October PR
        assert month[0].merged_prs == 5
        assert month[0].level == "⭐ Regular"
        everyone = lb.standings(contributions)
        assert everyone[0].points == 4 * 5 + 5 + 30

    def test_level_thresholds(self) -> None:
        assert lb.level(0, 0, 0) == ""
        assert lb.level(0, 2, 6) == lb.SUPPORTER
        assert lb.level(1, 0, 15) == "🌱 Newcomer"
        assert lb.level(3, 0, 30) == "🔧 Contributor"
        assert lb.level(10, 9, 100) == "⭐ Regular"
        assert lb.level(10, 10, 130) == "🏅 Core"

    def test_sorted_by_points_then_name(self) -> None:
        when = lb.parse_time("2026-10-10T00:00:00Z")
        rows = lb.standings(
            [
                lb.Contribution("zed", "review", 3, when, 1),
                lb.Contribution("amy", "review", 3, when, 2),
                lb.Contribution("max", "bug", 5, when, 3),
            ]
        )
        assert [r.login for r in rows] == ["max", "amy", "zed"]


class TestRendering:
    def setup_method(self) -> None:
        prs = [
            pr(1, "ann", "2026-10-22T00:00:00Z", "Closes #2"),
            pr(2, "bob", "2026-10-21T00:00:00Z", "Closes #1"),
            pr(3, "cat", "2026-10-21T00:00:00Z", "Closes #1"),
            pr(4, "boss", "2026-10-20T00:00:00Z", association="OWNER"),
        ]
        self.contributions = lb.score_prs(prs, ISSUES)
        self.page = lb.render(
            self.contributions,
            repo="o/r",
            now=NOW,
            maintainer_logins=lb.maintainers(prs, []),
        )

    def test_sections_marker_and_ranking(self) -> None:
        page = self.page
        assert "# 🏆 Contributor leaderboard" in page
        assert "## This month (October 2026)" in page
        assert "## All time" in page
        assert page.rstrip().endswith(lb.MARKER)
        first = page.index("[@ann]")
        assert first < page.index("[@bob]")
        assert "| 🥇 |" in page
        # bob and cat tie on points, so they share 2nd place
        assert page.split("## All time")[1].count("| 🥈 |") == 2

    def test_maintainers_are_listed_separately_and_not_ranked(self) -> None:
        ranked, team = self.page.split("## Maintainers (not ranked)")
        assert "@boss" not in ranked
        assert "@boss" in team

    def test_newcomers_this_week_exclude_maintainers(self) -> None:
        section = self.page.split("## 🎉 First PR merged this week")[1].split("##")[0]
        assert "@ann (#1)" in section
        assert "@boss" not in section

    def test_empty_board(self) -> None:
        page = lb.render([], repo="o/r", now=NOW, maintainer_logins=set())
        assert "_Nobody yet: be the first!_" in page
        assert "_None this week. Yours could be next!_" in page
        assert "Maintainers" not in page


class TestHelpers:
    def test_linked_issues(self) -> None:
        assert lb.linked_issues("Closes #12 and fixes #3; resolved #12. See #99.") == [3, 12]
        assert lb.linked_issues(None) == []

    def test_maintainers_from_prs_and_reviews(self) -> None:
        found = lb.maintainers(
            [pr(1, "boss", None, association="OWNER")],
            [
                review(
                    1,
                    "x",
                    "helper",
                    "Looks good to me.",
                    "2026-10-01T00:00:00Z",
                    association="COLLABORATOR",
                )
            ],
        )
        assert found == {"boss", "helper"}

    def test_main_needs_environment(self) -> None:
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=True):
            assert lb.main([]) == 2


# ------------------------------------------------------------------ README card


class TestSvgCard:
    def test_card_lists_this_months_top_contributors_without_maintainers(self) -> None:
        prs = [
            pr(1, "ann", "2026-10-22T00:00:00Z", "Closes #2"),
            pr(2, "bob", "2026-10-21T00:00:00Z", "Closes #1"),
            pr(3, "boss", "2026-10-20T00:00:00Z", association="OWNER"),
        ]
        svg = lb.render_svg(
            lb.score_prs(prs, ISSUES), now=NOW, maintainer_logins=lb.maintainers(prs, [])
        )
        assert svg.startswith("<svg ")
        assert svg.rstrip().endswith("</svg>")
        assert "October 2026" in svg
        assert svg.index("@ann") < svg.index("@bob")
        assert "35 pts" in svg
        assert "@boss" not in svg

    def test_card_falls_back_to_all_time_then_to_an_invitation(self) -> None:
        old = lb.score_prs([pr(1, "ann", "2026-09-01T00:00:00Z")], ISSUES)
        assert "all time" in lb.render_svg(old, now=NOW, maintainer_logins=set())
        assert "be the first" in lb.render_svg([], now=NOW, maintainer_logins=set())

    def test_card_escapes_xml_and_caps_rows(self) -> None:
        when = lb.parse_time("2026-10-10T00:00:00Z")
        many = [lb.Contribution(f"user{i}", "bug", 5 + i, when, i) for i in range(15)]
        many.append(lb.Contribution("a<b&c", "bug", 100, when, 99))
        svg = lb.render_svg(many, now=NOW, maintainer_logins=set())
        assert "a&lt;b&amp;c" in svg
        assert "<b&c" not in svg
        assert svg.count(" pts</text>") == 10


# ---------------------------------------------------------------- celebration


class TestCelebration:
    BOARD = "https://github.com/o/r/issues/1"

    def contributions(self) -> list[lb.Contribution]:
        prs = [
            pr(1, "ann", "2026-10-01T00:00:00Z", "Closes #1"),
            pr(2, "ann", "2026-10-02T00:00:00Z"),
            pr(3, "ann", "2026-10-22T00:00:00Z", "Closes #2", labels=("points: +20",)),
            pr(4, "boss", "2026-10-22T00:00:00Z", association="OWNER"),
        ]
        reviews = [
            review(3, "ann", "rev", "Ran it locally, works.", "2026-10-21T00:00:00Z"),
            review(
                3, "ann", "boss", "Looks good to me.", "2026-10-21T00:00:00Z", association="OWNER"
            ),
        ]
        return [*lb.score_prs(prs, ISSUES), *lb.score_reviews(reviews)]

    def message(self, number: int, author: str) -> str | None:
        return lb.celebration(
            self.contributions(),
            pr_number=number,
            author=author,
            now=NOW,
            maintainer_logins={"boss"},
            board_url=self.BOARD,
        )

    def test_points_breakdown_rank_level_up_and_next_level(self) -> None:
        msg = self.message(3, "ann")
        assert msg is not None
        assert "@ann" in msg
        assert "+50 points" in msg  # help wanted 30 + maintainer bonus 20
        assert "help wanted issue +30" in msg
        assert "maintainer bonus +20" in msg
        assert "**#1** this month" in msg
        assert "Level up: 🔧 Contributor" in msg  # third merged PR
        assert "⭐ Regular is 2 more merged PRs away" in msg
        assert "@rev" in msg  # non-maintainer reviewer thanked
        assert "@boss" not in msg
        assert self.BOARD in msg
        assert msg.rstrip().endswith(lb.CELEBRATE_MARKER)

    def test_first_pr_mentions_bonus_and_no_level_up_wording_twice(self) -> None:
        msg = self.message(1, "ann")
        assert msg is not None
        assert "first merged PR +5" in msg
        assert msg.count("Level up") == 1  # nobody → 🌱 Newcomer

    def test_no_level_up_when_level_is_unchanged(self) -> None:
        msg = self.message(2, "ann")  # second PR: still a Newcomer
        assert msg is not None
        assert "Level up" not in msg
        assert "🔧 Contributor is 1 more merged PR away" in msg

    def test_maintainer_prs_get_no_message_unless_someone_reviewed(self) -> None:
        assert self.message(4, "boss") is None


# ------------------------------------------------------------------ GitHub I/O


class FakeGitHub(lb.GitHub):
    """Records requests instead of calling the API."""

    def __init__(self, *, branch_exists: bool, comments: list[str] | None = None) -> None:
        super().__init__("o/r", "token")
        self.calls: list[tuple[str, str, Any]] = []
        self.branch_exists = branch_exists
        self.comments = comments or []

    def _request(self, method: str, url: str, data: Any = None) -> tuple[Any, str]:
        self.calls.append((method, url, data))
        if url == "git/trees":
            return {"sha": "tree1"}, ""
        if url == "git/commits":
            return {"sha": "commit1"}, ""
        return {}, ""

    def _exists(self, path: str) -> Any:
        self.calls.append(("GET", path, None))
        return {"object": {}} if self.branch_exists else None

    def paginate(self, path: str) -> list[Any]:
        return [{"body": body} for body in self.comments]


class TestPublishing:
    def test_svg_creates_the_branch_the_first_time(self) -> None:
        gh = FakeGitHub(branch_exists=False)
        gh.publish_svg("<svg/>")
        posts = [(m, u) for m, u, _ in gh.calls if m != "GET"]
        assert posts == [("POST", "git/trees"), ("POST", "git/commits"), ("POST", "git/refs")]
        tree = gh.calls[0][2]["tree"][0]
        assert (tree["path"], tree["content"]) == ("leaderboard.svg", "<svg/>")
        assert gh.calls[1][2]["parents"] == []
        assert gh.calls[-1][2] == {"ref": "refs/heads/leaderboard", "sha": "commit1"}

    def test_svg_force_moves_an_existing_branch(self) -> None:
        gh = FakeGitHub(branch_exists=True)
        gh.publish_svg("<svg/>")
        assert gh.calls[-1] == (
            "PATCH",
            "git/refs/heads/leaderboard",
            {"sha": "commit1", "force": True},
        )

    def test_comment_once(self) -> None:
        gh = FakeGitHub(branch_exists=True)
        assert gh.comment_once(5, "hi " + lb.CELEBRATE_MARKER, lb.CELEBRATE_MARKER)
        assert gh.calls[-1][:2] == ("POST", "issues/5/comments")
        again = FakeGitHub(branch_exists=True, comments=["old " + lb.CELEBRATE_MARKER])
        assert not again.comment_once(5, "hi", lb.CELEBRATE_MARKER)
        assert again.calls == []
