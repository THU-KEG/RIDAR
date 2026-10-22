from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Everything after the first `--` is a command for a subcommand to run (ridar runtime),
    # kept out of argparse so its own options are never parsed as ours.
    trailing: list[str] = []
    if "--" in argv:
        cut = argv.index("--")
        argv, trailing = argv[:cut], argv[cut + 1:]
    args = build_parser().parse_args(argv)
    args.trailing_command = trailing
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    return args.command(args)


def build_parser() -> argparse.ArgumentParser:
    from .collector import list_adapters

    adapter_names = tuple(adapter.name for adapter in list_adapters())
    parser = argparse.ArgumentParser(
        prog="ridar",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Collect research-agent trajectories, detect integrity violations, "
        "and intervene at runtime.\n"
        "Run `ridar <command> --help` for usage and examples.",
    )
    commands = parser.add_subparsers(
        dest="subcommand",
        required=True,
        title="commands",
        metavar="{collect,adapters,monitor,detect,runtime,export}",
    )

    collect = commands.add_parser(
        "collect",
        help="Collect and normalize a trajectory",
        description="Collect one trajectory from --path or --session, or batch collect "
        "entries from a JSONL manifest. Raw inputs and a normalized trace are written "
        "under --runs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
        "  ridar collect claude-code --path EXP/logs/claude --run-id RUN_ID\n"
        "  ridar collect opencode --session ses_... --run-id RUN_ID\n"
        "  ridar collect --manifest manifest.jsonl",
    )
    collect.add_argument(
        "adapter",
        nargs="?",
        choices=adapter_names,
        help="Source harness; omit only when using --manifest",
    )
    collect.add_argument("--path", help="Directory holding the original trajectory")
    collect.add_argument(
        "--session", help="Session id to locate in the agent's local storage"
    )
    collect.add_argument(
        "--home", help="Agent storage root or database path (default: local storage)"
    )
    collect.add_argument("--run-id", help="Run directory name under --runs")
    collect.add_argument("--runs", default="runs", help="Collected runs directory (default: runs)")
    collect.add_argument("--task-id", help="Optional task identifier stored with the run")
    collect.add_argument(
        "--manifest", help="Collect every JSON object in this JSONL manifest"
    )
    collect.add_argument(
        "--skip-invalid", action="store_true",
        help="Skip malformed source records instead of failing",
    )
    collect.set_defaults(command=_collect)

    adapters_command = commands.add_parser(
        "adapters",
        help="List supported trajectory sources",
        description="List the source harnesses accepted by `ridar collect`.",
    )
    adapters_command.set_defaults(command=_list_adapters)

    monitor = commands.add_parser(
        "monitor",
        help="Browse runs in a local web UI",
        description="Serve a read-only web UI for browsing runs under --runs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
        "  ridar monitor --port 8766",
    )
    monitor.add_argument("--runs", default="runs", help="Runs directory (default: runs)")
    monitor.add_argument("--host", default="127.0.0.1", help="Listen address (default: 127.0.0.1)")
    monitor.add_argument("--port", type=int, required=True, help="Listen port (required)")
    monitor.set_defaults(command=_monitor)

    detect = commands.add_parser(
        "detect",
        help="Detect research integrity violations",
        description="Run the Analyzer--Verifier workflow over collected trajectories. "
        "The Analyzer audits every segment and the Verifier independently validates "
        "candidate findings against the taxonomy. Results are written to each run's "
        "detections/ directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
        "  ridar detect RUN_ID\n"
        "  ridar detect RUN_ID_1 RUN_ID_2 --runs runs\n"
        "  ridar detect --all --runs runs\n"
        "  ridar detect RUN_ID --dry-run",
    )
    detect.add_argument(
        "run_ids", nargs="*", metavar="RUN_ID", help="Run IDs under --runs",
    )
    detect.add_argument("--all", action="store_true", help="Detect every collected run")
    detect.add_argument("--runs", default="runs", help="Collected runs directory")
    detect.add_argument(
        "--config",
        default="config.toml",
        help="Project Detector TOML (default: ./config.toml)",
    )
    detect.add_argument(
        "--dry-run",
        action="store_true",
        help="Prepare and print one run's Analyzer command without launching it",
    )
    detect.add_argument(
        "--force", action="store_true", help="Redetect runs with completed reports"
    )
    detect.set_defaults(command=_detect)

    runtime = commands.add_parser(
        "runtime",
        help="Detect violations while an agent runs",
        description="Launch the agent command given after `--`, collect its trajectory, and "
        "detect research integrity violations as it runs. Add --intervene to return findings "
        "to the agent for correction.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
        "  ridar runtime claude-code --run-id RUN_ID --cwd WORKSPACE --intervene -- "
        "claude --dangerously-skip-permissions -p\n"
        "  ridar runtime codex --run-id RUN_ID --intervene -- "
        "codex exec --skip-git-repo-check -c model=...\n"
        "the agent command follows `--`; RIDAR injects intervention hooks automatically",
    )
    runtime.add_argument(
        "adapter", choices=("claude-code", "codex", "opencode"),
        help="Harness used to launch the agent",
    )
    runtime.add_argument("--run-id", required=True, help="New run directory name under --runs")
    runtime.add_argument("--runs", default="runs", help="Collected runs directory (default: runs)")
    runtime.add_argument("--config", default="config.toml", help="Project Detector TOML (default: ./config.toml)")
    runtime.add_argument("--cwd", help="Working directory for the agent (default: current directory)")
    runtime.add_argument("--prompt", help="File piped to the agent's stdin")
    runtime.add_argument(
        "--storage",
        help="Native storage the agent writes: the Claude Code projects directory, the Codex sessions "
        "directory, or the OpenCode database (default: the agent's home location)",
    )
    runtime.add_argument("--poll", type=float, default=2.0, help="Seconds between storage polls (default: 2)")
    runtime.add_argument("--task-id", help="Optional task identifier stored with the run")
    runtime.add_argument("--dry-run", action="store_true", help="Print the Analyzer and agent commands without launching anything")
    runtime.add_argument(
        "--output", default="label/detect-online.jsonl",
        help="Canonical results JSONL (default: label/detect-online.jsonl; empty disables)",
    )
    runtime.add_argument(
        "--intervene", action="store_true",
        help="Return detected violations to the agent for correction during execution and before exit",
    )
    runtime.add_argument("--barrier-wait", type=float, default=3600.0, help="Maximum seconds to wait for final detection before exit (default: 3600)")
    runtime.add_argument("--max-rounds", type=int, default=3, help="Maximum intervention rounds (default: 3)")
    runtime.add_argument(
        "--process-channel", choices=("context", "deny"), default="context",
        help="How findings reach the agent between tool calls: context adds them after a tool call (default); "
        "deny refuses the agent's next tool call once with the findings as the reason",
    )
    runtime.add_argument("--max-pending-steps", type=int, default=8, help=argparse.SUPPRESS)
    runtime.add_argument("--max-investigation-workers", type=int, default=8, help=argparse.SUPPRESS)
    runtime.add_argument("--max-open-investigations", type=int, default=24, help=argparse.SUPPRESS)
    runtime.add_argument("--investigation-timeout-seconds", type=float, default=180.0, help=argparse.SUPPRESS)
    runtime.add_argument("--window-records", type=int, default=40, help="Max records bundled into one judged step (default: 40; 1 = one turn per step)")
    runtime.add_argument("--window-chars", type=int, default=60000, help=argparse.SUPPRESS)
    runtime.set_defaults(command=_runtime)

    export = commands.add_parser(
        "export",
        help="Export finalized detection results to JSONL",
        description="Recursively export completed report.json files from one or more "
        "result directories into one canonical JSONL file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
        "  ridar export detection-results --output results/detect.jsonl\n"
        "  ridar export results-a results-b --output results/detect.jsonl",
    )
    export.add_argument(
        "results_dirs", nargs="+", metavar="RESULTS_DIR",
        help="Directory containing report.json files at any depth",
    )
    export.add_argument("--output", required=True, help="Canonical JSONL output path")
    export.set_defaults(command=_export)


    detector_mcp = commands.add_parser(
        "detect-mcp",
        description="Stateful Detector MCP server speaking JSON-RPC over "
        "stdio. Claude Code starts\nit automatically from .mcp.json during a "
        "detection session; there is no reason\nto run it by hand.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    detector_mcp.add_argument(
        "--config",
        default="config.toml",
        help=argparse.SUPPRESS,
    )
    detector_mcp.add_argument("--log-dir", help=argparse.SUPPRESS)
    detector_mcp.add_argument("--output-dir", help=argparse.SUPPRESS)
    detector_mcp.add_argument("--run-dir", help=argparse.SUPPRESS)
    detector_mcp.add_argument("--verifier-workdir", help=argparse.SUPPRESS)
    detector_mcp.add_argument("--online", action="store_true", help=argparse.SUPPRESS)
    detector_mcp.add_argument("--monitor-dir", help=argparse.SUPPRESS)
    detector_mcp.add_argument("--run-id", help=argparse.SUPPRESS)
    detector_mcp.add_argument("--max-pending-steps", type=int, default=8, help=argparse.SUPPRESS)
    detector_mcp.add_argument("--max-investigation-workers", type=int, default=8, help=argparse.SUPPRESS)
    detector_mcp.add_argument("--max-open-investigations", type=int, default=24, help=argparse.SUPPRESS)
    detector_mcp.add_argument("--investigation-timeout-seconds", type=float, default=180.0, help=argparse.SUPPRESS)
    detector_mcp.add_argument("--window-records", type=int, default=40, help="Max records bundled into one judged step (1 = one turn per step)")
    detector_mcp.add_argument("--window-chars", type=int, default=60000, help=argparse.SUPPRESS)
    detector_mcp.set_defaults(command=_detector_mcp)

    return parser


def _collect(args: argparse.Namespace) -> int:
    if args.manifest:
        return _collect_manifest(args)
    if not args.run_id:
        raise SystemExit("Provide --run-id")
    if args.adapter is None:
        raise SystemExit("Provide an adapter with --path or --session")
    return _collect_one(args)


def _collect_manifest(args: argparse.Namespace) -> int:
    if any(
        value is not None
        for value in (
            args.adapter,
            args.path,
            args.session,
            args.home,
            args.run_id,
            args.task_id,
        )
    ):
        raise SystemExit(
            "--manifest cannot be combined with an adapter or per-run source options"
        )
    manifest = Path(args.manifest).expanduser().resolve()
    if not manifest.is_file():
        raise SystemExit(f"Manifest does not exist: {manifest}")
    allowed = {
        "adapter",
        "path",
        "session",
        "home",
        "run_id",
        "task_id",
        "skip_invalid",
    }
    count = 0
    with manifest.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise SystemExit(
                    f"{manifest}:{line_number}: invalid JSON: {error}"
                ) from error
            if not isinstance(row, dict):
                raise SystemExit(f"{manifest}:{line_number}: expected a JSON object")
            unknown = sorted(set(row) - allowed)
            if unknown:
                raise SystemExit(
                    f"{manifest}:{line_number}: unknown field(s): {', '.join(unknown)}"
                )
            for key in ("path", "home"):
                value = row.get(key)
                if isinstance(value, str) and not Path(value).expanduser().is_absolute():
                    row[key] = str((manifest.parent / value).expanduser().resolve())
            item = argparse.Namespace(
                adapter=row.get("adapter"),
                path=row.get("path"),
                session=row.get("session"),
                home=row.get("home"),
                run_id=row.get("run_id"),
                runs=args.runs,
                task_id=row.get("task_id"),
                skip_invalid=row.get("skip_invalid", args.skip_invalid),
                manifest=None,
            )
            try:
                _validate_manifest_item(item)
                _collect(item)
            except (OSError, ValueError, SystemExit) as error:
                raise SystemExit(f"{manifest}:{line_number}: {error}") from error
            count += 1
    if count == 0:
        raise SystemExit(f"Manifest is empty: {manifest}")
    print(f"Manifest collected: {count} run(s)")
    return 0


def _validate_manifest_item(item: argparse.Namespace) -> None:
    from .collector import get_adapter

    if not isinstance(item.adapter, str):
        raise ValueError("adapter must name a supported collector")
    try:
        get_adapter(item.adapter)
    except KeyError as error:
        raise ValueError("adapter must name a supported collector") from error
    if not isinstance(item.run_id, str) or not item.run_id:
        raise ValueError("run_id must be a non-empty string")
    if not isinstance(item.skip_invalid, bool):
        raise ValueError("skip_invalid must be a boolean")


def _collect_one(args: argparse.Namespace) -> int:
    from .collector.offline import collect_run

    try:
        run_dir, record_count = collect_run(
            adapter_name=args.adapter,
            path=args.path,
            session=getattr(args, "session", None),
            home=getattr(args, "home", None),
            run_id=args.run_id,
            runs=args.runs,
            task_id=args.task_id,
            strict=not args.skip_invalid,
        )
    except (FileExistsError, ValueError) as error:
        raise SystemExit(str(error)) from error
    print(f"Collected {record_count} records")
    print(f"Run: {run_dir}")
    return 0


def _list_adapters(_args: argparse.Namespace) -> int:
    from .collector import list_adapters

    for adapter in list_adapters():
        print(adapter.name)
    return 0


def _monitor(args: argparse.Namespace) -> int:
    from .monitor import serve

    try:
        serve(
            args.runs,
            host=args.host,
            port=args.port,
        )
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            raise SystemExit(
                f"Monitor port {args.host}:{args.port} is already in use. "
                f"Use --port {args.port + 1} or stop the existing process."
            ) from None
        raise
    return 0


def _detector_mcp(args: argparse.Namespace) -> int:
    if args.online:
        from .detector.online.mcp import serve as serve_online

        if not args.monitor_dir or not args.run_id:
            raise SystemExit("--online requires --monitor-dir and --run-id")
        serve_online(
            args.monitor_dir, args.run_id,
            max_pending_steps=args.max_pending_steps,
            max_investigation_workers=args.max_investigation_workers,
            max_open_investigations=args.max_open_investigations,
            investigation_timeout_seconds=args.investigation_timeout_seconds,
            window_max_records=args.window_records,
            window_max_chars=args.window_chars,
        )
        return 0
    from .detector.offline.mcp import serve

    serve(
        config_path=args.config,
        log_dir=args.log_dir,
        output_dir=args.output_dir,
        run_dir=args.run_dir,
        verifier_workdir=args.verifier_workdir,
    )
    return 0


def _detect(args: argparse.Namespace) -> int:
    from .detector.offline.config import ConfigError, load_detector_config

    runs = _select_run_ids(args.run_ids, args.all, args.runs)
    if args.dry_run and len(runs) != 1:
        raise SystemExit("--dry-run requires exactly one RUN_ID")
    try:
        config = load_detector_config(args.config)
    except ConfigError as error:
        raise SystemExit(str(error)) from error
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []
    batch = args.all or len(runs) > 1
    for run_id, run_dir in runs.items():
        if not args.force and _has_completed_report(run_dir):
            skipped.append(run_id)
            print(f"[ridar-detect] skipping completed run: {run_id}", file=sys.stderr)
            continue
        try:
            status = _detect_one(args, config, run_dir)
        except SystemExit as error:
            if not batch:
                raise
            failed.append((run_id, str(error)))
            print(f"[ridar-detect] failed {run_id}: {error}", file=sys.stderr)
            continue
        except Exception as error:
            if not batch:
                raise
            failed.append((run_id, f"{type(error).__name__}: {error}"))
            print(
                f"[ridar-detect] failed {run_id}: {type(error).__name__}: {error}",
                file=sys.stderr,
            )
            continue
        if status:
            failed.append((run_id, f"exit status {status}"))

    if batch:
        completed = len(runs) - len(skipped) - len(failed)
        print(
            f"[ridar-detect] batch complete: {completed} completed, "
            f"{len(skipped)} skipped, {len(failed)} failed",
            file=sys.stderr,
        )
    return 1 if failed else 0


def _select_run_ids(run_ids: list[str], all_runs: bool, runs: str | Path) -> dict[str, Path]:
    """Collected runs to act on, by id: the given ids, or every run under ``runs`` at any depth."""
    from .collector.offline import collected_runs

    if all_runs and run_ids:
        raise SystemExit("RUN_ID and --all are mutually exclusive")
    if not all_runs and not run_ids:
        raise SystemExit("Provide at least one RUN_ID or --all")
    root = Path(runs).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"Runs directory does not exist: {root}")
    found = collected_runs(root)
    if all_runs:
        if not found:
            raise SystemExit(f"No collected runs found under {root}")
        return found
    selected = list(dict.fromkeys(run_ids))
    unknown = [run_id for run_id in selected if run_id not in found]
    if unknown:
        raise SystemExit(f"No collected run {unknown[0]!r} under {root}")
    return {run_id: found[run_id] for run_id in selected}


def _has_completed_report(run_dir: Path) -> bool:
    report = run_dir / "detections" / "report.json"
    if not report.is_file():
        return False
    try:
        value = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("status") == "completed"


def _detect_one(args: argparse.Namespace, config, run_dir: Path) -> int:
    import subprocess

    from .detector.offline.config import ConfigError, isolated_environment
    from .detector.offline.install import install
    from .detector.offline.preflight import PreflightError, run_preflight
    from .detector.offline.view import DetectionView

    extracted = run_dir / "extracted"
    if not (extracted / "run.json").is_file() or not (extracted / "trace.jsonl").is_file():
        raise SystemExit(f"No extracted/run.json and extracted/trace.jsonl under {run_dir}")
    metadata = json.loads((extracted / "run.json").read_text(encoding="utf-8"))
    if metadata.get("status") == "failed":
        raise SystemExit("Collection status is failed; this run cannot be detected")
    log_dir = run_dir / "detections"
    if args.dry_run:
        if config.mode == "verifier-only":
            print(
                json.dumps(
                    {
                        "mode": "verifier-only",
                        "run": str(run_dir),
                        "note": "no Analyzer session is launched; the AutoApproveAnalyzer "
                        "driver runs the Verifier completion loop in-process",
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        view = DetectionView(run_dir)
        installed = install(
            view.root,
            config_path=config.path,
            log_dir=log_dir,
            output_dir=run_dir / "detections",
            run_dir=view.evidence,
            verifier_workdir=view.verifier_scratch,
        )
        command = _detect_command(
            config,
            view.evidence,
            Path(installed["mcp_config"]),
            Path(installed["prompt"]),
        )
        print(
            json.dumps(
                {
                    "command": command,
                    "cwd": str(view.analyzer_scratch),
                    "temporary_view": str(view.root),
                    "note": "Remove temporary_view after running the printed command.",
                },
                ensure_ascii=False,
            )
        )
        # Dry-run intentionally leaves its self-contained view alive so
        # the printed command remains executable.
        return 0
    with DetectionView(run_dir) as view:
        components = _preflight_components(config.mode)
        component_names = " and ".join(name.title() for name in components)
        print(
            f"[ridar-detect] checking {component_names} "
            f"API{'s' if len(components) > 1 else ''}...",
            file=sys.stderr,
        )
        try:
            if config.mode == "default":
                run_preflight(config, run_dir=view.evidence)
            else:
                run_preflight(
                    config,
                    run_dir=view.evidence,
                    components=components,
                )
        except (ConfigError, PreflightError) as error:
            raise SystemExit(str(error)) from error
        print("[ridar-detect] API preflight passed", file=sys.stderr)
        from .detector.offline.logs import reset_logs

        reset_logs(log_dir)
        stale_dialogue = run_dir / "detections" / "dialogue.jsonl"
        if stale_dialogue.is_file():
            stale_dialogue.unlink()
        analyzer_result: dict | None = None
        if config.mode == "verifier-only":
            status = _detect_verifier_only(config, view, run_dir)
        else:
            installed = install(
                view.root,
                config_path=config.path,
                log_dir=log_dir,
                output_dir=run_dir / "detections",
                run_dir=view.evidence,
                verifier_workdir=view.verifier_scratch,
            )
            command = _detect_command(
                config,
                view.evidence,
                Path(installed["mcp_config"]),
                Path(installed["prompt"]),
            )
            env = isolated_environment()
            env.update(config.analyzer.resolved_environment())
            if config.mode != "analyzer-only":
                # This explicitly named variable is inherited only so the MCP child
                # can construct the Verifier from this same config file.
                env[config.verifier.api_key_env] = os.environ[config.verifier.api_key_env]
            # The pipeline may dispatch phase subagents in the background; headless
            # Claude Code otherwise kills the session after waiting 600s for them.
            env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = "0"
            # Runs are independent: the headless session must not read or write auto memory.
            env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
            env["MCP_TOOL_TIMEOUT"] = str(config.analyzer.mcp_tool_timeout * 1000)
            print(
                "[ridar-detect] headless session starting; first progress appears at "
                "start_detection (may take a few minutes):",
                file=sys.stderr,
                flush=True,
            )
            popen_options = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "bufsize": 1,
            }
            proc = subprocess.Popen(
                command, cwd=view.analyzer_scratch, env=env, **popen_options
            )
            stop = threading.Event()
            watcher = threading.Thread(
                target=_report_progress,
                args=(run_dir / "detections" / "dialogue.jsonl", stop),
                daemon=True,
            )
            watcher.start()
            from .detector.offline.logs import append_log

            assert proc.stdout is not None
            analyzer_log = log_dir / "analyzer.log"
            analyzer_result: dict | None = None
            for line in proc.stdout:
                if _skip_analyzer_log_event(line):
                    continue
                append_log(analyzer_log, "analyzer", line)
                analyzer_result = _analyzer_result_event(line) or analyzer_result
            status = proc.wait()
            stop.set()
            watcher.join(timeout=2)
    report = run_dir / "detections" / "report.json"
    report_value = None
    if report.is_file():
        try:
            report_value = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    if isinstance(report_value, dict) and analyzer_result is not None:
        from .detector.offline.files import write_json_atomic

        report_value.setdefault("usage", {})["analyzer"] = _analyzer_usage(analyzer_result)
        write_json_atomic(report, report_value)
    report_status = report_value.get("status") if isinstance(report_value, dict) else None
    if isinstance(report_value, dict) and report_status == "completed":
        print(f"Report: {report}")
    else:
        print("No completed report.json was finalized", file=sys.stderr)
        status = status or 1
    return status


def _preflight_components(mode: str) -> tuple[str, ...]:
    """Which model endpoints a mode actually calls; the default pipeline calls both."""
    return {
        "analyzer-only": ("analyzer",),
        "verifier-only": ("verifier",),
    }.get(mode, ("analyzer", "verifier"))


def _detect_verifier_only(config, view, run_dir: Path) -> int:
    """verifier-only ablation: no Analyzer session; the AutoApproveAnalyzer driver
    runs the Verifier's completion-review loop in-process over the same evidence view."""
    from .detector.offline.auto import AutoApproveAnalyzer
    from .detector.offline.state import DetectionState

    print(
        "[ridar-detect] verifier-only mode: no Analyzer session; driving the "
        "Verifier completion review directly:",
        file=sys.stderr,
        flush=True,
    )
    state = DetectionState(
        config=config,
        log_dir=run_dir / "detections",
        output_dir=run_dir / "detections",
        bound_run_dir=view.evidence,
        verifier_workdir=view.verifier_scratch,
    )
    stop = threading.Event()
    watcher = threading.Thread(
        target=_report_progress,
        args=(run_dir / "detections" / "dialogue.jsonl", stop),
        daemon=True,
    )
    watcher.start()
    try:
        AutoApproveAnalyzer(state).run()
    finally:
        state.close()
        stop.set()
        watcher.join(timeout=2)
    return 0


def _skip_analyzer_log_event(line: str) -> bool:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return False
    return (
        isinstance(event, dict)
        and event.get("type") == "system"
        and event.get("subtype") == "thinking_tokens"
    )


def _analyzer_result_event(line: str) -> dict | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) and event.get("type") == "result" else None


def _analyzer_usage(result: dict) -> dict:
    keys = ("usage", "modelUsage", "total_cost_usd", "duration_ms", "duration_api_ms", "num_turns")
    return {key: result.get(key) for key in keys}


def _report_progress(audit_path: Path, stop: threading.Event) -> None:
    """Tail the Detector dialogue log and print detection milestones to
    stderr while the headless session runs. Tolerates the file being
    recreated by start_detection and half-written trailing lines."""
    import time

    offset = 0
    buffer = ""
    last_size = -1
    started = time.monotonic()
    heartbeat = 0
    while not stop.wait(1.0):
        try:
            if not audit_path.is_file():
                elapsed = int(time.monotonic() - started)
                if elapsed >= heartbeat + 30:
                    heartbeat = elapsed
                    print(
                        f"[ridar-detect] waiting for the detector session to reach "
                        f"start_detection... ({elapsed}s; session boot + prompt load can "
                        "take a few minutes)",
                        file=sys.stderr,
                        flush=True,
                    )
                continue
            size = audit_path.stat().st_size
            if size == last_size:
                continue
            last_size = size
            if size < offset:
                offset, buffer = 0, ""
            with audit_path.open(encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                data = handle.read()
                offset = handle.tell()
            lines = (buffer + data).split("\n")
            buffer = lines.pop()
            for line in lines:
                if line.strip():
                    try:
                        _print_milestone(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except OSError:
            continue


def _print_milestone(record: dict) -> None:
    def emit(text: str) -> None:
        print(f"[ridar-detect] {text}", file=sys.stderr, flush=True)

    event = record.get("type")
    if record.get("error"):
        emit(f"{event} error: {str(record['error'])[:160]}")
        return
    if event == "detection_started":
        emit(
            f"detection started ({record.get('total_lines', '?')} records, "
            f"{len(record.get('segments') or [])} segments)"
        )
    elif event == "segment_marked":
        emit(
            f"Analyzer read segment {record.get('segment_id', '?')} "
            f"({record.get('marked', '?')}/{record.get('total', '?')})"
        )
    elif event == "analyzer_trace_scan_completed":
        emit(f"Analyzer completed trace pass ({record.get('total_lines', '?')} records)")
    elif event == "analyzer_proposal":
        emit(f"Analyzer proposed {record.get('proposal_id', '?')}")
    elif event == "verifier_started":
        emit(f"Verifier reviewing {record.get('proposal_id', '?')}")
    elif event == "verifier_review":
        cause = record.get("cause")
        suffix = f" ({cause})" if cause and cause != "none" else ""
        emit(
            f"Verifier {record.get('decision', '?')}{suffix}: "
            f"{record.get('proposal_id', '?')} r{record.get('revision', '?')}"
        )
    elif event == "finding_accepted":
        emit(
            f"finding accepted: {record.get('finding_id', '?')} "
            f"[{record.get('taxonomy', '?')}]"
        )
    elif event == "omission_proposed":
        emit(f"Verifier proposed omission {record.get('omission_id', '?')}")
    elif event == "omission_resolved":
        emit(
            f"Analyzer {record.get('decision', '?')}ed omission "
            f"{record.get('omission_id', '?')}"
        )
    elif event == "completion_review":
        emit(
            f"completion round {record.get('round', 1)} decision={record.get('decision', '?')} "
            f"({record.get('omissions', 0)} omissions)"
        )
    elif event == "completion_capped":
        emit(f"completion capped after {record.get('completion_rounds', '?')} rounds")
    elif event == "detection_completed":
        emit(
            f"detection completed ({record.get('findings', '?')} findings)"
        )


def _detect_command(
    config,
    run_dir: Path,
    mcp_config: Path,
    analyzer_prompt: Path,
) -> list[str]:
    prompt = (
        "Run the Detector Analyzer workflow using the supplied instructions and "
        "continue until completion."
    )
    command = [
        config.analyzer.executable,
        "-p",
        prompt,
        "--mcp-config",
        str(mcp_config),
        "--strict-mcp-config",
        "--setting-sources",
        "project",
        "--add-dir",
        str(run_dir),
        "--no-session-persistence",
        "--append-system-prompt-file",
        str(analyzer_prompt),
        "--model",
        config.analyzer.model,
        "--permission-mode",
        config.analyzer.permission_mode,
        "--tools",
        ",".join(config.analyzer.tools),
    ]
    if config.analyzer.autocompact:
        command.extend(["--autocompact", config.analyzer.autocompact])
    command.extend(["--verbose", "--output-format", "stream-json"])
    return command


def _runtime(args: argparse.Namespace) -> int:
    from .collector.offline import run_directory
    from .collector.registry import get_adapter
    from .detector.offline.config import ConfigError, load_detector_config
    from .detector.online.install import PoolLimits
    from .detector.online.run import live

    agent_command = list(getattr(args, "trailing_command", []))
    if not agent_command:
        raise SystemExit("give the agent command after --, for example: -- claude --dangerously-skip-permissions -p")
    try:
        get_adapter(args.adapter)
    except KeyError as error:
        raise SystemExit(str(error)) from error
    try:
        run_dir = run_directory(args.runs, args.run_id)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    try:
        config = load_detector_config(args.config)
    except ConfigError as error:
        raise SystemExit(str(error)) from error
    limits = PoolLimits(
        max_pending_steps=args.max_pending_steps,
        max_investigation_workers=args.max_investigation_workers,
        max_open_investigations=args.max_open_investigations,
        investigation_timeout_seconds=args.investigation_timeout_seconds,
        window_max_records=args.window_records,
        window_max_chars=args.window_chars,
    )
    return live(
        run_dir, config, adapter=args.adapter, command=agent_command, prompt=args.prompt, cwd=args.cwd,
        storage=args.storage, poll=args.poll, task_id=args.task_id, limits=limits,
        output_jsonl=args.output or None, dry_run=args.dry_run,
        intervene=args.intervene, barrier_wait=args.barrier_wait,
        max_rounds=args.max_rounds, process_channel=args.process_channel,
    )


def _export(args: argparse.Namespace) -> int:
    from .detector.offline.results import rebuild_results

    try:
        result = rebuild_results(args.results_dirs, args.output)
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0
