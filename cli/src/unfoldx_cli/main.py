"""unfoldx CLI entry point: argument parsing and dispatch."""

from __future__ import annotations

import argparse
import math
import sys

from . import __version__, commands, resources
from .api import ApiClient
from .config import load_settings
from .credentials import require_token
from .errors import EXIT_INTERRUPT, UnfoldxError
from .output import Emitter


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UnfoldxError("usage", message, code="invalid_arguments")


def _positive_number(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def _add_global_options(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    # Global options are also attached to every subcommand so agents can put
    # them anywhere (e.g. `unfoldx runs status ... --json`). Subcommand copies use
    # SUPPRESS defaults so they never overwrite a value parsed before the
    # subcommand name.
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="machine mode: one JSON object on stdout (auto-enabled when stdout is not a TTY)",
    )
    parser.add_argument("--api-base", default=default, help="override API base URL (https required)")
    parser.add_argument("--timeout", type=_positive_number, default=default, help="total seconds per HTTP operation, including retries")


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(
        prog="unfoldx",
        description="Delegate explainer videos to the UnfoldX main agent and follow them to the finished film.",
    )
    parser.add_argument("--version", action="version", version=f"unfoldx {__version__}")
    _add_global_options(parser, suppress=False)
    common = Parser(add_help=False)
    _add_global_options(common, suppress=True)
    sub = parser.add_subparsers(dest="command", required=True)

    def group(name: str, help_text: str):
        return sub.add_parser(name, help=help_text, parents=[common]).add_subparsers(
            dest=f"{name}_command", required=True)

    login = sub.add_parser("login", help="authorize this machine in the browser (device code)", parents=[common])
    login.add_argument("--client-name", help="name shown on the approval page (default: unfoldx-cli @ host)")
    sub.add_parser("logout", help="revoke this machine's credential and forget it", parents=[common])
    sub.add_parser("whoami", help="the credential in use: scopes, budget, expiry", parents=[common])
    sub.add_parser("capabilities", help="operations, routes and limits open to this credential", parents=[common])
    sub.add_parser("doctor", help="diagnose credentials, connectivity and version", parents=[common])

    workspaces = group("workspaces", "conversations that hold runs")
    listing = workspaces.add_parser("list", help="workspaces with pagination and search", parents=[common])
    listing.add_argument("--limit", type=int, default=20, help="page size (1–100)")
    listing.add_argument("--cursor", help="next_cursor from the previous page")
    listing.add_argument("--query", help="search workspace titles")
    listing.add_argument("--state", choices=("queued", "running", "waiting_input", "completed", "failed", "cancelled"))
    workspaces.add_parser("show", help="a workspace and its runs", parents=[common]).add_argument("workspace_id")

    runs = group("runs", "send the main agent a message and follow it to its film")
    submit = runs.add_parser("submit", help="ask for a video (a new workspace, or a follow-up in one)",
                             parents=[common])
    submit.add_argument("--goal", help="what the video should do, in your own words")
    submit.add_argument("--url", action="append", help="a web source (repeatable)")
    submit.add_argument("--file", action="append", help="a local file to upload as material (repeatable)")
    submit.add_argument("--material", action="append", help="an uploaded material id (repeatable)")
    submit.add_argument("--workspace", help="follow up in this workspace (e.g. 'make the opening shorter')")
    submit.add_argument("--input-json", help="the request as JSON from a file or - (instead of flags)")
    submit.add_argument("--command-id", help="idempotency key to use (default: remembered per request)")
    submit.add_argument("--new", action="store_true", help="start a new command even if this one ran before")
    answer = runs.add_parser("answer", help="answer the run's confirmation card", parents=[common])
    answer.add_argument("run_id")
    answer.add_argument("--question", help="question id (default: the open route card)")
    answer.add_argument("--route", help="choose this quoted production route")
    answer.add_argument("--mode", choices=("autonomous", "interactive"), default="autonomous",
                        help="with --route: finish on its own, or stop for the storyboard first")
    answer.add_argument("--style", help="with --route: one of the route's style candidates")
    answer.add_argument("--note", help="with --route: an extra request for the production")
    answer.add_argument("--angle", help="with --route: one of the card's explanation angles (default: the recommended one)")
    answer.add_argument("--answer", help="the answer text")
    answer.add_argument("--answer-json", help="the answer as JSON")
    runs.add_parser("cancel", help="stop the run (repeating is harmless)", parents=[common]).add_argument("run_id")
    for name, help_text in (("status", "state and the open question, if any"),
                            ("questions", "open confirmation cards with quotes and approval links"),
                            ("result", "the finished film's artifacts"),
                            ("usage", "credits held and charged")):
        runs.add_parser(name, help=help_text, parents=[common]).add_argument("run_id")
    events = runs.add_parser("events", help="progress events after a cursor", parents=[common])
    events.add_argument("run_id")
    events.add_argument("--after-seq", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)
    wait = runs.add_parser("wait", help="poll until the run finishes or asks a question", parents=[common])
    wait.add_argument("run_id")
    wait.add_argument("--after-seq", type=int, default=0, help="resume printing events after this cursor")
    wait.add_argument("--interval", type=float, help="seconds between polls (default 5)")
    wait.add_argument("--wait-timeout", type=float, help="give up after this many seconds (default 3600)")

    materials = group("materials", "files the main agent works from")
    materials.add_parser("upload", help="upload a local file; prints its material id",
                         parents=[common]).add_argument("path")
    materials.add_parser("show", help="a material's state", parents=[common]).add_argument("material_id")

    commands_group = group("commands", "find what a write command started")
    commands_group.add_parser("show", help="the run a command id started", parents=[common]).add_argument(
        "command_id")

    artifacts = group("artifacts", "finished films and covers")
    artifacts.add_parser("show", help="artifact metadata", parents=[common]).add_argument("artifact_id")
    download = artifacts.add_parser("download", help="download through a short-lived signed link", parents=[common])
    download.add_argument("artifact_id")
    download.add_argument("--overwrite", action="store_true", help="explicitly replace this run’s existing file")
    download.add_argument("--out", default=".", help="output directory")
    groups = resources.add_parsers(sub, common, {"materials": materials})
    export = groups['works'].add_parser('export', parents=[common], help='download video/cover/subtitles/storyboard/transcript/audio/ZIP')
    export.add_argument('run_id')
    export.add_argument('--format', required=True, choices=('video','cover','subtitles','storyboard','transcript','audio','zip'))
    export.add_argument('--out', default='.')
    export.add_argument('--overwrite', action='store_true')
    wait_sf = groups['superfast'].add_parser('wait', parents=[common], help='wait for the separate superfast job')
    wait_sf.add_argument('job_id')
    wait_sf.add_argument('--wait-timeout', type=_positive_number, default=600)
    wait_sf.add_argument('--interval', type=_positive_number, default=2)
    download_sf = groups['superfast'].add_parser('download', parents=[common], help='download the finished superfast film')
    download_sf.add_argument('job_id')
    download_sf.add_argument('--out', default='.')
    download_sf.add_argument('--overwrite', action='store_true')
    return parser


_READS = {
    ("whoami", None): "/v2/me",
    ("capabilities", None): "/v2/capabilities",
    ("workspaces", "list"): "/v2/workspaces",
    ("workspaces", "show"): "/v2/workspaces/{workspace_id}",
    ("runs", "status"): "/v2/runs/{run_id}",
    ("runs", "questions"): "/v2/runs/{run_id}/questions",
    ("runs", "result"): "/v2/runs/{run_id}/result",
    ("runs", "usage"): "/v2/runs/{run_id}/usage",
    ("artifacts", "show"): "/v2/artifacts/{artifact_id}",
    ("materials", "show"): "/v2/materials/{material_id}",
    ("commands", "show"): "/v2/commands/{command_id}",
}
_WRITES = {
    ("runs", "submit"): commands.cmd_runs_submit,
    ("runs", "answer"): commands.cmd_runs_answer,
    ("runs", "cancel"): commands.cmd_runs_cancel,
    ("materials", "upload"): commands.cmd_materials_upload,
}


def _dispatch(args, emit: Emitter) -> int:
    settings = load_settings(args.api_base, args.timeout)
    if args.command == "login":
        return commands.cmd_login(args, settings, emit)
    if args.command == "logout":
        return commands.cmd_logout(args, settings, emit)
    if args.command == "doctor":
        return commands.cmd_doctor(args, settings, emit)

    client = ApiClient(settings.api_base, require_token(settings.api_base), settings.timeout_sec, client="cli")
    if getattr(args, "resource_operation", None):
        return resources.dispatch(args, client, emit)
    action = getattr(args, f"{args.command}_command", None)
    from .exports import export_work, superfast_wait, superfast_download
    if (args.command, action) == ('works', 'export'):
        emit.result(export_work(client,args.run_id,args.format,args.out,args.overwrite))
        return 0
    if (args.command, action) == ('superfast', 'wait'):
        emit.result(superfast_wait(client,args.job_id,args.wait_timeout,args.interval))
        return 0
    if (args.command, action) == ('superfast', 'download'):
        emit.result(superfast_download(client,args.job_id,args.out,args.overwrite))
        return 0
    if (args.command, action) == ("workspaces", "list"):
        emit.result(client.get("/v2/workspaces", limit=args.limit, cursor=args.cursor, q=args.query, state=args.state))
        return 0
    if (args.command, action) in _READS:
        return commands.cmd_get(_READS[(args.command, action)])(args, client, emit)
    if (args.command, action) in _WRITES:
        return _WRITES[(args.command, action)](args, client, emit)
    if (args.command, action) == ("runs", "events"):
        return commands.cmd_runs_events(args, client, emit)
    if (args.command, action) == ("runs", "wait"):
        return commands.cmd_runs_wait(args, client, emit, settings)
    if (args.command, action) == ("artifacts", "download"):
        return commands.cmd_artifacts_download(args, client, emit)
    raise UnfoldxError("usage", f"Unknown command: {args.command} {action or ''}".strip())


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    emit = Emitter("--json" in arguments or not sys.stdout.isatty())
    try:
        args = build_parser().parse_args(arguments)
        return _dispatch(args, emit)
    except UnfoldxError as exc:
        emit.error(exc)
        return exc.exit_code
    except BrokenPipeError:
        return 1
    except OSError as exc:
        error = UnfoldxError("usage", f"Local file operation failed: {exc.strerror or type(exc).__name__}", code="file_error")
        emit.error(error)
        return error.exit_code
    except KeyboardInterrupt:
        emit.note("interrupted")
        return EXIT_INTERRUPT


def cli() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    cli()
