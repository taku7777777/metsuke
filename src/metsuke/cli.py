"""metsuke — command line entry point."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

VIEW_NAMES = ("dist", "period", "cache", "trend")
TASK_LABELS = ("feature", "incident", "design", "refactor", "chore")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="metsuke", description="Claude Code cost watch")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("archive", help="run one archiver pass (incremental, idempotent)")
    p_sync = sub.add_parser("sync", help="archive + ingest (one shot)")
    p_sync.add_argument("--quiet", action="store_true")
    sub.add_parser("rebuild", help="drop ledger and replay the whole archive")
    p_unlock = sub.add_parser("unlock", help="deprecated no-op (hard stop is disabled)")
    p_unlock.add_argument("minutes", nargs="?", type=int, default=120)
    p_unlock.add_argument("--off", action="store_true")
    p_nudges = sub.add_parser("nudges", help="nudge conversion summary")
    p_nudges.add_argument("--json", action="store_true")
    p_mark = sub.add_parser("mark", help="record an intervention marker")
    mark_sub = p_mark.add_subparsers(dest="mark_cmd", required=True)
    p_mark_start = mark_sub.add_parser("start")
    p_mark_start.add_argument("--category", required=True)
    p_mark_start.add_argument("--hypothesis", required=True)
    p_mark_start.add_argument("--expected")
    p_mark_end = mark_sub.add_parser("end")
    p_mark_end.add_argument("marker_id", nargs="?")
    p_mark_verdict = mark_sub.add_parser("verdict")
    p_mark_verdict.add_argument("marker_id")
    p_mark_verdict.add_argument("verdict", choices=("win", "loss", "inconclusive"))
    p_mark_verdict.add_argument("--note")
    p_mark_verdict.add_argument("--saving-usd", type=float)
    p_mark_verdict.add_argument("--saving-low-usd", type=float)
    p_mark_verdict.add_argument("--saving-high-usd", type=float)
    p_mark_verdict.add_argument("--saving-basis")
    p_done = sub.add_parser("done", help="record a prompt outcome")
    p_done.add_argument("label", choices=("completed", "reverted", "abandoned", "partial"))
    p_done.add_argument("--prompt", default="last")
    p_regime = sub.add_parser("regime", help="record an external regime event")
    regime_sub = p_regime.add_subparsers(dest="regime_cmd", required=True)
    p_regime_add = regime_sub.add_parser("add")
    p_regime_add.add_argument("kind")
    p_regime_add.add_argument("detail")
    p_approve = sub.add_parser("approve", help="approve an analyst proposal")
    p_approve.add_argument("name")
    p_deadman = sub.add_parser("deadman", help="check that last week's report exists")
    p_deadman.add_argument("--now", type=float)
    p_invoice = sub.add_parser("invoice", help="record or reconcile a monthly invoice")
    p_invoice.add_argument("month", nargs="?")
    p_invoice.add_argument("usd", nargs="?", type=float)
    p_invoice.add_argument("--note")
    p_invoice.add_argument("--check", metavar="YYYY-MM")
    p_invoice.add_argument("--json", action="store_true")
    p_roi = sub.add_parser("roi", help="tool return on investment")
    p_roi.add_argument("--json", action="store_true")
    p_roi.add_argument("--days", type=int, help="limit ROI to the latest N days")
    p_roi.add_argument(
        "--add-cost", choices=("maintenance", "review", "interruption", "storage", "other")
    )
    p_roi.add_argument("--minutes", type=float)
    p_roi.add_argument("--usd", type=float)
    p_roi.add_argument("--note")
    p_task = sub.add_parser("task", help="track task outcomes and cost efficiency")
    task_sub = p_task.add_subparsers(dest="task_cmd", required=True)
    p_task_start = task_sub.add_parser("start")
    p_task_start.add_argument("title")
    p_task_start.add_argument(
        "--category", choices=TASK_LABELS,
        required=True,
    )
    p_task_start.add_argument("--goal")
    p_task_start.add_argument("--project")
    p_task_attach = task_sub.add_parser("attach")
    p_task_attach.add_argument("task_id")
    p_task_attach.add_argument("--prompt", default="last")
    p_task_finish = task_sub.add_parser("finish")
    p_task_finish.add_argument("task_id", nargs="?")
    p_task_finish.add_argument(
        "--outcome", choices=("completed", "partial", "abandoned"), required=True
    )
    p_task_finish.add_argument("--quality", type=int, choices=range(1, 6))
    p_task_finish.add_argument("--rework-minutes", type=float)
    p_task_finish.add_argument("--note")
    p_task_status = task_sub.add_parser("status")
    p_task_status.add_argument("--json", action="store_true")
    p_dashboard = sub.add_parser("dashboard", help="maintain the local dashboard server")
    dashboard_sub = p_dashboard.add_subparsers(dest="dashboard_cmd", required=True)
    p_dashboard_serve = dashboard_sub.add_parser("serve")
    p_dashboard_serve.add_argument("--open", action="store_true")
    dashboard_sub.add_parser("status")
    dashboard_sub.add_parser("stop")
    p_config = sub.add_parser("config", help="show the effective central configuration")
    p_config.add_argument("--json", action="store_true")
    p_prices = sub.add_parser("prices", help="show effective bundled prices")
    p_prices.add_argument("--json", action="store_true")
    p_ttl = sub.add_parser("ttl-review", help="evaluate whether TTL intervention is worthwhile")
    p_ttl.add_argument("--days", type=int, default=28)
    p_ttl.add_argument("--json", action="store_true")
    p_today = sub.add_parser("today", help="today's spend")
    p_today.add_argument("--json", action="store_true")
    p_explain = sub.add_parser("explain", help="why did this prompt cost what it cost")
    p_explain.add_argument("prompt_id", nargs="?", default="last")
    p_explain.add_argument("--json", action="store_true")
    p_explain.add_argument("--html", action="store_true")
    p_explain.add_argument("--open", action="store_true")
    p_trace = sub.add_parser("trace", help="show a session lineage tree")
    p_trace.add_argument("session")
    p_trace.add_argument("--json", action="store_true")
    p_trace.add_argument("--html", action="store_true")
    p_trace.add_argument("--open", action="store_true")
    p_trace.add_argument("--focus", metavar="REQUEST_ID")
    p_view = sub.add_parser("view", help="generate a decision-support view")
    p_view.add_argument("name", choices=VIEW_NAMES)
    period = p_view.add_mutually_exclusive_group()
    period.add_argument("--days", type=int)
    period.add_argument("--today", action="store_true")
    period.add_argument("--week", nargs="?", const="current")
    period.add_argument("--month", nargs="?", const="current")
    period.add_argument("--from", dest="date_from")
    p_view.add_argument("--to", dest="date_to")
    p_view.add_argument("--project")
    p_view.add_argument("--as-of")
    p_view.add_argument("--open", action="store_true")
    sub.add_parser("week", help="last seven days summary")
    p_verify = sub.add_parser("verify", help="verify archive integrity against sources")
    p_verify.add_argument("--sample", type=int, default=10, help="number of files to verify")
    p_verify.add_argument("--path", help="verify one specific relative path")
    p_doctor = sub.add_parser("doctor", help="self-diagnosis")
    p_doctor.add_argument("--json", action="store_true")
    sub.add_parser("notify-test", help="send a visible macOS notification test")
    sub.add_parser("backup", help="encrypted offsite backup (restic)")
    sub.add_parser("backup-verify", help="restore one file from backup and check it")
    p_install = sub.add_parser("install", help="install or refresh local integrations")
    p_install.add_argument("--git-root")
    p_install.add_argument("--with-git-hooks", action="store_true")
    p_install.add_argument("--skip-git", action="store_true")
    p_install.add_argument("--skip-launchd", action="store_true")
    p_install.add_argument("--skip-claude-hooks", action="store_true")
    p_install.add_argument("--skip-statusline", action="store_true")
    p_install.add_argument("--skip-otel", action="store_true")
    p_uninstall = sub.add_parser("uninstall", help="remove local integrations (dry-run by default)")
    p_uninstall.add_argument("--apply", action="store_true")
    p_uninstall.add_argument("--purge-data", action="store_true")
    p_uninstall.add_argument("--git-root")

    args = parser.parse_args(argv)

    if args.cmd == "archive":
        from . import archiver

        stats = archiver.run()
        print(
            f"files={stats.files_seen} +segments={stats.segments} "
            f"+bytes={stats.bytes_captured:,} gen_bumps={stats.generations_bumped} "
            f"errors={len(stats.errors)}"
        )
        for e in stats.errors[:5]:
            print(f"  error: {e}", file=sys.stderr)
        return 1 if stats.errors else 0

    if args.cmd == "sync":
        from . import archiver, config, ingest, ledger, state

        with _sync_lock(config.sync_lock_path(), blocking=False) as acquired:
            if not acquired:
                return 0
            try:
                a = archiver.run()
                conn = ledger.connect()
                i = ingest.run(conn)
                config.last_sync_error_path().unlink(missing_ok=True)
                state.write(conn, notify=True)
                conn.close()
            except Exception as exc:
                config.ensure_dirs()
                marker = config.last_sync_error_path()
                tmp = marker.with_name(marker.name + f".tmp-{os.getpid()}")
                tmp.write_text(
                    json.dumps(
                        {"ts": time.time(), "error": f"{type(exc).__name__}: {exc}"},
                        ensure_ascii=False,
                    )
                )
                os.chmod(tmp, config.FILE_MODE)
                os.replace(tmp, marker)
                if not args.quiet:
                    print(f"sync skipped: {exc}", file=sys.stderr)
                return 0
        if not args.quiet:
            print(
                f"archive: +{a.segments} segments | ingest: +{i.records} records "
                f"(quarantined={i.quarantined}, new_models={i.new_models or '-'})"
            )
        return 0

    if args.cmd == "rebuild":
        from . import config, ingest, ledger, state

        with _sync_lock(config.sync_lock_path(), blocking=True):
            s = ingest.rebuild()
            conn = ledger.connect()
            state.write(conn, notify=False)
            conn.close()
        print(f"rebuilt: segments={s.segments} records={s.records} quarantined={s.quarantined}")
        return 0

    if args.cmd == "unlock":
        print("daily budget hard stop is disabled; unlock is a compatibility no-op")
        return 0

    if args.cmd == "nudges":
        return _nudges(as_json=args.json)

    if args.cmd == "mark":
        return _mark(args)

    if args.cmd == "done":
        return _done(args.label, args.prompt)

    if args.cmd == "regime":
        return _regime_add(args.kind, args.detail)

    if args.cmd == "approve":
        return _approve(args.name)

    if args.cmd == "deadman":
        return _deadman(args.now)

    if args.cmd == "invoice":
        return _invoice(args)

    if args.cmd == "roi":
        if args.add_cost:
            if args.days is not None:
                print("roi --add-cost cannot be combined with --days", file=sys.stderr)
                return 2
            return _roi_add(args)
        if args.minutes is not None or args.usd is not None or args.note is not None:
            print("--minutes/--usd/--note require --add-cost", file=sys.stderr)
            return 2
        return _roi(args.json, args.days)

    if args.cmd == "task":
        return _task(args)

    if args.cmd == "dashboard":
        from .dashboard import server as dashboard_server

        if args.dashboard_cmd == "serve":
            try:
                if args.open:
                    import webbrowser

                    def open_dashboard(running_server):
                        nonce = running_server.auth.issue_bootstrap_nonce()
                        webbrowser.open(
                            f"http://{dashboard_server.LOOPBACK_HOST}:"
                            f"{running_server.port}/bootstrap?nonce={nonce}"
                        )

                    dashboard_server.serve(on_started=open_dashboard)
                else:
                    dashboard_server.serve()
            except dashboard_server.DashboardServerError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            return 0
        status = dashboard_server.server_status()
        if args.dashboard_cmd == "status":
            print("running" if status.running else "stale" if status.stale else "stopped")
            return 0 if status.running else 1
        if dashboard_server.stop():
            print("stopping")
            return 0
        print("dashboard is not running", file=sys.stderr)
        return 1

    if args.cmd == "config":
        return _config(args.json)

    if args.cmd == "prices":
        return _prices(args.json)

    if args.cmd == "ttl-review":
        return _ttl_review(args.days, args.json)

    if args.cmd == "today":
        from . import report

        return report.today(as_json=args.json)

    if args.cmd == "explain":
        from . import report

        return report.explain(
            args.prompt_id, as_json=args.json, as_html=args.html, open_html=args.open
        )

    if args.cmd == "trace":
        from . import report

        return report.trace(
            args.session,
            as_json=args.json,
            as_html=args.html,
            open_html=args.open,
            focus=args.focus,
        )

    if args.cmd == "view":
        import sqlite3

        from . import ledger, trace_html
        from . import viewgen
        from .viewgen import window as view_window

        try:
            conn = ledger.connect_readonly()
            try:
                window = view_window.resolve(
                    conn,
                    days=args.days,
                    today=args.today,
                    week=args.week,
                    month=args.month,
                    date_from=args.date_from,
                    date_to=args.date_to,
                    project=args.project,
                    as_of=args.as_of,
                )
                path = viewgen.generate(args.name, window, conn=conn)
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            print(
                f"view generation failed: {exc}。metsuke sync を先に実行してください", file=sys.stderr
            )
            return 1
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"view generation failed: {exc}", file=sys.stderr)
            return 1
        if path is None:
            print("view generation failed", file=sys.stderr)
            return 1
        print(path)
        if args.open and not trace_html.open_browser(path):
            print("warning: could not open browser", file=sys.stderr)
        return 0

    if args.cmd == "week":
        from . import report

        return report.week()

    if args.cmd == "verify":
        from . import archiver

        if args.path:
            rels = [args.path]
        else:
            rels = sorted({e["path"] for e in archiver.manifest_entries()})
            random.shuffle(rels)
            rels = rels[: args.sample]
        bad = 0
        for rel in rels:
            ok = archiver.verify_against_source(rel)
            print(f"{'✅' if ok else '❌'} {rel}")
            bad += 0 if ok else 1
        return 1 if bad else 0

    if args.cmd == "doctor":
        from . import doctor

        return doctor.run(as_json=args.json)

    if args.cmd == "notify-test":
        from . import state

        status = state._notify(
            "metsuke notification test",
            "✅ metsukeのmacOS通知経路が動作しています",
        )
        print(json.dumps(status, ensure_ascii=False))
        if status["macos"] == "accepted":
            print("バナーまたは通知センターに表示されたか確認してください。")
            return 0
        return 1

    if args.cmd == "backup":
        from . import backup

        return backup.run()

    if args.cmd == "backup-verify":
        from . import backup

        return backup.verify_restore()

    if args.cmd in {"install", "uninstall"}:
        script = Path(__file__).resolve().parents[2] / "scripts" / f"{args.cmd}.sh"
        command = ["bash", str(script)]
        if args.git_root:
            command.extend(("--git-root", args.git_root))
        for flag in (
            "with_git_hooks",
            "skip_git",
            "skip_launchd",
            "skip_claude_hooks",
            "skip_statusline",
            "skip_otel",
            "apply",
            "purge_data",
        ):
            if getattr(args, flag, False):
                command.append("--" + flag.replace("_", "-"))
        return subprocess.run(command, check=False).returncode

    return 2


@contextmanager
def _sync_lock(path, blocking: bool):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    os.chmod(path, 0o600)
    try:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle, flags)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        handle.close()


def _nudges(as_json: bool = False) -> int:
    from rich.console import Console
    from rich.table import Table

    from . import ledger

    if not ledger.db_path().exists():
        if as_json:
            print(json.dumps({"summary": [], "recent": []}))
        else:
            print("no nudge data")
        return 0
    conn = ledger.connect_readonly()
    since = time.time() - 14 * 86400
    summary = [dict(r) for r in conn.execute(
        """SELECT rule,COUNT(*) fired,SUM(decided_ts IS NOT NULL) decided,
           SUM(outcome='followed') followed,
           SUM(outcome='not_followed') not_followed,
           SUM(outcome='unknown') unknown,
           AVG(CASE WHEN outcome IN ('followed','not_followed') THEN followed END)
             followed_rate
           FROM nudge WHERE fired_ts>=? GROUP BY rule ORDER BY rule""",
        (since,),
    )]
    recent = [dict(r) for r in conn.execute(
        """SELECT fired_ts,rule,session_id,followed,outcome,outcome_reason
           FROM nudge ORDER BY fired_ts DESC LIMIT 20"""
    )]
    conn.close()
    if as_json:
        print(json.dumps({"summary": summary, "recent": recent}, ensure_ascii=False))
        return 0
    table = Table(title="Nudges — last 14 days")
    for name in ("rule", "fired", "observed", "unknown", "followed"):
        table.add_column(name)
    for row in summary:
        rate = "—" if row["followed_rate"] is None else f"{row['followed_rate']:.0%}"
        observed = row["followed"] + row["not_followed"]
        table.add_row(
            row["rule"], str(row["fired"]), str(observed), str(row["unknown"]), rate
        )
    Console().print(table)
    for row in recent:
        stamp = time.strftime("%m-%d %H:%M", time.localtime(row["fired_ts"]))
        followed = {"followed": "✓", "not_followed": "×", "unknown": "?"}.get(
            row["outcome"], "—"
        )
        print(
            f"{stamp}  {row['rule']:22s} {row['session_id'][:8]:8s} "
            f"{followed} {row['outcome_reason'] or ''}"
        )
    return 0


def _mark(args) -> int:
    from . import judgment

    now = time.time()
    if args.mark_cmd == "start":
        marker_id = f"iv-{time.time_ns():x}"
        judgment.record(
            "marker_start",
            {
                "marker_id": marker_id,
                "ts_start": now,
                "category": args.category,
                "hypothesis": args.hypothesis,
                "expected_effect": args.expected,
            },
            ts=now,
        )
        print(f"{marker_id} recorded; next sync will add it to the ledger")
        return 0
    marker_id = args.marker_id
    if args.mark_cmd == "end" and marker_id is None:
        marker_id = _latest_open_marker()
        if marker_id is None:
            print("no open marker found", file=sys.stderr)
            return 1
    if args.mark_cmd == "end":
        judgment.record("marker_end", {"marker_id": marker_id, "ts_end": now}, ts=now)
        print(f"{marker_id} ended; next sync will update the ledger")
        return 0
    low, point, high = args.saving_low_usd, args.saving_usd, args.saving_high_usd
    if any(value is not None and value < 0 for value in (low, point, high)):
        print("saving estimates must be non-negative", file=sys.stderr)
        return 2
    if low is not None and high is not None and low > high:
        print("saving range requires low <= high", file=sys.stderr)
        return 2
    if point is None:
        if low is not None and high is not None:
            point = (low + high) / 2.0
        elif low is not None:
            point = low
        elif high is not None:
            point = high
    if point is not None and (
        (low is not None and point < low) or (high is not None and point > high)
    ):
        print("saving point estimate must be inside the range", file=sys.stderr)
        return 2
    judgment.record(
        "marker_verdict",
        {
            "marker_id": marker_id,
            "verdict": args.verdict,
            "decided_by": "human",
            "verdict_ts": now,
            "note": args.note,
            "saving_usd": point,
            "saving_low_usd": args.saving_low_usd,
            "saving_high_usd": args.saving_high_usd,
            "saving_basis": args.saving_basis,
        },
        ts=now,
    )
    print(f"{marker_id} verdict recorded; next sync will update the ledger")
    return 0


def _latest_open_marker() -> str | None:
    from . import ledger

    if not ledger.db_path().exists():
        return None
    conn = ledger.connect_readonly()
    row = conn.execute(
        "SELECT marker_id FROM marker WHERE ts_end IS NULL ORDER BY ts_start DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _latest_prompt() -> str | None:
    from . import ledger

    if not ledger.db_path().exists():
        return None
    conn = ledger.connect_readonly()
    row = conn.execute("SELECT prompt_id FROM v_prompt_cost ORDER BY ts DESC LIMIT 1").fetchone()
    conn.close()
    return row[0] if row else None


def _active_task() -> str | None:
    from . import config

    try:
        task_id = config.active_task_path().read_text().strip()
    except OSError:
        return None
    return task_id or None


def _write_active_task(task_id: str | None) -> None:
    from . import config

    path = config.active_task_path()
    if task_id is None:
        path.unlink(missing_ok=True)
        return
    config.ensure_dirs()
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(task_id + "\n")
    os.chmod(tmp, config.FILE_MODE)
    os.replace(tmp, path)


def _task(args) -> int:
    from . import judgment, ledger

    now = time.time()
    if args.task_cmd == "start":
        if _active_task():
            print(f"active task already exists: {_active_task()}", file=sys.stderr)
            return 1
        task_id = f"task-{time.time_ns():x}"
        judgment.record(
            "task_start",
            {
                "task_id": task_id,
                "title": args.title,
                "goal": args.goal,
                "category": args.category,
                "project": args.project,
                "ts_start": now,
                "created_by": "human",
            },
            ts=now,
        )
        _write_active_task(task_id)
        print(f"{task_id} started; prompts will be attached until task finish")
        return 0
    if args.task_cmd == "attach":
        prompt_id = _latest_prompt() if args.prompt == "last" else args.prompt
        if prompt_id is None:
            print("no prompt found", file=sys.stderr)
            return 1
        judgment.record(
            "task_attach",
            {
                "task_id": args.task_id,
                "prompt_id": prompt_id,
                "attached_ts": now,
                "source": "manual",
                "confidence": 1.0,
            },
            ts=now,
        )
        print(f"{prompt_id} attached to {args.task_id}; next sync will update the ledger")
        return 0
    if args.task_cmd == "finish":
        task_id = args.task_id or _active_task()
        if not task_id:
            print("no active task", file=sys.stderr)
            return 1
        if args.rework_minutes is not None and args.rework_minutes < 0:
            print("rework minutes must be non-negative", file=sys.stderr)
            return 2
        judgment.record(
            "task_finish",
            {
                "task_id": task_id,
                "ts_end": now,
                "outcome": args.outcome,
                "quality_score": args.quality,
                "rework_minutes": args.rework_minutes,
                "note": args.note,
            },
            ts=now,
        )
        if _active_task() == task_id:
            _write_active_task(None)
        print(f"{task_id} finished; next sync will update the ledger")
        return 0
    if not ledger.db_path().exists():
        data = []
    else:
        conn = ledger.connect_readonly()
        data = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM v_task_efficiency ORDER BY ts_start DESC LIMIT 50"
            )
        ]
        conn.close()
    if args.json:
        print(json.dumps({"active_task": _active_task(), "tasks": data}, ensure_ascii=False))
    else:
        print(f"active: {_active_task() or 'none'}")
        for row in data:
            print(
                f"  {row['task_id']} {row['status']:8s} ${row['cost_usd'] or 0:.2f} "
                f"{row['title']}"
            )
    return 0


def _done(label: str, prompt_id: str) -> int:
    from . import judgment

    if prompt_id == "last":
        prompt_id = _latest_prompt()
        if prompt_id is None:
            print("no prompt found", file=sys.stderr)
            return 1
    now = time.time()
    judgment.record(
        "outcome",
        {
            "prompt_id": prompt_id,
            "ts": now,
            "label": label,
            "lines_added": None,
            "lines_removed": None,
            "commits": None,
            "source": "manual",
        },
        ts=now,
    )
    print(f"outcome recorded for {prompt_id}; next sync will add it to the ledger")
    return 0


def _regime_add(kind: str, detail: str) -> int:
    from . import judgment

    now = time.time()
    judgment.record(
        "regime", {"ts": now, "regime_kind": kind, "detail": detail}, ts=now
    )
    print("regime event recorded; next sync will add it to the ledger")
    return 0


def _proposal_items(proposal: dict) -> list[tuple[str, dict]]:
    if not isinstance(proposal, dict) or not isinstance(proposal.get("rationale"), str):
        raise ValueError("proposal requires a rationale")
    kind = proposal.get("kind")
    items = proposal.get("items")
    if not isinstance(items, list):
        raise ValueError("proposal items must be a list")
    result = []
    if kind == "task_label":
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("prompt_id"), str) or not isinstance(item.get("label"), str):
                raise ValueError("invalid task_label item")
            if item["label"] not in TASK_LABELS:
                raise ValueError("invalid task_label taxonomy")
            result.append(
                (
                    "task_label",
                    {
                        "prompt_id": item["prompt_id"],
                        "label": item["label"],
                        "decided_by": "ai+human",
                    },
                )
            )
    elif kind == "marker_verdict":
        for item in items:
            valid = (
                isinstance(item, dict)
                and isinstance(item.get("marker_id"), str)
                and item.get("verdict") in {"win", "loss", "inconclusive"}
                and isinstance(item.get("note"), str)
            )
            if not valid:
                raise ValueError("invalid marker_verdict item")
            result.append(
                (
                    "marker_verdict",
                    {
                        "marker_id": item["marker_id"],
                        "verdict": item["verdict"],
                        "note": item["note"],
                        "decided_by": "ai+human",
                    },
                )
            )
    else:
        raise ValueError(f"unknown proposal kind: {kind}")
    return result


def _approve(name: str) -> int:
    from . import config, judgment

    proposal_name = name if name.endswith(".json") else f"{name}.json"
    path = config.proposals_dir() / proposal_name
    if path.parent != config.proposals_dir() or not path.is_file():
        print(f"proposal not found: {path}", file=sys.stderr)
        return 1
    try:
        raw = path.read_text()
    except OSError as exc:
        print(f"invalid proposal: {exc}", file=sys.stderr)
        return 1
    print(raw, end="" if raw.endswith("\n") else "\n")
    if not sys.stdin.isatty():
        print("TTY required", file=sys.stderr)
        return 1
    if input("apply? [y/N] ").strip().lower() != "y":
        print("cancelled")
        return 1
    try:
        proposal = json.loads(raw)
        items = _proposal_items(proposal)
    except (TypeError, ValueError) as exc:
        print(f"invalid proposal: {exc}", file=sys.stderr)
        return 1
    now = time.time()
    for offset, (kind, payload) in enumerate(items):
        event_ts = now + offset / 1_000_000
        if kind == "marker_verdict":
            payload["verdict_ts"] = event_ts
        judgment.record(kind, payload, ts=event_ts)
    applied = path.with_name(f"applied-{path.name}")
    path.replace(applied)
    print(f"applied {len(items)} items; next sync will update the ledger")
    return 0


def _deadman(now: float | None) -> int:
    import datetime as dt

    from . import config, state

    current = dt.datetime.fromtimestamp(time.time() if now is None else now).date()
    previous = current - dt.timedelta(days=7)
    iso = previous.isocalendar()
    report_id = f"{iso.year}-W{iso.week:02d}"
    path = config.reports_dir() / f"{report_id}.md"
    if path.is_file():
        print(f"ok: {path}")
        return 0
    state._notify(
        "metsuke deadman",
        f"先週レポート {report_id} が見つかりません — 週次アナリストが止まっています",
    )
    return 1


def _valid_month(value: str) -> bool:
    import datetime as dt

    try:
        dt.datetime.strptime(value, "%Y-%m")
        return len(value) == 7
    except ValueError:
        return False


def _invoice(args) -> int:
    from . import judgment

    if args.check:
        if args.month is not None or args.usd is not None:
            print("invoice --check does not accept month/usd positionals", file=sys.stderr)
            return 2
        return _invoice_check(args.check, args.json)
    if args.month is None or args.usd is None:
        print("usage: metsuke invoice <YYYY-MM> <usd> [--note]", file=sys.stderr)
        return 2
    if not _valid_month(args.month):
        print("month must be YYYY-MM", file=sys.stderr)
        return 2
    now = time.time()
    judgment.record(
        "invoice",
        {"month": args.month, "billed_usd": args.usd, "note": args.note, "ts": now},
        ts=now,
    )
    data = {"month": args.month, "billed_usd": args.usd, "pending_sync": True}
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(f"invoice {args.month} ${args.usd:.2f} recorded; next sync will update the ledger")
    return 0


def _invoice_check(month: str, as_json: bool) -> int:
    from . import ledger

    if not _valid_month(month):
        print("month must be YYYY-MM", file=sys.stderr)
        return 2
    if not ledger.db_path().exists():
        print("ledger not found", file=sys.stderr)
        return 1
    conn = ledger.connect_readonly()
    invoice = conn.execute("SELECT * FROM invoice WHERE month=?", (month,)).fetchone()
    if invoice is None:
        conn.close()
        if as_json:
            print(json.dumps({"month": month, "registered": False}))
        else:
            print(f"invoice {month}: billed amount not registered")
        return 1
    ledger_total = conn.execute(
        """SELECT COALESCE(SUM(cost_usd),0) FROM v_request_cost
           WHERE strftime('%Y-%m',ts,'unixepoch')=?""",
        (month,),
    ).fetchone()[0]
    unaccounted = conn.execute(
        "SELECT COALESCE(SUM(input_side_lower_usd),0) FROM v_unaccounted WHERE month_utc=?",
        (month,),
    ).fetchone()[0]
    billed = invoice["billed_usd"]
    residual = billed - (ledger_total + unaccounted)
    residual_pct = 100.0 * residual / billed if billed else None
    calibratable = residual_pct is not None and abs(residual_pct) <= 5
    models = []
    if calibratable and ledger_total:
        rows = conn.execute(
            """SELECT COALESCE(model,'?') model,SUM(cost_usd) cost_usd
               FROM v_request_cost WHERE strftime('%Y-%m',ts,'unixepoch')=?
               GROUP BY model ORDER BY cost_usd DESC""",
            (month,),
        )
        models = [
            {
                "model": row["model"],
                "cost_share_pct": 100.0 * row["cost_usd"] / ledger_total,
                "allocated_residual_usd": residual * row["cost_usd"] / ledger_total,
            }
            for row in rows
        ]
    conn.close()
    data = {
        "month": month,
        "registered": True,
        "billed_usd": billed,
        "ledger_usd": ledger_total,
        "unaccounted_usd": unaccounted,
        "residual_usd": residual,
        "residual_pct": residual_pct,
        "price_calibration_candidate": calibratable,
        "model_allocation": models,
    }
    if as_json:
        print(json.dumps(data, ensure_ascii=False))
        return 0
    pct = "n/a" if residual_pct is None else f"{residual_pct:+.2f}%"
    print(f"{month}: billed ${billed:.2f}")
    print(f"  ledger ${ledger_total:.2f} + unaccounted lower ${unaccounted:.2f}")
    print(f"  residual ${residual:+.2f} ({pct})")
    if calibratable:
        print("  price calibration candidate (display only):")
        for row in models:
            print(
                f"    {row['model']}: {row['cost_share_pct']:.1f}% "
                f"→ residual {row['allocated_residual_usd']:+.2f}"
            )
    else:
        print("  unexplained residual — price calibration prohibited; investigate causes")
    return 0


def _roi(as_json: bool, days: int | None = None) -> int:
    from . import config, ledger

    if days is not None and days < 1:
        print("--days must be positive", file=sys.stderr)
        return 2
    since = time.time() - days * 86400 if days is not None else 0.0
    data = {
        "window_days": days,
        "since_ts": since if days is not None else None,
        "saving_usd": 0.0,
        "saving_low_usd": 0.0,
        "saving_high_usd": 0.0,
        "winning_markers": 0,
        "analyst_cost_usd": 0.0,
        "recorded_cost_usd": 0.0,
        "recorded_minutes": 0.0,
        "hourly_value_usd": config.float_value(
            "METSUKE_HOURLY_VALUE_USD", config.HOURLY_VALUE_USD
        ),
        "time_cost_usd": 0.0,
        "total_known_cost_usd": 0.0,
        "cost_complete": True,
        "roi_ratio": None,
        "roi_low": None,
        "roi_high": None,
        "cost_breakdown": [],
    }
    if ledger.db_path().exists():
        conn = ledger.connect_readonly()
        marker = conn.execute(
            """SELECT COALESCE(SUM(COALESCE(saving_usd,0)),0) saving,
                      COALESCE(SUM(COALESCE(saving_low_usd,saving_usd,0)),0) saving_low,
                      COALESCE(SUM(COALESCE(saving_high_usd,saving_usd,0)),0) saving_high,
                      COUNT(*) n
               FROM marker WHERE verdict='win' AND COALESCE(verdict_ts,ts_start)>=?""",
            (since,),
        ).fetchone()
        analyst = conn.execute(
            """SELECT COALESCE(SUM(v.cost_usd),0) FROM v_request_cost v
               JOIN session s ON s.session_id=v.session_id
               WHERE s.project LIKE '%metsuke-analyst%' AND v.ts>=?""",
            (since,),
        ).fetchone()[0]
        recorded = conn.execute(
            """SELECT kind,COALESCE(SUM(minutes),0) minutes,COALESCE(SUM(usd),0) usd
               FROM roi_cost WHERE ts>=? GROUP BY kind ORDER BY kind""",
            (since,),
        ).fetchall()
        conn.close()
        minutes = sum(row["minutes"] for row in recorded)
        direct = sum(row["usd"] for row in recorded)
        hourly = data["hourly_value_usd"]
        time_cost = minutes * hourly / 60.0
        total_cost = analyst + direct + time_cost
        data.update(
            saving_usd=marker["saving"],
            saving_low_usd=marker["saving_low"],
            saving_high_usd=marker["saving_high"],
            winning_markers=marker["n"],
            analyst_cost_usd=analyst,
            recorded_cost_usd=direct,
            recorded_minutes=minutes,
            time_cost_usd=time_cost,
            total_known_cost_usd=total_cost,
            cost_complete=not (minutes > 0 and hourly <= 0),
            roi_ratio=marker["saving"] / total_cost if total_cost else None,
            roi_low=marker["saving_low"] / total_cost if total_cost else None,
            roi_high=marker["saving_high"] / total_cost if total_cost else None,
            cost_breakdown=[dict(row) for row in recorded],
        )
    if as_json:
        print(json.dumps(data, ensure_ascii=False))
        return 0
    ratio = "n/a" if data["roi_ratio"] is None else f"{data['roi_ratio']:.2f}x"
    print(
        f"ROI: savings ${data['saving_usd']:.2f} "
        f"[${data['saving_low_usd']:.2f}, ${data['saving_high_usd']:.2f}] / "
        f"known costs ${data['total_known_cost_usd']:.2f} = {ratio} "
        f"({data['winning_markers']} winning markers)"
    )
    print(
        f"  analyst ${data['analyst_cost_usd']:.2f}; recorded ${data['recorded_cost_usd']:.2f}; "
        f"human {data['recorded_minutes']:.0f}min × ${data['hourly_value_usd']:.2f}/h"
    )
    if not data["cost_complete"]:
        print("  ⚠ human time is recorded but METSUKE_HOURLY_VALUE_USD is not configured")
    return 0


def _roi_add(args) -> int:
    from . import judgment

    if args.minutes is None and args.usd is None:
        print("roi --add-cost requires --minutes or --usd", file=sys.stderr)
        return 2
    if (args.minutes is not None and args.minutes < 0) or (
        args.usd is not None and args.usd < 0
    ):
        print("ROI costs must be non-negative", file=sys.stderr)
        return 2
    now = time.time()
    cost_id = f"roi-{time.time_ns():x}"
    judgment.record(
        "roi_cost",
        {
            "cost_id": cost_id,
            "ts": now,
            "cost_kind": args.add_cost,
            "minutes": args.minutes,
            "usd": args.usd,
            "note": args.note,
            "source": "human",
        },
        ts=now,
    )
    print(f"{cost_id} recorded; next sync will update ROI")
    return 0


def _config(as_json: bool) -> int:
    from . import config

    try:
        file_values = config.file_settings()
    except ValueError as exc:
        print(f"invalid config: {exc}", file=sys.stderr)
        return 1
    effective = {
        key: config.value(key)
        for key in config.CONFIG_KEYS
        if config.value(key) is not None
    }
    data = {
        "path": str(config.config_path()),
        "exists": config.config_path().is_file(),
        "file_keys": sorted(file_values),
        "effective": effective,
    }
    if as_json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(f"config: {data['path']} ({'present' if data['exists'] else 'missing'})")
        for key, value in effective.items():
            print(f"  {key}={value}")
    return 0


def _prices(as_json: bool) -> int:
    import datetime as dt
    import sqlite3
    from importlib import resources

    from . import ledger

    today = dt.datetime.now(dt.UTC).date().isoformat()
    if ledger.db_path().exists():
        conn = None
        try:
            conn = ledger.connect_readonly()
            models = [
                dict(row)
                for row in conn.execute(
                    """SELECT model,valid_from,valid_to,in_usd,out_usd,fast_x,source_url
                       FROM price WHERE valid_from<=? AND (valid_to IS NULL OR ?<valid_to)
                       ORDER BY model""",
                    (today, today),
                )
            ]
            server_tools = [
                dict(row)
                for row in conn.execute(
                    """SELECT tool,valid_from,valid_to,usd_per_unit,source_url
                       FROM price_server_tool
                       WHERE valid_from<=? AND (valid_to IS NULL OR ?<valid_to)
                       ORDER BY tool""",
                    (today, today),
                )
            ]
            version = conn.execute(
                "SELECT value FROM meta WHERE key='bundled_price_version'"
            ).fetchone()
            overlap = conn.execute(
                "SELECT * FROM v_health WHERE check_name='price_range_overlap'"
            ).fetchone()
        except sqlite3.Error as exc:
            message = f"ledger unavailable: {exc}"
            if as_json:
                print(json.dumps({"error": message}, ensure_ascii=False))
            else:
                print(message, file=sys.stderr)
            return 1
        finally:
            if conn is not None:
                conn.close()
        version_value = version[0] if version else None
        overlap_value = dict(overlap) if overlap else None
        source = "ledger"
    else:
        bundled = json.loads(resources.files("metsuke").joinpath("prices.json").read_text())
        ledger._validate_price_ranges(bundled["models"], "model")
        ledger._validate_price_ranges(bundled.get("server_tools", []), "tool")
        default_fast = bundled["defaults"]["fast_x"]
        source_url = bundled.get("source_url")
        models = [
            {
                "model": row["model"],
                "valid_from": row["valid_from"],
                "valid_to": row.get("valid_to"),
                "in_usd": row["in_usd"],
                "out_usd": row["out_usd"],
                "fast_x": row.get("fast_x", default_fast),
                "source_url": row.get("source_url", source_url),
            }
            for row in bundled["models"]
            if row["valid_from"] <= today
            and (row.get("valid_to") is None or today < row["valid_to"])
        ]
        server_tools = [
            {
                "tool": row["tool"],
                "valid_from": row["valid_from"],
                "valid_to": row.get("valid_to"),
                "usd_per_unit": row["usd_per_unit"],
                "source_url": row.get("source_url", source_url),
            }
            for row in bundled.get("server_tools", [])
            if row["valid_from"] <= today
            and (row.get("valid_to") is None or today < row["valid_to"])
        ]
        models.sort(key=lambda row: row["model"])
        server_tools.sort(key=lambda row: row["tool"])
        version_value = str(bundled.get("version", "unknown"))
        overlap_value = {
            "check_name": "price_range_overlap",
            "status": "ok",
            "value": "0",
            "detail": "bundled file validated; ledger not initialized",
        }
        source = "bundled_file"
        checked_at = bundled.get("checked_at")
    if source == "ledger":
        bundled = json.loads(resources.files("metsuke").joinpath("prices.json").read_text())
        checked_at = bundled.get("checked_at")
    data = {
        "as_of_utc": today,
        "source": source,
        "bundled_version": version_value,
        "bundled_checked_at": checked_at,
        "models": models,
        "server_tools": server_tools,
        "range_status": overlap_value,
    }
    if as_json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        status = overlap_value["status"] if overlap_value else "unknown"
        print(
            f"prices {data['bundled_version']} as of {today} UTC "
            f"(checked {checked_at or 'unknown'}, {status}, {source})"
        )
        for row in models:
            print(
                f"  {row['model']}: ${row['in_usd']:g}/${row['out_usd']:g} MTok "
                f"fast×{row['fast_x']:g}"
            )
        for row in server_tools:
            print(f"  tool {row['tool']}: ${row['usd_per_unit']:g}/unit")
    return 0 if overlap_value is None or overlap_value["status"] == "ok" else 1


def _ttl_review(days: int, as_json: bool) -> int:
    from . import ledger

    if days < 1:
        print("--days must be positive", file=sys.stderr)
        return 2
    if not ledger.db_path().exists():
        data = {
            "window_days": days,
            "active_days": 0,
            "evidence_span_days": 0,
            "decision": "insufficient_data",
        }
    else:
        conn = ledger.connect_readonly()
        start = time.time() - days * 86400
        evidence = conn.execute(
            """SELECT COUNT(DISTINCT date(ts,'unixepoch','localtime')) active_days,
                      COALESCE(julianday(MAX(date(ts,'unixepoch','localtime')))-
                               julianday(MIN(date(ts,'unixepoch','localtime')))+1,0) span_days,
                      COALESCE(SUM(cache_write_usd),0) all_cache_write_usd
               FROM v_request_cost WHERE ts>=?""",
            (start,),
        ).fetchone()
        expiry = conn.execute(
            """SELECT COUNT(*) n,COALESCE(SUM(r.cache_write_usd),0) rebuild_usd
               FROM v_cache_identity c JOIN v_request_cost r ON r.request_id=c.request_id
               WHERE c.ts>=? AND c.cause='ttl_expiry'""",
            (start,),
        ).fetchone()
        conn.close()
        span = int(evidence["span_days"] or 0)
        active = int(evidence["active_days"] or 0)
        rebuild = float(expiry["rebuild_usd"] or 0)
        total_write = float(evidence["all_cache_write_usd"] or 0)
        share = 100.0 * rebuild / total_write if total_write else None
        daily = rebuild / span if span else 0.0
        if span < min(days, 28) or active < 10:
            decision = "insufficient_data"
        elif daily < 5.0 or (share is not None and share < 10.0):
            decision = "deprioritize"
        else:
            decision = "continue_experiment"
        data = {
            "window_days": days,
            "active_days": active,
            "evidence_span_days": span,
            "ttl_expiry_events": expiry["n"],
            "ttl_expiry_rebuild_usd": rebuild,
            "all_cache_write_usd": total_write,
            "ttl_share_pct": share,
            "avoidable_usd_per_calendar_day": daily,
            "decision": decision,
            "thresholds": {
                "minimum_span_days": min(days, 28),
                "minimum_active_days": 10,
                "continue_daily_usd": 5.0,
                "continue_share_pct": 10.0,
            },
            "caveat": "ttl_expiry is intrinsic-evidence classification, not a causal guarantee",
        }
    if as_json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(
            f"TTL review: {data['decision']} — span {data['evidence_span_days']}d / "
            f"active {data['active_days']}d"
        )
        if "ttl_expiry_rebuild_usd" in data:
            share = data["ttl_share_pct"]
            share_text = "n/a" if share is None else f"{share:.1f}%"
            print(
                f"  expiry rebuild ${data['ttl_expiry_rebuild_usd']:.2f}, "
                f"{share_text} of cache writes, "
                f"${data['avoidable_usd_per_calendar_day']:.2f}/calendar day"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
