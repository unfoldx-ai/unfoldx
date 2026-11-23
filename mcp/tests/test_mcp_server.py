"""unfoldx-mcp over an in-memory MCP session against the real-HTTP fake API."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
import pytest

UNFOLDX = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(UNFOLDX / "cli" / "tests")]

from fake_platform import FILM, RUN, TOKEN, serve  # noqa: E402

CORE_READS = {"unfoldx_capabilities", "unfoldx_credits", "unfoldx_catalog", "unfoldx_workspaces", "unfoldx_run_get",
              "unfoldx_materials"}
CORE_WRITES = {"unfoldx_material_upload", "unfoldx_work_export", "unfoldx_run_submit", "unfoldx_run_answer",
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


def _session(action, **options):
    from mcp.client.client import Client
    from unfoldx_mcp.server import build_server

    async def run():
        async with Client(build_server(**options)) as client:
            return await action(client)
    return anyio.run(run)


def _call(name, arguments=None, **options):
    return _session(lambda client: client.call_tool(name, arguments or {}), **options)


def _status():
    return _call("unfoldx_run_get", {"method": "status", "run_id": RUN})


def test_default_server_offers_only_the_core_tools(platform):
    names = {tool.name for tool in _session(lambda client: client.list_tools()).tools}
    assert names == CORE_READS | CORE_WRITES


def test_every_tool_declares_object_schemas_and_honest_hints(platform):
    from unfoldx_mcp.contracts import TOOLSETS
    tools = {tool.name: tool for tool in _session(lambda client: client.list_tools(), toolsets=set(TOOLSETS)).tools}
    assert len(tools) == 28
    for tool in tools.values():
        assert tool.input_schema["type"] == "object"
        assert tool.output_schema["type"] == "object" and tool.output_schema["oneOf"]
    assert all(tools[name].annotations.read_only_hint is True for name in CORE_READS)
    assert all(tools[name].annotations.read_only_hint is False for name in CORE_WRITES)
    assert tools["unfoldx_run_submit"].annotations.idempotent_hint is True  # keyed by command_id
    assert tools["unfoldx_run_cancel"].annotations.destructive_hint is True
    assert tools["unfoldx_run_get"].input_schema["properties"]["method"]["enum"] == [
        "status", "events", "questions", "result", "usage", "command"]


def test_reads_return_the_v2_envelope_as_structured_content_and_text(platform):
    _logged_in(platform)
    platform.run_states = ["waiting_input"]
    status = _status()
    assert status.is_error is False
    assert status.structured_content["schema"] == "unfoldx.v2" and status.structured_content["ok"] is True
    assert status.structured_content["data"]["question"]["approval_url"].endswith("/chat/abc123")
    assert json.loads(status.content[0].text) == status.structured_content


def test_credits_tell_the_agent_how_much_it_may_approve_alone(platform):
    _logged_in(platform)
    credits = _call("unfoldx_credits")
    assert credits.is_error is False, credits.structured_content
    assert credits.structured_content["data"]["auto_approve_limit"] == 300
    assert credits.structured_content["data"]["credential"]["window"] == "rolling_24h"


def test_capabilities_with_fields_this_client_does_not_know_still_come_back(platform):
    _logged_in(platform)
    capabilities = _call("unfoldx_capabilities")
    assert capabilities.is_error is False, capabilities.structured_content
    assert capabilities.structured_content["data"]["skills"][0]["id"] == "explain"


def test_business_failures_are_tool_errors_with_the_same_envelope(platform):
    missing = _status()
    assert missing.is_error is True and missing.structured_content["error"]["code"] == "not_logged_in"
    assert "unfoldx login" in missing.structured_content["error"]["message"]

    _logged_in(platform)
    refused = _call("unfoldx_workspaces", {"method": "list"})
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
    result = _call('unfoldx_run_get', {'method': 'events', 'run_id': RUN, 'after_seq': -1})
    assert result.is_error is True
    assert result.structured_content['error']['code'] == 'invalid_arguments'
    result = _call('unfoldx_run_get', {'run_id': RUN})
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
    found = _call('unfoldx_run_get', {'method': 'command', 'command_id': args['command_id']})
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
    result = _status()
    assert result.is_error and result.structured_content['error']['code'] == 'invalid_response'


def test_resource_tools_validate_before_writing_and_explain_scopes(platform):
    from unfoldx_mcp.contracts import TOOLSETS
    _logged_in(platform)
    tools={t.name:t for t in _session(lambda c:c.list_tools(), toolsets=set(TOOLSETS)).tools}
    assert tools['unfoldx_works'].annotations.read_only_hint
    assert tools['unfoldx_share_write'].annotations.destructive_hint
    assert 'settings:write' in tools['unfoldx_settings_update'].description
    result=_call('unfoldx_share_write',{'method':'revoke','run_id':RUN,'confirm':False}, toolsets={'shares'})
    assert result.is_error and result.structured_content['error']['code']=='invalid_arguments'
    assert not any(method=='DELETE' for method,path,body in platform.requests)


def test_readonly_toolsets_exclude_writes_and_refuse_direct_calls(platform):
    _logged_in(platform)
    names={t.name for t in _session(lambda c:c.list_tools(), toolsets={'shares'}, read_only=True).tools}
    assert names == {'unfoldx_share_get'}
    refused=_call('unfoldx_share_write',{'method':'revoke','run_id':RUN,'confirm':True}, toolsets={'shares'}, read_only=True)
    assert refused.is_error and refused.structured_content['error']['code']=='tool_disabled'
    assert _call('unfoldx_run_submit',{'goal':'x','command_id':'mcp-off-0001'}, toolsets={'shares'}).structured_content[
        'error']['code']=='tool_disabled'
    assert not any(method in ('DELETE', 'POST') for method,path,body in platform.requests)


def test_memory_deletes_publishing_and_platform_logins_are_not_tools(platform):
    from unfoldx_mcp.contracts import TOOLSETS
    _logged_in(platform)
    names={t.name for t in _session(lambda c:c.list_tools(), toolsets=set(TOOLSETS)).tools}
    assert not {n for n in names if 'memory' in n or 'login' in n or 'publishing' in n or n.endswith('_delete')}
    refused=_call('unfoldx_memory_show',{})
    assert refused.is_error and refused.structured_content['error']['code']=='unknown_tool'


def test_every_request_says_it_comes_from_the_mcp_server(platform, tmp_path):
    _logged_in(platform)
    notes = tmp_path / "notes.md"
    notes.write_text("# 量子纠缠", encoding="utf-8")
    assert _call("unfoldx_material_upload", {"path": str(notes)}).is_error is False
    assert _status().is_error is False
    assert platform.objects and len(platform.clients) >= 4
    assert set(platform.clients) == {"mcp"}


def test_work_export_saves_the_film_and_never_overwrites_by_default(platform, tmp_path, monkeypatch):
    _logged_in(platform)
    original = platform.handle
    def handle(method, path, query, headers, body):
        if path == f'/api/v2/works/{RUN}/exports':
            return platform.envelope({'run_id': RUN, 'metadata': {'title': '月相'}, 'unavailable': [], 'note': None,
                                      'files': [{'role': 'video', 'filename': 'moon.mp4',
                                                 'url': platform.base + '/files/film.mp4'}]})
        if path == '/files/film.mp4':
            return 200, FILM
        return original(method, path, query, headers, body)
    monkeypatch.setattr(platform, 'handle', handle)
    args = {'run_id': RUN, 'format': 'video', 'out': str(tmp_path / 'out')}
    saved = _call('unfoldx_work_export', args)
    assert saved.is_error is False, saved.structured_content
    assert Path(saved.structured_content['data']['files'][0]['path']).read_bytes() == FILM
    again = _call('unfoldx_work_export', args)
    assert again.is_error and again.structured_content['error']['code'] == 'file_exists'
    extra = _call('unfoldx_work_export', {**args, 'bogus': 1})
    assert extra.is_error and extra.structured_content['error']['code'] == 'invalid_arguments'


def test_a_response_is_checked_against_the_method_that_was_called(platform, monkeypatch):
    _logged_in(platform)
    original = platform.handle
    def events_shaped(method, path, query, headers, body):
        if path == f'/api/v2/runs/{RUN}':
            return original(method, f'/api/v2/runs/{RUN}/events', {'after_seq': ['0']}, headers, body)
        return original(method, path, query, headers, body)
    monkeypatch.setattr(platform, 'handle', events_shaped)
    assert _status().structured_content['error']['code'] == 'invalid_response'


def test_topics_and_schedules_are_their_own_toolsets(platform):
    from unfoldx_mcp.contracts import TOOLSETS
    assert {"topics", "schedules"} <= set(TOOLSETS)
    names = {tool.name for tool in _session(lambda client: client.list_tools(), toolsets={"topics", "schedules"}).tools}
    assert {"unfoldx_topics", "unfoldx_schedules"} <= names
    assert "unfoldx_run_submit" not in names


SCHEDULE = {
    "method": "create", "title": "每日选题简报",
    "frequency": {"kind": "daily", "time": "09:00", "timezone": "Asia/Shanghai"},
    "scope": {"topic_ids": ["t-agent"], "event_ids": [], "text": ""},
    "deliverable": {"kind": "brief", "angles": 3, "plan": True, "language": "Chinese", "reply_language": "zh"},
    "notify": "important",
}


def test_the_schedules_tool_lists_a_task_it_created(platform):
    _logged_in(platform)
    created = _call("unfoldx_schedules", SCHEDULE, toolsets={"schedules"})
    assert created.is_error is False and created.structured_content["data"]["schedule_id"] == 1
    listed = _call("unfoldx_schedules", {"method": "list"}, toolsets={"schedules"})
    assert [row["title"] for row in listed.structured_content["data"]["schedules"]] == ["每日选题简报"]


@pytest.mark.parametrize("field,required", [("scope", "event_ids"), ("deliverable", "reply_language")])
def test_schedules_rejects_an_incomplete_nested_object_before_creating(platform, field, required):
    _logged_in(platform)
    fields = {key: value for key, value in SCHEDULE[field].items() if key != required}
    result = _call("unfoldx_schedules", {**SCHEDULE, field: fields}, toolsets={"schedules"})
    assert result.is_error and result.structured_content["error"]["code"] == "invalid_arguments"
    listed = _call("unfoldx_schedules", {"method": "list"}, toolsets={"schedules"})
    assert listed.structured_content["data"]["schedules"] == []


def test_readonly_can_read_topics_and_schedules_without_exposing_their_writes(platform):
    _logged_in(platform)
    options = {"toolsets": {"topics", "schedules"}, "read_only": True}
    tools = {tool.name: tool for tool in _session(lambda client: client.list_tools(), **options).tools}
    assert tools["unfoldx_topics"].input_schema["properties"]["method"]["enum"] == ["list"]
    assert tools["unfoldx_schedules"].input_schema["properties"]["method"]["enum"] == ["list", "get", "runs"]
    for name in tools:
        assert tools[name].annotations.read_only_hint is True
        assert _call(name, {"method": "list"}, **options).structured_content["ok"] is True
    refused = _call("unfoldx_schedules", SCHEDULE, **options)
    assert refused.is_error and refused.structured_content["error"]["code"] == "tool_disabled"
    assert _call("unfoldx_schedules", {"method": "list"}, **options).structured_content["data"]["schedules"] == []


def test_publish_tool_prepares_outside_the_web_card(platform):
    _logged_in(platform)
    from unfoldx_mcp.contracts import TOOLSETS
    assert "publish" in set(TOOLSETS)
    prepared = _call("unfoldx_publish", {"method": "prepare", "run_id": RUN, "platform": "douyin",
                                         "title": "一个 am｜五个关于爱的词",
                                         "tags": ["英语单词"]}, toolsets={"publish"})
    assert prepared.is_error is False, prepared.content
    intent = prepared.structured_content["data"]
    assert intent["state"] == "created" and intent["handoff_url"]
    status = _call("unfoldx_publish", {"method": "status", "intent_id": intent["intent_id"]}, toolsets={"publish"})
    assert status.structured_content["data"]["intent_id"] == intent["intent_id"]
