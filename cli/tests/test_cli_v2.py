"""unfoldx CLI v2 against a real-HTTP stand-in of /api/v2: login, exit codes, wait, download."""

from __future__ import annotations

import hashlib
import json
import stat

from fake_platform import FILM, RUN, TOKEN


def _login(cli):
    code, out, err = cli("login", "--client-name", "unfoldx-cli test")
    assert code == 0, out
    return out, err


def test_login_stores_the_token_privately_and_never_prints_it(cli, platform, tmp_path):
    out, err = _login(cli)
    assert out["schema"] == "unfoldx.v2" and out["ok"] is True
    assert out["data"]["credential_id"] == "cred_0123456789abcdef"
    assert "cli-authorize?code=ABCD-EFGH" in err
    stored = tmp_path / "config" / "credentials.json"
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert TOKEN not in json.dumps(out) and TOKEN not in err
    assert [body for method, path, body in platform.requests if path.endswith("/auth/device")] == [
        {"client_name": "unfoldx-cli test"}]

    code, me, _ = cli("whoami")
    assert code == 0 and me["data"]["credential_id"] == "cred_0123456789abcdef"


def test_login_keeps_polling_through_a_transient_server_error(cli, platform):
    platform.token_hiccups = 2
    out, _ = _login(cli)
    assert out["data"]["credential_id"] == "cred_0123456789abcdef"


def test_a_dropped_connection_is_retried(cli, platform, monkeypatch):
    _login(cli)
    from unfoldx_cli import api
    monkeypatch.setattr(api.time, "sleep", lambda _seconds: None)
    platform.disconnects = 1
    code, result, _ = cli('runs', 'submit', '--goal', '响应丢失恢复', '--command-id', 'cli-disconnect-001')
    assert code == 0 and result['data']['idempotent_replay']
    assert len(platform.commands) == 1


def test_server_errors_keep_their_type_code_and_exit_code(cli):
    code, out, _ = cli("whoami")
    assert code == 3 and out["error"]["type"] == "auth_failed" and out["error"]["code"] == "not_logged_in"

    _login(cli)
    code, out, _ = cli("workspaces", "list")
    assert code == 3 and out["error"]["code"] == "insufficient_scope"
    code, out, _ = cli("runs", "status", "run_missing")
    assert code == 5 and out["error"]["type"] == "not_found"


def test_wait_stops_at_a_question_with_its_approval_link(cli, platform):
    _login(cli)
    code, out, err = cli("runs", "wait", RUN, "--interval", "0")
    assert code == 5 and out["error"]["type"] == "needs_input" and out["error"]["code"] == "question_open"
    assert out["error"]["details"]["question"]["approval_url"] == "https://dev.unfoldx.ai/chat/abc123"
    assert "正在写脚本" in err


def test_wait_on_a_run_waiting_with_no_question_it_can_answer_points_to_the_page(cli, platform):
    """API v2 answers waiting_input with question null when the run waits on something only the page shows."""
    _login(cli)
    platform.open_question = False
    code, out, err = cli("runs", "wait", RUN, "--interval", "0")
    assert code == 5 and (out["error"]["type"], out["error"]["code"]) == ("needs_input", "no_open_question")
    assert out["error"]["details"]["web_url"] == "https://unfoldx.ai/chat/ws_abc123"
    assert "https://unfoldx.ai/chat/ws_abc123" in out["error"]["message"]


def test_wait_returns_the_result_and_download_writes_the_film(cli, platform, tmp_path):
    _login(cli)
    platform.run_states = ["running", "completed"]
    code, out, _ = cli("runs", "wait", RUN, "--interval", "0")
    assert code == 0 and out["data"]["result"]["artifacts"][0]["artifact_id"] == f"{RUN}.video"

    code, out, _ = cli("artifacts", "download", f"{RUN}.video", "--out", str(tmp_path / "films"))
    assert code == 0
    saved = tmp_path / "films" / RUN / "abc123.mp4"
    assert saved.read_bytes() == FILM
    assert out["data"]["sha256"] == hashlib.sha256(FILM).hexdigest() and out["data"]["bytes"] == len(FILM)
    assert "sig=1" not in json.dumps(out)


def test_logout_revokes_the_credential_on_the_server_and_forgets_it(cli, platform, tmp_path):
    _login(cli)
    code, out, _ = cli("logout")
    assert code == 0 and out["data"] == {"logged_out": True, "revoked_on_server": True}
    assert platform.revoked
    assert "tokens" not in json.loads((tmp_path / "config" / "credentials.json").read_text()) or \
        not json.loads((tmp_path / "config" / "credentials.json").read_text())["tokens"]
    code, out, _ = cli("whoami")
    assert code == 3 and out["error"]["code"] == "not_logged_in"


def test_a_token_is_bound_to_the_api_it_was_issued_by(cli, platform, monkeypatch):
    _login(cli)
    monkeypatch.setenv("UNFOLDX_API_BASE", "https://api.unfoldx.ai/api")
    code, out, _ = cli("whoami")
    assert code == 3 and out["error"]["code"] == "not_logged_in"


def test_redaction_covers_the_whole_url_safe_token():
    from unfoldx_cli.redact import redact_text
    assert redact_text(f"token {TOKEN} end") == "token ***redacted*** end"


def test_submit_uploads_files_and_a_rerun_replays_the_same_command(cli, platform, tmp_path):
    _login(cli)
    notes = tmp_path / "notes.pdf"
    notes.write_bytes(b"%PDF-1.4 entanglement notes")
    argv = ("runs", "submit", "--goal", "讲清楚量子纠缠", "--file", str(notes), "--url", "https://example.com/a")
    code, out, _ = cli(*argv)
    assert code == 0, out
    command_id = out["data"]["command_id"]
    assert out["data"]["run"]["run_id"] == RUN and out["data"]["idempotent_replay"] is False
    [(key, sent)] = platform.objects.items()
    assert sent == notes.read_bytes() and key.endswith("notes.pdf")
    [body] = platform.commands.values()
    assert body["goal"] == "讲清楚量子纠缠" and body["urls"] == ["https://example.com/a"]
    assert [m["state"] for m in platform.materials.values()] == ["ready"]
    assert body["material_ids"] == list(platform.materials)

    code, again, err = cli(*argv)  # e.g. after a crash: no second upload, same command, same run
    assert code == 0 and again["data"]["command_id"] == command_id and again["data"]["idempotent_replay"] is True
    assert "--new" in err
    assert len(platform.objects) == 1 and len(platform.commands) == 1

    code, fresh, _ = cli(*argv, "--new")
    assert code == 0 and fresh["data"]["command_id"] != command_id and len(platform.commands) == 2

    code, found, _ = cli("commands", "show", command_id)
    assert code == 0 and found["data"]["run_id"] == RUN

    request = tmp_path / "request.json"
    request.write_text(json.dumps({"goal": "开头改短", "workspace_id": "ws_abc123"}))
    code, out, _ = cli("runs", "submit", "--input-json", str(request))
    assert code == 0 and platform.commands[out["data"]["command_id"]]["workspace_id"] == "ws_abc123"
    code, out, _ = cli("runs", "submit", "--input-json", str(request), "--goal", "冲突")
    assert code == 2 and out["error"]["type"] == "usage"


def test_answering_a_route_card_sends_the_quoted_choice_or_returns_the_approval_link(cli, platform):
    _login(cli)
    code, out, _ = cli("runs", "answer", RUN, "--route", "template")
    assert code == 0, out
    assert platform.answers == [{"question_id": "q-route", "answer": {
        "action": "choose_route", "route": "template", "quote_ref": "qr-template", "execution_mode": "autonomous"}}]

    # A route the card offers but the user cannot pick says why; one it never offered is just unknown.
    code, out, _ = cli("runs", "answer", RUN, "--route", "fine")
    assert code == 2 and out["error"]["code"] == "route_unavailable"
    assert "精细制作当前没有可用的制作环境" in out["error"]["message"] and len(platform.answers) == 1
    code, out, _ = cli("runs", "answer", RUN, "--route", "slides")
    assert code == 2 and out["error"]["code"] == "unknown_route"

    platform.over_budget = True
    code, out, _ = cli("runs", "answer", RUN, "--route", "template")
    assert code == 5 and out["error"]["code"] == "approval_required"
    assert out["error"]["details"]["approval_url"] == "https://dev.unfoldx.ai/chat/abc123"

    code, out, _ = cli("runs", "answer", RUN, "--question", "q-route", "--answer-json", '{"action": "x"}')
    assert code == 5  # still over budget: the server decides, the CLI only passes the object through
    platform.over_budget = False
    code, out, _ = cli("runs", "answer", RUN, "--question", "q-other", "--answer", "开头再短一点")
    assert code == 0 and platform.answers[-1] == {"question_id": "q-other", "answer": "开头再短一点"}


def test_a_route_card_with_angles_answers_the_chosen_or_recommended_angle(cli, platform):
    _login(cli)
    platform.angles = [{"id": "angle-1", "title": "为什么不能超光速", "pitch": "从误解切入"},
                       {"id": "angle-2", "title": "贝尔实验", "pitch": "沿着实验走一遍"}]
    code, out, err = cli("runs", "answer", RUN, "--route", "template")
    assert code == 0 and platform.answers[-1]["answer"]["angle_id"] == "angle-1"
    assert "angle-1" in err and "--angle" in err  # the default is said, not silent
    code, out, _ = cli("runs", "answer", RUN, "--route", "template", "--angle", "angle-2")
    assert code == 0 and platform.answers[-1]["answer"]["angle_id"] == "angle-2"
    code, out, _ = cli("runs", "answer", RUN, "--route", "template", "--angle", "angle-9")
    assert code == 2 and out["error"]["code"] == "unknown_angle" and len(platform.answers) == 2
    platform.angles = None
    code, out, _ = cli("runs", "answer", RUN, "--route", "template", "--angle", "angle-1")
    assert code == 2 and out["error"]["code"] == "unknown_angle"


def test_cancel_reports_the_cancelled_run(cli, platform):
    _login(cli)
    code, out, _ = cli("runs", "cancel", RUN)
    assert code == 0 and out["data"]["run"]["state"] == "cancelled"


def test_explicit_command_file_retry_reuses_upload(cli, platform, tmp_path):
    _login(cli)
    file = tmp_path / 'notes.md'
    file.write_text('月球反射太阳光')
    args = ('runs', 'submit', '--goal', '讲月相', '--file', str(file), '--command-id', 'explicit-retry-001')
    first = cli(*args)[1]
    code, again, _ = cli(*args)
    assert code == 0 and again['data']['idempotent_replay'] is True
    assert again['data']['run'] == first['data']['run']
    assert len(platform.objects) == 1


def test_interactive_route_uses_the_web_mode(cli, platform):
    _login(cli)
    code, out, _ = cli('runs', 'answer', RUN, '--route', 'template', '--mode', 'interactive')
    assert code == 0, out
    assert platform.answers[-1]['answer']['execution_mode'] == 'interactive'


def test_api_redirect_never_forwards_a_bearer(cli, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_GET(self):
            if self.path == '/api/v2/me':
                self.send_response(302)
                self.send_header('Location', f'http://localhost:{self.server.server_port}/target')
                self.end_headers()
            else:
                received.append(self.headers.get('Authorization'))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"schema":"unfoldx.v2","ok":true,"data":{}}')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('UNFOLDX_TOKEN', TOKEN)
    try:
        code, out, _ = cli('--api-base', f'http://127.0.0.1:{server.server_port}/api', 'whoami')
        assert received == [], 'credentials reached a different origin'
        assert code == 6 and out['error']['code'] == 'api_redirect'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_parser_and_local_file_errors_are_machine_readable(cli, tmp_path):
    code, out, _ = cli('runs', 'events', RUN, '--limit', 'oops')
    assert code == 2 and out['error']['code'] == 'invalid_arguments'
    _login(cli)
    code, out, _ = cli('runs', 'submit', '--input-json', str(tmp_path/'missing.json'))
    assert code == 2 and out['error']['code'] == 'file_error'


def test_download_preserves_versions_and_requires_explicit_overwrite(cli, tmp_path):
    _login(cli)
    args=('artifacts','download',RUN+'.video','--out',str(tmp_path/'films'))
    code, out, _=cli(*args)
    assert code == 0 and (tmp_path/'films'/RUN/'abc123.mp4').read_bytes() == FILM
    code, out, _=cli(*args)
    assert code == 2 and out['error']['code'] == 'file_exists'
    code, out, _=cli(*args,'--overwrite')
    assert code == 0 and out['data']['sha256'] == hashlib.sha256(FILM).hexdigest()


def test_retry_after_exceeding_deadline_returns_without_early_retry(platform, monkeypatch):
    from unfoldx_cli import api
    from unfoldx_cli.errors import UnfoldxError
    import pytest
    original=platform.handle
    def limited(method,path,query,headers,body):
        if path.endswith('/me'):
            return platform.fail(429,'rate_limited','slow_down',retryable=True,retry_after_sec=90)
        return original(method,path,query,headers,body)
    monkeypatch.setattr(platform,'handle',limited)
    sleeps=[];monkeypatch.setattr(api.time,'sleep',sleeps.append)
    with pytest.raises(UnfoldxError) as caught:
        api.ApiClient(platform.base+'/api',TOKEN,1,client='cli').get('/v2/me')
    assert caught.value.retry_after_sec == 90 and sleeps == []


def test_tty_review_shows_narration_price_and_executable_next_step(capsys):
    from unfoldx_cli.output import Emitter
    Emitter(False).result({'run_id':RUN,'state':'waiting_input','question':{
        'question_id':'q-story','answerable':True,'contract':'unfoldx.storyboard-review.v1',
        'text':'请审阅','storyboard':{'slides':[{'title':'月相','narration':'月亮反射太阳光。','uid':'one'}]},
        'answer_examples':['continue']}})
    text=capsys.readouterr().out
    assert '月亮反射太阳光。' in text and 'unfoldx runs answer' in text and '--question q-story' in text
    assert '"storyboard"' not in text


def test_tty_submit_shows_the_page_where_the_user_can_watch_the_run(capsys):
    from unfoldx_cli.output import Emitter
    Emitter(False).result({'run':{'run_id':RUN,'workspace_id':'ws_abc123','web_url':'https://unfoldx.ai/chat/ws_abc123',
                                  'state':'queued','question':None,'settlement_status':'pending'},'idempotent_replay':False})
    assert '网页: https://unfoldx.ai/chat/ws_abc123' in capsys.readouterr().out


def test_a_corrupt_command_receipt_does_not_start_a_new_run(cli, platform, tmp_path):
    _login(cli)
    args = ('runs', 'submit', '--goal', '不要因本地回执损坏而重开')
    assert cli(*args)[0] == 0
    receipt, = (tmp_path/'config'/'commands').glob('*.json')
    receipt.write_text('{')
    code, out, _ = cli(*args)
    assert code == 6 and out['error']['code'] == 'invalid_command_receipt'
    assert len(platform.commands) == 1


def test_a_corrupt_credential_file_reports_its_real_problem(cli, tmp_path):
    _login(cli)
    (tmp_path/'config'/'credentials.json').write_text('{')
    code, out, _ = cli('whoami')
    assert code == 6 and out['error']['code'] == 'invalid_credential_store'


def test_http_date_retry_after_is_not_retried_early(monkeypatch):
    import io
    from datetime import datetime, timedelta, timezone
    from email.message import Message
    from email.utils import format_datetime
    from urllib.error import HTTPError
    from unfoldx_cli.api import ApiClient
    headers = Message()
    headers['Retry-After'] = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120), usegmt=True)
    error = ApiClient._parse_error(HTTPError('http://localhost', 429, 'rate limited', headers, io.BytesIO(b'not json')))
    assert 119 <= error.retry_after_sec <= 120 and error.retryable


def test_interrupted_overwrite_preserves_the_existing_file(cli, platform, tmp_path):
    _login(cli)
    args = ('artifacts', 'download', RUN+'.video', '--out', str(tmp_path/'films'))
    assert cli(*args)[0] == 0
    dest = tmp_path/'films'/RUN/'abc123.mp4'
    platform.truncate_download = True
    code, out, _ = cli(*args, '--overwrite')
    assert code == 6 and out['error']['code'] == 'download_failed'
    assert dest.read_bytes() == FILM and not list(dest.parent.glob('*.part'))


def test_completed_without_video_shows_the_reply_instead_of_claiming_delivery(capsys):
    from unfoldx_cli.output import Emitter
    Emitter(False).result({'run_id': RUN, 'state': 'completed', 'result': {
        'ready': False, 'artifacts': [], 'message': '本轮成本上限已到，尚未交付。'}})
    text=capsys.readouterr().out
    assert '本轮成本上限已到，尚未交付。' in text and '未交付新视频' in text
    assert 'artifacts download' not in text


def test_resource_commands_require_confirmation_and_send_typed_input(cli, platform, monkeypatch, tmp_path):
    _login(cli)
    original=platform.handle
    def handle(method,path,query,headers,body):
        if path==f'/api/v2/works/{RUN}/share' and method=='DELETE':
            platform.requests.append((method,path,body));return platform.envelope({'run_id':RUN,'shared':False})
        if path=='/api/v2/settings/pronunciations' and method=='PUT':
            platform.requests.append((method,path,body));return platform.envelope({**body,'revision':body['revision']+1})
        return original(method,path,query,headers,body)
    monkeypatch.setattr(platform,'handle',handle)
    code,out,_=cli('shares','revoke',RUN);assert code==2 and out['error']['code']=='confirmation_required'
    code,out,_=cli('shares','revoke',RUN,'--yes');assert code==0 and out['data']['shared'] is False
    assert platform.requests[-1][2]=={'confirm':True}
    document={'revision':0,'entries':[{'term':'UnfoldX','replacement':'开物'}]}
    source=tmp_path/'pronunciations.json';source.write_text(json.dumps(document))
    code,out,_=cli('pronunciations','update','--input-json',str(source))
    assert code==0 and out['data']['revision']==1 and out['data']['entries']==document['entries']
    assert platform.requests[-1][0]=='PUT' and platform.requests[-1][2]==document


def test_superfast_cli_retains_command_identity(cli, platform, monkeypatch):
    _login(cli)
    original=platform.handle
    def handle(method,path,query,headers,body):
        if path=='/api/v2/superfast/jobs':
            assert headers['Idempotency-Key']=='superfast-cli-01'
            assert body=={'chat':'月相'}
            return platform.envelope({'job_id':'sf_123456789012','command_id':headers['Idempotency-Key'],'billing':{'status':'not_metered'}})
        return original(method,path,query,headers,body)
    monkeypatch.setattr(platform,'handle',handle)
    code,out,_=cli('superfast','submit','--chat','月相','--command-id','superfast-cli-01')
    assert code==0 and out['data']['billing']['status']=='not_metered'


def test_export_zip_contains_real_files_and_keeps_existing_archive(cli, platform, tmp_path, monkeypatch):
    import zipfile
    _login(cli)
    original=platform.handle
    def handle(method,path,query,headers,body):
        if path==f'/api/v2/works/{RUN}/exports':
            return platform.envelope({'run_id':RUN,'metadata':{'title':'月相'},'files':[
                {'role':'video','filename':'moon.mp4','url':platform.base+'/files/film.mp4?sig=secret'},
                {'role':'storyboard','filename':'storyboard.json','content':'{"slides":[]}'},
                {'role':'transcript','filename':'transcript.txt','content':'月球反射太阳光'}],
                'unavailable':['subtitles'],'note':'no fabricated timing'})
        if path=='/files/film.mp4':return 200,FILM
        return original(method,path,query,headers,body)
    monkeypatch.setattr(platform,'handle',handle)
    args=('works','export',RUN,'--format','zip','--out',str(tmp_path/'exports'))
    code,out,_=cli(*args);assert code==0,out
    from pathlib import Path
    target=Path(out['data']['files'][0]['path']);original_bytes=target.read_bytes()
    with zipfile.ZipFile(target) as archive:
        assert archive.read('moon.mp4')==FILM
        assert archive.read('transcript.txt').decode()=='月球反射太阳光'
        assert 'sig=secret' not in archive.read('manifest.json').decode()
        assert json.loads(archive.read('manifest.json'))['unavailable']==['subtitles']
    code,out,_=cli(*args);assert code==2 and out['error']['code']=='file_exists'
    assert target.read_bytes()==original_bytes
    code,out,_=cli('works','export',RUN,'--format','subtitles','--out',str(tmp_path/'exports'))
    assert code==5 and out['error']['code']=='export_unavailable'


def test_superfast_download_creates_job_directory_and_preserves_existing_file(cli, platform, tmp_path, monkeypatch):
    from pathlib import Path
    _login(cli)
    original = platform.handle
    def handle(method, path, query, headers, body):
        if path == '/api/v2/superfast/jobs/sf_123456789012/access':
            return platform.envelope({'url': platform.base + '/files/film.mp4', 'billing': {'status': 'not_metered'}})
        if path == '/files/film.mp4':
            return 200, FILM
        return original(method, path, query, headers, body)
    monkeypatch.setattr(platform, 'handle', handle)
    args = ('superfast', 'download', 'sf_123456789012', '--out', str(tmp_path / 'fresh'))
    code, out, _ = cli(*args)
    assert code == 0, out
    target = Path(out['data']['path'])
    assert target.read_bytes() == FILM and target.parent.name == 'sf_123456789012'
    code, out, _ = cli(*args)
    assert code != 0 and target.read_bytes() == FILM


def test_tty_superfast_edit_shows_the_reply_with_the_new_film(capsys):
    from unfoldx_cli.output import Emitter
    Emitter(False).result({'job_id':'sf_123456789012','status':'completed','video_available':True,
                          'parent_job_id':'sf_aaaaaaaaaaaa','reply':'已将第二段的标题改为“升力从哪里来”'})
    text=capsys.readouterr().out
    assert '已将第二段的标题改为“升力从哪里来”' in text and 'superfast download sf_123456789012' in text


def test_tty_superfast_answer_without_a_film_does_not_suggest_a_download(capsys):
    from unfoldx_cli.output import Emitter
    Emitter(False).result({'job_id':'sf_123456789012','status':'completed','video_available':False,
                          'parent_job_id':'sf_aaaaaaaaaaaa','reply':'这部片子一共 5 段'})
    text=capsys.readouterr().out
    assert '这部片子一共 5 段' in text and 'superfast download' not in text


def test_tty_superfast_reports_measured_duration_separately_from_target(capsys):
    from unfoldx_cli.output import Emitter
    Emitter(False).result({'job_id':'sf_123456789012','status':'completed','duration':{
        'duration_seconds':34.1,'target_duration_seconds':20,'delta_ratio':0.705,'duration_warning':'已超过目标'}})
    text=capsys.readouterr().out
    assert '34.1' in text and '20' in text and '70.5%' in text


def test_tty_superfast_download_shows_the_saved_file_without_restarting_wait(capsys):
    from unfoldx_cli.output import Emitter
    Emitter(False).result({'job_id':'sf_123456789012','path':'/tmp/film.mp4','bytes':1200,'sha256':'abc123'})
    text=capsys.readouterr().out
    assert '/tmp/film.mp4' in text and 'abc123' in text and 'superfast wait' not in text


def test_every_request_says_it_comes_from_the_cli(cli, platform, tmp_path):
    _login(cli)
    notes = tmp_path / "notes.pdf"
    notes.write_bytes(b"%PDF-1.4 entanglement notes")
    assert cli("runs", "submit", "--goal", "讲清楚量子纠缠", "--file", str(notes))[0] == 0
    assert cli("artifacts", "download", f"{RUN}.video", "--out", str(tmp_path / "films"))[0] == 0
    assert platform.objects and len(platform.clients) > 5
    assert set(platform.clients) == {"cli"}
