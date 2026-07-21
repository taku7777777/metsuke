"""Text reports: `metsuke today` and `metsuke explain` (Stage 1 — TUI comes in Stage 2-7)."""

from __future__ import annotations

import datetime as dt
import functools
import json
import sqlite3
import sys
from collections import Counter

from rich.console import Console
from rich.tree import Tree

from . import config, ledger
from .viewmodel import prompt as prompt_viewmodel


LEDGER_UNAVAILABLE_MESSAGE = "ledger unavailable。metsuke sync を先に実行してください"


def _ledger_unavailable_message(exc: sqlite3.DatabaseError) -> str:
    return f"ledger unavailable: {exc}。metsuke sync を先に実行してください"


def _requires_ledger(func):
    @functools.wraps(func)
    def guarded(*args, **kwargs) -> int:
        if not ledger.db_path().exists():
            print(LEDGER_UNAVAILABLE_MESSAGE, file=sys.stderr)
            return 1
        try:
            return func(*args, **kwargs)
        except (sqlite3.IntegrityError, sqlite3.ProgrammingError):
            raise
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
            print(_ledger_unavailable_message(exc), file=sys.stderr)
            return 1

    return guarded


def dominant_term(rows) -> tuple[str, float]:
    dominant = prompt_viewmodel.dominant_term(rows)
    return dominant.term, dominant.share_pct


@_requires_ledger
def today(as_json: bool = False) -> int:
    conn = ledger.connect_readonly()
    day = conn.execute("SELECT date('now','localtime')").fetchone()[0]
    total = conn.execute(
        "SELECT n_requests, cost_usd, cache_read_tok, cache_creation_tok, output_tok "
        "FROM v_daily WHERE day=?",
        (day,),
    ).fetchone()
    by_model = conn.execute(
        "SELECT model, COUNT(*) n, SUM(cost_usd) c FROM v_request_cost "
        "WHERE date(ts,'unixepoch','localtime')=? GROUP BY model ORDER BY c DESC",
        (day,),
    ).fetchall()
    top = conn.execute(
        "SELECT p.prompt_id, v.cost_usd, v.n_requests, substr(COALESCE(p.text,'(no text)'),1,60) t "
        "FROM v_prompt_cost v LEFT JOIN prompt p ON p.prompt_id=v.prompt_id "
        "WHERE date(v.ts,'unixepoch','localtime')=? ORDER BY v.cost_usd DESC LIMIT 5",
        (day,),
    ).fetchall()
    if as_json:
        print(
            json.dumps(
                {
                    "day": day,
                    "total": dict(total) if total else None,
                    "by_model": [dict(r) for r in by_model],
                    "top_prompts": [dict(r) for r in top],
                },
                ensure_ascii=False,
            )
        )
        return 0
    if not total:
        print(f"{day}: no data")
        return 0
    pace = ""
    try:
        cached = json.loads(config.state_json_path().read_text())["today"]
        if cached.get("pace_ratio") is not None:
            pace = f"  pace {cached['pace_ratio']:.2f}x / landing ${cached['landing_usd']:.2f}"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    print(f"{day}  ${total['cost_usd']:.2f}  ({total['n_requests']} requests){pace}")
    for r in by_model:
        print(f"  {r['model'] or '?':24s} {r['n']:5d} req  ${r['c']:.2f}")
    if top:
        print("  top prompts:")
        for r in top:
            print(f"    ${r['cost_usd']:.2f}  {r['n_requests']:3d}req  {r['t']}")
    return 0


@_requires_ledger
def explain(
    prompt_id: str = "last", as_json: bool = False, as_html: bool = False,
    open_html: bool = False,
) -> int:
    conn = ledger.connect_readonly()
    if prompt_id == "last":
        row = conn.execute(
            "SELECT prompt_id FROM v_prompt_cost ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if not row:
            print("no prompts in ledger")
            return 1
        prompt_id = row["prompt_id"]
    else:
        rows = conn.execute(
            """SELECT prompt_id FROM (
                   SELECT prompt_id FROM prompt
                   UNION SELECT prompt_id FROM request WHERE prompt_id IS NOT NULL)
               WHERE prompt_id LIKE ?
               ORDER BY CASE WHEN prompt_id=? THEN 0 ELSE 1 END,prompt_id""",
            (prompt_id + "%", prompt_id),
        ).fetchall()
        exact = [row for row in rows if row["prompt_id"] == prompt_id]
        if exact:
            prompt_id = exact[0]["prompt_id"]
        elif len(rows) == 1:
            prompt_id = rows[0]["prompt_id"]
        else:
            print("prompt prefix is ambiguous" if rows else "no such prompt")
            return 1

    model = prompt_viewmodel.query(conn, prompt_id)
    if model is None:
        print(f"prompt {prompt_id}: no requests")
        return 1
    if as_html:
        from . import trace_html

        path = trace_html.generate(model.session_id)
        if path is None:
            print(f"prompt {prompt_id}: no requests")
            return 1
        fragment = f"#prompt={prompt_id}"
        print(f"{path}{fragment}")
        if open_html and not trace_html.open_browser(path, fragment):
            print("warning: could not open browser", file=sys.stderr)
        return 0
    total = model.amount.raw or 0
    dom_key, dom_share = model.dominant.term, model.dominant.share_pct

    if as_json:
        print(
            json.dumps(
                {
                    "prompt_id": prompt_id,
                    "text": model.text[:200] if model.text else None,
                    "total_usd": total,
                    "n_requests": len(model.requests),
                    "dominant": {"term": dom_key, "share_pct": round(dom_share, 1)},
                    "requests": [
                        {
                            "ts": request.ts,
                            "model": request.model,
                            "agent_id": request.agent_id,
                            "in": request.input_tok,
                            "cache_read": request.cache_read_tok,
                            "cache_w5m": request.cache_w5m_tok,
                            "cache_w1h": request.cache_w1h_tok,
                            "out": request.output_tok,
                            "cost_usd": request.amount.raw,
                            "interrupted": int(request.interrupted),
                            "tools": request.tool_count,
                        }
                        for request in model.requests
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0

    print(f"prompt {prompt_id}  total ${total:.3f}  ({len(model.requests)} requests)")
    if model.text:
        print(f"  「{model.text[:80]}...」" if len(model.text) > 80 else f"  「{model.text}」")
    print(f"  支配項: {dom_key} ({dom_share:.0f}%)")
    main_cost = sum(
        request.amount.raw or 0 for request in model.requests if not request.agent_id
    )
    agent_cost = total - main_cost
    if agent_cost:
        n_agents = len({request.agent_id for request in model.requests if request.agent_id})
        print(f"  内訳: 本体 ${main_cost:.3f} / サブエージェント{n_agents}体 ${agent_cost:.3f}")
    print(f"  {'time':8s} {'model':22s} {'read':>9s} {'create':>8s} {'out':>6s} {'tools':>5s} {'$':>7s}")
    for request in model.requests:
        import datetime as dt

        t = dt.datetime.fromtimestamp(request.ts).strftime("%H:%M:%S") if request.ts else "?"
        tag = f"↳{request.agent_id[:6]}" if request.agent_id else ""
        flag = " ⚡中断" if request.interrupted else ""
        print(
            f"  {t:8s} {(request.model or '?')[:22]:22s} {request.cache_read_tok or 0:9,d} "
            f"{(request.cache_w5m_tok or 0) + (request.cache_w1h_tok or 0):8,d} "
            f"{request.output_tok if request.output_tok is not None else 0:6,d} "
            f"{request.tool_count:5d} {request.amount.raw or 0:7.3f}{tag}{flag}"
        )
    return 0


def _resolve_session(conn, value: str) -> str:
    if value == "last":
        row = conn.execute("SELECT session_id FROM request ORDER BY ts DESC LIMIT 1").fetchone()
        if not row:
            raise ValueError("no sessions in ledger")
        return row[0]
    rows = conn.execute(
        "SELECT DISTINCT session_id FROM request WHERE session_id LIKE ?", (value + "%",)
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("session prefix is ambiguous" if rows else "session not found")
    return rows[0][0]


def _resolve_focus(conn, session_id: str, value: str) -> str | None:
    exact = conn.execute(
        "SELECT request_id FROM v_request_cost WHERE session_id=? AND request_id=?",
        (session_id, value),
    ).fetchone()
    if exact:
        return exact[0]
    count = conn.execute(
        "SELECT COUNT(*) FROM v_request_cost WHERE session_id=? AND request_id LIKE ?",
        (session_id, value + "%"),
    ).fetchone()[0]
    if count == 1:
        return conn.execute(
            "SELECT request_id FROM v_request_cost WHERE session_id=? AND request_id LIKE ?",
            (session_id, value + "%"),
        ).fetchone()[0]
    if not count:
        print(f"warning: focus request not found in session: {value}", file=sys.stderr)
        return None
    rows = conn.execute(
        "SELECT request_id FROM v_request_cost "
        "WHERE session_id=? AND request_id LIKE ? ORDER BY request_id LIMIT 10",
        (session_id, value + "%"),
    ).fetchall()
    candidates = ", ".join(row[0] for row in rows)
    extra = f" …他 {count - 10} 件" if count > 10 else ""
    raise ValueError(
        f"focus request prefix is ambiguous: {candidates}{extra}\n"
        "request_id は先頭16文字あれば台帳全体で一意です"
    )


def _identity_label(item: dict) -> str:
    stamp = dt.datetime.fromtimestamp(item["ts"]).strftime("%H:%M")
    amount = item["cache_write_usd"] or 0
    money = f"${amount:,.2f}"
    return f"⚡{item['cause']} {money} {stamp}"


@_requires_ledger
def trace(
    session: str,
    as_json: bool = False,
    as_html: bool = False,
    open_html: bool = False,
    focus: str | None = None,
) -> int:
    conn = ledger.connect_readonly()
    try:
        sid = _resolve_session(conn, session)
    except ValueError as exc:
        print(str(exc))
        return 1
    try:
        focus_id = _resolve_focus(conn, sid, focus) if focus else None
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if as_html:
        from . import trace_html

        path = trace_html.generate(sid, focus=focus_id)
        if path is None:
            print("no requests in session")
            return 1
        fragment = f"#request={focus_id}" if focus_id else ""
        print(f"{path}{fragment}")
        if open_html and not trace_html.open_browser(path, fragment):
            print("warning: could not open browser", file=sys.stderr)
        return 0
    reqs = conn.execute(
        "SELECT * FROM v_request_cost WHERE session_id=? ORDER BY ts", (sid,)
    ).fetchall()
    prompts = conn.execute("SELECT * FROM prompt WHERE session_id=? ORDER BY ts", (sid,)).fetchall()
    identity = {
        r["request_id"]: dict(r)
        for r in conn.execute(
            """SELECT ci.cause,ci.ts,ci.request_id,r.cache_write_usd
               FROM v_cache_identity ci JOIN v_request_cost r USING(request_id)
               WHERE ci.session_id=?""",
            (sid,),
        )
    }
    agents = {
        r["agent_id"]: dict(r)
        for r in conn.execute("SELECT * FROM agent WHERE session_id=?", (sid,))
    }
    models = Counter(r["model"] or "?" for r in reqs)
    data = {
        "session_id": sid,
        "cost_usd": sum(r["cost_usd"] or 0 for r in reqs),
        "models": dict(models),
        "prompts": [],
    }
    for prompt in prompts:
        group = [r for r in reqs if r["prompt_id"] == prompt["prompt_id"]]
        main = [r for r in group if not r["agent_id"]]
        agent_groups = {}
        for row in group:
            if row["agent_id"]:
                agent_groups.setdefault(row["agent_id"], []).append(row)
        data["prompts"].append(
            {
                "prompt_id": prompt["prompt_id"],
                "ts": prompt["ts"],
                "text": (prompt["text"] or "")[:40],
                "cost_usd": sum(r["cost_usd"] or 0 for r in group),
                "main": {
                    "n_requests": len(main),
                    "tokens": sum(
                        (r["input_tok"] or 0)
                        + (r["cache_read_tok"] or 0)
                        + (r["cache_w5m_tok"] or 0)
                        + (r["cache_w1h_tok"] or 0)
                        + (r["output_tok"] or 0)
                        for r in main
                    ),
                    "identity_breaks": [
                        identity[r["request_id"]] for r in main if r["request_id"] in identity
                    ],
                    "focused": focus_id is not None
                    and any(r["request_id"] == focus_id for r in main),
                },
                "agents": [
                    {
                        "agent_id": aid,
                        "agent_type": agents.get(aid, {}).get("agent_type"),
                        "resolved_model": agents.get(aid, {}).get("resolved_model"),
                        "cost_usd": sum(r["cost_usd"] or 0 for r in rows),
                        "n_requests": len(rows),
                        "identity_breaks": [
                            identity[r["request_id"]] for r in rows if r["request_id"] in identity
                        ],
                        "focused": focus_id is not None
                        and any(r["request_id"] == focus_id for r in rows),
                    }
                    for aid, rows in agent_groups.items()
                ],
            }
        )
    if as_json:
        print(json.dumps(data, ensure_ascii=False))
        return 0
    root = Tree(
        f"session {sid}  ${data['cost_usd']:.2f}  "
        + ", ".join(f"{m}:{n}" for m, n in models.items())
    )
    for item in data["prompts"]:
        stamp = dt.datetime.fromtimestamp(item["ts"]).strftime("%H:%M:%S") if item["ts"] else "?"
        node = root.add(f"{stamp} ${item['cost_usd']:.2f} {item['text']}")
        breaks = " ".join(_identity_label(x) for x in item["main"]["identity_breaks"])
        main_focused = item["main"]["focused"]
        node.add(
            f"{'▶ ' if main_focused else ''}main {item['main']['n_requests']}req {item['main']['tokens']:,}tok {breaks}",
            style="bold yellow" if main_focused else None,
        )
        for agent in item["agents"]:
            breaks = " ".join(_identity_label(x) for x in agent["identity_breaks"])
            node.add(
                f"{'▶ ' if agent['focused'] else ''}agent {agent['agent_type'] or '?'} / {agent['resolved_model'] or '?'} ${agent['cost_usd']:.2f} {agent['n_requests']}req {breaks}",
                style="bold yellow" if agent["focused"] else None,
            )
    Console().print(root)
    return 0


@_requires_ledger
def week() -> int:
    conn = ledger.connect_readonly()
    print("last 7 days")
    for r in conn.execute(
        "SELECT day,n_requests,cost_usd FROM v_daily WHERE day>=date('now','localtime','-6 days') ORDER BY day"
    ):
        print(f"  {r['day']}  ${r['cost_usd']:.2f}  {r['n_requests']} req")
    print("identity breaks")
    for r in conn.execute(
        "SELECT cause,COUNT(*) n FROM v_cache_identity WHERE ts>=strftime('%s','now','-6 days') GROUP BY cause ORDER BY n DESC"
    ):
        print(f"  {r['cause']}: {r['n']}")
    avg = conn.execute(
        "SELECT AVG(startup_context_tok) FROM v_context_overhead WHERE day>=date('now','localtime','-6 days')"
    ).fetchone()[0]
    print(f"startup overhead avg: {avg or 0:.0f} tok")
    print("top prompts")
    for r in conn.execute(
        "SELECT prompt_id,cost_usd,n_requests FROM v_prompt_cost WHERE ts>=strftime('%s','now','-6 days') ORDER BY cost_usd DESC LIMIT 5"
    ):
        print(f"  ${r['cost_usd']:.2f} {r['n_requests']}req {r['prompt_id']}")
    print("models")
    for r in conn.execute(
        "SELECT model,COUNT(*) n,SUM(cost_usd) c FROM v_request_cost WHERE ts>=strftime('%s','now','-6 days') GROUP BY model ORDER BY c DESC"
    ):
        print(f"  {r['model'] or '?'}: ${r['c']:.2f} {r['n']}req")
    return 0
