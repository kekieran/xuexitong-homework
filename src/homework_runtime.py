"""Project-relative paths plus the browser used for controlled platform work."""
from pathlib import Path
import argparse
import ctypes
import hashlib
import os
import shutil
import subprocess
import shlex

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APP_DIR / 'data'
UI_DIR = APP_DIR / 'ui'
DATA_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_DIR = DATA_DIR / 'browser_data'
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
    """Chromium arguments for the dedicated assistant window.

    The app window size is fixed - no caller ever asked for a different one.
    """
    args = [str(find_browser()), '--remote-debugging-address=127.0.0.1',
            '--remote-debugging-port=' + str(port),
            '--no-proxy-server', '--user-data-dir=' + str(PROFILE_DIR), '--profile-directory=Default']
    if app:
        width, height = 1440, 980
        args += ['--app=' + url, f'--window-size={width},{height}']
    elif url:
        args += ['--new-window', url]
    return args


def chrome_args(url=None, app=False, port=9222):
    """Compatibility alias for browser_args(), kept for older callers and tests.

    Do not delete it and do not collapse it into `chrome_args = browser_args`:
    the module-level function is referenced by tests/test_retention.py. New code
    should call browser_args() directly.
    """
    return browser_args(url, app, port)


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
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError('无法核实专用浏览器目录，请关闭旧的助手浏览器后重新启动助手。') from None
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
