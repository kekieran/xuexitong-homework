"""Restore the lab session through the platform's observed official SSO bridge.

The caller owns the browser context and its persistence.  This module only
performs GET requests; it never copies cookies or opens report pages in a browser.
"""
from __future__ import annotations

import re
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from homework_lab import dispose_response as _dispose


CENTRAL_ORIGIN = 'https://jcxygl.chaoxing.com'
LOGIN_REQUIRED = 'LOGIN_REQUIRED:实验报告需要重新登录'
NETWORK_ERROR = '实验报告登录核对失败，请检查网络后重试'
_ORIGINS = {'https://mooc.istudy.szpu.edu.cn', CENTRAL_ORIGIN}
_BRIDGE_ORIGIN = 'https://passport.istudy.szpu.edu.cn'
_BRIDGE_PATH = '/api/otherdomaincookie'
_REDIRECT_STAGES = {
    ('passport.istudy.szpu.edu.cn', _BRIDGE_PATH): 0,
    ('passport2.chaoxing.com', '/api/302ohterdomain'): 1,
    ('jcxygl.chaoxing.com', '/lab'): 2,
    ('jcxygl.chaoxing.com', '/lab/'): 2,
}
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_TIMEOUT_MS = 18000
_MAX_REDIRECTS = 5


def _get(context, url, **kwargs):
    try:
        return context.request.get(url, timeout=_TIMEOUT_MS, max_redirects=0,
                                   fail_on_status_code=False, **kwargs)
    except Exception:
        raise RuntimeError(NETWORK_ERROR) from None


def _space_login(context, origin, fid):
    response = None
    try:
        response = _get(context, origin + '/lab/api/auth/spaceLogin',
                        params={'fid': fid, 'deptId': fid}, headers={'server': 'true'})
        if response.status in _REDIRECT_CODES or response.status in (401, 403):
            raise RuntimeError(LOGIN_REQUIRED)
        if not 200 <= response.status < 300:
            raise RuntimeError(NETWORK_ERROR)
        try:
            payload = response.json()
        except Exception:
            raise RuntimeError(NETWORK_ERROR) from None
        if not isinstance(payload, dict):
            raise RuntimeError(NETWORK_ERROR)
        return payload.get('code')
    finally:
        _dispose(response)


def _redirect_stage(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.port not in (None, 443)):
            return None
        return _REDIRECT_STAGES.get((parsed.hostname, parsed.path))
    except (TypeError, ValueError):
        return None


def _school_bridge(context, fid):
    backurl = CENTRAL_ORIGIN + '/lab/#/redirect?' + urlencode({
        'key': 'labTemplate/report/passportRedirect', 'fid': fid,
    })
    current = _BRIDGE_ORIGIN + _BRIDGE_PATH + '?' + urlencode({
        'domain': 'https://passport2.chaoxing.com', 'backurl': backurl, 'fid': fid,
    })
    stage = 0
    redirects = 0
    while True:
        response = None
        try:
            response = _get(context, current)
            if response.status in _REDIRECT_CODES:
                if redirects >= _MAX_REDIRECTS:
                    raise RuntimeError(LOGIN_REQUIRED)
                location = response.headers.get('location') or response.headers.get('Location')
                if not isinstance(location, str) or not location.strip():
                    raise RuntimeError(LOGIN_REQUIRED)
                target = urljoin(current, location)
                next_stage = _redirect_stage(target)
                # The observed bridge must visit passport2 before reaching lab.
                # A loop or another identity provider requires an interactive login.
                if next_stage is None or next_stage != stage + 1:
                    raise RuntimeError(LOGIN_REQUIRED)
                parsed = urlsplit(target)
                current = urlunsplit(parsed._replace(fragment=''))
                stage = next_stage
                redirects += 1
                continue
            if not 200 <= response.status < 300:
                if response.status in (401, 403):
                    raise RuntimeError(LOGIN_REQUIRED)
                raise RuntimeError(NETWORK_ERROR)
            if stage != 2:
                raise RuntimeError(LOGIN_REQUIRED)
            return
        finally:
            _dispose(response)


def ensure_session(context, origin, fid):
    """Return the authenticated origin, or raise a fixed, non-sensitive error."""
    if origin not in _ORIGINS:
        raise ValueError('不支持的实验报告登录地址')
    fid = str(fid)
    if not re.fullmatch(r'[1-9][0-9]{0,11}', fid):
        raise ValueError('实验报告学校信息无效')
    code = _space_login(context, origin, fid)
    if code == 1000:
        return origin
    if code != 1005 or fid != '1860':
        raise RuntimeError(LOGIN_REQUIRED)
    _school_bridge(context, fid)
    if _space_login(context, CENTRAL_ORIGIN, fid) != 1000:
        raise RuntimeError(LOGIN_REQUIRED)
    return CENTRAL_ORIGIN
