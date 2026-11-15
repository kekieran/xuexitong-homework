"""Project-relative paths plus the browser used for controlled platform work."""
from pathlib import Path
import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import shlex
import time
import urllib.request
from urllib.parse import quote, urlsplit

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APP_DIR / 'data'
UI_DIR = APP_DIR / 'ui'
DATA_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_DIR = DATA_DIR / 'browser_data'
UI_PROFILE_DIR = DATA_DIR / 'ui_browser_data'
PROJECT_ID = hashlib.sha256(str(APP_DIR.resolve()).casefold().encode('utf-8')).hexdigest()[:16]


CHROMIUM_EXECUTABLES = {
    'msedge.exe', 'chrome.exe', 'brave.exe', 'vivaldi.exe',
    'chromium.exe', 'opera.exe', 'opera_gx.exe',
}


def _windows_argv(command):
    """Split a command line into arguments for the current platform.

    Windows is parsed with CommandLineToArgvW so quoted executable paths survive
    intact; every other platform falls back to shlex.split. A command line that
    cannot be parsed yields an empty list - callers decide what that means.
    """
    if os.name != 'nt':
        return shlex.split(command)
    from ctypes import wintypes
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    shell.CommandLineToArgvW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    count = ctypes.c_int()
    argv = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not argv:
        return []
    try:
        return [argv[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(argv)


def default_browser_path():
    """Return the default browser executable when it is Chromium-compatible."""
    if os.name != 'nt':
        return None
    import winreg
    try:
        key_name = r'Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
            prog_id = winreg.QueryValueEx(key, 'ProgId')[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog_id + r'\shell\open\command') as key:
            command = winreg.QueryValue(key, None)
        arguments = _windows_argv(command)
        path = Path(os.path.expandvars(arguments[0])) if arguments else None
        return path.resolve() if path and path.is_file() and path.name.lower() in CHROMIUM_EXECUTABLES else None
    except (OSError, ValueError):
        return None


def _installed_browser_paths(executable):
    candidates = []
    located = shutil.which(executable)
    if located:
        candidates.append(Path(located))
    if os.name == 'nt':
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for flag in (0, winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    key_name = rf'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable}'
                    with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | flag) as key:
                        candidates.append(Path(winreg.QueryValue(key, None).strip('"')))
                except OSError:
                    pass
    locations = {
        'chrome.exe': ('Google', 'Chrome', 'Application', 'chrome.exe'),
        'msedge.exe': ('Microsoft', 'Edge', 'Application', 'msedge.exe'),
        'brave.exe': ('BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe'),
        'vivaldi.exe': ('Vivaldi', 'Application', 'vivaldi.exe'),
        'opera.exe': ('Programs', 'Opera', 'opera.exe'),
        'chromium.exe': ('Chromium', 'Application', 'chromium.exe'),
    }
    parts = locations.get(executable)
    if parts:
        for variable in ('PROGRAMW6432', 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
            if os.environ.get(variable):
                candidates.append(Path(os.environ[variable]).joinpath(*parts))
    return candidates


def find_browser():
    """Return the first usable Chromium-compatible executable, or fail loudly."""
    candidates = []
    custom = os.environ.get('HOMEWORK_BROWSER_PATH') or os.environ.get('HOMEWORK_CHROME_PATH')
    if custom:
        path = Path(os.path.expandvars(custom)).expanduser()
        candidates.append(path if path.is_absolute() else APP_DIR / path)
    # Controlled work prefers Chrome. If it is absent, use a compatible
    # default browser; an incompatible default is silently bypassed for Edge.
    candidates.extend(_installed_browser_paths('chrome.exe'))
    default = default_browser_path()
    if default:
        candidates.append(default)
    candidates.extend(_installed_browser_paths('msedge.exe'))
    for executable in ('brave.exe', 'vivaldi.exe', 'chromium.exe', 'opera.exe'):
        candidates.extend(_installed_browser_paths(executable))
    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        identity = str(resolved).casefold()
        if identity not in seen and candidate.is_file():
            return resolved
        seen.add(identity)
    raise RuntimeError('未找到可用于读取作业的浏览器')


def find_chrome():
    """Compatibility alias for find_browser(), kept for older callers and tests.

    Do not delete it and do not collapse it into `find_chrome = find_browser`:
    the module-level function is referenced by tests/test_retention.py,
    tests/test_ai.py and tests/test_workspace.py. New code should call
    find_browser() directly.
    """
    return find_browser()


def browser_args(url=None, app=False, port=9222):
    """Chromium arguments for the standalone UI shell or the account browser.

    The UI shell (`app=True`) keeps its own `UI_PROFILE_DIR` without binding the
    account CDP debugging port, so switching accounts never closes the app UI.
    The app window size is fixed - no caller ever asked for a different one.
    """
    profile = UI_PROFILE_DIR if app else PROFILE_DIR
    args = [str(find_browser())]
    if not app:
        args += ['--remote-debugging-address=127.0.0.1',
                 '--remote-debugging-port=' + str(port)]
    args += ['--no-proxy-server', '--user-data-dir=' + str(profile), '--profile-directory=Default',
             '--no-first-run', '--no-default-browser-check']
    if app:
        width, height = 1440, 980
        args += ['--app=' + url, f'--window-size={width},{height}']
    else:
        args += ['--new-window', url or 'about:blank']
    return args


def chrome_args(url=None, app=False, port=9222):
    """Compatibility alias for browser_args(), kept for older callers and tests.

    Do not delete it and do not collapse it into `chrome_args = browser_args`:
    the module-level function is referenced by tests/test_retention.py. New code
    should call browser_args() directly.
    """
    return browser_args(url, app, port)


FILL_LOADING_MARKER = 'cx-fill-loading'
_FILL_LOADING_STYLE = (
    'html,body{margin:0;height:100%;display:flex;align-items:center;justify-content:center;'
    'background:#f4f7fb;font-family:-apple-system,"Microsoft YaHei",sans-serif;color:#1e293b}'
    '.card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:28px 36px;'
    'box-shadow:0 8px 30px rgba(15,23,42,.08);text-align:center;max-width:420px}'
    '.spin{width:30px;height:30px;margin:0 auto 14px;border:3px solid #dbeafe;'
    'border-top-color:#2563eb;border-radius:50%;animation:s .8s linear infinite}'
    '@keyframes s{to{transform:rotate(360deg)}}'
    'h2{margin:0 0 6px;font-size:17px;font-weight:600;word-break:break-word}'
    'p{margin:0;font-size:13px;color:#64748b}'
)
_FILL_LOADING_HTML = (
    f'<!doctype html><html><head><meta charset="utf-8"><title>正在打开学习通作业…</title>'
    f'<style>{_FILL_LOADING_STYLE}</style></head>'
    f'<body><div id="{FILL_LOADING_MARKER}" class="card"><div class="spin"></div>'
    '<h2>正在打开学习通作答页…</h2><p>正在核对作业状态并准备自动填入</p></div></body></html>'
)
_FILL_LOADING_URL = 'data:text/html;charset=utf-8,' + quote(_FILL_LOADING_HTML, safe='')
_FILL_LOADING_MARKER_QUOTED = quote(FILL_LOADING_MARKER, safe='')


def fill_loading_url():
    """Return a lightweight inline loading splash URL for immediate browser launch."""
    return _FILL_LOADING_URL


def is_blank_or_loading_url(url):
    """Return True for empty tabs or our own fill-loading splash page."""
    text = str(url or '')
    if text in ('', 'about:blank'):
        return True
    return text.startswith('data:text/html') and (
        FILL_LOADING_MARKER in text or _FILL_LOADING_MARKER_QUOTED in text
    )


def show_loading_splash(page, title=None):
    """Render an inline loading card on a blank tab before navigation commits."""
    if page is None:
        return
    try:
        current_url = getattr(page, 'url', '') or ''
    except Exception:
        return
    if str(current_url) not in ('', 'about:blank') and not is_blank_or_loading_url(current_url):
        return
    heading = f'正在打开《{str(title).strip()}》…' if title and str(title).strip() else '正在打开学习通作答页…'
    try:
        page.evaluate(
            """([marker, styleText, heading]) => {
                let card = document.getElementById(marker);
                if (card) {
                    const h2 = card.querySelector('h2');
                    if (h2) h2.textContent = heading;
                    return;
                }
                if (!document.head || !document.body) return;
                document.title = heading;
                const style = document.createElement('style');
                style.textContent = styleText;
                document.head.appendChild(style);
                card = document.createElement('div');
                card.id = marker;
                card.className = 'card';
                const spin = document.createElement('div');
                spin.className = 'spin';
                const h2 = document.createElement('h2');
                h2.textContent = heading;
                const p = document.createElement('p');
                p.textContent = '正在核对作业状态并准备自动填入';
                card.append(spin, h2, p);
                document.body.replaceChildren(card);
            }""",
            [FILL_LOADING_MARKER, _FILL_LOADING_STYLE, heading],
        )
    except Exception:
        pass


class _NoCDPRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _local_cdp_url(value, *, websocket=False):
    """Reject credentials, redirects and non-loopback debugging endpoints."""
    if not isinstance(value, str) or re.search(r'[\s\\]', value):
        raise ValueError('Invalid local debugging address')
    parsed = urlsplit(value)
    schemes = {'ws', 'wss'} if websocket else {'http', 'https'}
    if (parsed.scheme not in schemes or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError('Invalid local debugging address')
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme in {'https', 'wss'} else 80)
    if not 1 <= port <= 65535:
        raise ValueError('Invalid local debugging port')
    if websocket:
        if not re.fullmatch(r'/devtools/browser/[A-Za-z0-9-]+', parsed.path):
            raise ValueError('Invalid browser debugging endpoint')
    elif parsed.path not in {'', '/'}:
        raise ValueError('Invalid local debugging path')
    return parsed, port


def connect_browser_direct(playwright, endpoint, *, timeout=2500):
    """Discover CDP without inheriting environment or Windows proxy settings.

    Playwright's HTTP discovery honors proxy environment variables even for
    localhost. Its WebSocket connection is direct, so only discovery needs this
    small local opener. Nothing changes the process or system proxy settings.
    """
    deadline = time.monotonic() + max(timeout, 1) / 1000
    try:
        if isinstance(endpoint, str) and endpoint.startswith(('ws:', 'wss:')):
            _local_cdp_url(endpoint, websocket=True)
            websocket_url = endpoint
        else:
            parsed, port = _local_cdp_url(endpoint)
            discovery = parsed._replace(path='/json/version', query='', fragment='').geturl()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoCDPRedirect())
            with opener.open(discovery, timeout=max(timeout, 1) / 1000) as response:
                if response.status != 200:
                    raise ValueError('Browser discovery failed')
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError('Browser discovery response too large')
            payload = json.loads(raw)
            websocket_url = payload.get('webSocketDebuggerUrl') if isinstance(payload, dict) else None
            target, target_port = _local_cdp_url(websocket_url, websocket=True)
            if (target.hostname != parsed.hostname or target_port != port
                    or target.scheme != ('wss' if parsed.scheme == 'https' else 'ws')):
                raise ValueError('Browser discovery changed the local address')
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise TimeoutError()
        return playwright.chromium.connect_over_cdp(websocket_url, timeout=remaining)
    except Exception:
        # Browser errors can include credentials or page URLs; keep this fixed.
        raise RuntimeError('无法直连助手专用浏览器，请重新打开助手窗口后重试。') from None


def verify_browser_profile(browser):
    """Do not accidentally use another Chrome listening on the same local port."""
    session = browser.new_browser_cdp_session()
    try:
        # SystemInfo works without --enable-automation (which adds an infobar).
        command = session.send('SystemInfo.getInfo').get('commandLine', '')
        if os.name == 'nt':
            arguments = _windows_argv(command)
            if not arguments:
                raise ValueError('Missing browser command line')
        else:
            arguments = shlex.split(command)
        profile = next((value.split('=', 1)[1] for value in arguments if value.startswith('--user-data-dir=')), None)
        if not profile or Path(profile).resolve() != PROFILE_DIR.resolve():
            raise RuntimeError('浏览器调试端口正被其他程序使用，请关闭旧的助手浏览器后重试。')
        if '--no-proxy-server' not in arguments:
            raise RuntimeError('旧的助手浏览器尚未启用直连，请关闭助手的浏览器窗口后重新打开；无需更改代理设置。')
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError('无法核实专用浏览器目录，请关闭旧的助手浏览器后重新启动助手。') from None
    finally:
        try:
            session.detach()
        except Exception:
            pass


def cdp_endpoint_alive(endpoint, *, timeout=0.35):
    """Return True when a local CDP /json/version endpoint responds cleanly."""
    try:
        parsed, _ = _local_cdp_url(endpoint)
        discovery = parsed._replace(path='/json/version', query='', fragment='').geturl()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoCDPRedirect())
        with opener.open(discovery, timeout=max(float(timeout), 0.1)) as response:
            return response.status == 200
    except Exception:
        return False


def _allow_foreground_window():
    if os.name != 'nt':
        return
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(-1)
    except Exception:
        pass


def _raise_windows_window(title=None, browser_pid=None):
    """Bring a visible Chromium window matching `title` or `browser_pid` to front."""
    if os.name != 'nt' or (not title and not browser_pid):
        return
    try:
        import ctypes.wintypes as wt
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        title_matches = []
        pid_matches = []
        enum_proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def callback(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            if title:
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    if title in buf.value:
                        title_matches.append(hwnd)
                        return False
            if browser_pid:
                pid = wt.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == browser_pid:
                    cls = ctypes.create_unicode_buffer(64)
                    user32.GetClassNameW(hwnd, cls, 64)
                    if cls.value == 'Chrome_WidgetWin_1':
                        pid_matches.append(hwnd)
            return True

        user32.EnumWindows(enum_proc(callback), 0)
        hwnd = title_matches[0] if title_matches else (pid_matches[0] if pid_matches else None)
        if not hwnd:
            return
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        fg = user32.GetForegroundWindow()
        if fg == hwnd:
            return
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        cur_tid = kernel32.GetCurrentThreadId()
        attached = False
        if fg_tid and fg_tid != cur_tid:
            attached = bool(user32.AttachThreadInput(fg_tid, cur_tid, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(fg_tid, cur_tid, False)
    except Exception:
        pass


def activate_target(browser, target_id, *, session=None):
    """Unminimize, activate, and bring the Chromium target window to the front."""
    if not target_id or browser is None:
        return
    owns_session = session is None
    if owns_session:
        session = browser.new_browser_cdp_session()
    try:
        _allow_foreground_window()
        window = session.send('Browser.getWindowForTarget', {'targetId': target_id})
        if window.get('bounds', {}).get('windowState') == 'minimized':
            session.send('Browser.setWindowBounds', {
                'windowId': window['windowId'],
                'bounds': {'windowState': 'normal'},
            })
        session.send('Target.activateTarget', {'targetId': target_id})
        try:
            info = session.send('Target.getTargetInfo', {'targetId': target_id}).get('targetInfo', {})
            title = (info.get('title') or '').strip()
            clean_title = title if title and title != 'about:blank' else None
            browser_pid = None
            if os.name == 'nt':
                try:
                    procs = session.send('SystemInfo.getProcessInfo').get('processInfo', [])
                    browser_pid = next((p.get('id') for p in procs if p.get('type') == 'browser'), None)
                except Exception:
                    browser_pid = None
            _raise_windows_window(title=clean_title, browser_pid=browser_pid)
        except Exception:
            pass
    finally:
        if owns_session:
            try:
                session.detach()
            except Exception:
                pass


def activate_page(context, page):
    """Surface a Playwright Page visibly in Chrome or Edge without breaking mocks."""
    if hasattr(page, 'bring_to_front'):
        page.bring_to_front()
    browser = getattr(context, 'browser', None)
    if browser is None or not hasattr(context, 'new_cdp_session'):
        return
    cdp = None
    try:
        cdp = context.new_cdp_session(page)
        info = cdp.send('Target.getTargetInfo').get('targetInfo', {})
        target_id = info.get('targetId')
        if target_id:
            activate_target(browser, target_id)
    except Exception:
        pass
    finally:
        if cdp is not None:
            try:
                cdp.detach()
            except Exception:
                pass


def open_visible_url(browser, url, *, app=False):
    """Open or surface `url` inside an already-running verified CDP browser."""
    verify_browser_profile(browser)
    session = browser.new_browser_cdp_session()
    try:
        targets = session.send('Target.getTargets').get('targetInfos', [])
        pages = [t for t in targets if t.get('type') == 'page']
        parsed = urlsplit(url)
        target_id = None
        if app:
            for item in pages:
                p = urlsplit(item.get('url', ''))
                if (p.scheme, p.netloc, p.path) == (parsed.scheme, parsed.netloc, parsed.path):
                    target_id = item.get('targetId')
                    break
        else:
            for item in pages:
                if item.get('url') == url:
                    target_id = item.get('targetId')
                    break
        if not target_id:
            created = session.send('Target.createTarget', {
                'url': url,
                'newWindow': True,
                'background': False,
            })
            target_id = created.get('targetId')
        if target_id:
            activate_target(browser, target_id, session=session)
    finally:
        try:
            session.detach()
        except Exception:
            pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--chrome', '--browser', dest='browser', action='store_true',
                        help='start the dedicated assistant browser')
    args = parser.parse_args()
    find_browser()
    if args.browser:
        subprocess.Popen(browser_args(), creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
