"""Value-level snapshots for the existing self-contained dashboard views.

The parser is deliberately test-only.  P0 uses it once at the legacy HTML boundary;
P1 can compare its JSON-serializable view models with the committed goldens directly.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from metsuke import ingest
from metsuke.redaction import REDACTION_VERSION


_SPACE = re.compile(r"[ \t\r\f\v]+")
_GENERATED = re.compile(r"generated=\S+")


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list[_Node | str] = field(default_factory=list)


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in {"br", "meta", "link", "img", "input", "hr"}:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if self.stack[-1].tag not in {"script", "style"}:
            self.stack[-1].children.append(data)


def _nodes(node: _Node, tag: str | None = None):
    for child in node.children:
        if isinstance(child, _Node):
            if tag is None or child.tag == tag:
                yield child
            yield from _nodes(child, tag)


def _classes(node: _Node) -> set[str]:
    return set(node.attrs.get("class", "").split())


def _text(node: _Node) -> str:
    parts: list[str] = []

    def visit(value: _Node | str) -> None:
        if isinstance(value, str):
            parts.append(value)
            return
        if value.tag == "br":
            parts.append("\n")
            return
        for child in value.children:
            visit(child)

    visit(node)
    lines = [_SPACE.sub(" ", line).strip() for line in "".join(parts).splitlines()]
    return "\n".join(line for line in lines if line)


def _first(root: _Node, predicate) -> _Node:
    return next(node for node in _nodes(root) if predicate(node))


def extract_dashboard_values(html: str, *, view: str, timezone: str) -> dict:
    """Extract every user-meaningful value while ignoring layout and CSS classes."""
    parser = _TreeParser()
    parser.feed(html)
    root = parser.root
    header = _first(root, lambda node: node.tag == "header")
    main = _first(root, lambda node: node.tag == "main")
    footer = _first(root, lambda node: node.tag == "footer")
    period = _first(header, lambda node: "dim" in _classes(node))
    total = _first(header, lambda node: "total" in _classes(node))

    tables = []
    for table in _nodes(main, "table"):
        headers = [_text(node) for node in _nodes(table, "th")]
        rows = []
        for row in _nodes(table, "tr"):
            cells = []
            for cell in (node for node in row.children if isinstance(node, _Node) and node.tag == "td"):
                value = {"text": _text(cell)}
                if "title" in cell.attrs:
                    value["title"] = cell.attrs["title"]
                cells.append(value)
            if cells:
                rows.append(cells)
        tables.append({"headers": headers, "rows": rows})

    charts = []
    for svg in _nodes(main, "svg"):
        charts.append(
            {
                "titles": [_text(node) for node in _nodes(svg, "title")],
                "labels": [_text(node) for node in _nodes(svg, "text")],
            }
        )

    titled_values = []
    for node in _nodes(main):
        if "title" in node.attrs and node.tag != "td":
            titled_values.append(
                {"tag": node.tag, "text": _text(node), "title": node.attrs["title"]}
            )

    stamp = _GENERATED.sub("generated=<generated-at>", _text(footer))
    return {
        "schema_version": 1,
        "view": view,
        "timezone": timezone,
        "title": _text(_first(header, lambda node: node.tag == "h1")),
        "period": _text(period),
        "total": _text(total),
        "stamp": stamp,
        "text": [
            _text(node)
            for node in _nodes(main)
            if node.tag in {"h2", "h3", "button", "th", "td"}
            or "insight" in _classes(node)
            or "dim" in _classes(node)
            or "legend" in _classes(node)
        ],
        "tables": tables,
        "charts": charts,
        "titled_values": titled_values,
    }


def _ledger_window(conn: sqlite3.Connection, start: str | None, end: str | None) -> dict:
    request_where = ""
    prompt_where = ""
    params: tuple[str, ...] = ()
    if start is not None and end is not None:
        request_where = (
            " WHERE datetime(r.ts,'unixepoch','localtime')>=?"
            " AND datetime(r.ts,'unixepoch','localtime')<?"
        )
        prompt_where = (
            " WHERE datetime(p.ts,'unixepoch','localtime')>=?"
            " AND datetime(p.ts,'unixepoch','localtime')<?"
        )
        params = (f"{start} 00:00:00", f"{end} 00:00:00")
    row = conn.execute(
        """SELECT COALESCE(SUM(r.cost_usd),0),COUNT(*),
                  COUNT(DISTINCT r.prompt_id),COUNT(DISTINCT r.session_id),
                  COUNT(DISTINCT s.project),SUM(r.cost_usd IS NULL)
           FROM v_request_cost r LEFT JOIN session s USING(session_id)"""
        + request_where,
        params,
    ).fetchone()
    prompt_rows = conn.execute(
        "SELECT COUNT(*) FROM prompt p" + prompt_where,
        params,
    ).fetchone()[0]
    return {
        "from": start,
        "to_exclusive": end,
        "total_usd": row[0],
        "request_count": row[1],
        "cost_bearing_prompt_count": row[2],
        "session_count": row[3],
        "project_count": row[4],
        "unknown_cost_request_count": row[5] or 0,
        "prompt_table_rows": prompt_rows,
    }


def snapshot_ledger_aggregates(path: Path) -> dict:
    """Read an operator ledger without returning any content, names, or IDs."""
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        maximum = conn.execute(
            "SELECT date(MAX(ts),'unixepoch','localtime') FROM v_request_cost"
        ).fetchone()[0]
        if maximum is None:
            raise ValueError("ledger has no cost-bearing requests")
        latest = dt.date.fromisoformat(maximum)
        latest_end = latest + dt.timedelta(days=1)
        seven_start = latest - dt.timedelta(days=6)
        schema = "\n".join(
            row[0] or ""
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
            )
        )
        ledger_parser = conn.execute(
            "SELECT COALESCE(MAX(parser_version),0) FROM request"
        ).fetchone()[0]
        redaction_row = conn.execute(
            "SELECT value FROM meta WHERE key='redaction_version'"
        ).fetchone()
        return {
            "snapshot_schema_version": 1,
            "ledger_schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "ledger_schema_sha256": hashlib.sha256(schema.encode()).hexdigest(),
            "parser_version": ingest.PARSER_VERSION,
            "ledger_parser_version": ledger_parser,
            "redaction_version": REDACTION_VERSION,
            "ledger_redaction_version": int(redaction_row[0]) if redaction_row else 0,
            "measured_at": dt.datetime.now(dt.UTC).isoformat(),
            "timezone": time.tzname[0],
            "windows": {
                "latest_day": _ledger_window(conn, str(latest), str(latest_end)),
                "latest_7_days": _ledger_window(conn, str(seven_start), str(latest_end)),
                "all_observed": _ledger_window(conn, None, None),
            },
        }
    finally:
        conn.close()


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="write an identifier-free dashboard ledger snapshot"
    )
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    snapshot = snapshot_ledger_aggregates(args.ledger)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    _main()
