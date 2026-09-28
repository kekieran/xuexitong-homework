"""Incremental homework sync. No answer submission endpoints are used here."""
from __future__ import annotations
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
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit, parse_qs
from playwright.sync_api import sync_playwright
import homework_dom as dom
import homework_runtime as runtime

APP_DIR = runtime.APP_DIR
DATA_DIR = runtime.DATA_DIR
PROFILE_DIR = runtime.PROFILE_DIR
CHROME_EXECUTABLE = runtime.find_browser(required=False)
CHAOXING_CDP_URL = os.environ.get('CHAOXING_CDP_URL', 'http://127.0.0.1:9222')
STATE_FILE = DATA_DIR / 'state.json'
DRAFTS_FILE = DATA_DIR / 'answer_drafts.json'
OVERRIDES_FILE = DATA_DIR / 'assignment_overrides.json'
_OVERRIDES_CACHE = (None, {})
LOG_FILE = DATA_DIR / 'reminder.log'
LATEST_FILE = DATA_DIR / 'latest_assignments.txt'
UI_NOTIFICATION_FILE = DATA_DIR / 'ui_notification.json'
CONTENT_DIR = DATA_DIR / 'homework_content'
NOTICE_URL = 'https://notice.chaoxing.com/pc/notice/myNotice'
NOTICE_API_URL = 'https://notice.chaoxing.com/pc/notice/getNoticeList'
COURSES_URL = 'https://mooc1-2.chaoxing.com/course/phone/courselistdata?courseFolderId=0&isFiled=0&query='
STATE_LOCK = threading.RLock()
CONNECTED_BROWSER = None
BACKGROUND_CONTEXTS = {}
PROGRESS = lambda message: None
CONTENT_VERSION = 2

def now_iso():
    return datetime.now().isoformat(timespec='seconds')

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
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding='utf-8-sig'))

def write_json(path, value):
    with STATE_LOCK:
        temp = path.with_name(path.name + f'.{os.getpid()}.{threading.get_ident()}.tmp')
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)

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

def browser_operation():
    return process_transaction('Local\\ChaoxingHomeworkBrowserOperation_' + runtime.PROJECT_ID)

def draft_transaction():
    return process_transaction('Local\\ChaoxingHomeworkDraftTransaction_' + runtime.PROJECT_ID, 10000)

def parse_datetime(value):
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
                seen = set()
                while key in aliases and key not in seen:
                    seen.add(key)
                    key = aliases[key]
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
            values[key] = {'completed': completed, 'deadline': parse_datetime(deadline).isoformat(timespec='minutes') if deadline else None, 'updated_at': datetime.now().isoformat(timespec='microseconds')}
        else:
            values.pop(key, None)
        write_json(OVERRIDES_FILE, values)
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
    key = match_record(records, candidate)
    if not key:
        identity = '|'.join(sorted(candidate.get('identities') or [])) or normalize(candidate['course']) + '|' + normalize(candidate['title'])
        key = 'work:' + hashlib.sha256(identity.encode()).hexdigest()[:24]
        records.setdefault(key, {'key': key, 'first_seen': now_iso(), 'questions': [], 'status': 'unknown'})
    record = records[key]
    incoming_notice = parse_datetime(candidate.get('notice_sent'))
    known_notice = parse_datetime(record.get('deadline_notice_time') or record.get('notice_sent'))
    stale_notice = bool(incoming_notice and known_notice and incoming_notice < known_notice)
    candidate_deadline = candidate.get('deadline')
    accept_deadline = bool(candidate_deadline)
    if candidate.get('deadline_source') == 'notice' and record.get('deadline'):
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
        if field in {'entry_url', 'notice_sent'} and stale_notice and record.get(field):
            continue
        if field == 'start' and record.get('start'):
            continue
        record[field] = value
    if parse_datetime(record.get('deadline')):
        record['deadline_kind'] = 'known'
    if accept_deadline and incoming_notice:
        record['deadline_notice_time'] = candidate.get('notice_sent')
    record['last_seen'] = now_iso()
    return record

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
                        existing = load_draft(keep)
                        incoming = load_draft(remove)
                        existing.setdefault('answers', {})
                        for qid, answer in incoming.get('answers', {}).items():
                            existing['answers'].setdefault(qid, answer)
                        if incoming.get('legacy_text') and incoming['legacy_text'] != existing.get('legacy_text'):
                            existing['legacy_text'] = (existing.get('legacy_text', '') + '\n\n' + incoming['legacy_text']).strip()
                        existing.setdefault('merged_drafts', {})[remove] = source
                        drafts[keep] = existing
                        write_json(DRAFTS_FILE, drafts)
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
        browser = playwright.chromium.connect_over_cdp(CHAOXING_CDP_URL, timeout=2500)
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

def request(context, url, **kwargs):
    for attempt in range(2):
        try:
            response = context.request.get(url, timeout=15000, **kwargs)
            if response.status >= 400:
                raise RuntimeError(f'服务器返回 HTTP {response.status}')
            return response
        except Exception as exc:
            if attempt:
                raise RuntimeError(safe_error(exc)) from None
            time.sleep(0.5)

def fetch_html(context, url):
    if not dom.platform_url(url):
        raise ValueError('作业地址不属于已支持的学习通平台')
    response = request(context, url)
    markup, final = response.text(), response.url
    if dom.is_login(markup, final):
        raise RuntimeError('LOGIN_REQUIRED:' + (urlsplit(url).hostname or ''))
    if not markup.strip():
        raise RuntimeError('服务器返回空页面')
    return markup, final

def parse_notice_attachment(raw):
    try:
        entries = json.loads(raw) if isinstance(raw, str) else raw or []
    except ValueError:
        return None, []
    url, attachments = None, []
    for entry in entries if isinstance(entries, list) else []:
        web = entry.get('att_web', {}) if isinstance(entry, dict) else {}
        if isinstance(web, dict) and str(web.get('examOrWork', '')).lower() == 'work':
            url = web.get('url') or url
    return url, attachments

def collect_notices(context, state, stats):
    seen = set(state.get('notice_seen') or [])
    cursor, used, fresh_ids = '', set(), []
    page_limit = 30 if not state.get('notice_backfilled') else 10
    ended = False
    pending_cursors = list(state.get('notice_pending_cursors') or [])
    if state.get('notice_backfill_cursor') and state['notice_backfill_cursor'] not in pending_cursors:
        pending_cursors.append(state['notice_backfill_cursor'])
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
        container = payload.get('notices') if isinstance(payload, dict) else None
        # The real API terminates pagination with {"status": true}, without a
        # notices object. Only accept that form after a nonempty cursor.
        if cursor and isinstance(payload, dict) and payload.get('status') is True and container is None:
            container = {'list': [], 'lastPage': 1}
        if not isinstance(payload, dict) or not payload.get('status') or not isinstance(container, dict):
            raise RuntimeError('通知读取失败，可能需要重新登录')
        notices = container.get('list') or []
        ids = [str(n.get('idCode', '')) for n in notices]
        for n in notices:
            content = str(n.get('content') or '')
            def field(label):
                m = re.search(re.escape(label) + r'\s*[：:]\s*([^\r\n]+)', content)
                return m.group(1).strip() if m else ''
            course, title = field('课程名称'), field('作业名称')
            if not course or not title:
                continue
            url, _ = parse_notice_attachment(n.get('attachment'))
            start, deadline = time_fields(content)
            item = {'course': course, 'title': title, 'start': start, 'deadline': deadline,
                    'deadline_source': 'notice' if deadline else None,
                    'notice_sent': standard_time(n.get('sendTime')), 'entry_url': url,
                    'identities': dom.url_identity(url), 'source_notice_ids': [str(n.get('idCode'))], 'status': 'unknown'}
            merge_record(state['assignments'], item)
        fresh_ids.extend(ids)
        page_known = bool(ids) and all(i in seen for i in ids)
        at_end = not notices or str(container.get('lastPage')) == '1'
        next_cursor = str(container.get('lastGetId') or '')
        if (page_known or at_end) and pending_cursors:
            next_cursor = pending_cursors.pop(0)
        elif at_end or (state.get('notice_backfilled') and page_known):
            ended = True
            break
        if not next_cursor or next_cursor in used:
            ended = True
            break
        used.add(next_cursor)
        cursor = next_cursor
        time.sleep(0.25)
    state['notice_seen'] = list(dict.fromkeys(fresh_ids + list(seen)))[:1200]
    if ended:
        state['notice_backfilled'] = True
        state.pop('notice_backfill_cursor', None)
        state.pop('notice_pending_cursors', None)
    else:
        state['notice_backfill_cursor'] = cursor
        state['notice_pending_cursors'] = list(dict.fromkeys([cursor] + pending_cursors))
        stats['warnings'].append('通知较多，本次达到分页上限，已有作业仍保留。')

def collect_course_rows(context, state, stats):
    markup, _ = fetch_html(context, COURSES_URL)
    courses = dom.parse(markup).all('li', attr='courseid')
    if not courses:
        raise RuntimeError('课程列表结构无法识别，请检查登录状态')
    targets = {}
    for course in courses:
        cid, clazz = course.attrs.get('courseid'), course.attrs.get('clazzid')
        if cid and clazz:
            targets[('mooc1.chaoxing.com', cid, clazz)] = dom.clean_text(course.first('dt') or course)
    # School-hosted courses have a separate login and are absent from the main list.
    for r in state['assignments'].values():
        if record_group(r) == 'history':
            continue
        u = urlsplit(r.get('entry_url') or r.get('work_url') or '')
        q = {k.lower(): v[0] for k, v in parse_qs(u.query).items() if v}
        if u.hostname == 'mooc.istudy.szpu.edu.cn' and q.get('courseid') and q.get('classid'):
            targets[(u.hostname, q['courseid'], q['classid'])] = r.get('course', '')
    for (host, cid, clazz), name in targets.items():
        try:
            markup, base = fetch_html(context, f'https://{host}/work/task-list?courseId={cid}&classId={clazz}&vx=1')
            stats['course_lists'] += 1
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
                merge_record(state['assignments'], candidate)
            time.sleep(0.15)
        except Exception as exc:
            stats['warnings'].append(f'{name}：{safe_error(exc)}')

def asset_downloader(context):
    def download(url):
        u = urlsplit(url)
        host = u.hostname or ''
        if u.scheme not in {'https', 'http'} or not (host.endswith(('.chaoxing.com', '.chaoxing.com.cn', '.chaoxing.net')) or host in {'mooc.istudy.szpu.edu.cn', 'cs.istudy.szpu.edu.cn'}):
            return url
        name = hashlib.sha256(url.encode()).hexdigest()[:32]
        folder = CONTENT_DIR / 'assets'
        folder.mkdir(parents=True, exist_ok=True)
        existing = list(folder.glob(name + '.*'))
        if existing:
            return '/assets/' + existing[0].name
        try:
            response = request(context, url)
            suffix = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/webp': '.webp'}.get(response.headers.get('content-type', '').split(';')[0])
            data = response.body()
            # The school image server sometimes labels JPEG bytes as image/png.
            if data.startswith(b'\x89PNG\r\n\x1a\n'):
                suffix = '.png'
            elif data.startswith(b'\xff\xd8\xff'):
                suffix = '.jpg'
            elif data.startswith((b'GIF87a', b'GIF89a')):
                suffix = '.gif'
            elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
                suffix = '.webp'
            if suffix and len(data) <= 12_000_000:
                (folder / (name + suffix)).write_bytes(data)
                return '/assets/' + name + suffix
        except Exception:
            pass
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
    elif re.search(r'/do(?:home)?work', urlsplit(url).path, re.I) and ((parse_datetime(record.get('deadline')) or datetime.min) > datetime.now() or record.get('deadline_kind') == 'none'):
        record.pop('availability', None)
    if not deadline and record.get('deadline_kind') != 'none' and not record.get('deadline'):
        record['deadline_reason'] = '平台页面未显示截止时间，暂列待核实'
    if re.search(r'作业已提交|您已提交|提交成功', text):
        record['status'] = 'submitted'
    record['identities'] = sorted(set(record.get('identities') or []) | set(dom.page_identity(markup, url)))
    record['metadata_checked_at'] = now_iso()
    return text

def read_content(context, record, stats, force=False):
    url = record.get('entry_url') or record.get('list_url') or record.get('work_url')
    if not url:
        record['content_status'] = '缺少作答入口，等待通知或课程列表补全'
        return
    stats['details'] += 1
    from homework_navigation import resolve_work_page
    markup, final = resolve_work_page(context, record, fetch_html)
    update_metadata(record, markup, final)
    record['content_entry_checked'] = url
    record['work_url'] = final
    if record_group(record) == 'history':
        return
    questions = dom.parse_questions(markup, final)
    if not questions:
        entry = dom.phone_entry(markup, final)
        if entry:
            markup, final = fetch_html(context, entry)
            update_metadata(record, markup, final)
            if record_group(record) == 'history':
                return
            questions = dom.parse_questions(markup, final)
    record['work_url'] = final
    if not questions:
        record['content_status'] = '页面没有可识别的原题，未把摘要或题号列表存为题目'
        return
    questions = dom.parse_questions(markup, final, asset_downloader(context))
    record['questions'] = questions
    # Old versions collected links from the entire page, including plugin updates.
    # Keep that snapshot, but display only links inside verified question content.
    record.setdefault('legacy_attachments', record.get('attachments') or [])
    attachments = {}
    for question in questions:
        for fragment in [question.get('html', ''), *[option.get('html', '') for option in question.get('options', [])]]:
            for anchor in dom.parse(fragment).all('a'):
                href = anchor.attrs.get('href') or ''
                if href.startswith(('https://', 'http://')):
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


def check_once(headless=True, force_key=None, on_auth=None):
    with browser_operation():
        return _check_once(headless, force_key, on_auth)

def _check_once(headless=True, force_key=None, on_auth=None):
    state = load_state()
    stats = {'notice_pages': 0, 'course_lists': 0, 'details': 0, 'cache_hits': 0, 'warnings': []}
    connected = False
    try:
        with sync_playwright() as p:
            context = open_context(p, headless)
            try:
                if headless:
                    import homework_auth
                    auth = homework_auth.status(restore=True, context=context, progress=PROGRESS, on_state=on_auth)
                    if not any(auth['sites'].values()):
                        raise RuntimeError('请先登录作业通')
                    if not auth['logged_in']:
                        stats['warnings'].append('部分站点尚未登录')
                if not force_key:
                    log('正在同步通知与课程作业状态…')
                    for collector in (collect_notices, collect_course_rows):
                        try:
                            collector(context, state, stats)
                            connected = True
                        except Exception as exc:
                            stats['warnings'].append(safe_error(exc))
                else:
                    connected = True
                reconcile_aliases(state)
                for key, record in list(state['assignments'].items()):
                    if key not in state['assignments']:
                        continue
                    if force_key and key != force_key:
                        continue
                    identity_probe = needs_identity_probe(record, state['assignments'])
                    entry_upgrade = needs_entry_upgrade(record)
                    if record_group(record) == 'history' and key != force_key and not record.get('metadata_needs_check') and not identity_probe:
                        continue
                    attempted = parse_datetime(record.get('content_attempted_at'))
                    valid = record.get('content_version') == CONTENT_VERSION and bool(record.get('questions'))
                    checked = parse_datetime(record.get('metadata_checked_at'))
                    due = record.get('metadata_needs_check') or not checked or datetime.now() - checked > timedelta(hours=6)
                    if not force_key and record.get('content_error') and attempted and datetime.now() - attempted < timedelta(minutes=30) and not record.get('metadata_needs_check') and not entry_upgrade:
                        stats['cache_hits'] += 1
                        continue
                    if not force_key and not record.get('metadata_needs_check') and not identity_probe and not entry_upgrade and ((valid and not due) or (not valid and attempted and datetime.now() - attempted < timedelta(minutes=30))):
                        stats['cache_hits'] += 1
                        continue
                    if not record.get('entry_url') and not record.get('list_url') and not record.get('work_url'):
                        continue
                    log(f'正在核对：{record.get("course")} / {record.get("title")}')
                    record['content_attempted_at'] = now_iso()
                    try:
                        read_content(context, record, stats, force=bool(force_key))
                        if identity_probe:
                            record['identity_probe_done'] = True
                        record.pop('content_error', None)
                        reconcile_aliases(state)
                        # A proven alias may reveal a better PC entry only after
                        # the legacy mobile content was read earlier in this pass.
                        merged_key = state.get('record_aliases', {}).get(key, key)
                        merged = state['assignments'].get(merged_key)
                        if merged and needs_entry_upgrade(merged):
                            read_content(context, merged, stats, force=bool(force_key))
                            merged.pop('content_error', None)
                            reconcile_aliases(state)
                    except Exception as exc:
                        reason = safe_error(exc)
                        record['content_error'] = '该课程的平台需要重新登录' if 'LOGIN_REQUIRED' in reason else reason
                        stats['warnings'].append(f'{record.get("course")}：{record["content_error"]}')
            finally:
                close_context(context)
    except Exception as exc:
        stats['warnings'].append(safe_error(exc))
    state['last_check'] = now_iso()
    if connected:
        state['last_success'] = now_iso()
    state['last_stats'] = stats
    save_state(state)
    LATEST_FILE.write_text(render_summary(active_pending(state)) + '\n', encoding='utf-8')
    log(f'同步完成：通知 {stats["notice_pages"]} 页，课程 {stats["course_lists"]} 门，详情 {stats["details"]} 次，复用 {stats["cache_hits"]} 项。')
    return 0 if connected else 2

def render_summary(items):
    return '\n'.join(f'{a.get("course")}｜{a.get("title")}｜{a.get("deadline") or "平台未设截止时间"}' for a in items) or '当前没有已确认仍可作答的待办作业。'

def notify(title, message):
    if os.environ.get('CHAOXING_GUI') == '1':
        write_json(UI_NOTIFICATION_FILE, {'title': title, 'message': message, 'created_at': now_iso()})
    else:
        subprocess.Popen(['msg.exe', os.environ.get('USERNAME', '*'), '/TIME:300', title + '\n' + message[:900]], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

def ensure_chrome(url=None, app=False):
    port = urlsplit(CHAOXING_CDP_URL).port or 9222
    args = runtime.browser_args(url, app=app, port=port)
    subprocess.Popen(args, creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))

LOGIN_TARGET_ID = None
LOGIN_TARGET_PLATFORM = None
LOGIN_URLS = {'chaoxing': NOTICE_URL, 'school': 'https://passport.istudy.szpu.edu.cn/'}


def login_page_platform(url):
    parsed = urlsplit(url)
    if parsed.hostname == 'passport.istudy.szpu.edu.cn':
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


def login_mode(platform='chaoxing'):
    global LOGIN_TARGET_ID, LOGIN_TARGET_PLATFORM
    if platform not in LOGIN_URLS:
        raise ValueError('未知的登录平台')
    # An already-running Chrome can acknowledge a command-line launch without
    # opening its URLs. Create visible windows through its verified connection.
    with browser_operation(), sync_playwright() as playwright:
        try:
            browser = playwright.chromium.connect_over_cdp(CHAOXING_CDP_URL, timeout=1000)
        except Exception:
            ensure_chrome()
            browser = None
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                try:
                    browser = playwright.chromium.connect_over_cdp(CHAOXING_CDP_URL, timeout=500)
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
                    navigate_login_target(browser, target_id, LOGIN_URLS[platform])
            else:
                target = session.send('Target.createTarget', {
                    'url': LOGIN_URLS[platform], 'newWindow': True, 'background': False,
                })
                if not target.get('targetId'):
                    raise RuntimeError('登录窗口未能打开，请重试登录。')
                target_id = target['targetId']
            LOGIN_TARGET_ID = target_id
            LOGIN_TARGET_PLATFORM = platform
            window = session.send('Browser.getWindowForTarget', {'targetId': target_id})
            if window.get('bounds', {}).get('windowState') == 'minimized':
                session.send('Browser.setWindowBounds', {'windowId': window['windowId'], 'bounds': {'windowState': 'normal'}})
            session.send('Target.activateTarget', {'targetId': target_id})
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

def save_draft(key, payload):
    if not isinstance(payload.get('answers'), dict):
        raise ValueError('答案格式不正确')
    with STATE_LOCK, draft_transaction():
        key = canonical_key(key)
        if key not in load_state()['assignments']:
            raise ValueError('作业不存在')
        if record_group(load_state()['assignments'][key]) == 'history':
            raise ValueError('历史作业仅保留基本信息，不再保存答案')
        drafts = read_json(DRAFTS_FILE, {})
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
        drafts[key] = value
        write_json(DRAFTS_FILE, drafts)
    return value

def canonical_key(key):
    aliases = load_state().get('record_aliases') or {}
    seen = set()
    while key in aliases and key not in seen:
        seen.add(key)
        key = aliases[key]
    return key
