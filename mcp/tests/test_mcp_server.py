"""unfoldx-mcp over an in-memory MCP session against the real-HTTP fake API."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
import pytest

UNFOLDX = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(UNFOLDX / "cli" / "tests")]

from fake_platform import RUN, TOKEN, serve  # noqa: E402

READS = {"unfoldx_capabilities", "unfoldx_whoami", "unfoldx_workspaces_list", "unfoldx_workspace_get",
         "unfoldx_run_status", "unfoldx_run_events", "unfoldx_run_questions", "unfoldx_run_result",
         "unfoldx_run_usage", "unfoldx_artifact_get", "unfoldx_material_get", "unfoldx_command_get"}
WRITES = {"unfoldx_artifact_access", "unfoldx_material_upload", "unfoldx_run_submit", "unfoldx_run_answer",
          "unfoldx_run_cancel"}


@pytest.fixture
def platform(tmp_path, monkeypatch):
    with serve() as fake:
        monkeypatch.setenv("UNFOLDX_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("UNFOLDX_NO_KEYRING", "1")
        monkeypatch.setenv("UNFOLDX_API_BASE", fake.base + "/api")
        monkeypatch.delenv("UNFOLDX_TOKEN", raising=False)
        yield fake


def _logged_in(platform):
    from unfoldx_cli.credentials import save_token
    save_token(platform.base + "/api", TOKEN)


def _session(action):
    from mcp.client.client import Client
    from unfoldx_mcp.server import build_server

    async def run():
        async with Client(build_server()) as client:
            return await action(client)
    return anyio.run(run)


def _call(name, arguments=None):
    return _session(lambda client: client.call_tool(name, arguments or {}))


def test_every_tool_declares_object_schemas_and_honest_hints(platform):
    tools = {tool.name: tool for tool in _session(lambda client: client.list_tools()).tools}
    assert READS | WRITES <= set(tools)
    for tool in tools.values():
        assert tool.input_schema["type"] == "object"
        assert tool.output_schema["type"] == "object" and tool.output_schema["oneOf"]
    assert all(tools[name].annotations.read_only_hint is True for name in READS)
    assert all(tools[name].annotations.read_only_hint is False for name in WRITES)
    access = tools["unfoldx_artifact_access"].annotations
    assert access.read_only_hint is False and access.idempotent_hint is False
    assert tools["unfoldx_run_submit"].annotations.idempotent_hint is True  # keyed by command_id
    assert tools["unfoldx_run_cancel"].annotations.destructive_hint is True


def test_reads_return_the_v2_envelope_as_structured_content_and_text(platform):
    _logged_in(platform)
    platform.run_states = ["waiting_input"]
    status = _call("unfoldx_run_status", {"run_id": RUN})
    assert status.is_error is False
    assert status.structured_content["schema"] == "unfoldx.v2" and status.structured_content["ok"] is True
    assert status.structured_content["data"]["question"]["approval_url"].endswith("/chat/abc123")
    assert json.loads(status.content[0].text) == status.structured_content

    access = _call("unfoldx_artifact_access", {"artifact_id": f"{RUN}.video"})
    assert access.structured_content["data"]["url"].startswith(platform.base + "/files/")


def test_business_failures_are_tool_errors_with_the_same_envelope(platform):
    missing = _call("unfoldx_run_status", {"run_id": RUN})
    assert missing.is_error is True and missing.structured_content["error"]["code"] == "not_logged_in"
    assert "unfoldx login" in missing.structured_content["error"]["message"]

    _logged_in(platform)
    refused = _call("unfoldx_workspaces_list")
    assert refused.is_error is True and refused.structured_content["ok"] is False
    assert refused.structured_content["error"]["code"] == "insufficient_scope"
    assert json.loads(refused.content[0].text) == refused.structured_content
    assert TOKEN not in refused.content[0].text


def test_submit_answer_and_cancel_go_through_the_same_api(platform, tmp_path):
    _logged_in(platform)
    notes = tmp_path / "notes.md"
    notes.write_text("# 量子纠缠", encoding="utf-8")
    material = _call("unfoldx_material_upload", {"path": str(notes)})
    assert material.is_error is False and material.structured_content["data"]["state"] == "ready"
    material_id = material.structured_content["data"]["material_id"]

    first = _call("unfoldx_run_submit", {"goal": "讲清楚量子纠缠", "material_ids": [material_id], "command_id": "mcp-known-first-001"})
    data = first.structured_content["data"]
    assert data["run"]["run_id"] == RUN and data["command_id"] == "mcp-known-first-001"
    again = _call("unfoldx_run_submit", {"goal": "讲清楚量子纠缠", "material_ids": [material_id],
                                         "command_id": data["command_id"]})
    assert again.structured_content["data"]["idempotent_replay"] is True

    platform.over_budget = True
    held = _call("unfoldx_run_answer", {"run_id": RUN, "question_id": "q-route", "answer": {
        "action": "choose_route", "route": "template", "quote_ref": "qr-template", "execution_mode": "autonomous"}})
    assert held.is_error is True and held.structured_content["error"]["code"] == "approval_required"
    assert held.structured_content["error"]["details"]["approval_url"].endswith("/chat/abc123")

    cancelled = _call("unfoldx_run_cancel", {"run_id": RUN})
    assert cancelled.structured_content["data"]["run"]["state"] == "cancelled"


def test_sdk_argument_errors_keep_the_public_envelope(platform):
    _logged_in(platform)
    result = _call('unfoldx_run_events', {'run_id': RUN, 'after_seq': -1})
    assert result.is_error is True
    assert result.structured_content['ok'] is False
    assert result.structured_content['error']['code'] == 'invalid_arguments'
    assert json.loads(result.content[0].text) == result.structured_content


def test_submit_requires_a_known_command_id_before_any_write(platform):
    _logged_in(platform)
    r=_call('unfoldx_run_submit',{'goal':'不能因丢响应重开任务'})
    assert r.is_error and r.structured_content['error']['code']=='invalid_arguments'
    assert not platform.commands


def test_lost_submit_response_preserves_identity_and_reports_transport_error(platform, monkeypatch):
    _logged_in(platform)
    from unfoldx_cli import api
    monkeypatch.setattr(api.time, 'sleep', lambda _: None)
    platform.disconnects = 3
    args = {'goal': '断连测试', 'command_id': 'mcp-disconnect-001'}
    error = _call('unfoldx_run_submit', args)
    assert error.is_error and error.structured_content['error']['code'] == 'connection_failed'
    assert error.structured_content['error']['details']['command_id'] == args['command_id']
    found = _call('unfoldx_command_get', {'command_id': args['command_id']})
    assert found.structured_content['data']['run_id'] == RUN
    retried = _call('unfoldx_run_submit', args)
    assert retried.structured_content['data']['idempotent_replay'] and len(platform.commands) == 1


def test_output_schema_rejects_a_malformed_server_run(platform, monkeypatch):
    _logged_in(platform)
    original = platform.handle
    def invalid(method, path, query, headers, body):
        if path.endswith('/' + RUN):
            return platform.envelope({'run_id': RUN, 'state': 'invented'})
        return original(method, path, query, headers, body)
    monkeypatch.setattr(platform, 'handle', invalid)
    result = _call('unfoldx_run_status', {'run_id': RUN})
    assert result.is_error and result.structured_content['error']['code'] == 'invalid_response'


def test_resource_tools_validate_before_writing_and_explain_scopes(platform):
    _logged_in(platform)
    tools={t.name:t for t in _session(lambda c:c.list_tools()).tools}
    assert tools['unfoldx_materials_list'].annotations.read_only_hint
    assert tools['unfoldx_share_revoke'].annotations.destructive_hint
    assert 'settings:write' in tools['unfoldx_pronunciations_update'].description
    result=_call('unfoldx_share_revoke',{'run_id':RUN,'confirm':False})
    assert result.is_error and result.structured_content['error']['code']=='invalid_arguments'
    assert not any(method=='DELETE' for method,path,body in platform.requests)


def test_readonly_toolsets_exclude_writes_and_refuse_direct_calls(platform):
    from mcp.client.client import Client
    from unfoldx_mcp.server import build_server
    _logged_in(platform)
    async def scenario():
        async with Client(build_server(toolsets={'shares'}, read_only=True)) as client:
            names={t.name for t in (await client.list_tools()).tools}
            assert 'unfoldx_share_show' in names
            assert 'unfoldx_share_revoke' not in names and 'unfoldx_run_submit' not in names
            refused=await client.call_tool('unfoldx_share_revoke',{'run_id':RUN,'confirm':True})
            assert refused.is_error and refused.structured_content['error']['code']=='tool_disabled'
    anyio.run(scenario)
    assert not any(method=='DELETE' for method,path,body in platform.requests)


def test_memory_deletes_and_platform_logins_are_not_tools(platform):
    _logged_in(platform)
    names={t.name for t in _session(lambda c:c.list_tools()).tools}
    assert not {n for n in names if 'memory' in n or 'login' in n or n.endswith('_delete')}
    assert {n for n in names if 'publishing' in n} == {
        'unfoldx_publishing_accounts', 'unfoldx_publishing_drafts', 'unfoldx_publishing_draft', 'unfoldx_publishing_retry'}
    refused=_call('unfoldx_memory_show',{})
    assert refused.is_error and refused.structured_content['error']['code']=='unknown_tool'


def test_every_request_says_it_comes_from_the_mcp_server(platform, tmp_path):
    _logged_in(platform)
    notes = tmp_path / "notes.md"
    notes.write_text("# 量子纠缠", encoding="utf-8")
    assert _call("unfoldx_material_upload", {"path": str(notes)}).is_error is False
    assert _call("unfoldx_run_status", {"run_id": RUN}).is_error is False
    assert platform.objects and len(platform.clients) >= 4
    assert set(platform.clients) == {"mcp"}
