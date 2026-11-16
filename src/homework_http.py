"""Lightweight, proxy-independent concurrent HTTP(S) cookie session for homework sync."""
from __future__ import annotations

import email.message
import gzip
import http.client
import http.cookiejar
import json
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

import homework_shared as shared


_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_POOL_IDLE_SEC = 25.0
_SSL_CONTEXT_LOCK = threading.Lock()
_SHARED_SSL_CONTEXT: ssl.SSLContext | None = None


def _shared_ssl_context() -> ssl.SSLContext:
    global _SHARED_SSL_CONTEXT
    if _SHARED_SSL_CONTEXT is None:
        with _SSL_CONTEXT_LOCK:
            if _SHARED_SSL_CONTEXT is None:
                _SHARED_SSL_CONTEXT = ssl.create_default_context()
    return _SHARED_SSL_CONTEXT


class _HeaderAdapter:
    def __init__(self, msg):
        self._msg = msg

    def info(self):
        return self._msg


class FastResponse:
    """Subset of Playwright APIResponse used across the homework engine."""

    def __init__(self, status: int, url: str, headers: dict[str, str], body: bytes):
        self.status = int(status)
        self.url = str(url)
        self.headers = dict(headers)
        self._body = body

    def body(self) -> bytes:
        return self._body

    def text(self) -> str:
        ctype = self.headers.get('content-type', '')
        match = re.search(r'charset\s*=\s*["\']?([A-Za-z0-9._-]+)', ctype, re.I)
        encoding = match.group(1) if match else 'utf-8'
        try:
            return self._body.decode(encoding, errors='replace')
        except LookupError:
            return self._body.decode('utf-8', errors='replace')

    def json(self):
        return json.loads(self.text())

    def dispose(self):
        self._body = b''


def _merge_url_params(url: str, params) -> str:
    if not params:
        return url
    parsed = urllib.parse.urlsplit(url)
    existing = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    extra = list(params.items()) if isinstance(params, dict) else list(params)
    merged = urllib.parse.urlencode([(str(k), '' if v is None else str(v)) for k, v in existing + extra])
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, merged, parsed.fragment))


def _decode_body(raw: bytes, encoding: str) -> bytes:
    enc = (encoding or '').strip().lower()
    if not raw or not enc:
        return raw
    if 'gzip' in enc or 'x-gzip' in enc:
        try:
            return gzip.decompress(raw)
        except Exception:
            return raw
    if 'deflate' in enc:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            try:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
            except Exception:
                return raw
    return raw


def has_session_cookies(session_path: Path) -> bool:
    """True when browser_session.json exists and contains unexpired Chaoxing cookies."""
    state = shared.read_json_file(session_path, None, corrupted='fallback')
    if not isinstance(state, dict):
        return False
    cookies = state.get('cookies')
    if not isinstance(cookies, list) or not cookies:
        return False
    now = time.time()
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get('domain') or '').lower()
        name = str(cookie.get('name') or '')
        if not name or 'chaoxing.com' not in domain:
            continue
        expires = cookie.get('expires')
        if expires is None or float(expires) <= 0 or float(expires) > now:
            return True
    return False


def supports_concurrency(context) -> bool:
    """Explicit opt-in check so MagicMock contexts stay single-threaded."""
    return context is not None and getattr(context, 'supports_concurrent_requests', False) is True


class _RequestNamespace:
    def __init__(self, context: FastCookieContext):
        self._ctx = context

    def get(self, url, *, params=None, timeout=15000, headers=None, max_redirects=None, fail_on_status_code=False):
        return self._ctx._send('GET', url, params=params, timeout=timeout, headers=headers,
                               max_redirects=max_redirects, fail_on_status_code=fail_on_status_code)

    def post(self, url, *, params=None, form=None, data=None, timeout=15000, headers=None, max_redirects=None, fail_on_status_code=False):
        return self._ctx._send('POST', url, params=params, form=form, data=data, timeout=timeout,
                               headers=headers, max_redirects=max_redirects, fail_on_status_code=fail_on_status_code)


class FastCookieContext:
    """Thread-safe direct HTTPS session backed by Playwright storage_state cookies."""

    is_fast_http_context = True
    supports_concurrent_requests = True

    def __init__(self, session_path: Path | None = None, *, state: dict | None = None, run_in_browser=None):
        self.session_path = Path(session_path) if session_path else None
        self.run_in_browser = run_in_browser
        self._lock = threading.RLock()
        self._tls = threading.local()
        self._all_pools: list[dict] = []
        self._jar = http.cookiejar.CookieJar()
        self._meta: dict[tuple[str, str, str], dict] = {}
        self._origins: list = []
        self._dirty = False
        self._get_cache: dict[str, tuple[float, int, str, bytes]] = {}
        self._ssl_ctx = _shared_ssl_context()
        self.request = _RequestNamespace(self)
        if state is not None:
            self._load_state_dict(state)
        elif self.session_path is not None:
            self.reload_storage_state()

    def reload_storage_state(self):
        if self.session_path is None:
            return
        state = shared.read_json_file(self.session_path, {}, corrupted='fallback')
        with self._lock:
            self._jar = http.cookiejar.CookieJar()
            self._meta.clear()
            self._origins = []
            self._get_cache.clear()
            if isinstance(state, dict):
                self._load_state_dict(state)
            self._dirty = False

    def remember_response(self, url: str, status: int, final_url: str, body: bytes) -> None:
        if status != 200 or not url or final_url != url or not body:
            return
        with self._lock:
            self._get_cache[url] = (time.monotonic(), int(status), str(final_url), bytes(body))

    def cached_response(self, url: str, max_age: float = 15.0) -> bytes | None:
        with self._lock:
            entry = self._get_cache.get(url)
            if not entry:
                return None
            ts, status, final_url, body = entry
            if status != 200 or final_url != url or (time.monotonic() - ts) > max_age:
                self._get_cache.pop(url, None)
                return None
            return body

    def _load_state_dict(self, state: dict):
        self._origins = list(state.get('origins') or [])
        self.add_cookies(state.get('cookies') or [], mark_dirty=False)

    def add_cookies(self, cookies: list[dict], *, mark_dirty: bool = True):
        now = time.time()
        with self._lock:
            for item in cookies or ():
                if not isinstance(item, dict):
                    continue
                name = str(item.get('name') or '')
                domain = str(item.get('domain') or '')
                if not name or not domain:
                    continue
                path = str(item.get('path') or '/')
                raw_expires = item.get('expires')
                try:
                    exp_num = float(raw_expires) if raw_expires is not None else -1.0
                except (TypeError, ValueError):
                    exp_num = -1.0
                if exp_num > 0 and exp_num <= now:
                    continue
                expires_int = int(exp_num) if exp_num > 0 else None
                cookie = http.cookiejar.Cookie(
                    version=0,
                    name=name,
                    value=str(item.get('value') or ''),
                    port=None,
                    port_specified=False,
                    domain=domain,
                    domain_specified=True,
                    domain_initial_dot=domain.startswith('.'),
                    path=path,
                    path_specified=True,
                    secure=bool(item.get('secure', False)),
                    expires=expires_int,
                    discard=(expires_int is None),
                    comment=None,
                    comment_url=None,
                    rest={'HttpOnly': None} if item.get('httpOnly') else {},
                )
                self._jar.set_cookie(cookie)
                self._meta[(domain, path, name)] = {
                    'httpOnly': bool(item.get('httpOnly', False)),
                    'sameSite': item.get('sameSite') or 'Lax',
                }
            if mark_dirty:
                self._dirty = True
                self._get_cache.clear()

    def clear_cookies(self):
        with self._lock:
            self._jar = http.cookiejar.CookieJar()
            self._meta.clear()
            self._get_cache.clear()
            self._dirty = True

    def cookies(self) -> list[dict]:
        with self._lock:
            result = []
            now = time.time()
            for cookie in self._jar:
                if cookie.expires is not None and cookie.expires <= now:
                    continue
                meta = self._meta.get((cookie.domain, cookie.path, cookie.name), {})
                http_only = bool(meta.get('httpOnly') or ('HttpOnly' in getattr(cookie, '_rest', {})))
                entry = {
                    'name': cookie.name,
                    'value': cookie.value,
                    'domain': cookie.domain,
                    'path': cookie.path,
                    'expires': float(cookie.expires) if cookie.expires is not None else -1,
                    'httpOnly': http_only,
                    'secure': bool(cookie.secure),
                }
                if meta.get('sameSite'):
                    entry['sameSite'] = meta['sameSite']
                result.append(entry)
            return result

    def storage_state(self) -> dict:
        with self._lock:
            return {'cookies': self.cookies(), 'origins': list(self._origins)}

    def save_storage_state(self):
        if self.session_path is None:
            return
        with self._lock:
            if not self._dirty:
                return
            snapshot = self.storage_state()
            self._dirty = False
        shared.write_json_file(self.session_path, snapshot)

    def close(self):
        self.save_storage_state()
        for pool in list(self._all_pools):
            for entry in list(pool.values()):
                conn = entry[0]
                try:
                    conn.close()
                except Exception:
                    pass
            pool.clear()

    def _thread_pool(self) -> dict:
        pool = getattr(self._tls, 'pool', None)
        if pool is None:
            pool = {}
            self._tls.pool = pool
            with self._lock:
                self._all_pools.append(pool)
        return pool

    def _get_conn(self, scheme: str, host: str, port: int, timeout_sec: float):
        pool = self._thread_pool()
        key = (scheme, host, port)
        cached = pool.pop(key, None)
        if cached is not None:
            conn, used_at = cached
            if time.monotonic() - used_at < _POOL_IDLE_SEC:
                conn.timeout = timeout_sec
                if getattr(conn, 'sock', None) is not None:
                    try:
                        conn.sock.settimeout(timeout_sec)
                    except OSError:
                        pass
                return conn, True
            try:
                conn.close()
            except Exception:
                pass
        if scheme == 'https':
            conn = http.client.HTTPSConnection(host, port=port, timeout=timeout_sec, context=self._ssl_ctx)
        else:
            conn = http.client.HTTPConnection(host, port=port, timeout=timeout_sec)
        return conn, False

    def _put_conn(self, scheme: str, host: str, port: int, conn):
        pool = self._thread_pool()
        old = pool.get((scheme, host, port))
        if old is not None and old[0] is not conn:
            try:
                old[0].close()
            except Exception:
                pass
        pool[(scheme, host, port)] = (conn, time.monotonic())

    def _single_hop(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout_sec: float):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
            raise ValueError('无效的请求地址')
        if any(c.isspace() or ord(c) < 32 for c in url):
            raise ValueError('无效的请求地址')
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        target = parsed.path or '/'
        if parsed.query:
            target = target + '?' + parsed.query

        req_headers: dict[str, str] = {}
        lower_keys = set()
        for k, v in (headers or {}).items():
            req_headers[str(k)] = str(v)
            lower_keys.add(str(k).lower())
        if 'user-agent' not in lower_keys:
            req_headers['User-Agent'] = _USER_AGENT
        if 'accept' not in lower_keys:
            req_headers['Accept'] = 'application/json, text/html, application/xhtml+xml, */*;q=0.9'
        if 'accept-encoding' not in lower_keys:
            req_headers['Accept-Encoding'] = 'gzip, deflate'
        if 'accept-language' not in lower_keys:
            req_headers['Accept-Language'] = 'zh-CN,zh;q=0.9'

        cookie_req = urllib.request.Request(url, method=method)
        with self._lock:
            self._jar.add_cookie_header(cookie_req)
        cookie_header = cookie_req.get_header('Cookie')
        if cookie_header and 'cookie' not in lower_keys:
            req_headers['Cookie'] = cookie_header

        for attempt in range(2):
            conn, was_pooled = self._get_conn(parsed.scheme, host, port, timeout_sec)
            try:
                conn.request(method, target, body=body, headers=req_headers)
                resp = conn.getresponse()
                raw = resp.read()
                msg = resp.headers
                status = resp.status
                will_close = resp.will_close or msg.get('connection', '').lower() == 'close'
                if will_close:
                    try:
                        conn.close()
                    except Exception:
                        pass
                else:
                    self._put_conn(parsed.scheme, host, port, conn)
                break
            except (http.client.HTTPException, OSError, EOFError):
                try:
                    conn.close()
                except Exception:
                    pass
                if attempt == 0 and was_pooled:
                    continue
                raise

        if msg.get_all('Set-Cookie'):
            with self._lock:
                self._jar.extract_cookies(_HeaderAdapter(msg), cookie_req)
                self._dirty = True

        decompressed = _decode_body(raw, msg.get('Content-Encoding', ''))
        lower_headers = {str(k).lower(): str(v) for k, v in msg.items()}
        return status, lower_headers, decompressed, msg

    def _send(self, method: str, url: str, *, params=None, form=None, data=None,
              timeout=15000, headers=None, max_redirects=None, fail_on_status_code=False) -> FastResponse:
        current_url = _merge_url_params(str(url), params)
        current_method = method.upper()
        req_headers = dict(headers or {})
        body: bytes | None = None
        if form is not None:
            pairs = list(form.items()) if isinstance(form, dict) else list(form)
            body = urllib.parse.urlencode([(str(k), '' if v is None else str(v)) for k, v in pairs]).encode('utf-8')
            if not any(k.lower() == 'content-type' for k in req_headers):
                req_headers['Content-Type'] = 'application/x-www-form-urlencoded'
        elif data is not None:
            if isinstance(data, bytes):
                body = data
            elif isinstance(data, str):
                body = data.encode('utf-8')
            else:
                body = json.dumps(data, ensure_ascii=False).encode('utf-8')
                if not any(k.lower() == 'content-type' for k in req_headers):
                    req_headers['Content-Type'] = 'application/json'

        timeout_sec = max(0.5, float(timeout or 15000) / 1000.0)
        redirect_limit = 20 if max_redirects is None else max(0, int(max_redirects))
        hops = 0
        while True:
            status, resp_headers, content, _msg = self._single_hop(
                current_method, current_url, req_headers, body, timeout_sec
            )
            if status in _REDIRECT_CODES and hops < redirect_limit:
                location = resp_headers.get('location')
                if not location:
                    break
                next_url = urllib.parse.urljoin(current_url, location)
                next_parsed = urllib.parse.urlsplit(next_url)
                if next_parsed.scheme not in {'http', 'https'}:
                    break
                hops += 1
                prev_host = urllib.parse.urlsplit(current_url).netloc
                if next_parsed.netloc != prev_host:
                    # Drop cross-origin referer/origin/cookie overrides on redirect hops.
                    for hdr in list(req_headers):
                        if hdr.lower() in {'cookie', 'authorization'}:
                            req_headers.pop(hdr, None)
                if status in {301, 302, 303} and current_method != 'GET':
                    current_method = 'GET'
                    body = None
                    for hdr in list(req_headers):
                        if hdr.lower() in {'content-type', 'content-length'}:
                            req_headers.pop(hdr, None)
                current_url = next_url
                continue
            break

        if fail_on_status_code and status >= 400:
            raise RuntimeError(f'HTTP {status}')
        return FastResponse(status, current_url, resp_headers, content)
