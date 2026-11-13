"""Dual-mode output.

Machine mode (--json or stdout is not a TTY): stdout carries exactly one JSON
object per invocation, the same `unfoldx.v2` envelope the API uses; all
progress/log lines go to stderr. Everything passes through the redaction filter.
"""

from __future__ import annotations

import json
import shlex
import sys

from . import SCHEMA
from .errors import UnfoldxError
from .redact import redact, redact_text


class Emitter:
    def __init__(self, json_mode: bool):
        self.json_mode = json_mode

    def note(self, message: str) -> None:
        print(redact_text(message), file=sys.stderr)

    def result(self, data: dict) -> None:
        if self.json_mode:
            print(json.dumps(redact({"schema": SCHEMA, "ok": True, "data": data}), ensure_ascii=False))
        else:
            print("\n".join(_human(redact(data))))

    def error(self, err: UnfoldxError) -> None:
        if self.json_mode:
            print(json.dumps(redact({"schema": SCHEMA, "ok": False, "error": err.to_payload()}), ensure_ascii=False))
            return
        print(redact_text(f"error[{err.type}/{err.code}]: {err.message}"), file=sys.stderr)
        if err.details:
            print("\n".join(_human(redact(err.details))), file=sys.stderr)
        if err.retryable:
            after = f" after {err.retry_after_sec}s" if err.retry_after_sec else ""
            print(f"hint: this error is retryable{after}.", file=sys.stderr)


def _human(data: dict) -> list[str]:
    """Human output is a view of the same data; never invent answers or infer permission."""
    if 'job_id' in data:
        lines = [f"任务: {data['job_id']}  状态: {data.get('status', '已下载' if data.get('path') else '已提交')}"]
        if (data.get('billing') or {}).get('status') == 'not_metered':
            lines.append('尚未接入积分账本；不是免费承诺，主 Agent 积分预算不适用。')
        if data.get('duration'):
            duration = data['duration']
            measured, target, delta = (duration.get(k) for k in ('duration_seconds', 'target_duration_seconds', 'delta_ratio'))
            lines.append(f"成片时长: {measured if measured is not None else '未知'} 秒；目标: {target if target is not None else '未指定'} 秒。")
            if delta is not None:
                lines.append(f"时长偏差: {delta:+.1%}")
            for key in ('duration_warning', 'duration_note'):
                if duration.get(key):
                    lines.append(duration[key])
        if data.get('path'):
            return lines + [f"已保存: {data['path']}", f"SHA-256: {data.get('sha256', '')}"]
        if data['job_id'].startswith('sf_'):
            if data.get('reply'):
                lines.append('回复: ' + str(data['reply']))
            answered_without_film = data.get('status') == 'completed' and data.get('video_available') is False
            if not answered_without_film and data.get('status') not in ('failed', 'cancelled'):
                lines.append('下一步: unfoldx superfast ' + ('download' if data.get('status')=='completed' else 'wait') + ' ' + shlex.quote(data['job_id']))
        if data.get('timeline'):
            lines.append('时间线: ' + json.dumps(data['timeline'], ensure_ascii=False))
        if data.get('error'):
            lines.append(str(data['error']))
        return lines
    for collection in ('items','kits','voices','styles','accounts'):
        rows = data.get(collection)
        if isinstance(rows,list) and all(isinstance(row,dict) for row in rows):
            lines=[]
            for row in rows:
                identifier=next((row[k] for k in ('material_id','run_id','brand_kit_id','account_id','draft_id','id') if k in row),'')
                label=next((row[k] for k in ('title','filename','name','display_name','account_name') if row.get(k)),'')
                state=row.get('status') or row.get('state') or ''
                lines.append(f'{identifier}  {label}  {state}'.strip())
            if data.get('next_page'):
                lines.append(f"下一页: --page {data['next_page']}（保持当前筛选条件）")
            return lines or ['没有匹配的结果。']
    if "run" in data:
        return _human(data["run"]) + ([f"命令 ID: {data['command_id']}"] if data.get("command_id") else [])
    if "workspaces" in data:
        lines = [f"{row['workspace_id']}  {row.get('title') or '未命名'}  ({row['runs']} 次运行)"
                 for row in data['workspaces']]
        if data.get('next_cursor'):
            args = ['unfoldx', 'workspaces', 'list', '--cursor', data['next_cursor']]
            query = data['query']
            for flag, key in [('--query', 'q'), ('--state', 'state'), ('--limit', 'limit')]:
                if query.get(key):
                    args.extend([flag, str(query[key])])
            lines.append("下一页: " + shlex.join(args))
        return lines or ['没有匹配的工作区。']
    run_id = data.get('run_id')
    lines = []
    if run_id:
        lines.append(f"运行: {run_id}" + (f"  状态: {data['state']}" if data.get('state') else ''))
    if data.get('web_url'):
        lines.append(f"网页: {data['web_url']}")
    if 'charged_credits' in data:
        lines.append(f"已结算积分: {data['charged_credits'] if data['charged_credits'] is not None else '待结算'}")
        for role, hold in data.get('holds', {}).items():
            lines.append(f"{role}: {hold['credits']} 积分 ({hold['status']})")
    question = data.get('question')
    if question:
        lines.extend(_question(question, run_id))
    if 'questions' in data:
        for question in data['questions']:
            lines.extend(_question(question, run_id))
    if data.get('state') in ('queued', 'running') and run_id:
        lines.append('下一步: unfoldx runs wait ' + shlex.quote(run_id))
    result = data.get('result') or data
    if result.get('message'):
        lines.append(result['message'])
    if data.get('state') == 'completed' and result.get('ready') is False:
        lines.append('本轮消息已结束，未交付新视频。请根据回复决定下一条消息。')
    if 'artifacts' in result:
        for artifact in result['artifacts']:
            lines.append('下载: unfoldx artifacts download ' + shlex.quote(artifact['artifact_id']) + ' --out ./out')
    if data.get('path'):
        lines.extend([f"已保存: {data['path']}", f"SHA-256: {data.get('sha256', '')}"])
    if data.get('recovery_command'):
        lines.append('恢复查询: ' + data['recovery_command'])
    if data.get('next_action'):
        lines.append(data['next_action'])
    if data.get('failure'):
        lines.extend(_human(data['failure']))
    return lines or [json.dumps(data, ensure_ascii=False, indent=2)]


def _question(card: dict, run_id: str | None) -> list[str]:
    lines = [card.get('text') or '请确认']
    for quote in (card.get('route_choice') or {}).get('quotes', []):
        lines.append(f"{quote['route']}: 预计 {quote.get('low', '?')}–{quote.get('high', '?')} 积分，预留 {quote['hold']} 积分")
    for i, slide in enumerate((card.get('storyboard') or {}).get('slides', []), 1):
        lines.extend([f"{i}. {slide.get('title') or slide['uid']}", slide.get('spoken_text') or slide.get('narration', '')])
    if not card.get('answerable'):
        lines.append('此卡片需在授权入口处理: ' + card.get('approval_url', ''))
    elif run_id:
        for example in card.get('answer_examples', []):
            flag, value = ('--answer', example) if isinstance(example, str) else ('--answer-json', json.dumps(example, ensure_ascii=False))
            lines.append('可用操作: ' + shlex.join(['unfoldx', 'runs', 'answer', run_id, '--question', card['question_id'], flag, value]))
    return lines
