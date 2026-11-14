"""One local sign-in for Chaoxing and the discovered school host, with user-bound Windows storage."""
from __future__ import annotations

import base64
import ctypes
import json
import time
from urllib.parse import quote, urlsplit

from playwright.sync_api import sync_playwright

import homework_engine as engine
import homework_school as school

SCHOOL_URL = 'https://mooc.istudy.szpu.edu.cn/visit/interaction'
SITES = {'chaoxing': engine.NOTICE_URL, 'school': SCHOOL_URL}
LOGIN_URLS = dict(SITES, school='https://passport.istudy.szpu.edu.cn/login?fid=1860&refer=' + quote(SCHOOL_URL, safe=''))
SITE_LABELS = {'chaoxing': '学习通', 'school': '学校站点'}
CREDENTIAL_FILE = engine.runtime.DATA_DIR / 'login_credentials.bin'
_AUTO_FAILED = False
_DEFAULT_SITES = dict(SITES)
_DEFAULT_LOGIN_URLS = dict(LOGIN_URLS)
_PREFLIGHT_PAGES = {}


class BrowserUnavailable(RuntimeError):
    pass


class DataBlob(ctypes.Structure):
    _fields_ = [('cbData', ctypes.c_ulong), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def _custom_sites() -> bool:
    return SITES != _DEFAULT_SITES or LOGIN_URLS != _DEFAULT_LOGIN_URLS


def _site_url(platform: str) -> str:
    if _custom_sites() or platform != 'school':
        return SITES[platform]
    return school.school_catalog_url() or SITES[platform]


def _login_url(platform: str) -> str:
    if _custom_sites() or platform != 'school':
        return LOGIN_URLS[platform]
    return school.school_login_url() or LOGIN_URLS[platform]


def _is_playwright_context(context) -> bool:
    return bool(context) and getattr(type(context), '__module__', '').startswith('playwright')


def _can_fast_fanya(context) -> bool:
    return engine.os.name == 'nt' and not _custom_sites() and _is_playwright_context(context)


def _aes_cbc_encrypt(plaintext: str, key: str = 'u2oh6Vu^HWe4_AES') -> str:
    raw = plaintext.encode('utf-8')
    pad_len = 16 - (len(raw) % 16)
    padded = raw + bytes([pad_len] * pad_len)
    k = key.encode('utf-8')
    iv = bytearray(k)
    bcrypt = ctypes.WinDLL('bcrypt')
    alg = ctypes.c_void_p()
    hkey = ctypes.c_void_p()
    if bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(alg), ctypes.c_wchar_p('AES'), None, 0) != 0:
        raise RuntimeError('AES provider unavailable')
    try:
        mode = 'ChainingModeCBC\x00'.encode('utf-16le')
        if bcrypt.BCryptSetProperty(alg, ctypes.c_wchar_p('ChainingMode'), mode, len(mode), 0) != 0:
            raise RuntimeError('AES CBC mode unavailable')
        if bcrypt.BCryptGenerateSymmetricKey(alg, ctypes.byref(hkey), None, 0, k, len(k), 0) != 0:
            raise RuntimeError('AES key setup failed')
        try:
            out = (ctypes.c_ubyte * len(padded))()
            cb = ctypes.c_ulong()
            iv_buf = (ctypes.c_ubyte * 16).from_buffer(iv)
            if bcrypt.BCryptEncrypt(hkey, padded, len(padded), None, iv_buf, 16, out, len(padded), ctypes.byref(cb), 0) != 0:
                raise RuntimeError('AES encryption failed')
            return base64.b64encode(bytes(out[:cb.value])).decode('ascii')
        finally:
            bcrypt.BCryptDestroyKey(hkey)
    finally:
        bcrypt.BCryptCloseAlgorithmProvider(alg, 0)


def _fanya_endpoint(platform: str, login_url: str) -> tuple[str, str, str] | None:
    if platform == 'chaoxing':
        return 'https://passport2.chaoxing.com', '-1', SITES['chaoxing']
    if platform == 'school':
        parsed = urlsplit(str(login_url or '').strip())
        host = (parsed.hostname or '').lower()
        if parsed.scheme in {'http', 'https'} and parsed.netloc and (
            'passport' in host or '/login' in (parsed.path or '').lower()
        ):
            fid = ''
            for pair in (parsed.query or '').split('&'):
                if pair.lower().startswith('fid='):
                    val = pair.split('=', 1)[1].strip()
                    if val.isdigit() and val != '0':
                        fid = val
                        break
            fid = fid or school.school_fid('') or '-1'
            return f'{parsed.scheme}://{parsed.netloc}', fid, _site_url('school')
    return None


def _start_fanya_page(context, passport_origin: str, fid: str, refer: str, username: str, password: str):
    uname_enc = _aes_cbc_encrypt(username)
    pwd_enc = _aes_cbc_encrypt(password)
    login_page_url = f'{passport_origin}/login'
    page = context.new_page()
    try:
        page.route(
            login_page_url,
            lambda route: route.fulfill(
                status=200,
                content_type='text/html',
                body='<!doctype html><html><body></body></html>',
            ),
        )
        page.goto(login_page_url, wait_until='commit', timeout=5000)
        page.evaluate(
            """({fid, uname, password, refer}) => {
                const bodyStr = new URLSearchParams({
                    fid, uname, password, refer,
                    t: 'true', forbidotherlogin: '0', validate: '',
                    doubleFactorLogin: '0', independentId: '0', independentNameId: '0'
                }).toString();
                const sendOne = (ms) => {
                    const ctrl = new AbortController();
                    const timer = setTimeout(() => ctrl.abort(), ms);
                    return fetch('/fanyalogin', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                            'X-Requested-With': 'XMLHttpRequest'
                        },
                        body: bodyStr,
                        credentials: 'include',
                        signal: ctrl.signal
                    }).then(async (r) => {
                        if (!r.ok) throw new Error('HTTP ' + r.status);
                        return await r.json();
                    }).finally(() => clearTimeout(timer));
                };
                window.__loginPromise = new Promise((resolve) => {
                    let settled = false;
                    let lastErr = null;
                    let pending = 1;
                    let hedgeTimer = null;
                    const done = (val) => {
                        if (settled) return;
                        settled = true;
                        if (hedgeTimer) clearTimeout(hedgeTimer);
                        resolve(val);
                    };
                    const attach = (p) => {
                        p.then((data) => {
                            if (data && typeof data === 'object') {
                                done(data);
                            } else {
                                lastErr = new Error('invalid json');
                                if (--pending === 0) done({status: false, error: String(lastErr)});
                            }
                        }).catch((e) => {
                            lastErr = e;
                            if (--pending === 0) done({status: false, error: String(lastErr)});
                        });
                    };
                    attach(sendOne(12000));
                    hedgeTimer = setTimeout(() => {
                        if (settled) return;
                        pending += 1;
                        attach(sendOne(7500));
                    }, 4500);
                });
            }""",
            {'fid': str(fid or '-1'), 'uname': uname_enc, 'password': pwd_enc, 'refer': quote(refer, safe='')},
        )
        return page
    except Exception:
        try:
            if not page.is_closed():
                page.close()
        except Exception:
            pass
        return None


def _finish_fanya_page(page, label: str, progress) -> bool:
    try:
        res = page.evaluate('() => window.__loginPromise')
    except Exception:
        return False
    finally:
        try:
            if not page.is_closed():
                page.close()
        except Exception:
            pass
    if isinstance(res, dict) and res.get('status') is True:
        progress(f'{label}登录已确认')
        return True
    if isinstance(res, dict) and res.get('status') is False:
        raw = str(res.get('mes') or res.get('msg') or res.get('message') or '').strip()
        if any(word in raw for word in ('验证', '滑块')):
            return False
        if any(word in raw for word in ('密码', '账号', '用户名', '手机号')):
            raise RuntimeError(f'{label}：账号或密码未通过验证，请核对后重新登录')
        if any(word in raw for word in ('频繁', '次数', '锁定')):
            raise RuntimeError(f'{label}：登录尝试过于频繁，请稍后再试')
    return False


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
    if platform == 'school' and not _custom_sites() and school.is_chaoxing_only():
        return probe(context, 'chaoxing', timeout=timeout)
    site_url = _site_url(platform)
    response = None
    use_fast = not _custom_sites() and _is_playwright_context(context)
    req_timeout = min(timeout, 5000) if use_fast else timeout
    try:
        response = context.request.get(site_url, timeout=req_timeout)
        if response.status >= 400:
            raise RuntimeError()
        markup = response.text()
        return bool(markup.strip()) and urlsplit(response.url).hostname == urlsplit(site_url).hostname and not engine.dom.is_login(markup, response.url)
    except Exception:
        if use_fast:
            page = context.new_page()
            try:
                page.route(
                    '**/*',
                    lambda route: route.abort()
                    if route.request.resource_type in ('image', 'media', 'font', 'stylesheet', 'script')
                    else route.continue_(),
                )
                page.goto(site_url, wait_until='domcontentloaded', timeout=min(timeout, 6000))
                markup = page.content()
                return bool(markup.strip()) and urlsplit(page.url).hostname == urlsplit(site_url).hostname and not engine.dom.is_login(markup, page.url)
            except Exception:
                pass
            finally:
                try:
                    if not page.is_closed():
                        page.close()
                except Exception:
                    pass
        raise RuntimeError('暂时无法核对登录状态，请检查网络后重试') from None
    finally:
        engine.lab.dispose_response(response)


def _login_error(page):
    # Only return fixed messages; server errors can echo identifiers or secrets.
    for selector in ('#err-txt', '#phoneMsg', '#pwdMsg'):
        node = page.locator(selector).first
        if node.is_visible():
            raw_message = node.inner_text()
            message = raw_message.strip() if isinstance(raw_message, str) else ''
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


def _needs_verification(page) -> bool:
    for selector in ('#err-txt', '#phoneMsg', '#pwdMsg'):
        try:
            node = page.locator(selector).first
            if node.is_visible() is True:
                raw_message = node.inner_text()
                if isinstance(raw_message, str) and any(word in raw_message for word in ('验证', '滑块', '安全校验')):
                    return True
        except Exception:
            pass
    for selector in ('.nc-container', '.geetest_panel', '.geetest_widget', '#captcha', '.verify-wrap'):
        try:
            node = page.locator(selector).first
            if node.is_visible() is True:
                return True
        except Exception:
            pass
    return False


def _fill_fallback_form(page, username: str, password: str) -> bool:
    user_selectors = (
        'input[name="uname"]',
        'input[name="username"]',
        'input[name="phone"]',
        'input[type="text"]',
        'input[type="tel"]',
    )
    pwd_selectors = (
        'input[name="password"]',
        'input[name="pwd"]',
        'input[type="password"]',
    )
    btn_selectors = (
        'button[type="submit"]',
        'input[type="submit"]',
        '.login-btn',
        'button:has-text("登录")',
    )
    user_box = None
    for sel in user_selectors:
        loc = page.locator(sel).first
        if loc.is_visible() is True:
            user_box = loc
            break
    pwd_box = None
    for sel in pwd_selectors:
        loc = page.locator(sel).first
        if loc.is_visible() is True:
            pwd_box = loc
            break
    if not user_box or not pwd_box:
        return False
    user_box.fill(username)
    pwd_box.fill(password)
    for sel in btn_selectors:
        loc = page.locator(sel).first
        if loc.is_visible() is True:
            loc.click(timeout=10000)
            return True
    pwd_box.press('Enter')
    return True


def _submit(context, platform, username, password, progress=None):
    """Fill and drive one site's login form through a hidden page."""
    progress = progress or (lambda message: None)
    label = SITE_LABELS[platform]
    login_url = _login_url(platform)
    progress(f'正在登录{label}…')
    preflight = _PREFLIGHT_PAGES.pop((id(context), platform), None)
    if preflight is not None:
        if _finish_fanya_page(preflight, label, progress):
            return
    elif _can_fast_fanya(context):
        endpoint = _fanya_endpoint(platform, login_url)
        if endpoint is not None:
            fast_page = _start_fanya_page(context, *endpoint, username, password)
            if fast_page is not None and _finish_fanya_page(fast_page, label, progress):
                return
    page = context.new_page()
    if not _custom_sites() and hasattr(page, 'route'):
        try:
            page.route(
                '**/*',
                lambda route: route.abort()
                if (
                    route.request.resource_type in ('image', 'media', 'font')
                    or any(x in route.request.url.lower() for x in ('/createqr', '/getauthstatus', '/api/notice'))
                )
                else route.continue_(),
            )
        except Exception:
            pass
    try:
        page.goto(login_url, wait_until='domcontentloaded', timeout=10000)
    except Exception:
        page.close()
        raise RuntimeError('后台登录页面无法打开，请检查网络后重试') from None
    try:
        try:
            page.locator('#phone').wait_for(state='visible', timeout=6000)
            page.locator('#phone').fill(username)
            page.locator('#pwd').fill(password)
            page.locator('#loginBtn').click(timeout=10000)
        except Exception:
            if not _fill_fallback_form(page, username, password):
                try:
                    if probe(context, platform, timeout=4000):
                        progress(f'{label}登录已确认')
                        return
                except RuntimeError:
                    pass
                raise RuntimeError('登录表单未能填写，请重新登录') from None
        progress(f'正在确认{label}登录状态')
        deadline = time.monotonic() + 30
        next_probe = 0
        probe_succeeded = False
        verification_opened = False
        while time.monotonic() < deadline:
            if page.is_closed():
                raise RuntimeError('登录页面已关闭，请重新登录')
            page.wait_for_timeout(250)
            error = _login_error(page)
            if error:
                raise RuntimeError(f'{label}：{error}')
            if not verification_opened and _needs_verification(page):
                verification_opened = True
                deadline = max(deadline, time.monotonic() + 60)
                progress(f'{label}需要完成滑块验证，已自动打开验证窗口…')
                try:
                    engine.ensure_chrome(login_url, app=False)
                except Exception:
                    pass
            if verification_opened:
                try:
                    session = engine.read_json(engine.DATA_DIR / 'browser_session.json', None)
                    if isinstance(session, dict) and session.get('cookies') and hasattr(context, 'add_cookies'):
                        context.add_cookies(session['cookies'])
                except Exception:
                    pass
            if time.monotonic() < next_probe:
                continue
            try:
                if probe(context, platform, timeout=4000):
                    progress(f'{label}登录已确认')
                    return
                probe_succeeded = True
            except RuntimeError:
                pass
            next_probe = time.monotonic() + 0.5
        if not probe_succeeded:
            raise RuntimeError(f'{label}登录状态核对超时，请检查网络后重试')
        raise RuntimeError(f'{label}需要手动完成验证')
    finally:
        if not page.is_closed():
            page.close()


_DEFAULT_SUBMIT = _submit


def status(username=None, password=None, *, restore=False, force_restore=False,
           progress=None, on_state=None, context=None):
    """Return only site states to callers. Plaintext never enters response data."""
    global _AUTO_FAILED
    supplied = username is not None or password is not None
    if supplied and (not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password):
        raise ValueError('请输入账号和密码')
    credentials = (username.strip(), password) if supplied else None
    account_changed = False
    if supplied:
        prev_saved = (credential_info().get('username') or '').strip()
        cfg_before = school.load_config() if not _custom_sites() else {}
        prev_cfg_account = (cfg_before.get('account') or '').strip()
        has_unattributed_state = (
            not prev_saved
            and not prev_cfg_account
            and not _custom_sites()
            and (
                any((d / 'browser_session.json').exists() for d in {engine.DATA_DIR, engine.runtime.DATA_DIR})
                or cfg_before.get('mode', 'unknown') != 'unknown'
                or bool(cfg_before.get('fid'))
                or bool(cfg_before.get('uid'))
                or bool(cfg_before.get('school_origin'))
            )
        )
        if (
            (prev_saved and prev_saved != credentials[0])
            or (prev_cfg_account and prev_cfg_account != credentials[0])
            or has_unattributed_state
        ):
            account_changed = True
            for session_dir in {engine.DATA_DIR, engine.runtime.DATA_DIR}:
                try:
                    (session_dir / 'browser_session.json').unlink(missing_ok=True)
                except OSError:
                    pass
        # The user asked the assistant to remember these locally even when a
        # site still needs a captcha or another manual verification step.
        save_credentials(*credentials)
        if not _custom_sites():
            school.sync_account(credentials[0])
        _AUTO_FAILED = False
    if not credentials and restore and (force_restore or not _AUTO_FAILED):
        try:
            credentials = load_credentials()
        except RuntimeError:
            _AUTO_FAILED = True

    def authenticate(active_context):
        global _AUTO_FAILED
        if account_changed:
            if hasattr(active_context, 'clear_cookies'):
                try:
                    active_context.clear_cookies()
                except Exception:
                    pass
            sites = {name: False for name in SITES}
        elif supplied or force_restore:
            sites = {name: False for name in SITES}
        else:
            sites = {}
            for name in SITES:
                if name == 'chaoxing':
                    try:
                        cx_ok = probe(active_context, 'chaoxing')
                    except RuntimeError:
                        if not credentials:
                            raise
                        cx_ok = False
                    sites['chaoxing'] = cx_ok
                    if not _custom_sites() and cx_ok and school.load_config().get('mode') == 'unknown':
                        known_user = credentials[0] if credentials else (credential_info().get('username') or '').strip()
                        school.discover_school(active_context, username=known_user or None)
                elif name == 'school' and not _custom_sites() and school.is_chaoxing_only():
                    sites['school'] = bool(sites.get('chaoxing', False))
                elif name == 'school' and not _custom_sites() and school.load_config().get('mode') == 'unknown' and not sites.get('chaoxing'):
                    try:
                        sites['school'] = probe(active_context, 'school')
                    except RuntimeError:
                        sites['school'] = False
                else:
                    try:
                        sites[name] = probe(active_context, name)
                    except RuntimeError:
                        if not credentials:
                            raise
                        sites[name] = False
        if on_state:
            on_state({'logged_in': all(sites.values()), 'sites': dict(sites)})
        if credentials:
            prev_mode = school.load_config().get('mode') if not _custom_sites() else ''
            prev_school_url = _site_url('school') if 'school' in SITES else ''
            preflight_keys = []
            if _submit is _DEFAULT_SUBMIT and _can_fast_fanya(active_context):
                if not sites.get('chaoxing'):
                    ep = _fanya_endpoint('chaoxing', _login_url('chaoxing'))
                    if ep is not None:
                        pg = _start_fanya_page(active_context, *ep, *credentials)
                        if pg is not None:
                            key = (id(active_context), 'chaoxing')
                            _PREFLIGHT_PAGES[key] = pg
                            preflight_keys.append(key)
                if not sites.get('school') and not account_changed and school.load_config().get('mode') == 'school_site':
                    ep = _fanya_endpoint('school', _login_url('school'))
                    if ep is not None:
                        pg = _start_fanya_page(active_context, *ep, *credentials)
                        if pg is not None:
                            key = (id(active_context), 'school')
                            _PREFLIGHT_PAGES[key] = pg
                            preflight_keys.append(key)
            try:
                for platform in list(sites):
                    if not sites[platform]:
                        if platform == 'school' and not _custom_sites() and school.is_chaoxing_only():
                            sites['school'] = bool(sites.get('chaoxing', False))
                            if on_state:
                                on_state({'logged_in': all(sites.values()), 'sites': dict(sites)})
                            continue
                        _submit(active_context, platform, *credentials, progress=progress)
                        sites[platform] = True
                        if platform == 'chaoxing' and not _custom_sites():
                            if progress:
                                progress('正在识别所属学校…')
                            cfg = school.discover_school(active_context, username=credentials[0])
                            if progress and cfg.get('school_name'):
                                progress(f"已识别学校：{cfg['school_name']}")
                            if _site_url('school') != prev_school_url:
                                stale_school = _PREFLIGHT_PAGES.pop((id(active_context), 'school'), None)
                                if stale_school is not None:
                                    try:
                                        if not stale_school.is_closed():
                                            stale_school.close()
                                    except Exception:
                                        pass
                            if 'school' in sites:
                                if school.is_chaoxing_only():
                                    sites['school'] = True
                                elif school.load_config().get('mode') == 'school_site':
                                    if account_changed or not school._is_chaoxing_domain(urlsplit(_site_url('school')).hostname):
                                        sites['school'] = False
                                    elif prev_mode == 'unknown' or _site_url('school') != prev_school_url:
                                        try:
                                            sites['school'] = probe(active_context, 'school')
                                        except RuntimeError:
                                            sites['school'] = False
                        if on_state:
                            on_state({'logged_in': all(sites.values()), 'sites': dict(sites)})
            except Exception:
                _AUTO_FAILED = True
                raise
            finally:
                for key in preflight_keys:
                    stale = _PREFLIGHT_PAGES.pop(key, None)
                    if stale is not None:
                        try:
                            if not stale.is_closed():
                                stale.close()
                        except Exception:
                            pass
            if all(sites.values()):
                _AUTO_FAILED = False
        return {'logged_in': all(sites.values()), 'sites': sites}

    if context is None and not supplied and not restore and not _custom_sites():
        session = engine.read_json(engine.DATA_DIR / 'browser_session.json', None)
        if not isinstance(session, dict) or not session.get('cookies'):
            sites = {name: False for name in SITES}
            result = {'logged_in': False, 'sites': sites}
            if on_state:
                on_state({'logged_in': False, 'sites': dict(sites)})
            return result
    if context is not None:
        return authenticate(context)
    with engine.browser_operation(), sync_playwright() as playwright:
        active_context = engine.open_context(playwright, headless=True)
        try:
            return authenticate(active_context)
        finally:
            engine.close_context(active_context)
