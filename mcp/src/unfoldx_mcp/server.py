"""stdio MCP server: the unfoldx.v2 public API as short MCP tool calls.

Tools are generated from `x-mcp-tools` in the CLI's openapi.json; the three that touch local files are written
here. Each answers with the same envelope as the API. stdout carries only MCP protocol messages.
"""

from __future__ import annotations

import json
import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from jsonschema import Draft202012Validator, ValidationError as SchemaError
from pydantic import ValidationError
from mcp_types import CallToolResult, TextContent, ToolAnnotations, Tool
from pydantic import Field

from unfoldx_cli import SCHEMA
from unfoldx_cli.api import ApiClient
from unfoldx_cli.commands import upload_material
from unfoldx_cli.exports import export_work, superfast_download
from unfoldx_cli.resources import invoke
from unfoldx_cli.config import load_settings
from unfoldx_cli.credentials import require_token
from unfoldx_cli.errors import UnfoldxError
from unfoldx_cli.redact import redact

from . import __version__
from .contracts import (DEFAULT_TOOLSETS, TOOLS, TOOLSETS, description, hints, operation_for, operation_input_schema,
                        operation_output_schema, operations, output_schema, tool_input_schema)

INSTRUCTIONS = """UnfoldX turns a topic or material into an explainer video through its main agent.
A run is one message to that agent. Start one with unfoldx_run_submit (upload local files first with
unfoldx_material_upload; pass workspace_id to follow up, e.g. "make the opening shorter"). Choose command_id
BEFORE calling submit and reuse it on every retry; on an unknown outcome call unfoldx_run_get method=command.
Give the user the run's web_url: it is the page where they watch the run and confirm what it asks.
Follow a run with unfoldx_run_get method=status or method=events (pass the previous next_seq as after_seq;
poll every few seconds, never in a tight loop). When it is waiting_input, read method=questions.
Before answering a route card, read unfoldx_credits: auto_approve_limit is the largest hold this key may approve
alone. Answer with unfoldx_run_answer (copy quote_ref from the card) only when the user asked you to; if it comes
back approval_required, give the user the approval_url: spending above the key's budget is confirmed by the user
on the web, never by you.
When it is completed, read method=result: completed ends one message, not necessarily a video (check ready and
message). Save the film with unfoldx_work_export. method=usage shows what the run held and charged.
Superfast (toolset superfast) is a separate job; its hold counts against the same key budget.
Export only saved exact subtitles; use the separately named transcript when timing is unavailable.
If a call fails with not_logged_in, ask the user to run `unfoldx login` in a terminal."""

# Issues a new upload or writes a local file on each call, so it is not a pure read.
ISSUE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
OVERWRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)


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


GENERATED = [name for name, spec in TOOLS.items() if 'local' not in spec]


class UnfoldxServer(MCPServer):
    """Use public SDK extension points for typed errors and the shared output contract."""

    async def list_tools(self):
        tools = await super().list_tools()
        for tool in tools:
            tool.output_schema = output_schema(tool.name)
            tool.input_schema = {**tool.input_schema, 'additionalProperties': False}
        tools += [
            Tool(name=name, description=description(name, read_only=self.read_only),
                 input_schema=tool_input_schema(name, read_only=self.read_only),
                 output_schema=output_schema(name, read_only=self.read_only),
                 annotations=ToolAnnotations(open_world_hint=False, **hints(name, read_only=self.read_only)))
            for name in GENERATED if operations(name, read_only=self.read_only)
        ]
        return [tool for tool in tools
                if TOOLS[tool.name]['toolset'] in self.enabled_toolsets
                and not (self.read_only and not tool.annotations.read_only_hint)]

    async def call_tool(self, name, arguments, context=None):
        offered = {tool.name: tool for tool in await self.list_tools()}
        method = arguments.get('method') if isinstance(arguments, dict) else None
        blocked_method = (self.read_only and name in GENERATED and isinstance(method, str)
                          and method in operations(name) and method not in operations(name, read_only=True))
        if name in TOOLS and (name not in offered or blocked_method):
            return _tool_error(UnfoldxError('auth_failed', 'This tool is disabled by the server toolsets/read-only configuration.',
                                            code='tool_disabled'))
        expected = None
        try:
            if name in GENERATED:
                Draft202012Validator(offered[name].input_schema).validate(arguments)
                operation_id, values = operation_for(name, arguments)
                Draft202012Validator(operation_input_schema(operation_id)).validate(values)
                expected = operation_output_schema(operation_id)
                result = _call(lambda client: invoke(client, operation_id, values))
            else:
                if name in offered:
                    Draft202012Validator(offered[name].input_schema).validate(arguments)
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
            Draft202012Validator(expected or output_schema(name)).validate(result.structured_content)
        except SchemaError:
            return _tool_error(UnfoldxError('server_error', 'API response did not match the published contract.', code='invalid_response'))
        return result


def build_server(*, toolsets: set[str] | frozenset[str] = DEFAULT_TOOLSETS, read_only: bool = False) -> MCPServer:
    server = UnfoldxServer("unfoldx", version=__version__, instructions=INSTRUCTIONS)
    server.enabled_toolsets = set(toolsets)
    server.read_only = read_only

    @server.tool(annotations=ISSUE)
    def unfoldx_material_upload(
        path: Annotated[str, Field(description="absolute path of a local file (pdf, docx, pptx, md, image…), ≤100 MB")],
    ) -> CallToolResult:
        """Upload a local file as material for a run; returns its material_id once stored."""
        return _call(lambda client: upload_material(client, Path(path).expanduser()))

    @server.tool(annotations=OVERWRITE)
    def unfoldx_work_export(run_id: Annotated[str, Field(description="run id (run_…) of a finished work")],
                            format: Literal['video', 'cover', 'subtitles', 'storyboard', 'transcript', 'audio', 'zip'],
                            out: Annotated[str, Field(description="local directory to save into")],
                            overwrite: bool = False) -> CallToolResult:
        """Save an owned work export locally. Audio extraction needs ffmpeg and includes music; missing exact subtitles are never fabricated. Defaults to no overwrite."""
        return _call(lambda client: export_work(client, run_id, format, out, overwrite))

    @server.tool(annotations=OVERWRITE)
    def unfoldx_superfast_download(job_id: Annotated[str, Field(description="superfast job id (sf_…)")],
                                   out: Annotated[str, Field(description="local directory to save into")],
                                   overwrite: bool = False) -> CallToolResult:
        """Save a finished superfast film locally as <out>/<job_id>/film.mp4."""
        return _call(lambda client: superfast_download(client, job_id, out, overwrite))

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description='UnfoldX stdio MCP server')
    parser.add_argument('--toolsets', help='comma-separated toolsets, or all; default core. Available: ' + ','.join(TOOLSETS))
    parser.add_argument('--read-only', action='store_true', help='expose read-only tools and reject direct calls to disabled writes')
    args = parser.parse_args()
    toolsets = (set(TOOLSETS) if args.toolsets == 'all' else set(args.toolsets.split(','))) if args.toolsets else DEFAULT_TOOLSETS
    if not set(toolsets) <= set(TOOLSETS):
        parser.error('Unknown toolsets: ' + ', '.join(sorted(set(toolsets) - set(TOOLSETS))))
    build_server(toolsets=toolsets, read_only=args.read_only).run()
