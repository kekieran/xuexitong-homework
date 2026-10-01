"""One local sign-in for the two Chaoxing hosts, with user-bound Windows storage."""
from __future__ import annotations

import ctypes
import json
import time
from urllib.parse import quote, urlsplit

from playwright.sync_api import sync_playwright

import homework_engine as engine

SCHOOL_URL = 'https://mooc.istudy.szpu.edu.cn/visit/interaction'
SITES = {'chaoxing': engine.NOTICE_URL, 'school': SCHOOL_URL}
LOGIN_URLS = dict(SITES, school='https://passport.istudy.szpu.edu.cn/login?fid=1860&refer=' + quote(SCHOOL_URL, safe=''))
SITE_LABELS = {'chaoxing': '学习通', 'school': '学校站点'}
CREDENTIAL_FILE = engine.runtime.DATA_DIR / 'login_credentials.bin'
_AUTO_FAILED = False


class BrowserUnavailable(RuntimeError):
    pass


class DataBlob(ctypes.Structure):
    _fields_ = [('cbData', ctypes.c_ulong), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def _protect(data: bytes, decrypt=False) -> bytes:
    if not data or engine.os.name != 'nt':
        raise RuntimeError('本机加密功能不可用')
    source = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    incoming = DataBlob(len(data), source)
    outgoing = DataBlob()
    crypt32 = ctypes.WinDLL('Crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('Kernel32', use_last_error=True)
    if decrypt:
        function = crypt32.CryptUnprotectData
        function.argtypes = (ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(DataBlob))
        args = (ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing))
    else:
        function = crypt32.CryptProtectData
        function.argtypes = (ctypes.POINTER(DataBlob), ctypes.c_wchar_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(DataBlob))
        args = (ctypes.byref(incoming), 'Homework Assistant', None, None, None, 1, ctypes.byref(outgoing))
    function.restype = ctypes.c_int
    if not function(*args):
        raise RuntimeError('本机加密凭据不可用，请重新登录')
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
        kernel32.LocalFree(ctypes.cast(outgoing.pbData, ctypes.c_void_p))


def save_credentials(username: str, password: str) -> None:
    payload = json.dumps({'version': 1, 'username': username, 'password': password}, ensure_ascii=False).encode('utf-8')
    encrypted = _protect(payload)
    CREDENTIAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CREDENTIAL_FILE.with_suffix('.tmp')
    temporary.write_bytes(encrypted)
    temporary.replace(CREDENTIAL_FILE)


def load_credentials() -> tuple[str, str] | None:
    if not CREDENTIAL_FILE.exists():
        return None
    try:
        data = json.loads(_protect(CREDENTIAL_FILE.read_bytes(), decrypt=True))
        if data['version'] != 1 or not data['username'] or not data['password']:
            raise ValueError()
        return data['username'], data['password']
    except (OSError, ValueError, KeyError, UnicodeDecodeError, RuntimeError):
        raise RuntimeError('保存的登录信息已失效，请重新输入账号密码') from None


def credential_info():
    """Expose only whether credentials exist and the saved account name."""
    try:
        credentials = load_credentials()
        return {'saved': bool(credentials), 'username': credentials[0] if credentials else ''}
    except RuntimeError:
        return {'saved': False, 'username': ''}


def probe(context, platform: str, *, timeout=12000) -> bool:
    try:
        response = context.request.get(SITES[platform], timeout=timeout)
        if response.status >= 400:
            raise RuntimeError()
        markup = response.text()
        return bool(markup.strip()) and urlsplit(response.url).hostname == urlsplit(SITES[platform]).hostname and not engine.dom.is_login(markup, response.url)
    except Exception:
        raise RuntimeError('暂时无法核对登录状态，请检查网络后重试') from None


def _target_page(browser, context, target_id):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        for page in context.pages:
            session = context.new_cdp_session(page)
            try:
                target = session.send('Target.getTargetInfo')['targetInfo']
                if target['targetId'] == target_id:
                    return page
            finally:
                session.detach()
        time.sleep(.15)
    raise RuntimeError('登录窗口没有打开，请重新登录')


def _login_page(browser, context, target_id, platform):
    url = LOGIN_URLS[platform]
    session = browser.new_browser_cdp_session()
    try:
        infos = session.send('Target.getTargets').get('targetInfos', [])
        current = next((x for x in infos if x.get('targetId') == target_id and x.get('type') == 'page'), None)
        if not current:
            current = next((x for x in infos if x.get('type') == 'page' and engine.login_page_platform(x.get('url', ''))), None)
        if current:
            target_id = current['targetId']
            engine.navigate_login_target(browser, target_id, url)
        else:
            created = session.send('Target.createTarget', {'url': url, 'newWindow': True, 'background': False})
            target_id = created.get('targetId')
            if not target_id:
                raise RuntimeError('登录窗口没有打开，请重新登录')
        session.send('Target.activateTarget', {'targetId': target_id})
        window = session.send('Browser.getWindowForTarget', {'targetId': target_id})
        if window.get('bounds', {}).get('windowState') == 'minimized':
            session.send('Browser.setWindowBounds', {
                'windowId': window['windowId'], 'bounds': {'windowState': 'normal'}})
    finally:
        session.detach()
    return _target_page(browser, context, target_id), target_id


def _login_error(page):
    # Only return fixed messages; server errors can echo identifiers or secrets.
    for selector in ('#err-txt', '#phoneMsg', '#pwdMsg'):
        node = page.locator(selector).first
        if node.is_visible():
            message = node.inner_text().strip()
            if not message:
                continue
            if any(word in message for word in ('密码', '账号', '用户名', '手机号')):
                return '账号或密码未通过验证，请核对后重新登录'
            if any(word in message for word in ('频繁', '次数', '锁定')):
                return '登录尝试过于频繁，请稍后再试'
            if any(word in message for word in ('验证', '滑块')):
                return None  # Allow the user to complete the website's challenge.
            return '学习通未接受本次登录，请查看登录窗口中的提示'
    return None


def _submit(browser, context, target_id, platform, username, password, progress=None, silent=False):
    progress = progress or (lambda message: None)
    label = SITE_LABELS[platform]
    progress(f'正在登录{label}…')
    if silent:
        page = context.new_page()
        try:
            page.goto(LOGIN_URLS[platform], wait_until='domcontentloaded', timeout=15000)
        except Exception:
            page.close()
            raise RuntimeError('后台登录页面无法打开，请检查网络后重试') from None
    else:
        page, target_id = _login_page(browser, context, target_id, platform)
    try:
        try:
            page.locator('#phone').wait_for(state='visible', timeout=15000)
            page.locator('#phone').fill(username)
            page.locator('#pwd').fill(password)
            page.locator('#loginBtn').click(timeout=10000)
        except Exception:
            raise RuntimeError('登录表单未能填写，请重新登录') from None
        progress(f'正在确认{label}登录状态' + ('' if silent else '；如网页要求滑块或验证码，请在登录窗口完成'))
        deadline = time.monotonic() + (30 if silent else 120)
        next_probe = 0
        probe_succeeded = False
        while time.monotonic() < deadline:
            if page.is_closed():
                raise RuntimeError('登录页面已关闭，请重新登录')
            page.wait_for_timeout(1000)
            error = _login_error(page)
            if error:
                raise RuntimeError(f'{label}：{error}')
            if time.monotonic() < next_probe:
                continue
            try:
                if probe(context, platform, timeout=4000):
                    progress(f'{label}登录已确认')
                    return target_id
                probe_succeeded = True
            except RuntimeError:
                pass
            next_probe = time.monotonic() + 3
        if not probe_succeeded:
            raise RuntimeError(f'{label}登录状态核对超时，请检查网络后重试')
        if silent:
            raise RuntimeError(f'{label}需要手动完成验证')
        raise RuntimeError(f'{label}尚未完成验证，请在登录窗口完成滑块、短信或其他验证后重试；窗口已保留')
    finally:
        if silent and not page.is_closed():
            page.close()


def status(username=None, password=None, *, restore=False, force_restore=False,
           existing_only=False, progress=None, on_state=None, context=None):
    """Return only site states to callers. Plaintext never enters response data."""
    global _AUTO_FAILED
    supplied = username is not None or password is not None
    if supplied and (not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password):
        raise ValueError('请输入账号和密码')
    credentials = (username.strip(), password) if supplied else None
    if supplied:
        # The user asked the assistant to remember these locally even when a
        # site still needs a captcha or another manual verification step.
        save_credentials(*credentials)
        _AUTO_FAILED = False
    if not credentials and restore and (force_restore or not _AUTO_FAILED):
        try:
            credentials = load_credentials()
        except RuntimeError:
            _AUTO_FAILED = True
    def authenticate(active_context):
        global _AUTO_FAILED
        sites = {name: probe(active_context, name) for name in SITES}
        if on_state:
            on_state({'logged_in': all(sites.values()), 'sites': dict(sites)})
        if credentials:
            try:
                for platform, logged_in in sites.items():
                    if not logged_in:
                        _submit(None, active_context, None, platform, *credentials,
                                progress=progress, silent=True)
                        sites[platform] = True
                        if on_state:
                            on_state({'logged_in': all(sites.values()), 'sites': dict(sites)})
            except Exception:
                _AUTO_FAILED = True
                raise
            if all(sites.values()):
                _AUTO_FAILED = False
        return {'logged_in': all(sites.values()), 'sites': sites}

    if context is not None:
        return authenticate(context)
    with engine.browser_operation(), sync_playwright() as playwright:
        active_context = engine.open_context(playwright, headless=True)
        try:
            return authenticate(active_context)
        finally:
            engine.close_context(active_context)
