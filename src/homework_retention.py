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

SUMMARY_FIELDS = frozenset(('key', 'title', 'course', 'status', 'availability',
    'deadline', 'deadline_kind', 'deadline_source', 'deadline_reason',
    'deadline_notice_time', 'start', 'first_seen', 'last_seen', 'identities',
    'source_notice_ids', 'entry_url', 'answer_url', 'work_url', 'list_url',
    'metadata_checked_at', 'status_checked_at', 'notice_sent', 'local_settings',
    'group', 'history_cleaned_at'))


def summary(record):
    return {key: value for key, value in record.items() if key in SUMMARY_FIELDS}


def platform(record):
    for field in ('entry_url', 'work_url', 'answer_url', 'list_url'):
        host = urlsplit(record.get(field) or '').hostname or ''
        if host == 'istudy.szpu.edu.cn' or host.endswith('.istudy.szpu.edu.cn'):
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
            'files': files, 'bytes': sum(p.stat().st_size for p in files),
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
