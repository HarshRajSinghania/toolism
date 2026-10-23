from __future__ import annotations

import json
from typing import Any

import pytest

from toolism.formats import parse
from toolism.measurement import TokenReport, ToolCount, measure
from toolism.pipeline import Rollback, slim
from toolism.report import (
    REPORT_VERSION,
    render_slim_report,
    render_token_report,
    slim_report_to_dict,
    token_report_to_dict,
)
from toolism.tokenizers import HeuristicTokenizer
from toolism.verify import Verdict, VerificationResult

H = HeuristicTokenizer()

TOOLS = parse(
    {
        "tools": [
            {
                "name": "search",
                "description": "Search.",
                "inputSchema": {
                    "type": "object",
                    "title": "searchArguments",
                    "properties": {"q": {"type": "string", "title": "Q"}},
                },
            },
            {"name": "ping", "inputSchema": {"type": "object"}},
        ]
    }
)


def test_measure_counts_function_tools() -> None:
    ts = parse(
        [
            {"name": "a", "input_schema": {"type": "object"}},
            {"type": "web_search_20250305", "name": "web_search"},
        ]
    )
    report = measure(ts, H)
    assert report.tokenizer == "heuristic"
    assert report.approximate
    assert report.tool_count == 1
    assert report.total == H.count_tool(ts.tools[0])
    assert report.tools == (ToolCount("a", report.total),)
    assert report.uncounted == ("web_search",)
    assert report.average == report.total


def test_empty_measure() -> None:
    report = measure(parse([]), H)
    assert (report.total, report.tool_count, report.average) == (0, 0, 0.0)
    rendered = render_token_report(report)
    assert "0 tokens across 0 tools" in rendered
    assert "No tools found in this input." in rendered
    assert "Tool definition tokens" not in rendered  # no empty table
    md = render_token_report(report, "markdown")
    assert "No tools found in this input." in md
    assert "| Tool |" not in md
    # JSON unchanged
    data = token_report_to_dict(report)
    assert data["tool_count"] == 0
    assert json.loads(render_token_report(report, "json")) == data


def test_token_report_dict_and_json() -> None:
    report = measure(TOOLS, H)
    data = token_report_to_dict(report)
    assert data["toolism_report"] == REPORT_VERSION
    assert data["kind"] == "measure"
    assert data["total_tokens"] == report.total
    assert [t["name"] for t in data["tools"]] == ["search", "ping"]
    assert all(isinstance(t["tokens"], int) for t in data["tools"])
    assert round(sum(t["percent"] for t in data["tools"])) == 100
    assert json.loads(render_token_report(report, "json")) == data
