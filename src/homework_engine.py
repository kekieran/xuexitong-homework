"""Incremental homework sync. No answer submission endpoints are used here."""
from __future__ import annotations
import base64
import hashlib
import html
import json
import os
import re
import subprocess
import threading
import time
import unicodedata
import ctypes
from contextlib import ExitStack, contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlsplit
from playwright.sync_api import sync_playwright
import homework_dom as dom
import homework_http as http_pool
import homework_runtime as runtime
import homework_shared as shared
import homework_lab as lab
import homework_school as school

_DEFAULT_SYNC_PLAYWRIGHT = sync_playwright

DATA_DIR = runtime.DATA_DIR
PROFILE_DIR = runtime.PROFILE_DIR
CHAOXING_CDP_URL = os.environ.get('CHAOXING_CDP_URL', 'http://127.0.0.1:9222')
STATE_FILE = DATA_DIR / 'state.json'
DRAFTS_FILE = DATA_DIR / 'answer_drafts.json'
OVERRIDES_FILE = DATA_DIR / 'assignment_overrides.json'
_OVERRIDES_CACHE = (None, {})
LOG_FILE = DATA_DIR / 'reminder.log'
LATEST_FILE = DATA_DIR / 'latest_assignments.txt'
CONTENT_DIR = DATA_DIR / 'homework_content'
NOTICE_URL = 'https://notice.chaoxing.com/pc/notice/myNotice'
NOTICE_API_URL = 'https://notice.chaoxing.com/pc/notice/getNoticeList'
COURSES_URL = 'https://mooc1-2.chaoxing.com/course/phone/courselistdata?courseFolderId=0&isFiled=0&query='
STATE_LOCK = threading.RLock()
CONNECTED_BROWSER = None
BACKGROUND_CONTEXTS = {}
PROGRESS = lambda message: None
CONTENT_VERSION = 2
ERROR_BACKOFF = timedelta(minutes=30)   # a failed read is not retried before this
METADATA_MAX_AGE = timedelta(hours=6)   # cached deadline/status is refreshed after this
ACTIVE_LOOKBACK_DAYS = 75
HISTORY_FOLD_DAYS = 45
TOAST_APP_ID = r'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'

def now_iso():
    """当前本地时间戳（秒精度），委托 homework_shared 统一实现。"""
    return shared.iso_timestamp()

def normalize(value):
    return re.sub(r'[《》<>（）()\[\]【】\s:：—_-]+', '', unicodedata.normalize('NFKC', value or '')).casefold()

def safe_error(exc):
    # Playwright request exceptions can include authentication headers in Call log.
    line = str(exc).split('Call log:')[0].splitlines()
    return re.sub(r'https?://\S+', '[请求地址]', line[0] if line else type(exc).__name__)[:180]

def log(message):
    with LOG_FILE.open('a', encoding='utf-8') as f:
        f.write(f'[{now_iso()}] {message}\n')
    PROGRESS(message)

def read_json(path, fallback):
    """读取 JSON 文件；文件不存在返回 fallback，损坏时抛出（委托 homework_shared）。"""
    return shared.read_json_file(path, fallback)

def write_json(path, value):
    """在 STATE_LOCK 内原子写入 JSON；临时名与缩进格式沿用既有约定。

    实际落盘由 homework_shared.write_json_file 完成（临时文件 + 整体替换），
    但写锁语义仍由本模块的 STATE_LOCK 提供，调用方无需额外加锁。
    """
    with STATE_LOCK:
        shared.write_json_file(path, value)

@contextmanager
def process_transaction(name, wait_ms=0):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
    kernel.ReleaseMutex.argtypes = (ctypes.c_void_p,)
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel.CreateMutexW(None, 0, name)
    if not handle:
        raise RuntimeError('无法建立同步锁，请稍后重试')
    acquired = kernel.WaitForSingleObject(handle, wait_ms) in (0, 128)
    try:
        if not acquired:
            raise RuntimeError('另一个作业刷新或填入操作正在进行，请稍后重试')
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)

def browser_operation(wait_ms=30000):
    return process_transaction('Local\\ChaoxingHomeworkBrowserOperation_' + runtime.PROJECT_ID, wait_ms)

def draft_transaction():
    return process_transaction('Local\\ChaoxingHomeworkDraftTransaction_' + runtime.PROJECT_ID, 10000)

def parse_datetime(value):
    """Canonical parser for stored and scraped timestamps.

    Timezone-aware input is converted to local naive time so every comparison in
    this module uses one clock. Two callers intentionally keep their own deadline
    handling: homework_fill._deadline_reason accepts a trailing 'Z' and compares
    timezone-aware, homework_workspace.dashboard normalises only aware values.
    Merge them only after checking those comparisons.
    """
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed
    except (ValueError, TypeError):
        return None

def standard_time(value, reference=None):
    if not value:
        return None
    found = re.search(r'(?:(\d{4})[-/.])?(\d{1,2})[-/.](\d{1,2})[ T\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?', str(value).replace('年', '-').replace('月', '-').replace('日', ''))
    if not found:
        return None
    year, month, day, hour, minute, sec = found.groups()
    ref = parse_datetime(reference) or datetime.now()
    try:
        candidate = datetime(int(year or ref.year), int(month), int(day), int(hour), int(minute), int(sec or 0))
        if not year and not reference and candidate > ref + timedelta(days=180):
            candidate = candidate.replace(year=candidate.year - 1)
        if not year and reference:
            candidates = []
            for y in (ref.year - 1, ref.year, ref.year + 1):
                try:
                    candidates.append(candidate.replace(year=y))
                except ValueError:
                    pass
            candidate = min(candidates, key=lambda x: abs(x - ref))
        return candidate.isoformat(timespec='minutes')
    except ValueError:
        return None

def time_fields(text, reference=None):
    def pick(labels):
        m = re.search(r'(?:' + labels + r')\s*[：:]\s*([^\r\n]+)', text)
        return standard_time(m.group(1), reference) if m else None
    start, end = pick('开始时间|发布时间'), pick('截止时间|结束时间|截止日期')
    date_pattern = r'(?:\d{4}[-/.])?\d{1,2}[-/.]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?'
    interval = re.search(r'作答时间\s*[：:]\s*(' + date_pattern + r')\s*(?:至|~|～|—|到)\s*(' + date_pattern + ')', text)
    if interval:
        start = standard_time(interval.group(1), reference)
        end = standard_time(interval.group(2), start or reference)
    return start, end

def load_state():
    """Load and normalise every stored record.

    Write-only fields: identity_source, source_notice_ids, last_seen,
    deadline_reason, question_count, requirements, content_images and the
    legacy_* snapshots are written for provenance (and read by older builds),
    but no decision in this codebase reads them back - retention.summary() only
    carries them through history compaction. Keep them unless the on-disk format
    is deliberately migrated; they are not dead code to delete.
    """
    with STATE_LOCK:
        state = read_json(STATE_FILE, {'version': 2, 'assignments': {}})
    for key, r in state.setdefault('assignments', {}).items():
        r['key'] = key
        r.setdefault('identities', dom.url_identity(r.get('work_url')))
        r.setdefault('entry_url', r.get('work_url'))
        r.setdefault('deadline_kind', 'known' if parse_datetime(r.get('deadline')) else 'unknown')
        if not isinstance(r.get('questions'), list):
            r['questions'] = []
        if r.get('content_version') != CONTENT_VERSION:
            r.setdefault('content_status', '待重新读取原题')
        for field in ('content_images', 'attachments', 'source_notice_ids'):
            if not isinstance(r.get(field), list):
                r[field] = []
    return state

def save_state(state):
    state['version'] = 2
    write_json(STATE_FILE, state)

def resolve_alias(aliases, key):
    """Follow a record-alias chain to its final key.

    Pure and in-memory on purpose: callers pass the alias map they already hold,
    so a merge published by another process mid-pass cannot desynchronise them.
    """
    seen = set()
    while key in aliases and key not in seen:
        seen.add(key)
        key = aliases[key]
    return key


def local_overrides():
    """Local choices are independent of crawled metadata and survive key aliases."""
    global _OVERRIDES_CACHE
    with STATE_LOCK:
        def stamp(path):
            st = path.stat() if path.exists() else None
            return str(path), st.st_mtime_ns if st else None, st.st_size if st else None
        signature = (stamp(OVERRIDES_FILE), stamp(STATE_FILE))
        if signature != _OVERRIDES_CACHE[0]:
            aliases = read_json(STATE_FILE, {}).get('record_aliases', {})
            resolved = {}
            for key, value in read_json(OVERRIDES_FILE, {}).items():
                key = resolve_alias(aliases, key)
                if key not in resolved or value.get('updated_at', '') > resolved[key].get('updated_at', ''):
                    resolved[key] = value
            _OVERRIDES_CACHE = (signature, resolved)
        return _OVERRIDES_CACHE[1]


def effective_record(item, overrides=None):
    result = dict(item)
    local = (local_overrides() if overrides is None else overrides).get(item.get('key'), {})
    result['local_settings'] = dict(local)
    if local.get('deadline'):
        result.update(deadline=local['deadline'], deadline_kind='known', deadline_source='local')
    return result


def set_local_override(key, completed, deadline):
    global _OVERRIDES_CACHE
    if not isinstance(completed, bool):
        raise ValueError('完成状态格式不正确')
    if deadline is not None and (not isinstance(deadline, str) or not parse_datetime(deadline)):
        raise ValueError('请选择有效的截止日期和时间')
    with STATE_LOCK:
        key = canonical_key(key)
        if key not in load_state().get('assignments', {}):
            raise ValueError('找不到这份作业，请刷新列表')
        values = dict(local_overrides())
        if completed or deadline:
            values[key] = {'completed': completed, 'deadline': shared.iso_timestamp(parse_datetime(deadline), timespec='minutes') if deadline else None, 'updated_at': shared.iso_timestamp(timespec='microseconds')}
        else:
            values.pop(key, None)
        write_json(OVERRIDES_FILE, values)
        _OVERRIDES_CACHE = (None, {})
    return dict(values.get(key, {}))


def record_group(item):
    item = effective_record(item)
    deadline = parse_datetime(item.get('deadline'))
    if item['local_settings'].get('completed') or item.get('status') == 'submitted' or item.get('availability') == 'closed' or (deadline and deadline <= datetime.now()):
        return 'history'
    if item.get('status') == 'pending' and (deadline or item.get('deadline_kind') == 'none'):
        return 'active'
    return 'review'

def active_pending(state):
    return sorted([effective_record(a) for a in state.get('assignments', {}).values() if record_group(a) == 'active'], key=lambda a: a.get('deadline') or '9999')

def match_record(records, candidate):
    identities = set(candidate.get('identities') or [])
    if identities:
        matches = [k for k, r in records.items() if identities.intersection(r.get('identities') or [])]
        if len(matches) == 1:
            return matches[0]
    pair = normalize(candidate.get('course')), normalize(candidate.get('title'))
    matches = []
    for key, r in records.items():
        if (candidate.get('source_kind') == lab.SOURCE_KIND) != (r.get('source_kind') == lab.SOURCE_KIND):
            continue
        if (normalize(r.get('course')), normalize(r.get('title'))) != pair:
            continue
        old = r.get('identities') or []
        # Reused titles must not merge different courses, classes, or IDs of the same type.
        conflict = False
        if identities and old:
            # Only an observed redirect/form can prove taskrefId -> workId aliases.
            if not identities.intersection(old):
                conflict = True
            if not any(a.split(':')[:3] == b.split(':')[:3] for a in identities for b in old):
                conflict = True
            if any(a.rsplit(':', 1)[0] == b.rsplit(':', 1)[0] and a != b for a in identities for b in old):
                conflict = True
        if not conflict:
            matches.append(key)
    return matches[0] if len(matches) == 1 else None

def merge_record(records, candidate):
    """Merge one crawled candidate into `records`, mutating it in place.

    Returns the stored record for convenience; both call sites rely on the
    in-place update, which is why the return value is not read there.
    """
    key = match_record(records, candidate)
    if not key:
        identity = '|'.join(sorted(candidate.get('identities') or [])) or normalize(candidate['course']) + '|' + normalize(candidate['title'])
        key = 'work:' + hashlib.sha256(identity.encode()).hexdigest()[:24]
        records.setdefault(key, {'key': key, 'first_seen': now_iso(), 'questions': [], 'status': 'unknown'})
    record = records[key]
    # A verified removal of the deadline must clear an older date. Historical
    # reminder messages cannot change current platform metadata.
    if (candidate.get('source_kind') == lab.SOURCE_KIND
            and candidate.get('deadline_source') == 'detail'
            and candidate.get('deadline_kind') == 'none'):
        record.update(deadline=None, deadline_kind='none', deadline_source='detail')
        record.pop('availability', None)
    incoming_notice = parse_datetime(candidate.get('notice_sent'))
    known_notice = parse_datetime(record.get('deadline_notice_time') or record.get('notice_sent'))
    stale_notice = bool(incoming_notice and known_notice and incoming_notice < known_notice)
    candidate_deadline = candidate.get('deadline')
    accept_deadline = bool(candidate_deadline)
    if candidate.get('deadline_source') == 'notice' and (record.get('deadline')
            or (record.get('source_kind') == lab.SOURCE_KIND and record.get('deadline_source') == 'detail')):
        if stale_notice:
            accept_deadline = False
        checked = parse_datetime(record.get('metadata_checked_at'))
        if record.get('deadline_source') == 'detail' and (not incoming_notice or (checked and incoming_notice <= checked)):
            accept_deadline = False
    if accept_deadline and record.get('deadline') and candidate_deadline != record['deadline']:
        record['metadata_needs_check'] = True
        if (parse_datetime(candidate_deadline) or datetime.min) > datetime.now():
            record.pop('availability', None)
    for field in ('identities', 'source_notice_ids'):
        record[field] = sorted(set(record.get(field) or []) | set(candidate.get(field) or []))
    for field, value in candidate.items():
        if field in {'identities', 'source_notice_ids', 'key', 'first_seen'} or value is None or value == '' or value == []:
            continue
        if field == 'status' and value == 'unknown' and record.get('status') in {'pending', 'submitted'}:
            continue
        if field in {'deadline', 'deadline_source'} and not accept_deadline:
            continue
        if (field == 'deadline_kind' and candidate.get('source_kind') == lab.SOURCE_KIND
                and ((candidate.get('deadline_source') == 'notice' and not accept_deadline)
                     or (value == 'unknown' and record.get(field) in {'known', 'none'}))):
            continue
        if field in {'entry_url', 'notice_sent'} and stale_notice and record.get(field):
            continue
        if field == 'start' and record.get('start'):
            continue
        if field == 'lab_report':
            record[field] = dict(record.get(field) or {}, **{k: v for k, v in value.items() if v not in (None, '')})
            continue
        record[field] = value
    if parse_datetime(record.get('deadline')):
        record['deadline_kind'] = 'known'
    if accept_deadline and incoming_notice:
        record['deadline_notice_time'] = candidate.get('notice_sent')
    record['last_seen'] = now_iso()
    return record


def merge_draft_answers(base, incoming, *, incoming_wins=False):
    """Merge answer maps with an explicit conflict policy."""
    merged = dict(base or {})
    for qid, answer in (incoming or {}).items():
        if incoming_wins or qid not in merged:
            merged[qid] = answer
    return merged


def merge_draft_records(base, incoming, *, incoming_wins=False):
    """Merge two draft records (answers and legacy_text) with an explicit conflict policy."""
    merged = dict(base or {})
    merged['answers'] = merge_draft_answers(
        merged.get('answers'), (incoming or {}).get('answers'), incoming_wins=incoming_wins)
    old_text = (incoming or {}).get('legacy_text') or ''
    if old_text and old_text not in (merged.get('legacy_text') or ''):
        merged['legacy_text'] = ((merged.get('legacy_text') or '') + '\n\n' + old_text).strip()
    return merged

def reconcile_aliases(state):
    """Coalesce only identities proven by observed URL/form fields; preserve originals."""
    records = state['assignments']
    changed = True
    while changed:
        changed = False
        keys = list(records)
        for index, key in enumerate(keys):
            if key not in records:
                continue
            r = records[key]
            ids = set(r.get('identities') or [])
            for other in keys[index + 1:]:
                if other not in records or not ids.intersection(records[other].get('identities') or []):
                    continue
                pair = [key, other]
                # Legacy keys keep existing answer_drafts links and original image paths valid.
                pair.sort(key=lambda k: (k.startswith('work:'), records[k].get('first_seen') or '9999', k))
                keep, remove = pair
                a, b = records[keep], records[remove]
                # Choose sources before filling missing fields, otherwise copied timestamps
                # can make stale data appear equally fresh.
                best_content = dict(max((a, b), key=lambda x: (bool(x.get('content_complete')), x.get('content_last_read') or '')))
                newest = dict(max((a, b), key=lambda x: x.get('metadata_checked_at') or ''))
                newest_status = dict(max((a, b), key=lambda x: x.get('status_checked_at') or ''))
                state.setdefault('merged_records', {}).setdefault(remove, dict(b))
                state.setdefault('record_aliases', {})[remove] = keep
                for field, value in b.items():
                    if field not in a or a[field] in (None, '', []):
                        a[field] = value
                for field in ('identities', 'source_notice_ids', 'content_images'):
                    a[field] = sorted(set(a.get(field) or []) | set(b.get(field) or []))
                for field in ('questions', 'question_count', 'content_version', 'content_status', 'content_complete', 'content_last_read', 'content_entry_checked', 'requirements', 'answer_url', 'content_warnings', 'attachments'):
                    if field in best_content:
                        a[field] = best_content[field]
                if newest.get('metadata_checked_at'):
                    a.pop('availability', None)
                for field in ('deadline', 'deadline_kind', 'deadline_source', 'metadata_checked_at', 'availability'):
                    if field in newest:
                        a[field] = newest[field]
                if newest_status.get('status') in {'pending', 'submitted'}:
                    a['status'] = newest_status['status']
                    if newest_status.get('status_checked_at'):
                        a['status_checked_at'] = newest_status['status_checked_at']
                entries = [x for x in (a.get('entry_url'), b.get('entry_url')) if x]
                if entries:
                    a['entry_url'] = max(entries, key=lambda u: 'intoexamorwork' in u.lower())
                a['key'] = keep
                with STATE_LOCK, draft_transaction():
                    drafts = read_json(DRAFTS_FILE, {})
                    source = drafts.get(remove)
                    if source:
                        existing = merge_draft_records(load_draft(keep), load_draft(remove))
                        existing.setdefault('merged_drafts', {})[remove] = source
                        store_draft(keep, existing)
                    del records[remove]
                    # Publish the alias while holding the same short cross-process
                    # draft transaction. Autosave must not target a removed key in
                    # the gap between draft migration and state publication.
                    save_state(state)
                changed = True
                break
            if changed:
                break

def open_context(playwright, headless=True):
    global CONNECTED_BROWSER
    if headless:
        # Never read or log in through a visible window, even when CDP exists.
        browser = playwright.chromium.launch(headless=True,
            executable_path=str(runtime.find_browser()), args=['--no-proxy-server'])
        try:
            state = read_json(DATA_DIR / 'browser_session.json', None)
            context = browser.new_context(storage_state=state, viewport={'width': 1440, 'height': 960})
        except Exception:
            browser.close()
            raise
        BACKGROUND_CONTEXTS[id(context)] = browser
        return context
    try:
        browser = runtime.connect_browser_direct(playwright, CHAOXING_CDP_URL, timeout=2500)
    except Exception:
        browser = None
    if browser and browser.contexts:
        runtime.verify_browser_profile(browser)
        CONNECTED_BROWSER = browser
        context = browser.contexts[0]
        state = read_json(DATA_DIR / 'browser_session.json', {})
        if state.get('cookies'):
            context.add_cookies(state['cookies'])
        return context
    CONNECTED_BROWSER = None
    return playwright.chromium.launch_persistent_context(str(PROFILE_DIR), headless=headless,
        executable_path=str(runtime.find_browser()), viewport={'width': 1440, 'height': 960},
        args=['--no-proxy-server', '--profile-directory=Default'])

def close_context(context):
    global CONNECTED_BROWSER
    if getattr(context, 'is_fast_http_context', False) is True:
        context.close()
        return
    owner = BACKGROUND_CONTEXTS.pop(id(context), None)
    if owner is not None:
        try:
            write_json(DATA_DIR / 'browser_session.json', context.storage_state())
        finally:
            owner.close()
        return
    if CONNECTED_BROWSER is None:
        context.close()
    CONNECTED_BROWSER = None

_DEFAULT_OPEN_CONTEXT = open_context
_DEFAULT_CLOSE_CONTEXT = close_context

def request(context, url, **kwargs):
    for attempt in range(2):
        response = None
        try:
            response = context.request.get(url, timeout=15000, **kwargs)
            if response.status >= 400:
                status = response.status
                lab.dispose_response(response)
                raise RuntimeError(f'服务器返回 HTTP {status}')
            return response
        except Exception as exc:
            lab.dispose_response(response)
            if attempt:
                raise RuntimeError(safe_error(exc)) from None
            time.sleep(0.5)

def fetch_html(context, url):
    if not dom.platform_url(url):
        raise ValueError('作业地址不属于已支持的学习通平台')
    response = request(context, url)
    try:
        markup, final = response.text(), response.url
    finally:
        lab.dispose_response(response)
    if dom.is_login(markup, final):
        raise RuntimeError('LOGIN_REQUIRED:' + (urlsplit(url).hostname or ''))
    if not markup.strip():
        raise RuntimeError('服务器返回空页面')
    return markup, final

def parse_notice_attachment(raw):
    """Return the work entry URL carried by a notice attachment, if present."""
    try:
        entries = json.loads(raw) if isinstance(raw, str) else raw or []
    except ValueError:
        return None
    url = None
    for entry in entries if isinstance(entries, list) else []:
        web = entry.get('att_web', {}) if isinstance(entry, dict) else {}
        if isinstance(web, dict) and str(web.get('examOrWork', '')).lower() == 'work':
            url = web.get('url') or url
    return url

def _nonempty_draft_keys():
    """Return canonical assignment keys that currently have saved answers or attachments."""
    drafts = read_json(DRAFTS_FILE, {})
    if not isinstance(drafts, dict):
        return set()
    keys = set()
    for k, v in drafts.items():
        if isinstance(v, str) and v.strip():
            keys.add(k)
        elif isinstance(v, dict) and (v.get('answers') or v.get('legacy_text') or v.get('attachments')):
            keys.add(k)
    return keys


def is_stale_old_row(item, *, now=None, days=HISTORY_FOLD_DAYS, lookback_days=ACTIVE_LOOKBACK_DAYS):
    """True when an unmatched incoming item is an old completed/expired row outside active windows."""
    if not isinstance(item, dict):
        return False
    now = now or datetime.now()
    dl = parse_datetime(item.get('deadline'))
    if dl and dl > now:
        return False
    if (item.get('status') or 'unknown') == 'submitted':
        ref = dl or parse_datetime(item.get('start')) or parse_datetime(item.get('notice_sent'))
        return bool(ref and (now - ref) > timedelta(days=days))
    if dl and dl <= now:
        return (now - dl) > timedelta(days=days)
    ref = parse_datetime(item.get('start')) or parse_datetime(item.get('notice_sent'))
    return bool(ref and (now - ref) > timedelta(days=lookback_days))


def _is_record_active_or_protected(record, key=None, *, now=None, draft_keys=None, overrides=None):
    """True when an existing assignment must not be skipped or pruned as stale history."""
    now = now or datetime.now()
    eff = effective_record(record, overrides)
    if record_group(eff) != 'history':
        return True
    dl = parse_datetime(eff.get('deadline')) or parse_datetime(record.get('deadline'))
    if dl and dl > now:
        return True
    if draft_keys and key and key in draft_keys:
        return True
    return False


def _apply_renwu_fallback(record, fallback):
    record['source_kind'] = 'renwu_task'
    record['renwu_task'] = fallback.get('renwu_task') or {}
    answer_url = fallback.get('answer_url') or record.get('work_url') or record.get('entry_url') or ''
    if answer_url:
        record['answer_url'] = answer_url
        record['work_url'] = answer_url
    if fallback.get('title'):
        record['title'] = fallback['title']
    if fallback.get('start'):
        record['start'] = fallback['start']
    if fallback.get('deadline'):
        record['deadline'] = fallback['deadline']
        record['deadline_kind'] = 'known'
        record['deadline_reason'] = None
        record['deadline_source'] = 'page'
    card_status = fallback.get('card_status')
    if card_status == 'submitted':
        record['status'] = 'submitted'
    elif card_status == 'pending' and record.get('status') in {None, '', 'unknown'}:
        record['status'] = 'pending'
    elif record.get('status') in {None, '', 'unknown'}:
        record['status'] = 'pending'
    if fallback.get('identity'):
        record['identities'] = sorted(set(record.get('identities') or []) | {fallback['identity']})
    q = fallback.get('question')
    if q:
        record['questions'] = [q]
        record['requirements'] = q.get('text') or ''
        record['question_count'] = 1
    record['attachments'] = list(fallback.get('attachments') or [])
    record['content_version'] = CONTENT_VERSION
    record['content_status'] = 'ready'
    record['content_complete'] = True
    record['content_warnings'] = []
    now_stamp = now_iso()
    record['content_last_read'] = now_stamp
    record['metadata_checked_at'] = now_stamp
    record.pop('metadata_needs_check', None)


def _handle_read_error(state, record, key, exc, verified_sites, stats):
    from homework_navigation import NotInClassError
    if isinstance(exc, NotInClassError):
        try:
            import homework_accounts as accounts
            import homework_retention as retention
            if accounts.is_isolated() and verified_sites.get(retention.platform(record)):
                draft = None
                with STATE_LOCK, draft_transaction():
                    drafts = read_json(DRAFTS_FILE, {})
                    draft = drafts.pop(key, None)
                    if draft is not None:
                        write_json(DRAFTS_FILE, drafts)
                accounts.archive_quarantined_record(key, copy.deepcopy(record), draft)
                state['assignments'].pop(key, None)
                aliases = state.get('record_aliases') or {}
                for alias_key, target in list(aliases.items()):
                    if target == key:
                        aliases.pop(alias_key, None)
                stats['warnings'].append(f'已清理非当前账号课程作业：{record.get("course")} / {record.get("title")}')
                return
        except Exception:
            pass
    reason = safe_error(exc)
    record['content_error'] = '该课程的平台需要重新登录' if 'LOGIN_REQUIRED' in reason else reason
    stats['warnings'].append(f'{record.get("course")}：{record["content_error"]}')


def collect_notices(context, state, stats, *, _sink=None):
    seen = set(state.get('notice_seen') or [])
    cursor, used, fresh_ids = '', set(), []
    page_limit = 15 if not state.get('notice_backfilled') else 5
    ended = False
    lab_replay = not state.get('lab_notices_scanned')
    lab_cutoff = datetime.now() - timedelta(days=30)
    lookback_cutoff = datetime.now() - timedelta(days=ACTIVE_LOOKBACK_DAYS)
    course_meta = state.setdefault('course_meta', {})
    pending_cursors = list(state.get('notice_pending_cursors') or [])
    if state.get('notice_backfill_cursor') and state['notice_backfill_cursor'] not in pending_cursors:
        pending_cursors.append(state['notice_backfill_cursor'])
    school_discovered = False
    for page_index in range(page_limit):
        try:
            response = context.request.post(NOTICE_API_URL, form={
                'type': '0', 'notice_type': '0', 'lastValue': cursor, 'sort': '0', 'folderUUID': '',
                'kw': '', 'startTime': '', 'endTime': '', 'gKw': '', 'gName': '', 'year': '', 'tag': '',
                'fidsCode': '', 'filterSenderPuids': '', 'filterTags': ''}, timeout=18000,
                headers={'Origin': 'https://notice.chaoxing.com', 'Referer': NOTICE_URL, 'X-Requested-With': 'XMLHttpRequest'})
        except Exception as exc:
            if not fresh_ids:
                raise RuntimeError(safe_error(exc)) from None
            stats['warnings'].append('通知分页暂未完成，下次从中断位置继续')
            break
        stats['notice_pages'] += 1
        try:
            payload = response.json()
        except Exception:
            raise RuntimeError('通知读取失败，可能需要重新登录') from None
        finally:
            lab.dispose_response(response)
        container = payload.get('notices') if isinstance(payload, dict) else None
        # The real API terminates pagination with {"status": true}, without a
        # notices object. Only accept that form after a nonempty cursor.
        if cursor and isinstance(payload, dict) and payload.get('status') is True and container is None:
            container = {'list': [], 'lastPage': 1}
        if not isinstance(payload, dict) or not payload.get('status') or not isinstance(container, dict):
            raise RuntimeError('通知读取失败，可能需要重新登录')
        notices = container.get('list') or []
        ids = [str(n.get('idCode')) for n in notices if n.get('idCode')]
        for n in notices:
            candidate = lab.parse_notice(n)
            if candidate:
                if _sink is not None:
                    _sink.append(('lab', candidate))
                else:
                    existing = match_record(state['assignments'], candidate)
                    if lab.should_import(candidate, existing=bool(existing)):
                        merge_record(state['assignments'], candidate)
                continue
            content = str(n.get('content') or '')
            def field(label):
                m = re.search(re.escape(label) + r'\s*[：:]\s*([^\r\n]+)', content)
                return m.group(1).strip() if m else ''
            course, title = field('课程名称'), field('作业名称')
            if not course or not title:
                continue
            url = parse_notice_attachment(n.get('attachment'))
            if url:
                if school.note_notice_url(url, context):
                    school_discovered = True
            start, deadline = time_fields(content)
            item = {'course': course, 'title': title, 'start': start, 'deadline': deadline,
                    'deadline_source': 'notice' if deadline else None,
                    'notice_sent': standard_time(n.get('sendTime')), 'entry_url': url,
                    'identities': dom.url_identity(url),
                    'source_notice_ids': [str(n['idCode'])] if n.get('idCode') else [], 'status': 'unknown'}
            existing = match_record(state['assignments'], item)
            if not existing and is_stale_old_row(item):
                continue
            c_id, cl_id = course_ids(item)
            if c_id and cl_id and _sink is None:
                cm = course_meta.get(f'{c_id}:{cl_id}')
                if isinstance(cm, dict):
                    cm['dormant'] = False
                    cm['empty_streak'] = 0
            if _sink is not None:
                _sink.append(('work', item))
            else:
                merge_record(state['assignments'], item)
        fresh_ids.extend(ids)
        sent = [parse_datetime(standard_time(n.get('sendTime'))) for n in notices]
        valid_sent = [t for t in sent if t]
        old_cutoff_reached = bool(valid_sent) and all(t < lookback_cutoff for t in valid_sent)
        if lab_replay and (not notices or (sent and all(t and t < lab_cutoff for t in sent))
                           or str(container.get('lastPage')) == '1' or old_cutoff_reached):
            lab_replay = False
            state['lab_notices_scanned'] = True
        page_known = bool(ids) and all(i in seen for i in ids)
        at_end = not notices or str(container.get('lastPage')) == '1' or (old_cutoff_reached and not lab_replay)
        next_cursor = str(container.get('lastGetId') or '')
        if (page_known or at_end) and pending_cursors:
            next_cursor = pending_cursors.pop(0)
        elif at_end or (state.get('notice_backfilled') and page_known and not lab_replay):
            ended = True
            break
        if not next_cursor or next_cursor in used:
            ended = True
            break
        used.add(next_cursor)
        cursor = next_cursor
    state['notice_seen'] = list(dict.fromkeys(fresh_ids + list(seen)))[:1200]
    if ended:
        state['notice_backfilled'] = True
        state.pop('notice_backfill_cursor', None)
        state.pop('notice_pending_cursors', None)
    else:
        state['notice_backfill_cursor'] = cursor
        state['notice_pending_cursors'] = list(dict.fromkeys([cursor] + pending_cursors))
        stats['warnings'].append('通知较多，本次达到分页上限，已有作业仍保留。')
    if school_discovered:
        try:
            import homework_auth
            homework_auth.status(restore=True, context=context, progress=PROGRESS)
        except Exception:
            pass

def course_ids(record):
    """(courseId, classId) of the course that owns this record, or None."""
    for name in ('entry_url', 'work_url', 'list_url', 'answer_url'):
        course, clazz = dom.course_class_pair(record.get(name))
        if course and clazz:
            return course, clazz
    return None


def _collect_lab_targets_flat(context, state, records_view, work_items, *, _sink=None):
    """Run lab status queries in one flat HTTP-pooled executor and skip redundant cold-course submitted scans."""
    now = datetime.now()
    active_courses = set()
    for record in records_view.values():
        if record.get('source_kind') == lab.SOURCE_KIND and record_group(record) != 'history':
            info = record.get('lab_report') or {}
            active_courses.add((str(info.get('fid') or ''), str(info.get('course_id') or '0')))
    lab_meta = state.get('lab_meta') if isinstance(state.get('lab_meta'), dict) else {}

    flat_jobs = []
    submitted_requested = set()
    for idx, (target, origin) in enumerate(work_items):
        trusted = lab.trusted_origin(origin)
        fid = lab._identifier(target.get('fid'))
        course_id = lab._identifier(target.get('course_id'))
        if not trusted or not fid:
            continue
        meta_key = f'{fid}:{course_id}'
        meta_entry = lab_meta.get(meta_key) if isinstance(lab_meta.get(meta_key), dict) else {}
        prev_submitted = parse_datetime(meta_entry.get('submitted_checked_at'))
        filters = ['2']
        if course_id == '0' or (fid, course_id) in active_courses or not prev_submitted or (now - prev_submitted) > METADATA_MAX_AGE:
            filters.append('1')
            submitted_requested.add(idx)
        for sf in filters:
            flat_jobs.append((idx, trusted, fid, course_id, sf))

    def _run_job(job):
        idx, trusted, fid, course_id, sf = job
        try:
            rows_out, job_warnings = lab._collect_status_pages(context, trusted, fid, course_id, sf, None)
            return idx, sf, rows_out, job_warnings, None
        except Exception as exc:
            return idx, sf, [], [], safe_error(exc)

    grouped = {idx: {'rows': [], 'warnings': [], 'err': ''} for idx in range(len(work_items))}
    if flat_jobs:
        with ThreadPoolExecutor(max_workers=min(6, len(flat_jobs))) as pool:
            for idx, sf, rows_out, job_warnings, exc_msg in pool.map(_run_job, flat_jobs):
                bucket = grouped[idx]
                bucket['rows'].extend(rows_out)
                bucket['warnings'].extend(job_warnings)
                if exc_msg and not bucket['err']:
                    bucket['err'] = exc_msg

    checked_iso = now_iso()
    results = []
    for idx, (target, _origin) in enumerate(work_items):
        bucket = grouped[idx]
        candidates = {}
        for cand in bucket['rows']:
            canonical = cand['identities'][0]
            prev = candidates.get(canonical)
            if not prev or cand['status'] == 'submitted' or prev['status'] != 'submitted':
                candidates[canonical] = cand
        warnings = list(dict.fromkeys(bucket['warnings']))
        err_msg = bucket['err'] or (warnings[0] if warnings else '')
        if idx in submitted_requested and not err_msg:
            fid = lab._identifier(target.get('fid'))
            course_id = lab._identifier(target.get('course_id'))
            meta_key = f'{fid}:{course_id}'
            if _sink is not None:
                _sink.append(('lab_meta', (meta_key, checked_iso)))
            else:
                state.setdefault('lab_meta', {})[meta_key] = {'submitted_checked_at': checked_iso}
        results.append((target, list(candidates.values()), warnings, err_msg))
    return results


def collect_lab_reports(context, state, stats, only=None, *, _snapshot=None, _sink=None):
    """Use the ordinary assignment store; only eligible reports get details."""
    from homework_lab_auth import ensure_session
    from homework_lab_courses import discover_courses
    targets = {}
    records_view = _snapshot if _snapshot is not None else state['assignments']

    def add(target):
        origin, fid = lab.trusted_origin(target.get('origin')), str(target.get('fid') or '')
        if origin and fid:
            targets[(origin, fid, str(target.get('course_id') or '0'))] = dict(target, origin=origin, fid=fid)

    if not only:
        discovered, warnings = discover_courses(context)
        stats['warnings'].extend(warnings)
        for target in discovered:
            add(target)
        active_origin = school.school_origin(lab.DEFAULT_ORIGIN)
        active_fid = school.school_fid(lab.DEFAULT_FID)
        if active_origin and active_fid:
            add({'origin': active_origin, 'fid': active_fid, 'course_id': '0'})
    for key, record in records_view.items():
        if only and key != only:
            continue
        if record.get('source_kind') == lab.SOURCE_KIND:
            add(record.get('lab_report') or {})
        elif not only:
            for field in ('entry_url', 'work_url', 'list_url', 'answer_url'):
                raw = record.get(field) or ''
                course_id = dom.query_params(raw).get('courseid')
                host = urlsplit(raw).hostname
                if (host == 'mooc.istudy.szpu.edu.cn' or school.is_school_host(host)) and course_id:
                    add({'origin': f'https://{host}', 'fid': school.school_fid(lab.DEFAULT_FID),
                         'course_id': course_id, 'course': record.get('course', '')})

    session_keys = list(dict.fromkeys((t['origin'], t['fid']) for t in targets.values()))
    sessions = {}
    if http_pool.supports_concurrency(context) and len(session_keys) > 1:
        def _resolve_session(sk):
            try:
                return ensure_session(context, *sk)
            except Exception as exc:
                return RuntimeError(safe_error(exc))
        with ThreadPoolExecutor(max_workers=min(4, len(session_keys))) as pool:
            for sk, res in zip(session_keys, pool.map(_resolve_session, session_keys)):
                sessions[sk] = res
    else:
        for sk in session_keys:
            try:
                sessions[sk] = ensure_session(context, *sk)
            except Exception as exc:
                sessions[sk] = RuntimeError(safe_error(exc))

    checked, work_items, pre_errors = set(), [], []
    for target in targets.values():
        res = sessions[(target['origin'], target['fid'])]
        if isinstance(res, Exception):
            pre_errors.append((target, safe_error(res)))
            continue
        origin = res
        course_key = (origin, target['fid'], str(target.get('course_id') or '0'))
        if course_key in checked:
            continue
        checked.add(course_key)
        work_items.append((target, origin))

    def _collect_one(target, origin):
        try:
            candidates, warnings = lab.collect_reports(context, [dict(target, origin=origin)])
            return target, candidates, warnings, (warnings[0] if warnings else '')
        except Exception as exc:
            return target, [], [], safe_error(exc)

    if (
        not only
        and http_pool.supports_concurrency(context)
        and len(work_items) > 1
        and lab.collect_reports is getattr(lab, '_DEFAULT_COLLECT_REPORTS', lab.collect_reports)
    ):
        results = _collect_lab_targets_flat(context, state, records_view, work_items, _sink=_sink)
    elif http_pool.supports_concurrency(context) and len(work_items) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(work_items))) as pool:
            results = list(pool.map(lambda pair: _collect_one(*pair), work_items))
    else:
        results = [_collect_one(target, origin) for target, origin in work_items]

    for target, candidates, warnings, err_msg in results:
        stats['course_lists'] += 1
        stats['warnings'].extend(warnings)
        for candidate in candidates:
            if not candidate.get('course'):
                candidate['course'] = target.get('course') or target.get('title') or '未命名课程'
            if _sink is not None:
                _sink.append(('lab_row', candidate))
            else:
                existing = match_record(state['assignments'], candidate)
                if only and existing != only:
                    continue
                if lab.should_import(candidate, existing=bool(existing)):
                    merge_record(state['assignments'], candidate)
        if err_msg:
            reason = '该课程的平台需要重新登录' if 'LOGIN_REQUIRED' in err_msg else err_msg
            if not warnings or reason != warnings[0]:
                stats['warnings'].append(reason)
            if _sink is not None:
                _sink.append(('lab_err', (target, reason)))
            else:
                for record in state['assignments'].values():
                    info = record.get('lab_report') or {}
                    if (record.get('source_kind') == lab.SOURCE_KIND and record_group(record) != 'history'
                            and info.get('fid') == target['fid']
                            and str(info.get('course_id') or '0') == str(target.get('course_id') or '0')):
                        record['content_error'] = reason

    for target, reason in pre_errors:
        reason = '该课程的平台需要重新登录' if 'LOGIN_REQUIRED' in reason else reason
        stats['warnings'].append(reason)
        if _sink is not None:
            _sink.append(('lab_err', (target, reason)))
        else:
            for record in state['assignments'].values():
                info = record.get('lab_report') or {}
                if (record.get('source_kind') == lab.SOURCE_KIND and record_group(record) != 'history'
                        and info.get('fid') == target['fid']
                        and str(info.get('course_id') or '0') == str(target.get('course_id') or '0')):
                    record['content_error'] = reason


def _known_targets_for_only(records_view, only):
    """Resolve `(host, cid, clazz) -> course_name` from local records for single-course refresh."""
    targets = {}
    for r in records_view.values():
        if course_ids(r) != only:
            continue
        for name in ('list_url', 'work_url', 'answer_url', 'entry_url'):
            raw = r.get(name) or ''
            u = urlsplit(raw)
            host = u.hostname or ''
            if not host or 'notice.' in host or 'passport' in host:
                continue
            if host == 'mooc1.chaoxing.com' or host == 'mooc.istudy.szpu.edu.cn' or school.is_school_host(host):
                targets[(host, only[0], only[1])] = r.get('course', '') or targets.get((host, only[0], only[1]), '')
                break
    return targets


def collect_course_rows(context, state, stats, only=None, *, _snapshot=None, _sink=None, _meta_out=None):
    """Read course task lists; `only` restricts it to one (courseId, classId)."""
    records_view = _snapshot if _snapshot is not None else state['assignments']
    known_only = _known_targets_for_only(records_view, only) if only is not None else {}
    if known_only:
        targets = known_only
    else:
        try:
            markup, _ = fetch_html(context, COURSES_URL)
            courses = dom.parse(markup).all('li', attr='courseid')
        except Exception:
            # A one-course refresh may target a school-hosted course; that write
            # must not depend on the separate chaoxing course list.
            if only is None:
                raise
            courses = []
        if not courses and only is None:
            raise RuntimeError('课程列表结构无法识别，请检查登录状态')
        targets = {}
        for course in courses:
            cid, clazz = course.attrs.get('courseid'), course.attrs.get('clazzid')
            if cid and clazz:
                targets[('mooc1.chaoxing.com', cid, clazz)] = dom.clean_text(course.first('dt') or course)
        # School-hosted courses have a separate login and are absent from the main list.
        # Every stored link is inspected: a record's entry may be a notice URL while
        # its work page is the school host, and only the latter carries the ids.
        for r in records_view.values():
            if record_group(r) == 'history':
                continue
            for name in ('entry_url', 'work_url', 'list_url', 'answer_url'):
                raw = r.get(name) or ''
                u = urlsplit(raw)
                course, clazz = dom.course_class_pair(raw)
                if (u.hostname == 'mooc.istudy.szpu.edu.cn' or school.is_school_host(u.hostname)) and course and clazz:
                    targets[(u.hostname, course, clazz)] = r.get('course', '')

    now = datetime.now()
    draft_keys = _nonempty_draft_keys()
    overrides = local_overrides()
    protected_course_keys = set()
    existing_latest_by_course = {}
    for k, r in records_view.items():
        c_id, cl_id = course_ids(r)
        if not c_id or not cl_id:
            continue
        ck = f'{c_id}:{cl_id}'
        if _is_record_active_or_protected(r, k, now=now, draft_keys=draft_keys, overrides=overrides):
            protected_course_keys.add(ck)
        for d_str in (r.get('deadline'), r.get('start'), r.get('notice_sent')):
            dt = parse_datetime(d_str)
            if dt and (ck not in existing_latest_by_course or dt > existing_latest_by_course[ck]):
                existing_latest_by_course[ck] = dt

    course_meta = state.setdefault('course_meta', {})
    course_items = []
    for (host, cid, clazz), name in targets.items():
        if only and (cid, clazz) != only:
            continue
        ck = f'{cid}:{clazz}'
        meta = course_meta.get(ck) if isinstance(course_meta.get(ck), dict) else {}
        if only is None and meta.get('dormant') and ck not in protected_course_keys:
            if _meta_out is not None:
                _meta_out.setdefault('skipped_dormant', set()).add((cid, clazz))
            continue
        course_items.append(((host, cid, clazz), name))
    total = len(course_items)

    def _fetch_course(item):
        (host, cid, clazz), name = item
        try:
            markup, base = fetch_html(context, f'https://{host}/work/task-list?courseId={cid}&classId={clazz}&vx=1')
            rows = []
            for n in dom.parse(markup).all('li', attr='data'):
                url = html.unescape(urljoin(base, n.attrs['data']))
                if 'type=work' not in url:
                    continue
                title = dom.clean_text(n.first('p') or n)
                status = dom.explicit_status('\n'.join(dom.clean_text(s) for s in n.all('span')))
                candidate = {'course': name, 'title': title, 'status': status, 'list_url': url,
                             'identities': dom.url_identity(url), 'status_checked_at': now_iso()}
                start, deadline = time_fields(dom.clean_text(n))
                candidate.update(start=start, deadline=deadline)
                rows.append(candidate)
            return (host, cid, clazz), name, rows, None
        except Exception as exc:
            return (host, cid, clazz), name, None, f'{name}：{safe_error(exc)}'

    ordered_results = [None] * total
    if http_pool.supports_concurrency(context) and total > 1:
        progress_lock = threading.Lock()
        done_count = 0
        with ThreadPoolExecutor(max_workers=min(8, total)) as pool:
            future_map = {pool.submit(_fetch_course, item): idx for idx, item in enumerate(course_items)}
            for fut in as_completed(future_map):
                idx = future_map[fut]
                res = fut.result()
                ordered_results[idx] = res
                item_name = course_items[idx][1]
                with progress_lock:
                    done_count += 1
                    if only is None and total > 1 and item_name:
                        PROGRESS(f'正在核对课程（{done_count}/{total}）：{item_name}')
    else:
        for idx, item in enumerate(course_items):
            name = item[1]
            if only is None and total > 1 and name:
                PROGRESS(f'正在核对课程（{idx + 1}/{total}）：{name}')
            ordered_results[idx] = _fetch_course(item)

    lookback_cutoff = now - timedelta(days=ACTIVE_LOOKBACK_DAYS)
    meta_target = _meta_out.setdefault('course_meta', {}) if _meta_out is not None else course_meta
    for (_host, cid, clazz), _name, rows, warn in ordered_results:
        if warn:
            stats['warnings'].append(warn)
            continue
        stats['course_lists'] += 1
        ck = f'{cid}:{clazz}'
        row_dates = [dt for row in rows for dt in (parse_datetime(row.get('deadline')), parse_datetime(row.get('start'))) if dt]
        latest_dt = max(row_dates) if row_dates else existing_latest_by_course.get(ck)
        prev_meta = course_meta.get(ck) if isinstance(course_meta.get(ck), dict) else {}
        has_active_row = any(
            (row.get('status') or 'unknown') != 'submitted' and not is_stale_old_row(row, now=now)
            for row in rows
        )
        is_protected = ck in protected_course_keys or has_active_row
        if rows:
            empty_streak = 0
            dormant = bool(not is_protected and latest_dt and latest_dt < lookback_cutoff)
        else:
            empty_streak = 0 if is_protected else int(prev_meta.get('empty_streak') or 0) + 1
            dormant = bool(not is_protected and ((latest_dt and latest_dt < lookback_cutoff) or empty_streak >= 2))
        if only is not None:
            dormant = False
            empty_streak = 0
        meta_target[ck] = {
            'name': _name or prev_meta.get('name') or '',
            'latest_activity_at': shared.iso_timestamp(latest_dt) if latest_dt else prev_meta.get('latest_activity_at', ''),
            'last_checked_at': now_iso(),
            'empty_streak': empty_streak,
            'dormant': dormant,
        }
        for candidate in rows:
            existing = match_record(records_view, candidate)
            if only is None and not existing and is_stale_old_row(candidate, now=now):
                continue
            if _sink is not None:
                _sink.append(candidate)
            else:
                merge_record(state['assignments'], candidate)

IMAGE_MIME_SUFFIX = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/webp': '.webp'}


def image_mime(data):
    """Identify an image from its magic bytes, ignoring a wrong Content-Type.

    The school image server sometimes labels JPEG bytes as image/png, so the
    bytes win over the header. Shared with homework_ai, which forwards the same
    bytes to a vision model, so both agree on what a file actually is.
    """
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def asset_downloader(context):
    def download(url):
        u = urlsplit(url)
        host = u.hostname or ''
        if u.scheme not in {'https', 'http'} or not (host.endswith(('.chaoxing.com', '.chaoxing.com.cn', '.chaoxing.net')) or host in {'mooc.istudy.szpu.edu.cn', 'cs.istudy.szpu.edu.cn'} or school.is_asset_host(host)):
            return url
        name = hashlib.sha256(url.encode()).hexdigest()[:32]
        folder = CONTENT_DIR / 'assets'
        folder.mkdir(parents=True, exist_ok=True)
        existing = list(folder.glob(name + '.*'))
        if existing:
            return '/assets/' + existing[0].name
        response = None
        try:
            response = request(context, url)
            suffix = IMAGE_MIME_SUFFIX.get(response.headers.get('content-type', '').split(';')[0])
            data = response.body()
            # Sniffed bytes win over the header; an unknown format keeps the
            # header-derived suffix (or none) exactly as before.
            suffix = IMAGE_MIME_SUFFIX.get(image_mime(data)) or suffix
            if suffix and len(data) <= 12_000_000:
                dest = folder / (name + suffix)
                if not dest.exists():
                    tmp = folder / f'.{name}.{os.getpid()}.{threading.get_ident()}.tmp'
                    try:
                        tmp.write_bytes(data)
                        tmp.replace(dest)
                    finally:
                        tmp.unlink(missing_ok=True)
                return '/assets/' + name + suffix
        except Exception:
            pass
        finally:
            lab.dispose_response(response)
        return url
    return download

def update_metadata(record, markup, url):
    if re.search(r'/(?:getAllWork|task-list|courselistdata)(?:/|$)', urlsplit(url).path, re.I):
        raise RuntimeError('入口仍在课程作业列表，尚未定位到这份作业的详情')
    root = dom.parse(markup)
    text = dom.clean_text(root)
    start, deadline = time_fields(text, record.get('start'))
    if start:
        record['start'] = start
    if deadline:
        record.update(deadline=deadline, deadline_kind='known', deadline_source='detail')
        record.pop('metadata_needs_check', None)
    elif re.search(r'(?:截止|结束)(?:时间|日期)?\s*[：:]?\s*(?:不限|无限制|永久|无截止|未设置)|不限时|无截止时间', text):
        record.update(deadline=None, deadline_kind='none', deadline_source='detail')
    if re.search(r'作业已截止|作业已结束|超过截止时间|不在作答时间|本课程已结课|作业不支持作答', text):
        record['availability'] = 'closed'
    elif re.search(r'/selectworkquestionyipiyue(?:/|$)', urlsplit(url).path, re.I):
        record['availability'] = 'closed'
    elif re.search(r'/do(?:home)?work', urlsplit(url).path, re.I) and ((parse_datetime(record.get('deadline')) or datetime.min) > datetime.now() or record.get('deadline_kind') == 'none'):
        record.pop('availability', None)
    if not deadline and record.get('deadline_kind') != 'none' and not record.get('deadline'):
        record['deadline_reason'] = '平台页面未显示截止时间，暂列待核实'
    if re.search(r'作业已提交|您已提交|提交成功', text):
        record['status'] = 'submitted'
    record['identities'] = sorted(set(record.get('identities') or []) | set(dom.page_identity(markup, url)))
    record['metadata_checked_at'] = now_iso()
    return text

def read_content(context, record, stats):
    if record.get('source_kind') == lab.SOURCE_KIND:
        from homework_lab_auth import ensure_session
        target = record.get('lab_report') or {}
        origin = ensure_session(context, target.get('origin'), target.get('fid'))
        current = dict(record, lab_report=dict(target, origin=origin))
        stats['details'] += 1
        patch = lab.read_report(context, current)
        if patch.get('questions'):
            patch['content_version'] = CONTENT_VERSION
        record.update(patch)
        record.pop('metadata_needs_check', None)
        return
    url = record.get('entry_url') or record.get('list_url') or record.get('work_url')
    if not url:
        record['content_status'] = '缺少作答入口，等待通知或课程列表补全'
        return
    stats['details'] += 1
    from homework_navigation import resolve_empty_work_fallback, resolve_work_page
    markup, final = resolve_work_page(context, record, fetch_html)
    if not (record.get('status') == 'submitted' and re.search(r'/getallwork', urlsplit(final).path, re.I)):
        update_metadata(record, markup, final)
    record['content_entry_checked'] = url
    record['work_url'] = final
    if record_group(record) == 'history':
        return
    download = asset_downloader(context)
    img_urls = []
    parsed_markup = dom.parse(markup)
    is_school = school.is_school_host(urlsplit(final).hostname)
    for img in parsed_markup.all('img'):
        img_src = urljoin(final, img.attrs.get('data-original') or img.attrs.get('data-src') or img.attrs.get('src') or '')
        if img_src.startswith(('https://', 'http://')):
            if is_school:
                school.note_asset_url(img_src)
            if img_src not in img_urls:
                img_urls.append(img_src)
    if http_pool.supports_concurrency(context) and len(img_urls) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(img_urls))) as pool:
            list(pool.map(download, img_urls))
    questions = dom.parse_questions(markup, final, download)
    if not questions:
        fallback = resolve_empty_work_fallback(context, record, final, markup, fetch_html, asset=download)
        if isinstance(fallback, dict) and fallback.get('mode') == 'renwu_task':
            _apply_renwu_fallback(record, fallback)
            return
        if isinstance(fallback, dict) and fallback.get('mode') == 'html':
            fb_markup = fallback.get('markup') or ''
            fb_final = fallback.get('final') or final
            update_metadata(record, fb_markup, fb_final)
            record['work_url'] = fb_final
            if fallback.get('card_status') in {'pending', 'submitted'} and record.get('status') in {None, '', 'unknown'}:
                record['status'] = fallback['card_status']
            if record_group(record) == 'history':
                return
            markup, final = fb_markup, fb_final
            questions = dom.parse_questions(markup, final, download)
    if not questions:
        if not record.get('questions'):
            record['content_status'] = '页面没有可识别的原题，未把摘要或题号列表存为题目'
        return
    record['questions'] = questions
    # Old versions collected links from the entire page, including plugin updates.
    # Keep that snapshot, but display only links inside verified question content.
    record.setdefault('legacy_attachments', record.get('attachments') or [])
    attachments = {}
    for question in questions:
        for fragment in [question.get('html', ''), *[option.get('html', '') for option in question.get('options', [])]]:
            for anchor in dom.parse(fragment).all('a'):
                href = anchor.attrs.get('href') or ''
                if href.startswith(('https://', 'http://')) and not dom.platform_page_url(href):
                    attachments.setdefault(href, {'name': dom.clean_text(anchor) or '题目附件', 'url': href})
    record['attachments'] = list(attachments.values())
    record['content_version'] = CONTENT_VERSION
    record['content_status'] = 'ready'
    record['content_last_read'] = now_iso()
    record.setdefault('legacy_requirements', record.get('requirements', ''))
    record['requirements'] = '\n\n'.join(q['text'] for q in questions)
    record['question_count'] = len(questions)
    record['answer_url'] = final
    diagnostics = dom.question_diagnostics(markup, questions)
    record['content_complete'] = diagnostics['complete']
    record['content_warnings'] = diagnostics['warnings']
    if re.search(r'/do(?:home)?work', urlsplit(final).path, re.I) and record.get('status') == 'unknown':
        record['status'] = 'pending'
    if not record['content_complete']:
        record['content_status'] = '已读取部分题目，请在原页面核对剩余题目'

def needs_identity_probe(record, records):
    """Read a historical candidate once to resolve an uncertain legacy sibling."""
    if record_group(record) != 'history' or record.get('identity_probe_done'):
        return False
    ids = record.get('identities') or []
    pair = normalize(record.get('course')), normalize(record.get('title'))
    for sibling in records.values():
        other = sibling.get('identities') or []
        if sibling is record or record_group(sibling) != 'review' or set(ids).intersection(other):
            continue
        if (normalize(sibling.get('course')), normalize(sibling.get('title'))) != pair:
            continue
        if any(a.split(':')[:3] == b.split(':')[:3] and a.split(':')[-2] != b.split(':')[-2]
               for a in ids for b in other):
            return True
    return False


def needs_entry_upgrade(record):
    entry = record.get('entry_url') or ''
    answer = record.get('answer_url') or record.get('work_url') or ''
    return bool(record.get('questions') and not record.get('content_complete')
                and '/phone/' in answer.lower() and 'intoexamorwork' in entry.lower()
                and record.get('content_entry_checked') != entry)


def skipped_by_cache(record, *, force_key, valid, due, attempted, identity_probe, entry_upgrade):
    """True when this pass may reuse the stored content instead of re-reading.

    Two independent rules share one bypass list (an explicit refresh, an identity
    probe or an entry upgrade):
      1. a recent read error backs off, so a failing course is not retried on
         every pass;
      2. an unchanged record inside its metadata window is reused as it is.
    """
    if force_key or record.get('metadata_needs_check') or identity_probe or entry_upgrade:
        return False
    now = datetime.now()
    if record.get('content_error') and attempted and now - attempted < ERROR_BACKOFF:
        return True
    return (valid and not due) or (not valid and bool(attempted) and now - attempted < ERROR_BACKOFF)


def check_once(headless=True, force_key=None, on_auth=None):
    with browser_operation():
        return _check_once(headless, force_key, on_auth)


def check_submission(key, on_auth=None, wait=True):
    """Lightweight single-assignment check for submission status without locking UI actions."""
    canonical = canonical_key(key)
    with browser_operation(30000 if wait else 0):
        state = load_state()
        selected = state['assignments'].get(canonical)
        if not selected:
            raise ValueError('找不到这份作业，请刷新后重试')
        stats = {'notice_pages': 0, 'course_lists': 0, 'details': 0, 'cache_hits': 0, 'warnings': []}
        with ExitStack() as stack:
            context = _open_fast_or_browser_context(True)
            if context is None:
                playwright = stack.enter_context(sync_playwright())
                context = open_context(playwright, True)
            stack.callback(close_context, context)

            def _fetch_target_status():
                if selected.get('source_kind') == lab.SOURCE_KIND:
                    collect_lab_reports(context, state, stats, only=canonical)
                else:
                    ids = course_ids(selected)
                    if ids:
                        collect_course_rows(context, state, stats, only=ids)

            need_auth = not getattr(context, 'is_fast_http_context', False)
            if not need_auth:
                try:
                    _fetch_target_status()
                except Exception as exc:
                    if 'LOGIN_REQUIRED' in str(exc) or '登录' in str(exc):
                        need_auth = True
                    else:
                        raise
                else:
                    if any('LOGIN_REQUIRED' in w or '登录' in w for w in stats['warnings']):
                        stats['warnings'].clear()
                        need_auth = True
            if need_auth:
                import homework_auth
                auth = homework_auth.status(restore=True, context=context, progress=PROGRESS, on_state=on_auth)
                if not any(auth['sites'].values()):
                    raise RuntimeError('请先登录作业通')
                _fetch_target_status()

        reconcile_aliases(state)
        resolved_key = resolve_alias(state.get('record_aliases') or {}, canonical)
        record = state['assignments'].get(resolved_key) or state['assignments'].get(canonical) or {}
        state['last_check'] = now_iso()
        state['last_success'] = now_iso()
        save_state(state)
        try:
            import homework_accounts as accounts
            if accounts.is_isolated():
                accounts.sync_active_metadata()
        except Exception:
            pass
        latest = LATEST_FILE.with_name(LATEST_FILE.name + f'.{os.getpid()}.{threading.get_ident()}.tmp')
        latest.write_text(render_summary(active_pending(state)) + '\n', encoding='utf-8')
        latest.replace(LATEST_FILE)
        group = record_group(record) if record else ''
        submitted = bool(record and (record.get('status') == 'submitted' or group == 'history'))
        return {
            'key': resolved_key or canonical,
            'status': record.get('status', ''),
            'group': group,
            'submitted': submitted,
            'warnings': stats['warnings'],
        }

_DEFAULT_COLLECT_NOTICES = collect_notices
_DEFAULT_COLLECT_COURSE_ROWS = collect_course_rows
_DEFAULT_COLLECT_LAB_REPORTS = collect_lab_reports
_DEFAULT_READ_CONTENT = read_content


def _replay_collectors(state, notice_items, course_items, lab_sink):
    for kind, payload in notice_items:
        if kind == 'lab':
            existing = match_record(state['assignments'], payload)
            if lab.should_import(payload, existing=bool(existing)):
                merge_record(state['assignments'], payload)
        else:
            merge_record(state['assignments'], payload)
    for candidate in course_items:
        merge_record(state['assignments'], candidate)
    for kind, payload in lab_sink:
        if kind == 'lab_row':
            existing = match_record(state['assignments'], payload)
            if lab.should_import(payload, existing=bool(existing)):
                merge_record(state['assignments'], payload)
        elif kind == 'lab_meta':
            meta_key, stamp = payload
            state.setdefault('lab_meta', {})[meta_key] = {'submitted_checked_at': stamp}
        elif kind == 'lab_err':
            target, reason = payload
            for record in state['assignments'].values():
                info = record.get('lab_report') or {}
                if (record.get('source_kind') == lab.SOURCE_KIND and record_group(record) != 'history'
                        and info.get('fid') == target['fid']
                        and str(info.get('course_id') or '0') == str(target.get('course_id') or '0')):
                    record['content_error'] = reason


def _collect_top_level_parallel(context, state, stats):
    state_snapshot = copy.deepcopy(state['assignments'])
    before_school_origin = school.school_origin('')
    known_school_pairs = set()
    for r in state_snapshot.values():
        if record_group(r) == 'history':
            continue
        for name in ('entry_url', 'work_url', 'list_url', 'answer_url'):
            raw = r.get(name) or ''
            u = urlsplit(raw)
            course, clazz = dom.course_class_pair(raw)
            if (u.hostname == 'mooc.istudy.szpu.edu.cn' or school.is_school_host(u.hostname)) and course and clazz:
                known_school_pairs.add((course, clazz))

    notice_state = {
        'notice_seen': list(state.get('notice_seen') or []),
        'notice_backfilled': state.get('notice_backfilled'),
        'lab_notices_scanned': state.get('lab_notices_scanned'),
        'notice_pending_cursors': list(state.get('notice_pending_cursors') or []),
        'notice_backfill_cursor': state.get('notice_backfill_cursor'),
        'assignments': state_snapshot,
    }
    notice_stats = {'notice_pages': 0, 'course_lists': 0, 'details': 0, 'cache_hits': 0, 'warnings': []}
    course_stats = {'notice_pages': 0, 'course_lists': 0, 'details': 0, 'cache_hits': 0, 'warnings': []}
    lab_stats = {'notice_pages': 0, 'course_lists': 0, 'details': 0, 'cache_hits': 0, 'warnings': []}
    notice_items, course_items, lab_sink = [], [], []
    course_meta_out = {}
    notice_err = course_err = lab_err = None

    def _run_notices():
        collect_notices(context, notice_state, notice_stats, _sink=notice_items)

    def _run_courses():
        collect_course_rows(context, state, course_stats, _snapshot=state_snapshot, _sink=course_items, _meta_out=course_meta_out)

    def _run_labs():
        collect_lab_reports(context, state, lab_stats, _snapshot=state_snapshot, _sink=lab_sink)

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_notice = pool.submit(_run_notices)
        f_course = pool.submit(_run_courses)
        f_lab = pool.submit(_run_labs)
        for name, fut in (('notice', f_notice), ('course', f_course), ('lab', f_lab)):
            try:
                fut.result()
            except Exception as exc:
                if name == 'notice':
                    notice_err = exc
                elif name == 'course':
                    course_err = exc
                else:
                    lab_err = exc

    connected = False
    if notice_err is None:
        connected = True
        for key in ('notice_seen', 'notice_backfilled', 'lab_notices_scanned'):
            if key in notice_state:
                state[key] = notice_state[key]
        if 'notice_backfill_cursor' in notice_state:
            state['notice_backfill_cursor'] = notice_state['notice_backfill_cursor']
        else:
            state.pop('notice_backfill_cursor', None)
        if 'notice_pending_cursors' in notice_state:
            state['notice_pending_cursors'] = notice_state['notice_pending_cursors']
        else:
            state.pop('notice_pending_cursors', None)
    else:
        stats['warnings'].append(safe_error(notice_err))

    if course_err is None:
        connected = True
    else:
        stats['warnings'].append(safe_error(course_err))

    if lab_err is None:
        connected = True
    else:
        stats['warnings'].append(safe_error(lab_err))

    for sub in (notice_stats, course_stats, lab_stats):
        stats['notice_pages'] += sub['notice_pages']
        stats['course_lists'] += sub['course_lists']
        stats['details'] += sub['details']
        stats['cache_hits'] += sub['cache_hits']
        stats['warnings'].extend(sub['warnings'])

    _replay_collectors(state, notice_items, course_items, lab_sink)
    if course_meta_out.get('course_meta'):
        state.setdefault('course_meta', {}).update(course_meta_out['course_meta'])
    skipped_dormant = set(course_meta_out.get('skipped_dormant') or ())
    woken_dormant_pairs = set()
    for _kind, payload in notice_items:
        c_id, cl_id = course_ids(payload)
        if c_id and cl_id:
            cm = state.setdefault('course_meta', {}).get(f'{c_id}:{cl_id}')
            if isinstance(cm, dict):
                cm['dormant'] = False
                cm['empty_streak'] = 0
            if (c_id, cl_id) in skipped_dormant:
                woken_dormant_pairs.add((c_id, cl_id))
    for pair in sorted(woken_dormant_pairs):
        try:
            collect_course_rows(context, state, stats, only=pair)
        except Exception as exc:
            stats['warnings'].append(safe_error(exc))

    if notice_err is None:
        extra_pairs = set()
        for kind, payload in notice_items:
            if kind != 'work':
                continue
            url = payload.get('entry_url') or ''
            u = urlsplit(url)
            course, clazz = dom.course_class_pair(url)
            if (u.hostname == 'mooc.istudy.szpu.edu.cn' or school.is_school_host(u.hostname)) and course and clazz:
                if (course, clazz) not in known_school_pairs:
                    extra_pairs.add((course, clazz))
        for pair in sorted(extra_pairs):
            try:
                collect_course_rows(context, state, stats, only=pair)
            except Exception as exc:
                stats['warnings'].append(safe_error(exc))
        if school.school_origin('') != before_school_origin:
            try:
                collect_lab_reports(context, state, stats)
            except Exception as exc:
                stats['warnings'].append(safe_error(exc))

    return connected


def _open_fast_or_browser_context(headless):
    fast_mode = (
        headless
        and sync_playwright is _DEFAULT_SYNC_PLAYWRIGHT
        and open_context is _DEFAULT_OPEN_CONTEXT
        and close_context is _DEFAULT_CLOSE_CONTEXT
        and http_pool.has_session_cookies(DATA_DIR / 'browser_session.json')
    )
    if not fast_mode:
        return None
    def _run_in_browser(fn):
        with sync_playwright() as playwright:
            pw_ctx = open_context(playwright, headless)
            try:
                return fn(pw_ctx)
            finally:
                close_context(pw_ctx)
    return http_pool.FastCookieContext(DATA_DIR / 'browser_session.json', run_in_browser=_run_in_browser)


def _is_auth_warning_or_error(text):
    value = str(text or '')
    return 'LOGIN_REQUIRED' in value or '登录' in value or '请先登录' in value


def _run_single_refresh_pass(context, state, stats, force_key, verified_sites):
    """Refresh a single assignment, running task-list status and detail read in parallel when safe."""
    selected = state['assignments'].get(force_key) or {}
    if selected.get('source_kind') == lab.SOURCE_KIND:
        collect_lab_reports(context, state, stats, only=force_key)
        reconcile_aliases(state)
    else:
        ids = course_ids(selected)
        can_parallel = (
            http_pool.supports_concurrency(context)
            and bool(ids)
            and bool(selected)
            and selected.get('status') != 'submitted'
            and collect_course_rows is _DEFAULT_COLLECT_COURSE_ROWS
            and read_content is _DEFAULT_READ_CONTENT
            and bool(selected.get('entry_url') or selected.get('list_url') or selected.get('work_url'))
        )
        if can_parallel:
            snapshot = copy.deepcopy(state['assignments'])
            rows_sink, meta_out = [], {}
            course_stats = {'notice_pages': 0, 'course_lists': 0, 'details': 0, 'cache_hits': 0, 'warnings': []}
            read_stats = {'details': 0, 'warnings': []}
            rec_copy = copy.deepcopy(selected)
            rec_copy['content_attempted_at'] = now_iso()

            def _do_course():
                try:
                    collect_course_rows(context, state, course_stats, only=ids, _snapshot=snapshot, _sink=rows_sink, _meta_out=meta_out)
                    return None
                except Exception as exc:
                    return exc

            def _do_read():
                try:
                    read_content(context, rec_copy, read_stats)
                    return None
                except Exception as exc:
                    return exc

            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_course = pool.submit(_do_course)
                fut_read = pool.submit(_do_read)
                course_err = fut_course.result()
                read_err = fut_read.result()

            stats['course_lists'] += course_stats['course_lists']
            stats['warnings'].extend(course_stats['warnings'])
            stats['details'] += read_stats['details']
            stats['warnings'].extend(read_stats['warnings'])
            if meta_out:
                state.setdefault('course_meta', {}).update(meta_out)
            for candidate in rows_sink:
                merge_record(state['assignments'], candidate)
            if course_err is not None:
                stats['warnings'].append(safe_error(course_err))
            reconcile_aliases(state)
            merged_key = resolve_alias(state.get('record_aliases') or {}, force_key)
            record = state['assignments'].get(merged_key)
            if record is not None:
                if record.get('status') == 'submitted':
                    stats['cache_hits'] += 1
                    return
                if read_err is None:
                    list_url = record.get('list_url')
                    row_checked_at = record.get('status_checked_at')
                    record.update(rec_copy)
                    if list_url and not record.get('list_url'):
                        record['list_url'] = list_url
                    if row_checked_at:
                        record['status_checked_at'] = row_checked_at
                    record.pop('content_error', None)
                    reconcile_aliases(state)
                    final_key = resolve_alias(state.get('record_aliases') or {}, merged_key)
                    final_rec = state['assignments'].get(final_key)
                    if final_rec and needs_entry_upgrade(final_rec):
                        read_content(context, final_rec, stats)
                        final_rec.pop('content_error', None)
                        reconcile_aliases(state)
                else:
                    _handle_read_error(state, record, merged_key, read_err, verified_sites, stats)
            return

        if ids:
            try:
                collect_course_rows(context, state, stats, only=ids)
            except Exception as exc:
                stats['warnings'].append(safe_error(exc))
        reconcile_aliases(state)

    eligible = _collect_eligible_records(state, stats, force_key=force_key)
    _read_eligible_records(context, state, stats, eligible, verified_sites)


def _collect_eligible_records(state, stats, force_key=None):
    eligible = []
    for key, record in list(state['assignments'].items()):
        if key not in state['assignments']:
            continue
        if force_key and key != force_key:
            continue
        if force_key and record.get('status') == 'submitted':
            # The course row just proved it is submitted; its detail
            # page is read-only now and would only add a warning.
            stats['cache_hits'] += 1
            continue
        if record.get('source_kind') == lab.SOURCE_KIND and record_group(record) == 'history':
            # List metadata can reopen an extended/redo assignment;
            # an expired or submitted one never needs its old body.
            stats['cache_hits'] += 1
            continue
        identity_probe = record.get('source_kind') != lab.SOURCE_KIND and needs_identity_probe(record, state['assignments'])
        entry_upgrade = record.get('source_kind') != lab.SOURCE_KIND and needs_entry_upgrade(record)
        if record_group(record) == 'history' and key != force_key and not record.get('metadata_needs_check') and not identity_probe:
            continue
        attempted = parse_datetime(record.get('content_attempted_at'))
        valid = record.get('content_version') == CONTENT_VERSION and bool(record.get('questions'))
        checked = parse_datetime(record.get('content_last_read') if record.get('source_kind') == lab.SOURCE_KIND
                                 else record.get('metadata_checked_at'))
        due = record.get('metadata_needs_check') or not checked or datetime.now() - checked > METADATA_MAX_AGE
        if (record.get('content_error') == '网页作业标识与目标不一致，已停止读取'
                and not record.get('alias_retry_done')):
            record['alias_retry_done'] = True
            attempted = None
        if (record.get('content_complete') is False
                and '当前为手机分页题目，尚未核实作业全部题数' in (record.get('content_warnings') or [])
                and not record.get('phone_paging_verified')):
            record['phone_paging_verified'] = True
            due = True
        if skipped_by_cache(record, force_key=bool(force_key), valid=valid, due=due,
                            attempted=attempted, identity_probe=identity_probe,
                            entry_upgrade=entry_upgrade):
            stats['cache_hits'] += 1
            continue
        if not record.get('entry_url') and not record.get('list_url') and not record.get('work_url'):
            continue
        eligible.append((key, record, identity_probe))
    return eligible


def _read_eligible_records(context, state, stats, eligible, verified_sites):
    if http_pool.supports_concurrency(context) and len(eligible) > 1:
        PROGRESS(f'正在并发读取 {len(eligible)} 份作业详情…')
        work_copies = []
        for key, record, identity_probe in eligible:
            log(f'正在核对：{record.get("course")} / {record.get("title")}')
            record['content_attempted_at'] = now_iso()
            work_copies.append((key, copy.deepcopy(record), identity_probe))

        def _read_one(item):
            key, rec_copy, id_probe = item
            local_stats = {'details': 0, 'warnings': []}
            try:
                read_content(context, rec_copy, local_stats)
                return key, rec_copy, id_probe, local_stats, None
            except Exception as exc:
                return key, rec_copy, id_probe, local_stats, exc

        with ThreadPoolExecutor(max_workers=min(6, len(work_copies))) as pool:
            read_results = list(pool.map(_read_one, work_copies))

        for key, updated, identity_probe, local_stats, err in read_results:
            stats['details'] += local_stats['details']
            stats['warnings'].extend(local_stats['warnings'])
            record = state['assignments'].get(key)
            if not record:
                continue
            if err is None:
                record.clear()
                record.update(updated)
                if identity_probe:
                    record['identity_probe_done'] = True
                record.pop('content_error', None)
            else:
                _handle_read_error(state, record, key, err, verified_sites, stats)
        reconcile_aliases(state)
        upgraded = set()
        for key, _, _ in eligible:
            merged_key = resolve_alias(state.get('record_aliases') or {}, key)
            if merged_key in upgraded:
                continue
            upgraded.add(merged_key)
            merged = state['assignments'].get(merged_key)
            if merged and needs_entry_upgrade(merged):
                try:
                    read_content(context, merged, stats)
                    merged.pop('content_error', None)
                    reconcile_aliases(state)
                except Exception as exc:
                    _handle_read_error(state, merged, merged_key, exc, verified_sites, stats)
    else:
        for key, record, identity_probe in eligible:
            if key not in state['assignments']:
                continue
            log(f'正在核对：{record.get("course")} / {record.get("title")}')
            record['content_attempted_at'] = now_iso()
            try:
                read_content(context, record, stats)
                if identity_probe:
                    record['identity_probe_done'] = True
                record.pop('content_error', None)
                reconcile_aliases(state)
                # A proven alias may reveal a better PC entry only after
                # the legacy mobile content was read earlier in this pass.
                # Walk the full chain: a record can be merged more than once.
                merged_key = resolve_alias(state.get('record_aliases') or {}, key)
                merged = state['assignments'].get(merged_key)
                if merged and needs_entry_upgrade(merged):
                    read_content(context, merged, stats)
                    merged.pop('content_error', None)
                    reconcile_aliases(state)
            except Exception as exc:
                _handle_read_error(state, record, key, exc, verified_sites, stats)


def _check_once(headless=True, force_key=None, on_auth=None):
    state = load_state()
    stats = {'notice_pages': 0, 'course_lists': 0, 'details': 0, 'cache_hits': 0, 'warnings': []}
    connected = False
    try:
        with ExitStack() as stack:
            context = _open_fast_or_browser_context(headless)
            if context is None:
                playwright = stack.enter_context(sync_playwright())
                context = open_context(playwright, headless)
            stack.callback(close_context, context)
            verified_sites = {'chaoxing': True, 'school': True}
            fast_single = bool(headless and force_key and getattr(context, 'is_fast_http_context', False) is True)
            if headless and not fast_single:
                import homework_auth
                auth = homework_auth.status(restore=True, context=context, progress=PROGRESS, on_state=on_auth)
                verified_sites = dict(auth.get('sites') or {})
                if not any(auth['sites'].values()):
                    raise RuntimeError('请先登录作业通')
                if not auth['logged_in']:
                    stats['warnings'].append('部分站点尚未登录')
            if not force_key:
                log('正在同步通知与课程作业状态…')
                unpatched_collectors = (
                    collect_notices is _DEFAULT_COLLECT_NOTICES
                    and collect_course_rows is _DEFAULT_COLLECT_COURSE_ROWS
                    and collect_lab_reports is _DEFAULT_COLLECT_LAB_REPORTS
                )
                if unpatched_collectors and http_pool.supports_concurrency(context):
                    connected = _collect_top_level_parallel(context, state, stats)
                else:
                    for collector in (collect_notices, collect_course_rows, collect_lab_reports):
                        try:
                            collector(context, state, stats)
                            connected = True
                        except Exception as exc:
                            stats['warnings'].append(safe_error(exc))
                reconcile_aliases(state)
                eligible = _collect_eligible_records(state, stats, force_key=None)
                _read_eligible_records(context, state, stats, eligible, verified_sites)
            else:
                connected = True
                _run_single_refresh_pass(context, state, stats, force_key, verified_sites)
                target_rec = state['assignments'].get(resolve_alias(state.get('record_aliases') or {}, force_key)) or {}
                need_auth_retry = fast_single and (
                    any(_is_auth_warning_or_error(w) for w in stats['warnings'])
                    or _is_auth_warning_or_error(target_rec.get('content_error'))
                )
                if need_auth_retry:
                    import homework_auth
                    stats['warnings'] = [w for w in stats['warnings'] if not _is_auth_warning_or_error(w)]
                    if isinstance(target_rec, dict) and _is_auth_warning_or_error(target_rec.get('content_error')):
                        target_rec.pop('content_error', None)
                    auth = homework_auth.status(restore=True, context=context, progress=PROGRESS, on_state=on_auth)
                    verified_sites = dict(auth.get('sites') or {})
                    if not any(auth['sites'].values()):
                        raise RuntimeError('请先登录作业通')
                    if not auth['logged_in']:
                        stats['warnings'].append('部分站点尚未登录')
                    _run_single_refresh_pass(context, state, stats, force_key, verified_sites)
    except Exception as exc:
        stats['warnings'].append(safe_error(exc))
    state['last_check'] = now_iso()
    if connected:
        state['last_success'] = now_iso()
    state['last_stats'] = stats
    save_state(state)
    try:
        import homework_accounts as accounts
        if accounts.is_isolated():
            accounts.sync_active_metadata()
    except Exception:
        pass
    latest = LATEST_FILE.with_name(LATEST_FILE.name + f'.{os.getpid()}.{threading.get_ident()}.tmp')
    latest.write_text(render_summary(active_pending(state)) + '\n', encoding='utf-8')
    latest.replace(LATEST_FILE)
    log(f'同步完成：通知 {stats["notice_pages"]} 页，课程 {stats["course_lists"]} 门，详情 {stats["details"]} 次，复用 {stats["cache_hits"]} 项。')
    return 0 if connected else 2

def render_summary(items):
    return '\n'.join(f'{a.get("course")}｜{a.get("title")}｜{a.get("deadline") or "平台未设截止时间"}' for a in items) or '当前没有已确认仍可作答的待办作业。'

def _native_toast(title, message, persistent=False):
    """Show a Windows toast without opening a browser window or adding a dependency.

    `message` is one text block or a list of up to two (the toast layout has a
    title plus two body blocks). Persistent reminders use Windows' maximum
    three-day retention in Notification Center, with a system dismiss button.
    """
    if os.name != 'nt':
        return False
    blocks = [message] if isinstance(message, str) else list(message)
    heading = html.escape(str(title or '学习通作业助手')[:120], quote=True)
    texts = ''.join(f'<text>{html.escape(str(block)[:900], quote=True)}</text>' for block in blocks[:2] if block)
    if persistent and blocks:
        # Compact view: summary. Expanded view: one group per assignment,
        # with its name above quieter deadline and progress details.
        paragraphs = [paragraph for paragraph in str(blocks[0])[:900].split('\n\n') if paragraph]
        texts = f'<text hint-maxLines="2">{html.escape(paragraphs[0], quote=True)}</text>' if paragraphs else ''
        for paragraph in paragraphs[1:]:
            name, _, details = paragraph.partition('\n')
            texts += (
                '<group><subgroup>'
                f'<text hint-style="base" hint-wrap="true" hint-maxLines="2">{html.escape(name, quote=True)}</text>'
                f'<text hint-style="captionSubtle" hint-wrap="true" hint-maxLines="2">{html.escape(details, quote=True)}</text>'
                '</subgroup></group>'
            )
        if len(blocks) > 1 and blocks[1]:
            texts += f'<text placement="attribution">{html.escape(str(blocks[1])[:900], quote=True)}</text>'
    toast_xml = (
        '<toast duration="short">'
        + f'<visual><binding template="ToastGeneric"><text>{heading}</text>{texts}</binding></visual>'
        + ('<actions><action content="知道了" arguments="dismiss" activationType="system"/></actions>' if persistent else '')
        + '</toast>'
    )
    script = f"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = [Windows.Data.Xml.Dom.XmlDocument]::new()
$xml.LoadXml('{toast_xml.replace("'", "''")}')
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
{('$toast.ExpirationTime = [DateTimeOffset]::Now.AddDays(3)' if persistent else '')}
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{TOAST_APP_ID}').Show($toast)
"""
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    try:
        process = subprocess.Popen(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-EncodedCommand', encoded],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        try:
            return process.wait(timeout=12) == 0
        except subprocess.TimeoutExpired:
            process.kill()
            return False
    except OSError:
        return False


def notify(title, message, persistent=False):
    """Returns True when a native toast was shown, False when the fallback was used."""
    if _native_toast(title, message, persistent):
        return True
    text = message if isinstance(message, str) else '\n'.join(str(block) for block in message if block)
    # Keep a compatibility fallback for environments where PowerShell notifications are unavailable.
    try:
        subprocess.Popen(['msg.exe', os.environ.get('USERNAME', '*'), '/TIME:300', title + '\n' + text[:900]], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except OSError:
        pass
    return False

def ensure_chrome(url=None, app=False):
    port = urlsplit(CHAOXING_CDP_URL).port or 9222
    if not app and runtime.cdp_endpoint_alive(CHAOXING_CDP_URL):
        if url is None:
            return
        with sync_playwright() as playwright:
            browser = runtime.connect_browser_direct(playwright, CHAOXING_CDP_URL, timeout=1000)
            runtime.open_visible_url(browser, url, app=False)
        return
    if hasattr(runtime, '_allow_foreground_window'):
        runtime._allow_foreground_window()
    launch_url = runtime.fill_loading_url() if (url is None and not app and hasattr(runtime, 'fill_loading_url')) else url
    args = runtime.browser_args(launch_url, app=app, port=port)
    subprocess.Popen(args, creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))

LOGIN_TARGET_ID = None
LOGIN_TARGET_PLATFORM = None
# Visible-login targets for the CLI (--login). homework_auth keeps the fuller
# pair for the in-app flow - its school entry carries fid/refer and lands on the
# form directly - so this table is the minimal equivalent, not a copy to sync.
LOGIN_URLS = {'chaoxing': NOTICE_URL, 'school': 'https://passport.istudy.szpu.edu.cn/'}


def login_page_platform(url):
    parsed = urlsplit(url)
    if parsed.hostname == 'passport.istudy.szpu.edu.cn' or school.is_school_auth_host(parsed.hostname):
        return 'school'
    if parsed.hostname in {'passport2.chaoxing.com', 'passport2.chaoxing.com.cn'} or (
            parsed.hostname == 'notice.chaoxing.com' and parsed.path.startswith('/pc/notice/')):
        return 'chaoxing'
    return None


def navigate_login_target(browser, target_id, url):
    for context in browser.contexts:
        for page in context.pages:
            session = context.new_cdp_session(page)
            try:
                if session.send('Target.getTargetInfo')['targetInfo']['targetId'] != target_id:
                    continue
                result = session.send('Page.navigate', {'url': url})
                if result.get('errorText'):
                    raise RuntimeError('登录页面打开失败，请检查网络后重试。')
                return
            finally:
                session.detach()
    raise RuntimeError('登录窗口已关闭，请重新点击登录。')


def _login_mode_url(platform):
    if platform == 'school' and LOGIN_URLS.get('school') == 'https://passport.istudy.szpu.edu.cn/':
        return school.school_login_url() or school.auth_origin() or LOGIN_URLS['school']
    return LOGIN_URLS[platform]


def login_mode(platform='chaoxing'):
    global LOGIN_TARGET_ID, LOGIN_TARGET_PLATFORM
    if platform not in LOGIN_URLS:
        raise ValueError('未知的登录平台')
    target_url = _login_mode_url(platform)
    # An already-running Chrome can acknowledge a command-line launch without
    # opening its URLs. Create visible windows through its verified connection.
    with browser_operation(), sync_playwright() as playwright:
        try:
            browser = runtime.connect_browser_direct(playwright, CHAOXING_CDP_URL, timeout=1000)
        except Exception:
            ensure_chrome()
            browser = None
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                try:
                    browser = runtime.connect_browser_direct(playwright, CHAOXING_CDP_URL, timeout=500)
                    break
                except Exception:
                    time.sleep(.25)
            if browser is None:
                raise RuntimeError('未能打开助手专用浏览器，请关闭旧的助手浏览器后重试登录。')
        runtime.verify_browser_profile(browser)
        session = browser.new_browser_cdp_session()
        try:
            targets = session.send('Target.getTargets').get('targetInfos', [])
            candidates = [t for t in targets if t.get('type') == 'page' and (
                login_page_platform(t.get('url', '')) or (t['targetId'] == LOGIN_TARGET_ID and t.get('url', '') in ('', 'about:blank')))]
            target = next((t for t in candidates if t['targetId'] == LOGIN_TARGET_ID), None)
            target = target or next((t for t in candidates if login_page_platform(t['url']) == platform), None)
            target = target or next(iter(candidates), None)
            if target:
                target_id = target['targetId']
                current_platform = login_page_platform(target.get('url', '')) or LOGIN_TARGET_PLATFORM
                if current_platform != platform:
                    navigate_login_target(browser, target_id, target_url)
            else:
                target = session.send('Target.createTarget', {
                    'url': target_url, 'newWindow': True, 'background': False,
                })
                if not target.get('targetId'):
                    raise RuntimeError('登录窗口未能打开，请重试登录。')
                target_id = target['targetId']
            LOGIN_TARGET_ID = target_id
            LOGIN_TARGET_PLATFORM = platform
            runtime.activate_target(browser, target_id, session=session)
        finally:
            session.detach()
    return 0

def load_draft(key):
    key = canonical_key(key)
    with STATE_LOCK:
        value = read_json(DRAFTS_FILE, {}).get(key, {})
    if isinstance(value, str):
        return {'version': 2, 'answers': {}, 'legacy_text': value, 'updated_at': None}
    return value if isinstance(value, dict) else {'version': 2, 'answers': {}}

def store_draft(key, value):
    """Persist one complete draft record.

    The single place that rewrites answer_drafts.json, so every writer (UI
    autosave, alias migration, AI answers and templates) shares one
    read-modify-write shape. Callers already hold the draft transaction.
    """
    drafts = read_json(DRAFTS_FILE, {})
    drafts[key] = value
    write_json(DRAFTS_FILE, drafts)


def save_draft(key, payload):
    if not isinstance(payload.get('answers'), dict):
        raise ValueError('答案格式不正确')
    with STATE_LOCK, draft_transaction():
        key = canonical_key(key)
        # 只读一次状态：确保两次判定基于同一份快照，也少解析一次大 JSON。
        assignments = load_state()['assignments']
        if key not in assignments:
            raise ValueError('作业不存在')
        if record_group(assignments[key]) == 'history':
            raise ValueError('历史作业仅保留基本信息，不再保存答案')
        previous = load_draft(key)
        answers = payload['answers']
        changes = payload.get('_changed_questions')
        if changes is not None:
            if not isinstance(changes, list) or any(not isinstance(qid, str) for qid in changes):
                raise ValueError('答案变更格式不正确')
            # AI writes can finish between the UI's read and save. Apply only
            # questions actually edited by this client, preserving other answers.
            answers = dict(previous.get('answers', {}))
            for qid in changes:
                if qid in payload['answers']:
                    answers[qid] = payload['answers'][qid]
                else:
                    answers.pop(qid, None)
        value = {**previous, 'version': 2, 'answers': answers, 'legacy_text': str(payload.get('legacy_text', previous.get('legacy_text', ''))), 'updated_at': now_iso()}
        store_draft(key, value)
    return value

def canonical_key(key):
    """Resolve an alias chain against the alias map currently on disk."""
    return resolve_alias(load_state().get('record_aliases') or {}, key)
