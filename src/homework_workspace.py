"""UI-independent overview contract (version 1).

All progress describes local drafts, never platform submission. `today` ends at
local midnight; `within_3_days` and `within_7_days` are rolling, overlapping
windows containing only active assignments. Keys reference /api/state records.
"""
from datetime import datetime, timedelta

PROGRESS_STATES = ('unstarted', 'partial', 'complete', 'unread')


def has_value(value):
    if isinstance(value, list):
        return any(has_value(v) for v in value)
    return value is not None and bool(str(value).strip())


def question_complete(question, answer):
    kind, value = question.get('type'), answer.get('value')
    if kind in ('single', 'multiple', 'judgment'):
        options = {str(o.get('value')) for o in question.get('options', [])}
        values = value if isinstance(value, list) else [value]
        return bool(values) and all(has_value(v) and str(v) in options for v in values) and (kind == 'multiple' or len(values) == 1)
    if kind == 'blank':
        count = max(1, int(question.get('blank_count') or 1))
        values = value if isinstance(value, list) else [value]
        return len(values) == count and all(has_value(v) for v in values)
    if kind == 'upload':
        return bool(answer.get('files'))
    if kind == 'essay':
        return has_value(value) or bool(answer.get('files'))
    return False


def answer_progress(record, draft):
    draft = draft if isinstance(draft, dict) else {}
    answers = draft.get('answers') or {}
    questions = record.get('questions') or []
    answered = started = needs_review = 0
    for question in questions:
        answer = answers.get(str(question['id']), {})
        if not (has_value(answer.get('value')) or answer.get('files')):
            continue
        started += 1
        if not question.get('signature') or answer.get('signature') != question['signature']:
            needs_review += 1
        elif question_complete(question, answer):
            answered += 1
        elif question.get('type') not in ('single', 'multiple', 'judgment', 'blank', 'upload', 'essay'):
            needs_review += 1
    any_saved = any(has_value(a.get('value')) or a.get('files') for a in answers.values()) or has_value(draft.get('legacy_text'))
    total = len(questions)
    complete = total > 0 and answered == total and record.get('content_complete') is not False
    state = 'unread' if not total else 'complete' if complete else 'partial' if any_saved else 'unstarted'
    return {'state': state, 'total': total, 'answered': answered, 'started': started,
            'needs_review': needs_review, 'content_complete': record.get('content_complete') is not False,
            'percentage': int(answered * 100 / total + .5) if total else 0,
            'updated_at': draft.get('updated_at')}


def capabilities(record):
    return {'can_open': bool(record.get('answer_url') or record.get('entry_url') or record.get('work_url') or record.get('list_url')),
            'can_reread': record.get('group') != 'history', 'can_manage': True,
            'show_submission_bar': record.get('group') != 'history',
            'can_fill': record.get('group') == 'active' and bool(record.get('questions'))}


def dashboard(records, now=None):
    now = now or datetime.now()
    end_today = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    keys = {name: [] for name in ('active', 'review', 'history', 'today', 'within_3_days', 'within_7_days', *PROGRESS_STATES)}
    answered = total = 0
    for record in sorted(records, key=lambda r: r.get('deadline') or '9999'):
        key, group = record['key'], record['group']
        keys[group].append(key)
        if group != 'active':
            continue
        progress = record['progress']
        keys[progress['state']].append(key)
        answered += progress['answered']
        total += progress['total']
        try:
            deadline = datetime.fromisoformat(record.get('deadline') or '')
            if deadline.tzinfo:
                deadline = deadline.astimezone().replace(tzinfo=None)
        except ValueError:
            continue
        if deadline <= now:
            continue
        if deadline < end_today:
            keys['today'].append(key)
        for days in (3, 7):
            if deadline <= now + timedelta(days=days):
                keys[f'within_{days}_days'].append(key)
    return {'version': 1, 'generated_at': now.isoformat(timespec='seconds'),
            'counts': {name: len(value) for name, value in keys.items()},
            'questions': {'answered': answered, 'total': total}, 'keys': keys}
