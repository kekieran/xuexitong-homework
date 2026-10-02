"""Local-only homework assistant; reminders stay alive when its page closes."""
from __future__ import annotations
import argparse
import ctypes
import json
import mimetypes
import os
import secrets
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import homework_engine as backend
import homework_auth
import homework_workspace as workspace
import homework_retention as retention
import homework_ai

APP_DIR = backend.runtime.APP_DIR
DATA_DIR = backend.runtime.DATA_DIR
UI_DIR = backend.runtime.UI_DIR
RUNTIME_FILE = DATA_DIR / "ui_runtime.json"
SETTINGS_FILE = DATA_DIR / "ui_settings.json"
UPLOAD_DIR = DATA_DIR / "answer_attachments"
# The complete front-end whitelist for Handler.do_GET: request path -> (file name
# inside UI_DIR, Content-Type before the "; charset=utf-8" suffix). Every entry is
# served with the __APP_TOKEN__ placeholder replaced by the session token.
STATIC_ASSETS = {
    "/": ("ui.html", "text/html"),
    "/ui.js": ("ui.js", "text/javascript"),
    "/ui.css": ("ui.css", "text/css"),
    "/workspace.js": ("workspace.js", "text/javascript"),
    "/ai.js": ("ai.js", "text/javascript"),
}
TOKEN = secrets.token_urlsafe(32)
COOKIE = "homework_session"
JOB_LOCK = threading.Lock()
JOB = {"busy": False, "action": "", "message": "就绪", "revision": 0, "result": None}
AUTH_LOCK = threading.Lock()
AUTH = {"state": "checking", "sites": {"chaoxing": False, "school": False}}
STOP = threading.Event()
MAINTENANCE = threading.Event()
DEFAULT_REMINDER_TIMES = ()
MAX_REMINDER_TIMES = 12
REMINDER_WINDOW_SECONDS = 3 * 60
REMINDER_INTERVAL_SECONDS = 20
SETTINGS_LOCK = threading.Lock()
SETTINGS = None

def settings():
    global SETTINGS
    if SETTINGS is None:
        SETTINGS = backend.read_json(SETTINGS_FILE, {"shown_slots": []})
    return SETTINGS

def normalize_reminder_times(value):
    if value is None:
        value = DEFAULT_REMINDER_TIMES
    if not isinstance(value, (list, tuple)):
        raise ValueError('提醒时间格式不正确')
    result = set()
    for item in value:
        if not isinstance(item, str) or len(item) != 5 or item[2] != ':' or not item[:2].isdigit() or not item[3:].isdigit():
            raise ValueError('提醒时间格式不正确')
        hour, minute = int(item[:2]), int(item[3:])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError('提醒时间格式不正确')
        result.add(f'{hour:02d}:{minute:02d}')
    if len(result) > MAX_REMINDER_TIMES:
        raise ValueError(f'提醒时间最多设置 {MAX_REMINDER_TIMES} 个')
    return sorted(result)

def reminder_clock(value):
    """Split an already validated 'HH:MM' reminder time into (hour, minute)."""
    hour, minute = value.split(':', 1)
    return int(hour), int(minute)

def reminder_slot(now, value):
    """Slot marker for one reminder time when `now` is inside its window, else None."""
    hour, minute = reminder_clock(value)
    started = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if 0 <= (now - started).total_seconds() < REMINDER_WINDOW_SECONDS:
        return f'{now:%Y-%m-%d}-{value}'
    return None

def next_reminder_at(now, times):
    candidates = []
    for value in times:
        hour, minute = reminder_clock(value)
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        candidates.append(candidate)
    return min(candidates) if candidates else None

def reminder_snapshot():
    with SETTINGS_LOCK:
        current = settings()
        enabled = current.get('reminder_enabled', True) is not False
        times = normalize_reminder_times(current.get('reminder_times'))
    now = datetime.now()
    upcoming = next_reminder_at(now, times) if enabled else None
    return {'enabled': enabled, 'times': times, 'max_times': MAX_REMINDER_TIMES,
            'next_check': upcoming.isoformat(timespec='minutes') if upcoming else None}

def set_reminder(enabled, times=None):
    with SETTINGS_LOCK:
        current = settings()
        current['reminder_enabled'] = bool(enabled)
        if times is not None:
            current['reminder_times'] = normalize_reminder_times(times)
        elif 'reminder_times' not in current:
            current['reminder_times'] = list(DEFAULT_REMINDER_TIMES)
        backend.write_json(SETTINGS_FILE, current)
    return reminder_snapshot()

def show_reminder(records):
    """Toast body for the scheduled reminder.

    Deliberately a different wording from engine.render_summary(), which writes
    the CLI/text-file digest of the same records: this one is capped at five
    items because a Windows toast is truncated anyway. Keep them separate.
    """
    items = sorted(records, key=lambda record: backend.parse_datetime(record.get('deadline')) or datetime.max)
    lines = [f'共 {len(items)} 项未截止、未提交作业']
    for record in items[:5]:
        deadline = backend.parse_datetime(record.get('deadline'))
        deadline_text = deadline.strftime('%m-%d %H:%M 截止') if deadline else '平台未设截止时间'
        lines.append(f"{record.get('course') or '未命名课程'} · {record.get('title') or '未命名作业'}\n{deadline_text}")
    if len(items) > 5:
        lines.append(f'还有 {len(items) - 5} 项未列出，可在助手里查看全部。')
    backend.notify('作业提醒', '\n'.join(lines))

def auth_snapshot():
    with AUTH_LOCK:
        snapshot = dict(AUTH, sites=dict(AUTH['sites']))
    snapshot['credentials'] = homework_auth.credential_info()
    return snapshot

def set_auth(value):
    with AUTH_LOCK:
        AUTH.update(state='logged_in' if value['logged_in'] else 'login_required', sites=value['sites'])
    MAINTENANCE.set()

def job_snapshot():
    with JOB_LOCK:
        job = dict(JOB)
    job['auth'] = auth_snapshot()
    job['reminder'] = reminder_snapshot()
    return job

def progress(message, *args, **kwargs):
    with JOB_LOCK:
        JOB["message"] = str(message)

def start_job(action, key=None, credentials=None):
    with JOB_LOCK:
        if JOB["busy"]:
            return False
        JOB.update(busy=True, action=action, key=key, message="正在处理…", result=None)
    def run():
        try:
            if action == "refresh":
                code = backend.check_once(headless=True, force_key=key, on_auth=set_auth)
                warnings = backend.load_state().get('last_stats', {}).get('warnings', [])
                result = {"message": ("部分信息未更新" if warnings else "作业已更新") if code == 0 else
                          (warnings[0] if warnings else "刷新失败，请重试"), "error": code != 0}
            elif action == "login":
                set_auth(homework_auth.status(**credentials, progress=progress, on_state=set_auth))
                if auth_snapshot()['state'] != 'logged_in':
                    raise ValueError('登录尚未完成，请重试')
                result = {"message": "作业通已登录，两个站点的登录状态均已确认"}
            elif action in ("fill", "open"):
                import homework_fill
                result = (homework_fill.fill_assignment if action == "fill" else homework_fill.open_assignment)(key)
            elif action == 'ai':
                result = homework_ai.solve(key, progress=progress)
            elif action == 'ai-test':
                result = homework_ai.test_connection()
            else:
                raise ValueError("未知操作")
            with JOB_LOCK:
                JOB.update(message=result.get("message", "操作完成"), result=result)
        except Exception as exc:
            if action == 'login':
                with AUTH_LOCK:
                    AUTH['state'] = 'login_required'
            with JOB_LOCK:
                JOB.update(message=backend.safe_error(exc), result={"error": True})
        finally:
            MAINTENANCE.set()
            with JOB_LOCK:
                JOB["busy"] = False
                JOB["revision"] += 1
    threading.Thread(target=run, daemon=True).start()
    return True

def canonical_key(key):
    """Normalise one assignment key through backend.canonical_key().

    Forwarding layer only: every request handler in this module calls this name
    (never backend.canonical_key directly) so the app layer keeps a single seam
    that tests can patch. The normalisation rules themselves stay in the engine.
    """
    return backend.canonical_key(key)


def workspace_snapshot():
    """Additive API fields: per-record progress/capabilities plus dashboard v1."""
    with backend.STATE_LOCK:
        state = backend.load_state()
        overrides = backend.local_overrides()
        drafts = backend.read_json(backend.DRAFTS_FILE, {})
        records = []
        for key, item in state.get('assignments', {}).items():
            record = dict(backend.effective_record(item, overrides), key=key, group=backend.record_group(item))
            if record['group'] == 'history':
                record = retention.summary(record)
            record['auth_platform'] = retention.platform(record)
            # Display layer only: a confirmed login hides the stale error in this
            # response. retention.clear_login_errors() does the persistent cleanup.
            if retention.login_error(record) and auth_snapshot()['sites'].get(record['auth_platform']):
                record.pop('content_error', None)
            record['progress'] = workspace.answer_progress(record, {} if record['group'] == 'history' else drafts.get(key, {}))
            record['capabilities'] = workspace.capabilities(record)
            records.append(record)
    return {'assignments': records, 'record_aliases': state.get('record_aliases', {}),
            'last_success': state.get('last_success'), 'last_stats': state.get('last_stats'),
            'dashboard': workspace.dashboard(records)}

def assignment(key):
    record = backend.load_state().get("assignments", {}).get(canonical_key(key))
    if not record:
        raise ValueError("该作业已更新，请刷新列表")
    return record


def attachment_path(info):
    # Older drafts may have a full path from the previous computer. Resolve the
    # same identified file inside this project's attachment directory instead.
    saved = Path(info.get('path', ''))
    name = saved.name
    if not info.get('id') or Path(name).stem != info['id']:
        raise ValueError('附件标识不正确')
    path = (UPLOAD_DIR / name).resolve()
    if not path.is_relative_to(UPLOAD_DIR.resolve()) or not path.is_file():
        raise ValueError('附件不可用')
    return path

class Handler(BaseHTTPRequestHandler):
    server_version = "HomeworkLocal/2"
    def log_message(self, fmt, *args):
        pass
    def send(self, status, data, content_type="application/json; charset=utf-8", headers=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; object-src 'none'; frame-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)
    def allowed(self, write=False):
        host = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != host:
            return False
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        if COOKIE not in cookie or not secrets.compare_digest(cookie[COOKIE].value, TOKEN):
            return False
        return not write or (secrets.compare_digest(self.headers.get("X-App-Token", ""), TOKEN) and self.headers.get("Origin", "http://" + host) == "http://" + host)
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == "/" and query.get("token") == [TOKEN] and self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}":
            self.send(303, b"", headers={"Location": "/", "Set-Cookie": f"{COOKIE}={TOKEN}; HttpOnly; SameSite=Strict; Path=/"})
            return
        if not self.allowed():
            self.send(403, {"error": "请从桌面启动入口打开助手"})
            return
        try:
            if url.path in STATIC_ASSETS:
                name, content_type = STATIC_ASSETS[url.path]
                data = (UI_DIR / name).read_bytes().replace(b"__APP_TOKEN__", TOKEN.encode())
                self.send(200, data, content_type + "; charset=utf-8")
            elif url.path == '/api/ai/settings':
                self.send(200, homework_ai.public_settings())
            elif url.path == "/api/state":
                self.send(200, dict(workspace_snapshot(), job=job_snapshot()))
            elif url.path == "/api/dashboard":
                self.send(200, workspace_snapshot()['dashboard'])
            elif url.path == "/api/job":
                self.send(200, job_snapshot())
            elif url.path == "/api/draft":
                key = canonical_key(query.get("key", [""])[0])
                record = assignment(key)
                self.send(200, {"answers": {}} if backend.record_group(record) == 'history' else backend.load_draft(key))
            elif url.path.startswith("/assets/"):
                root = (Path(backend.CONTENT_DIR) / "assets").resolve()
                path = (root / urllib.parse.unquote(url.path.removeprefix("/assets/"))).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    raise ValueError("图片不存在")
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                if not mime.startswith("image/"):
                    raise ValueError("不支持的图片格式")
                self.send(200, path.read_bytes(), mime)
            elif url.path == "/api/attachment":
                key, file_id = canonical_key(query.get("key", [""])[0]), query.get("id", [""])[0]
                assignment(key)
                files = [f for a in backend.load_draft(key).get("answers", {}).values() for f in a.get("files", [])]
                match = next((f for f in files if f.get("id") == file_id), None)
                if not match:
                    raise ValueError("找不到已保存的附件")
                path = attachment_path(match)
                name = urllib.parse.quote(match.get("name", "answer"), safe="")
                self.send(200, path.read_bytes(), "application/octet-stream", {"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})
            else:
                self.send(404, {"error": "页面不存在"})
        except Exception as exc:
            self.send(400, {"error": backend.safe_error(exc)})
    def do_POST(self):
        if not self.allowed(write=True):
            self.send(403, {"error": "请求已失效，请重新打开助手"})
            return
        url = urllib.parse.urlsplit(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            limit = 30 * 1024 * 1024 if url.path == "/api/upload" else 2 * 1024 * 1024
            if length <= 0 or length > limit:
                raise ValueError("文件不能超过 30 MB" if url.path == "/api/upload" else "请求内容大小不正确")
            raw = self.rfile.read(length)
            if url.path == "/api/upload":
                query = urllib.parse.parse_qs(url.query)
                key, qid = canonical_key(query.get("key", [""])[0]), query.get("qid", [""])[0]
                with backend.STATE_LOCK:
                    record = assignment(key)
                    if backend.record_group(record) == 'history':
                        raise ValueError('历史作业仅保留基本信息，不再保存答案')
                    if not any(str(q["id"]) == qid for q in record.get("questions", [])):
                        raise ValueError("题目不存在，请重新打开该作业")
                    name = Path(query.get("name", ["answer"])[0]).name[:180]
                    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                    file_id = secrets.token_hex(16)
                    path = UPLOAD_DIR / (file_id + Path(name).suffix[:16])
                    path.write_bytes(raw)
                self.send(200, {"id": file_id, "name": name, "path": path.relative_to(APP_DIR).as_posix(), "size": length})
                return
            payload = json.loads(raw.decode("utf-8"))
            # Resolve the assignment key only where a branch needs it:
            # /api/ai/settings, /api/reminder and /api/exit take none at all.
            if url.path == '/api/ai/settings':
                if job_snapshot()['busy']:
                    raise ValueError('请等待当前操作结束')
                self.send(200, homework_ai.save_settings(payload))
            elif url.path == '/api/ai/template':
                if not isinstance(payload.get('adopt'), bool) or not isinstance(payload.get('qid'), str):
                    raise ValueError('模板操作不正确')
                key = canonical_key(payload.get("key"))
                self.send(200, homework_ai.apply_template(key, payload['qid'], payload['adopt']))
            elif url.path == "/api/draft":
                with backend.STATE_LOCK, backend.draft_transaction():
                    key = canonical_key(payload.get("key"))
                    assignment(key)
                    draft = payload["draft"]
                    if key != payload.get("key"):
                        merged = backend.load_draft(key)
                        merged["answers"] = backend.merge_draft_answers(
                            merged.get("answers"), draft.get("answers"), incoming_wins=True)
                        old_text = draft.get("legacy_text", "")
                        if old_text and old_text not in merged.get("legacy_text", ""):
                            merged["legacy_text"] = (merged.get("legacy_text", "") + "\n\n" + old_text).strip()
                        draft = merged
                    saved = backend.save_draft(key, draft)
                self.send(200, saved)
            elif url.path == "/api/assignment-settings":
                key = canonical_key(payload.get("key"))
                # Serialize with the crawler so it cannot restore purged content.
                with backend.browser_operation():
                    assignment(key)
                    result = backend.set_local_override(key, payload.get('completed'), payload.get('deadline'))
                    retention.cleanup_history()
                self.send(200, result)
            elif url.path == "/api/action":
                action = payload.get("action")
                key = canonical_key(payload.get("key"))
                if action not in ("refresh", "login", "open", "fill", "ai", "ai-test"):
                    raise ValueError("未知操作")
                credentials = None
                if action == 'login':
                    username, password = payload.get('username'), payload.get('password')
                    if payload.get('saved') is True:
                        if not homework_auth.credential_info()['saved']:
                            raise ValueError('保存的登录信息不可用，请重新输入')
                        credentials = {'restore': True, 'force_restore': True}
                    else:
                        if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
                            raise ValueError('请输入账号和密码')
                        credentials = {'username': username.strip(), 'password': password}
                if key:
                    assignment(key)
                if action in ("open", "fill", "ai") and not key:
                    raise ValueError("请先选择作业")
                self.send(202, job_snapshot()) if start_job(action, key, credentials) else self.send(409, {"error": "当前操作尚未完成，请稍候"})
            elif url.path == "/api/reminder":
                if not isinstance(payload.get("enabled"), bool):
                    raise ValueError("提醒开关格式不正确")
                self.send(200, set_reminder(payload['enabled'], payload.get('times')))
            elif url.path == "/api/exit":
                self.send(200, {"message": "助手已退出，提醒已停止"})
                STOP.set()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send(404, {"error": "接口不存在"})
        except Exception as exc:
            self.send(400, {"error": backend.safe_error(exc)})

def reminder_loop():
    while not STOP.wait(REMINDER_INTERVAL_SECONDS):
        now = datetime.now()
        with SETTINGS_LOCK:
            current = settings()
            times = normalize_reminder_times(current.get('reminder_times'))
            shown = current.setdefault("shown_slots", [])
            # The first configured time (sorted) whose window still contains `now`.
            slot = None
            for value in times:
                slot = reminder_slot(now, value)
                if slot:
                    break
            if current.get("reminder_enabled", True) is False or not slot or slot in shown:
                continue
            shown.append(slot)
            current["shown_slots"] = shown[-120:]
            backend.write_json(SETTINGS_FILE, current)
        active = backend.active_pending(backend.load_state())
        if active:
            show_reminder(active)

def auth_watch_loop():
    if STOP.wait(3):
        return
    while not STOP.is_set():
        delay = 120
        if not job_snapshot()['busy']:
            try:
                result = homework_auth.status(restore=False)
                set_auth(result)
                if not result['logged_in']:
                    delay = 30
            except homework_auth.BrowserUnavailable:
                delay = 5
            except Exception:
                delay = 5
                with AUTH_LOCK:
                    if AUTH['state'] == 'checking':
                        AUTH['state'] = 'login_required'
        else:
            delay = 5
        if STOP.wait(delay):
            break


def maintenance_loop():
    while not STOP.is_set():
        MAINTENANCE.clear()
        try:
            with backend.browser_operation():
                retention.clear_login_errors(auth_snapshot()['sites'])
                retention.cleanup_history()
        except Exception:
            # The crawler may own the browser lock, or a file may be temporarily
            # locked. Retry without interfering with the current user action.
            pass
        MAINTENANCE.wait(30)

def acquire_instance_lock():
    if os.name != "nt":
        return 1
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    suffix = "_" + backend.runtime.PROJECT_ID
    handle = kernel.CreateMutexW(None, 0, "Local\\ChaoxingHomeworkAssistant" + suffix)
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(handle)
        return None
    return handle

def main():
    parser = argparse.ArgumentParser(description="学习通作业助手")
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--no-browser", action="store_true", help="仅启动本地界面服务")
    args = parser.parse_args()
    instance = acquire_instance_lock()
    if instance is None:
        url = backend.read_json(RUNTIME_FILE, {}).get("url", "")
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname == "127.0.0.1" and parsed.scheme == "http":
            try:
                # HTTPError 403 is expected after the bootstrap redirect without a cookie jar.
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
                opener.open(url, timeout=2).close()
                if not args.background:
                    backend.ensure_chrome(url, app=True)
            except Exception:
                pass
        return
    retention.cleanup_history()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    backend.PROGRESS = progress
    url = f"http://127.0.0.1:{server.server_port}/?token={TOKEN}"
    backend.write_json(RUNTIME_FILE, {"url": url, "pid": os.getpid()})
    threading.Thread(target=reminder_loop, daemon=True).start()
    threading.Thread(target=auth_watch_loop, daemon=True).start()
    threading.Thread(target=maintenance_loop, daemon=True).start()
    if not args.background and not args.no_browser:
        threading.Thread(target=backend.ensure_chrome, kwargs={"url": url, "app": True}, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        STOP.set()
        server.server_close()
        RUNTIME_FILE.unlink(missing_ok=True)
        if os.name == "nt" and instance:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel.CloseHandle(instance)

if __name__ == "__main__":
    main()
