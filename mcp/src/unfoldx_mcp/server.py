"""stdio MCP server: the unfoldx.v2 public API as short MCP tool calls.

Each tool is one HTTP call through the CLI's ApiClient and answers with the same
envelope as the API. stdout carries only MCP protocol messages.
"""

from __future__ import annotations

import json
import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from jsonschema import Draft202012Validator, ValidationError as SchemaError
from pydantic import ValidationError
from mcp_types import CallToolResult, TextContent, ToolAnnotations, Tool
from pydantic import Field
from unfoldx_cli import SCHEMA
from unfoldx_cli.api import ApiClient, quote
from unfoldx_cli.commands import upload_material
from unfoldx_cli.exports import export_work
from unfoldx_cli.resources import OPERATIONS as RESOURCE_OPERATIONS, input_schema, invoke
from unfoldx_cli.config import load_settings
from unfoldx_cli.credentials import require_token
from unfoldx_cli.errors import UnfoldxError
from unfoldx_cli.redact import redact

from . import __version__
from .contracts import DEFINITIONS, output_schema, with_definitions

INSTRUCTIONS = """Use capabilities and catalog tools to discover current choices. Resource writes use separate scopes;
read current revisions before replacing settings. Superfast is a separate opt-in job with
billing not_metered: never present it as free or as governed by main-agent credit caps.
Export only saved exact subtitles; use the separately named transcript when timing is unavailable.
UnfoldX turns a topic or material into an explainer video through its main agent.
A run is one message to that agent: start one with unfoldx_run_submit (upload local files first with
unfoldx_material_upload; pass workspace_id to follow up, e.g. "make the opening shorter"). Keep the
command_id you chose BEFORE calling submit, and reuse it on every retry. command_id is required.
On an unknown write outcome, use unfoldx_command_get before considering a new command.
Give the user the run's web_url: it is the page where they watch the run and confirm what it asks.
Follow a run with unfoldx_run_status or unfoldx_run_events
(pass the previous next_seq as after_seq; poll every few seconds, never in a tight loop).
When a run is waiting_input, read unfoldx_run_questions. You may answer a route card with
unfoldx_run_answer (copy quote_ref from the card) when the user asked you to; if the answer comes
back approval_required, give the user the approval_url: spending above the credential's budget is
confirmed by the user on the web, never by you.
When it is completed, read unfoldx_run_result.message and ready: completed ends one message, not
necessarily a video. A response without a film can explain a budget stop; do not silently start a new
command. The result lists delivered artifacts and unfoldx_artifact_access
issues a one-hour download link. If a call fails with not_logged_in, ask the user to run
`unfoldx login` in a terminal."""

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
# Issues a new short-lived link or upload on each call, so it is not a pure read.
ISSUE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
# Same command id or same answer: the server returns what it already accepted.
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
STOP = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)

RunId = Annotated[str, Field(description="run id (run_…) from a workspace or a status call")]
ArtifactId = Annotated[str, Field(description="artifact id (<run_id>.video or <run_id>.cover) from a run result")]



def _call(fetch: Callable[[ApiClient], dict]) -> CallToolResult:
    try:
        settings = load_settings()
        client = ApiClient(settings.api_base, require_token(settings.api_base), settings.timeout_sec, client="mcp")
        envelope = redact({"schema": SCHEMA, "ok": True, "data": fetch(client)})
        return CallToolResult(structured_content=envelope,
                              content=[TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False))])
    except UnfoldxError as exc:
        return _tool_error(exc)
    except OSError as exc:
        return _tool_error(UnfoldxError("usage", f"Local file operation failed: {exc.strerror or type(exc).__name__}",
                                       code="file_error"))


def _tool_error(exc: UnfoldxError) -> CallToolResult:
    envelope = redact({"schema": SCHEMA, "ok": False, "error": exc.to_payload()})
    return CallToolResult(is_error=True, structured_content=envelope,
                          content=[TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False))])


class UnfoldxServer(MCPServer):
    """Use public SDK extension points for typed errors and the shared output contract."""

    async def list_tools(self):
        tools = await super().list_tools()
        for tool in tools:
            tool.output_schema = output_schema(tool.name)
            if tool.name == 'unfoldx_run_answer':
                tool.input_schema['properties']['answer'] = {'$ref': '#/$defs/Answer'}
                tool.input_schema['$defs'] = with_definitions({'$ref': '#/$defs/Answer'})['$defs']
        for name, (_, method, op) in RESOURCE_OPERATIONS.items():
            tools.append(Tool(name=name, description=op["description"] + " Required scope: " + op["x-scope"],
                input_schema=with_definitions(input_schema(op)), output_schema=output_schema(name),
                annotations=ToolAnnotations(read_only_hint=method=="GET", destructive_hint=op["x-destructive"],
                                            idempotent_hint=op["x-idempotent"], open_world_hint=True)))
        selected = []
        for tool in tools:
            group = RESOURCE_OPERATIONS[tool.name][2]['x-cli'][0] if tool.name in RESOURCE_OPERATIONS else (
                'materials' if tool.name.startswith('unfoldx_material_') else
                'works' if tool.name.startswith('unfoldx_artifact_') or tool.name == 'unfoldx_work_export' else 'core')
            if self.enabled_toolsets is not None and group not in self.enabled_toolsets:
                continue
            if self.read_only and not tool.annotations.read_only_hint:
                continue
            selected.append(tool)
        return selected

    async def call_tool(self, name, arguments, context=None):
        if self.enabled_toolsets is not None or self.read_only:
            if name not in {tool.name for tool in await self.list_tools()}:
                return _tool_error(UnfoldxError('auth_failed','This tool is disabled by the server toolsets/read-only configuration.',code='tool_disabled'))
        try:
            if name == 'unfoldx_run_answer' and 'answer' in arguments:
                Draft202012Validator({'$ref': '#/$defs/Answer', '$defs': DEFINITIONS}).validate(arguments['answer'])
            if name in RESOURCE_OPERATIONS:
                Draft202012Validator(with_definitions(input_schema(RESOURCE_OPERATIONS[name][2]))).validate(arguments)
                result = _call(lambda client: invoke(client, name, arguments))
            else:
                result = await super().call_tool(name, arguments, context)
        except SchemaError as exc:
            return _tool_error(UnfoldxError('invalid_request', exc.message, code='invalid_arguments'))
        except UnexpectedToolError:
            return _tool_error(UnfoldxError('server_error', 'The tool could not complete. Preserve the command id and retry the status query.', code='tool_failed'))
        except ToolError as exc:
            if isinstance(exc.__cause__, ValidationError):
                fields = ', '.join('.'.join(str(p) for p in item['loc']) for item in exc.__cause__.errors())
                return _tool_error(UnfoldxError('invalid_request', 'Invalid or missing arguments: ' + fields, code='invalid_arguments'))
            return _tool_error(UnfoldxError('invalid_request', str(exc), code='unknown_tool'))
        try:
            Draft202012Validator(output_schema(name)).validate(result.structured_content)
        except SchemaError:
            return _tool_error(UnfoldxError('server_error', 'API response did not match the published contract.', code='invalid_response'))
        return result


def build_server(*, toolsets: set[str] | None = None, read_only: bool = False) -> MCPServer:
    server = UnfoldxServer("unfoldx", version=__version__, instructions=INSTRUCTIONS)
    server.enabled_toolsets = toolsets
    server.read_only = read_only

    @server.tool(annotations=READ)
    def unfoldx_capabilities() -> CallToolResult:
        """Operations, scopes, credit budget, production routes, minimum client version and the skill catalog (what to ask
        the main agent for, each with a phrasing to send as a run message) for this credential."""
        return _call(lambda client: client.get("/v2/capabilities"))

    @server.tool(annotations=READ)
    def unfoldx_whoami() -> CallToolResult:
        """The credential in use: client name, scopes, budget and expiry."""
        return _call(lambda client: client.get("/v2/me"))

    @server.tool(annotations=READ)
    def unfoldx_workspaces_list(
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Field(description="next_cursor from the preceding page; keep the same query/state")] = None,
        query: Annotated[str | None, Field(max_length=200, description="search workspace titles")] = None,
        state: Literal['queued', 'running', 'waiting_input', 'completed', 'failed', 'cancelled'] | None = None,
    ) -> CallToolResult:
        """Find workspaces by title/run state; paginate until next_cursor is null."""
        return _call(lambda client: client.get("/v2/workspaces", limit=limit, cursor=cursor, q=query, state=state))

    @server.tool(annotations=READ)
    def unfoldx_workspace_get(
        workspace_id: Annotated[str, Field(description="workspace id from unfoldx_workspaces_list")],
    ) -> CallToolResult:
        """A workspace and every run in it, each with its state and open question."""
        return _call(lambda client: client.get(f"/v2/workspaces/{quote(workspace_id)}"))

    @server.tool(annotations=READ)
    def unfoldx_run_status(run_id: RunId) -> CallToolResult:
        """State (queued, running, waiting_input, completed, failed, cancelled), the open question and settlement."""
        return _call(lambda client: client.get(f"/v2/runs/{quote(run_id)}"))

    @server.tool(annotations=READ)
    def unfoldx_run_events(
        run_id: RunId,
        after_seq: Annotated[int, Field(ge=0, description="return events after this cursor (next_seq of the last call)")] = 0,
        limit: Annotated[int, Field(ge=1, le=200)] = 100,
    ) -> CallToolResult:
        """Progress, question and result events after a cursor, plus the run's current state and next_seq."""
        return _call(lambda client: client.get(f"/v2/runs/{quote(run_id)}/events", after_seq=after_seq, limit=limit))

    @server.tool(annotations=READ)
    def unfoldx_run_questions(run_id: RunId) -> CallToolResult:
        """Open confirmation cards (route choice with quotes, storyboard, publish) and the user's approval_url."""
        return _call(lambda client: client.get(f"/v2/runs/{quote(run_id)}/questions"))

    @server.tool(annotations=READ)
    def unfoldx_run_result(run_id: RunId) -> CallToolResult:
        """The finished film's title and artifacts; ready is false until the run completes."""
        return _call(lambda client: client.get(f"/v2/runs/{quote(run_id)}/result"))

    @server.tool(annotations=READ)
    def unfoldx_run_usage(run_id: RunId) -> CallToolResult:
        """Credits held and charged for the run, and whether settlement is complete."""
        return _call(lambda client: client.get(f"/v2/runs/{quote(run_id)}/usage"))

    @server.tool(annotations=READ)
    def unfoldx_artifact_get(artifact_id: ArtifactId) -> CallToolResult:
        """Artifact metadata: role and file name."""
        return _call(lambda client: client.get(f"/v2/artifacts/{quote(artifact_id)}"))

    @server.tool(annotations=ISSUE)
    def unfoldx_artifact_access(artifact_id: ArtifactId) -> CallToolResult:
        """A one-hour signed download link for the artifact; ask again for a fresh one."""
        return _call(lambda client: client.post(f"/v2/artifacts/{quote(artifact_id)}/access"))

    @server.tool(annotations=ISSUE)
    def unfoldx_material_upload(
        path: Annotated[str, Field(description="absolute path of a local file (pdf, docx, pptx, md, image…), ≤100 MB")],
    ) -> CallToolResult:
        """Upload a local file as material for a run; returns its material_id once stored."""
        return _call(lambda client: upload_material(client, Path(path).expanduser()))

    @server.tool(annotations=READ)
    def unfoldx_material_get(
        material_id: Annotated[str, Field(description="material id (mat_…)")],
    ) -> CallToolResult:
        """A material's file name, size and state (pending until its upload is confirmed)."""
        return _call(lambda client: client.get(f"/v2/materials/{quote(material_id)}"))

    @server.tool(annotations=WRITE)
    def unfoldx_run_submit(
        goal: Annotated[str, Field(min_length=1, max_length=10000, description="what the video should do")],
        command_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9._:-]{8,128}$", description="choose and save BEFORE submitting; reuse this exact id to retry or recover")],
        urls: Annotated[list[str] | None, Field(max_length=10, description="web sources")] = None,
        material_ids: Annotated[list[str] | None, Field(max_length=10, description="from unfoldx_material_upload")] = None,
        workspace_id: Annotated[str | None, Field(description="follow up in this workspace")] = None,
    ) -> CallToolResult:
        """Send the main agent a message: a new video, or a follow-up in a workspace. Returns the run and command_id."""
        key = command_id
        body = {"goal": goal, "urls": urls, "material_ids": material_ids, "workspace_id": workspace_id}
        return _call(lambda client: {"command_id": key, **client.post(
            "/v2/runs", {name: value for name, value in body.items() if value}, headers={"Idempotency-Key": key})})

    @server.tool(annotations=READ)
    def unfoldx_command_get(
        command_id: Annotated[str, Field(description="command_id returned by unfoldx_run_submit")],
    ) -> CallToolResult:
        """The run a command started, for when a submit's response was lost."""
        return _call(lambda client: client.get(f"/v2/commands/{quote(command_id)}"))

    @server.tool(annotations=WRITE)
    def unfoldx_run_answer(
        run_id: RunId,
        question_id: Annotated[str, Field(description="question_id from unfoldx_run_questions")],
        answer: Annotated[dict[str, Any] | str, Field(description=(
            "route card: {action: choose_route, route, quote_ref, execution_mode: autonomous|interactive, style_id?, "
            "note?, angle_id?}; use the card answer_schema/answer_examples. Storyboard: continue or narration_patch; publish: publish or cancel."))],
    ) -> CallToolResult:
        """Answer the run's confirmation card. Above the credential's budget this fails with approval_required."""
        return _call(lambda client: client.post(f"/v2/runs/{quote(run_id)}/answers",
                                                {"question_id": question_id, "answer": answer}))

    @server.tool(annotations=STOP)
    def unfoldx_run_cancel(run_id: RunId) -> CallToolResult:
        """Stop the run and release its holds; repeating it returns the run's current state."""
        return _call(lambda client: client.post(f"/v2/runs/{quote(run_id)}/cancel"))

    @server.tool(annotations=STOP)
    def unfoldx_work_export(run_id: RunId, format: Literal['video','cover','subtitles','storyboard','transcript','audio','zip'],
                           out: str, overwrite: bool = False) -> CallToolResult:
        """Save an owned work export locally. Audio extraction needs ffmpeg and includes music; missing exact subtitles are never fabricated. Defaults to no overwrite."""
        return _call(lambda client: export_work(client,run_id,format,out,overwrite))

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description='UnfoldX stdio MCP server')
    parser.add_argument('--toolsets', help='comma-separated domains; default all (core,materials,works,catalogs,covers,speech,shares,brands,brand-assets,preferences,pronunciations,publishing,superfast)')
    parser.add_argument('--read-only', action='store_true', help='expose read-only tools and reject direct calls to disabled writes')
    args = parser.parse_args()
    toolsets = set(args.toolsets.split(',')) if args.toolsets else None
    allowed = {'core'} | {op['x-cli'][0] for _,_,op in RESOURCE_OPERATIONS.values()}
    if toolsets is not None and not toolsets <= allowed:
        parser.error('Unknown toolsets: ' + ', '.join(sorted(toolsets - allowed)))
    build_server(toolsets=toolsets,read_only=args.read_only).run()
