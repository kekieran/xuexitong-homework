"""Historical records retain metadata only; plan exact referenced files first.

Never sweep a directory. Shared references in surviving records/drafts protect
files, and unreferenced uploads are not cleanup candidates.
"""
from collections import Counter
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
import homework_engine as engine
import homework_school as school

SUMMARY_FIELDS = frozenset(('key', 'title', 'course', 'status', 'availability',
    'deadline', 'deadline_kind', 'deadline_source', 'deadline_reason',
    'deadline_notice_time', 'start', 'first_seen', 'last_seen', 'identities',
    'source_notice_ids', 'entry_url', 'answer_url', 'work_url', 'list_url',
    'metadata_checked_at', 'status_checked_at', 'notice_sent', 'local_settings',
    'group', 'history_cleaned_at', 'source_kind', 'published_at', 'lab_report'))


def summary(record):
    result = {key: value for key, value in record.items() if key in SUMMARY_FIELDS}
    if isinstance(result.get('lab_report'), dict):
        result['lab_report'] = {key: value for key, value in result['lab_report'].items()
            if key in {'origin', 'fid', 'report_id', 'type', 'upload_type', 'course_id', 'belong_type',
                       'is_virtual', 'group_mode', 'status_code', 'status_label', 'read_at'}}
    return result


def platform(record):
    if record.get('source_kind') == 'lab_report':
        return 'lab'
    for field in ('entry_url', 'work_url', 'answer_url', 'list_url'):
        host = urlsplit(record.get(field) or '').hostname or ''
        if school.is_school_host(host) or host == 'istudy.szpu.edu.cn' or host.endswith('.istudy.szpu.edu.cn'):
            return 'school'
        if host == 'chaoxing.com' or host.endswith('.chaoxing.com'):
            return 'chaoxing'
    return None


def login_error(record):
    error = str(record.get('content_error') or '')
    return '需要重新登录' in error or 'LOGIN_REQUIRED' in error


def clear_login_errors(sites):
    """Invalidate only a login error whose own platform has just been verified."""
    with engine.STATE_LOCK:
        state = engine.read_json(engine.STATE_FILE, {})
        cleared = set()
        for record in state.get('assignments', {}).values():
            if login_error(record) and sites.get(platform(record)):
                cleared.add(f'{record.get("course")}：{record["content_error"]}')
                record.pop('content_error', None)
                record.pop('content_attempted_at', None)
        if cleared:
            stats = state.get('last_stats', {})
            if 'warnings' in stats:
                stats['warnings'] = [w for w in stats['warnings'] if w not in cleared]
            engine.save_state(state)
        return len(cleared)


def safe_file(path):
    """Require an actual file beneath the two owned cache folders, no links.

    The strictest of the three "is this the saved attachment" checks: this one
    feeds deletion, so it also refuses symlinks anywhere in the path, while
    homework_app.attachment_path and homework_fill._local_attachment_files only
    resolve the file and confirm it stays inside its folder. Do not relax this
    one to match them.
    """
    data = Path(engine.DATA_DIR).resolve()
    roots = (Path(engine.CONTENT_DIR), data / 'answer_attachments')
    path = Path(path)
    for root in roots:
        # Fail closed if tests/configuration point a cache outside this data dir.
        if root.is_symlink() or not root.resolve().is_relative_to(data) or root.resolve() == data:
            continue
        try:
            relative = path.absolute().relative_to(root.absolute())
        except ValueError:
            continue
        if '..' in relative.parts:
            continue
        current = root
        if any((current := current / part).is_symlink() for part in relative.parts):
            continue
        resolved = path.resolve()
        if resolved.is_relative_to(root.resolve()) and resolved.is_file():
            return resolved
    return None


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = []

    def handle_starttag(self, tag, attrs):
        self.values.extend(v for k, v in attrs if k in ('src', 'href', 'poster', 'data-src') and v)


def references(value):
    """Extract explicit local URLs/paths, including question/option HTML."""
    found = set()

    def accept(text):
        text = unquote(text)
        if text.startswith('/assets/'):
            path = Path(engine.CONTENT_DIR) / 'assets' / urlsplit(text).path[len('/assets/'):]
        elif text.startswith('data/homework_content/') or text.startswith('data/answer_attachments/'):
            path = Path(engine.DATA_DIR) / text[5:]
        elif Path(text).is_absolute():
            path = Path(text)
        else:
            return
        try:
            target = safe_file(path)
            if target:
                found.add(target)
        except (OSError, ValueError):
            pass

    def visit(item):
        if isinstance(item, dict):
            # Attachment identity allows old portable paths to resolve locally.
            if isinstance(item.get('path'), str) and item.get('id'):
                name = Path(item['path']).name
                if Path(name).stem == str(item['id']):
                    accept(str(Path(engine.DATA_DIR) / 'answer_attachments' / name))
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str):
            if '<' in item:
                parser = _Links()
                parser.feed(item)
                for text in parser.values:
                    accept(text)
            elif len(item) < 2048 and '\n' not in item:
                accept(item)

    visit(value)
    return found


def _clear_completed_warnings(state):
    """Remove only warnings attributable to a user's completion marks."""
    stats = state.get('last_stats') or {}
    warnings = stats.get('warnings') or []
    if not warnings:
        return
    completed, remaining = Counter(), Counter()
    overrides = engine.local_overrides()
    aliases = state.get('record_aliases', {})
    for key, record in state.get('assignments', {}).items():
        error = record.get('content_error')
        if not error:
            continue
        canonical = engine.resolve_alias(aliases, key)
        counts = completed if overrides.get(canonical, {}).get('completed') else remaining
        counts[f'{record.get("course")}：{error}'] += 1
    # Several assignments can produce the same course/error string. Keep the
    # copies still needed by unfinished assignments, even if errors were cached.
    current = Counter(w for w in warnings if isinstance(w, str))
    remove = {w: min(count, max(0, current[w] - remaining[w])) for w, count in completed.items()}
    kept = []
    for warning in warnings:
        if isinstance(warning, str) and remove.get(warning, 0):
            remove[warning] -= 1
        else:
            kept.append(warning)
    stats['warnings'] = kept


def _size(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


def plan_cleanup():
    """Read-only plan. Caller holds state/draft locks for an executable plan."""
    state = engine.read_json(engine.STATE_FILE, {'assignments': {}})
    drafts = engine.read_json(engine.DRAFTS_FILE, {})
    next_state, next_drafts = deepcopy(state), deepcopy(drafts)
    aliases = state.get('record_aliases', {})
    # The original error must be matched before history compaction drops it.
    _clear_completed_warnings(next_state)

    def canonical(key):
        return engine.resolve_alias(aliases, key)

    history = {key for key, r in state.get('assignments', {}).items()
               if engine.record_group(dict(r, key=key)) == 'history'}
    removed = []
    for collection in ('assignments', 'merged_records'):
        for key, record in next_state.get(collection, {}).items():
            if canonical(key) in history:
                removed.append(deepcopy(record))
                compact = summary(record)
                compact.setdefault('history_cleaned_at', engine.now_iso())
                next_state[collection][key] = compact
    for key in list(next_drafts):
        if canonical(key) in history:
            removed.append(next_drafts.pop(key))
        else:
            merged = next_drafts[key].get('merged_drafts', {}) if isinstance(next_drafts[key], dict) else {}
            for old_key in list(merged):
                if canonical(old_key) in history:
                    removed.append(merged.pop(old_key))
    pending = next_state.pop('history_cleanup_pending', [])
    candidates = references(removed)
    # Retry only previously planned, exact relative paths after a locked file.
    for name in pending:
        if isinstance(name, str) and not Path(name).is_absolute():
            target = safe_file(Path(engine.DATA_DIR) / name)
            if target:
                candidates.add(target)
    protected = references([next_state, next_drafts])
    files = sorted(candidates - protected)
    return {'state': next_state, 'drafts': next_drafts,
            'state_changed': next_state != state, 'drafts_changed': next_drafts != drafts,
            'history_count': len(history), 'draft_count': len(drafts) - len(next_drafts),
            'files': files, 'bytes': sum(_size(p) for p in files),
            'shared_files': len(candidates & protected)}


def cleanup_history():
    """Apply a fresh exact-reference plan under the writer locks.

    An explicit pending manifest survives interruption/locked files. No cache
    directory enumeration or recursive deletion is performed.
    """
    with engine.browser_operation(), engine.STATE_LOCK, engine.draft_transaction():
        plan = plan_cleanup()
        state = plan['state']
        relative = lambda path: path.relative_to(Path(engine.DATA_DIR).resolve()).as_posix()
        pending = [relative(path) for path in plan['files']]
        if pending:
            state['history_cleanup_pending'] = pending
        if plan['state_changed'] or pending:
            engine.save_state(state)
        if plan['drafts_changed']:
            engine.write_json(engine.DRAFTS_FILE, plan['drafts'])
        remaining = []
        deleted = 0
        for path in plan['files']:
            # Revalidate immediately before unlink; symlinks and traversal fail closed.
            try:
                if safe_file(path) != path:
                    remaining.append(relative(path))
                    continue
                path.unlink()
                deleted += 1
            except OSError:
                remaining.append(relative(path))
        if pending:
            if remaining:
                state['history_cleanup_pending'] = remaining
            else:
                state.pop('history_cleanup_pending', None)
            engine.save_state(state)
        return {'history_count': plan['history_count'], 'draft_count': plan['draft_count'],
                'deleted_files': deleted, 'pending_files': len(remaining),
                'shared_files': plan['shared_files']}


def _history_reference_time(record):
    """Return the most meaningful timestamp to evaluate a history record's age."""
    for field in ('deadline', 'start', 'notice_sent', 'status_checked_at', 'metadata_checked_at', 'history_cleaned_at'):
        dt = engine.parse_datetime(record.get(field))
        if dt is not None:
            return dt
    return None


def _draft_has_user_work(draft):
    if isinstance(draft, str):
        return bool(draft.strip())
    if not isinstance(draft, dict):
        return False
    return bool(draft.get('answers') or draft.get('legacy_text') or draft.get('attachments'))


def cleanup_old_history(days=45):
    """Prune aged history records (>days) without active deadlines or user drafts."""
    from datetime import datetime, timedelta
    try:
        days = max(1, int(days))
    except (TypeError, ValueError):
        days = 45
    now = datetime.now()
    cutoff = now - timedelta(days=days)
    removed_records = 0
    deleted_files = 0
    with engine.browser_operation(), engine.STATE_LOCK, engine.draft_transaction():
        state = engine.read_json(engine.STATE_FILE, {})
        if not isinstance(state, dict) or not isinstance(state.get('assignments'), dict):
            return {'removed_records': 0, 'deleted_files': 0}
        drafts = engine.read_json(engine.DRAFTS_FILE, {})
        if not isinstance(drafts, dict):
            drafts = {}
        overrides = engine.local_overrides()
        next_state = deepcopy(state)
        next_drafts = deepcopy(drafts)
        assignments = next_state.get('assignments') or {}
        removed_objs = []
        removed_keys = set()

        for key, record in list(assignments.items()):
            eff = engine.effective_record(record, overrides)
            group = engine.record_group(eff)
            dl = engine.parse_datetime(eff.get('deadline')) or engine.parse_datetime(record.get('deadline'))
            draft = next_drafts.get(key)
            if group != 'history' or (dl is not None and dl > now) or _draft_has_user_work(draft):
                continue
            ref = _history_reference_time(record)
            if ref is not None and ref < cutoff:
                removed_objs.append(assignments.pop(key))
                removed_keys.add(key)
                removed_records += 1
                if key in next_drafts and not _draft_has_user_work(next_drafts.get(key)):
                    next_drafts.pop(key, None)

        if not removed_records:
            return {'removed_records': 0, 'deleted_files': 0}

        aliases = next_state.get('record_aliases')
        if isinstance(aliases, dict):
            for ak in [k for k, v in aliases.items() if k in removed_keys or v in removed_keys]:
                aliases.pop(ak, None)

        engine.save_state(next_state)
        if next_drafts != drafts:
            engine.write_json(engine.DRAFTS_FILE, next_drafts)

        candidates = references(removed_objs)
        protected = references([next_state, next_drafts])
        for path in candidates - protected:
            try:
                if safe_file(path) != path:
                    continue
                path.unlink()
                deleted_files += 1
            except OSError:
                pass
    return {'removed_records': removed_records, 'deleted_files': deleted_files}
