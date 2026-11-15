from datetime import datetime
from pathlib import Path
import re
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

import homework_runtime as runtime
from homework_shared import collapse_whitespace, read_json_file, write_json_file

CHAOXING_BASE_URL = 'https://i.chaoxing.com/base'
_DEFAULT_CHAOXING_BASE_URL = CHAOXING_BASE_URL
NOTICE_API_URL = 'https://notice.chaoxing.com/pc/notice/getNoticeList'

FALLBACK_ORIGIN = 'https://mooc.istudy.szpu.edu.cn'
FALLBACK_AUTH_ORIGIN = 'https://passport.istudy.szpu.edu.cn'
FALLBACK_FID = '1860'
FALLBACK_ASSET_HOSTS = ('mooc.istudy.szpu.edu.cn', 'cs.istudy.szpu.edu.cn')

KNOWN_SCHOOL_SITES = {
    '1860': {
        'school_name': '深圳职业技术大学',
        'school_origin': FALLBACK_ORIGIN,
        'auth_origin': FALLBACK_AUTH_ORIGIN,
        'asset_hosts': list(FALLBACK_ASSET_HOSTS),
    }
}

SCHOOL_FILE = runtime.DATA_DIR / 'school_config.json'
_DEFAULT_SCHOOL_FILE = SCHOOL_FILE
_CONFIG_LOCK = threading.RLock()

DEFAULT_CONFIG = {
    'account': '',
    'uid': '',
    'fid': '',
    'school_name': '',
    'mode': 'unknown',
    'school_origin': '',
    'auth_origin': '',
    'login_url': '',
    'asset_hosts': [],
    'discovered_at': '',
}

_RESERVED_TLDS = ('.invalid', '.example', '.test', '.localhost')
_CHAOXING_PATH_MARKERS = (
    '/mooc-ans/',
    '/mooc2-ans/',
    '/visit/interaction',
    '/visit/courses',
    '/visit/stucoursemiddle',
    '/mycourse/studentcourse',
    '/work/dohomeworknew',
    '/work/task-list',
    '/work/getallwork',
    '/work/selectworkquestionyiwuru',
)


def _config_path():
    if SCHOOL_FILE != _DEFAULT_SCHOOL_FILE:
        return SCHOOL_FILE
    return runtime.DATA_DIR / 'school_config.json'


def _normalize_origin(value):
    parsed = urlsplit(str(value or '').strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc or not parsed.hostname:
        return ''
    if parsed.username or parsed.password:
        return ''
    return f'{parsed.scheme}://{parsed.netloc}'


def _is_reserved_host(hostname):
    host = (hostname or '').strip().lower()
    if not host or host in {'localhost', '127.0.0.1', '::1'}:
        return True
    if any(host == tld[1:] or host.endswith(tld) for tld in _RESERVED_TLDS):
        return True
    if re.fullmatch(r'\d+(?:\.\d+){3}', host):
        return True
    return False


def _is_chaoxing_domain(hostname):
    host = (hostname or '').strip().lower()
    return host == 'chaoxing.com' or host.endswith('.chaoxing.com')


def _school_domain_suffix(hostname):
    host = (hostname or '').strip().lower()
    if host.startswith('mooc.') and len(host) > 5:
        return host[5:]
    return ''


def _derive_asset_hosts(school_origin, extra=()):
    hosts = []
    host = (urlsplit(school_origin).hostname or '').strip().lower()
    if host:
        hosts.append(host)
        suffix = _school_domain_suffix(host)
        if suffix:
            for prefix in ('cs.', 'pan-yz.', 'p.'):
                candidate = prefix + suffix
                if candidate not in hosts:
                    hosts.append(candidate)
    for item in extra or ():
        h = str(item or '').strip().lower()
        if h and h not in hosts:
            hosts.append(h)
    return hosts


def _normalize_config(raw):
    data = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        for key in DEFAULT_CONFIG:
            if key in raw and raw[key] is not None:
                data[key] = raw[key]
    data['account'] = collapse_whitespace(data.get('account'))
    data['uid'] = collapse_whitespace(data.get('uid'))
    fid = collapse_whitespace(data.get('fid'))
    data['fid'] = fid if fid.isdigit() else ''
    data['school_name'] = collapse_whitespace(data.get('school_name'))
    mode = collapse_whitespace(data.get('mode'))
    data['mode'] = mode if mode in {'unknown', 'chaoxing_only', 'school_site'} else 'unknown'
    data['school_origin'] = _normalize_origin(data.get('school_origin'))
    data['auth_origin'] = _normalize_origin(data.get('auth_origin'))
    login_url = str(data.get('login_url') or '').strip()
    parsed_login = urlsplit(login_url)
    data['login_url'] = login_url if parsed_login.scheme in {'http', 'https'} and parsed_login.netloc else ''
    if data['mode'] == 'school_site' and not data['school_origin']:
        data['mode'] = 'unknown'
    if data['mode'] == 'chaoxing_only':
        data['school_origin'] = ''
        data['auth_origin'] = ''
        data['login_url'] = ''
        data['asset_hosts'] = []
    else:
        raw_hosts = data.get('asset_hosts') if isinstance(data.get('asset_hosts'), list) else []
        data['asset_hosts'] = _derive_asset_hosts(data['school_origin'], raw_hosts)
    data['discovered_at'] = collapse_whitespace(data.get('discovered_at'))
    return data


def load_config():
    with _CONFIG_LOCK:
        path = _config_path()
        raw = read_json_file(path, {}, corrupted='fallback')
        return _normalize_config(raw)


def save_config(config):
    with _CONFIG_LOCK:
        path = _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        normalized = _normalize_config(config)
        write_json_file(path, normalized)
        return normalized


def clear_config():
    with _CONFIG_LOCK:
        try:
            _config_path().unlink(missing_ok=True)
        except OSError:
            pass


def sync_account(username):
    username = collapse_whitespace(username)
    cfg = load_config()
    if not username:
        return cfg
    if cfg.get('account') != username and (
        cfg.get('account')
        or cfg.get('mode') != 'unknown'
        or cfg.get('fid')
        or cfg.get('uid')
        or cfg.get('school_origin')
    ):
        fresh = dict(DEFAULT_CONFIG, account=username)
        return save_config(fresh)
    if not cfg.get('account') and _config_path().exists():
        cfg['account'] = username
        return save_config(cfg)
    return cfg


def is_chaoxing_only():
    return load_config().get('mode') == 'chaoxing_only'


def has_school_site():
    cfg = load_config()
    if cfg['mode'] == 'chaoxing_only':
        return False
    if cfg['mode'] == 'school_site':
        return bool(cfg['school_origin'])
    return True


def school_origin(default=FALLBACK_ORIGIN):
    cfg = load_config()
    if cfg['mode'] == 'chaoxing_only':
        return ''
    if cfg['mode'] == 'school_site' and cfg['school_origin']:
        return cfg['school_origin']
    return default


def school_fid(default=FALLBACK_FID):
    cfg = load_config()
    if cfg['fid']:
        return cfg['fid']
    if cfg['mode'] == 'chaoxing_only':
        return ''
    return default


def auth_origin(default=FALLBACK_AUTH_ORIGIN):
    cfg = load_config()
    if cfg['mode'] == 'chaoxing_only':
        return ''
    if cfg['mode'] == 'school_site':
        return cfg['auth_origin'] or ''
    return default


def school_catalog_url():
    origin = school_origin()
    return f'{origin}/visit/interaction' if origin else ''


def school_login_url():
    cfg = load_config()
    if cfg['mode'] == 'chaoxing_only':
        return ''
    if cfg['login_url']:
        return cfg['login_url']
    catalog = school_catalog_url()
    a_origin = auth_origin()
    fid = school_fid('')
    if a_origin and catalog:
        if fid:
            return f'{a_origin}/login?fid={fid}&refer={quote(catalog, safe="")}'
        return f'{a_origin}/login?refer={quote(catalog, safe="")}'
    return catalog


def is_school_host(hostname):
    host = (hostname or '').strip().lower()
    if not host or _is_chaoxing_domain(host):
        return False
    cfg = load_config()
    if cfg['school_origin']:
        s_host = (urlsplit(cfg['school_origin']).hostname or '').lower()
        if s_host:
            if host == s_host:
                return True
            suffix = _school_domain_suffix(s_host)
            if suffix and (host == suffix or host.endswith('.' + suffix)):
                return True
    if host in (cfg.get('asset_hosts') or ()):
        return True
    if cfg['mode'] != 'chaoxing_only':
        if host == 'istudy.szpu.edu.cn' or host.endswith('.istudy.szpu.edu.cn'):
            return True
    return False


def is_school_auth_host(hostname):
    host = (hostname or '').strip().lower()
    if not host or _is_chaoxing_domain(host):
        return False
    cfg = load_config()
    if cfg['auth_origin']:
        a_host = (urlsplit(cfg['auth_origin']).hostname or '').lower()
        if a_host and host == a_host:
            return True
    if cfg['login_url']:
        l_host = (urlsplit(cfg['login_url']).hostname or '').lower()
        if l_host and host == l_host and not _is_chaoxing_domain(l_host):
            return True
    if cfg['mode'] != 'chaoxing_only' and host == 'passport.istudy.szpu.edu.cn':
        return True
    return False


def note_asset_url(url):
    parsed = urlsplit(str(url or '').strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if _is_chaoxing_domain(host) or _is_reserved_host(host):
        return False
    with _CONFIG_LOCK:
        cfg = load_config()
        hosts = list(cfg.get('asset_hosts') or [])
        if host not in hosts:
            hosts.append(host)
            cfg['asset_hosts'] = hosts
            save_config(cfg)
            return True
    return False


def is_asset_host(hostname):
    host = (hostname or '').strip().lower()
    if not host:
        return False
    if _is_chaoxing_domain(host) or is_school_host(host):
        return True
    cfg = load_config()
    if host in (cfg.get('asset_hosts') or ()):
        return True
    if cfg['mode'] != 'chaoxing_only' and host in FALLBACK_ASSET_HOSTS:
        return True
    return False


def _is_chaoxing_candidate_url(parsed):
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname.lower()
    if _is_chaoxing_domain(host) or _is_reserved_host(host):
        return False
    path = (parsed.path or '').lower()
    return any(marker in path for marker in _CHAOXING_PATH_MARKERS)


def is_platform_host(hostname):
    host = (hostname or '').strip().lower()
    if not host:
        return False
    return (
        _is_chaoxing_domain(host)
        or is_school_host(host)
        or is_school_auth_host(host)
        or host in {'mooc.istudy.szpu.edu.cn', 'passport.istudy.szpu.edu.cn'}
    )


def is_platform_url(url):
    parsed = urlsplit(str(url or '').strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc or not parsed.hostname:
        return False
    if is_platform_host(parsed.hostname):
        return True
    return _is_chaoxing_candidate_url(parsed)


def lab_hosts():
    hosts = {'jcxygl.chaoxing.com', 'mooc.istudy.szpu.edu.cn'}
    cfg = load_config()
    if cfg.get('school_origin'):
        s_host = (urlsplit(cfg['school_origin']).hostname or '').lower()
        if s_host:
            hosts.add(s_host)
    return hosts


def _extract_fid_from_text(text):
    raw = str(text or '')
    patterns = (
        r'<input[^>]+(?:id|name)=["\'](?:fid|siteFid|currentFid)["\'][^>]+value=["\'](\d+)["\']',
        r'<input[^>]+value=["\'](\d+)["\'][^>]+(?:id|name)=["\'](?:fid|siteFid|currentFid)["\']',
        r'["\']?(?:fid|currentFid|siteFid|schoolFid)["\']?\s*[:=]\s*["\']?(\d{2,10})\b',
        r'[?&]fid=(\d{2,10})\b',
    )
    for pattern in patterns:
        match = re.search(pattern, raw, re.I)
        if match and match.group(1) != '0':
            return match.group(1)
    return ''


def _extract_uid_from_text(text):
    raw = str(text or '')
    patterns = (
        r'<input[^>]+(?:id|name)=["\'](?:uid|userId|_uid)["\'][^>]+value=["\'](\d+)["\']',
        r'["\']?(?:uid|userId|_uid)["\']?\s*[:=]\s*["\']?(\d{4,20})\b',
    )
    for pattern in patterns:
        match = re.search(pattern, raw, re.I)
        if match and match.group(1) != '0':
            return match.group(1)
    return ''


def _extract_school_name_from_text(text):
    raw = str(text or '')
    patterns = (
        r'id=["\']siteName["\'][^>]*>([^<]+)<',
        r'class=["\'][^"\']*schoolName[^"\']*["\'][^>]*>([^<]+)<',
        r'["\']?(?:schoolName|orgName|fidName|unitName)["\']?\s*[:=]\s*["\']([^"\'<>]{2,40})["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, raw, re.I)
        if match:
            name = collapse_whitespace(match.group(1))
            if name and name not in {'个人空间', '学习通', '超星学习通'}:
                return name
    return ''


def _extract_candidate_origins_from_text(text):
    raw = str(text or '')
    candidates = []
    for match in re.finditer(r'https?://[^\s"\'<>\\]+', raw, re.I):
        url = match.group(0).rstrip('.,;)\'"')
        parsed = urlsplit(url)
        if _is_chaoxing_candidate_url(parsed):
            origin = f'{parsed.scheme}://{parsed.netloc}'
            if origin not in candidates:
                candidates.append(origin)
    return candidates


def _probe_school_login_entry(context, origin, fid=''):
    catalog = f'{origin}/visit/interaction'
    req = getattr(context, 'request', None)
    if not req or not hasattr(req, 'get'):
        return '', ''
    try:
        resp = req.get(catalog, max_redirects=0, timeout=10000)
        try:
            status = getattr(resp, 'status', 0)
            headers = getattr(resp, 'headers', {})
            location = (headers.get('location') or headers.get('Location') or '') if isinstance(headers, dict) else ''
            if isinstance(status, int) and status in {301, 302, 303, 307, 308} and isinstance(location, str) and location:
                target = urljoin(catalog, location)
                parsed = urlsplit(target)
                if parsed.scheme in {'http', 'https'} and parsed.netloc:
                    a_origin = f'{parsed.scheme}://{parsed.netloc}' if not _is_chaoxing_domain(parsed.hostname) else ''
                    return a_origin, target
        finally:
            if hasattr(resp, 'dispose'):
                resp.dispose()
    except Exception:
        pass
    host = (urlsplit(origin).hostname or '').lower()
    if host.startswith('mooc.') and len(host) > 5:
        scheme = urlsplit(origin).scheme or 'https'
        a_origin = f'{scheme}://passport.{host[5:]}'
        if fid:
            return a_origin, f'{a_origin}/login?fid={fid}&refer={quote(catalog, safe="")}'
        return a_origin, f'{a_origin}/login?refer={quote(catalog, safe="")}'
    return '', catalog


def _parse_notice_url_and_fid(notice):
    if not isinstance(notice, dict):
        return '', ''
    raw_att = notice.get('attachment')
    if raw_att:
        try:
            import json
            items = json.loads(raw_att) if isinstance(raw_att, str) else raw_att
            if isinstance(items, list):
                matched_url = ''
                fallback_url = ''
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    web = item.get('att_web')
                    if not isinstance(web, dict):
                        continue
                    att_url = collapse_whitespace(web.get('url'))
                    if not att_url:
                        continue
                    if str(web.get('examOrWork', '')).lower() == 'work':
                        matched_url = att_url
                    elif not fallback_url:
                        fallback_url = att_url
                chosen_url = matched_url or fallback_url
                if chosen_url:
                    return chosen_url, _extract_fid_from_text(chosen_url)
        except Exception:
            pass
    for key in ('content', 'rtf_content'):
        text = str(notice.get(key) or '')
        if not text:
            continue
        origins = _extract_candidate_origins_from_text(text)
        fid = _extract_fid_from_text(text)
        if origins:
            return origins[0] + '/visit/interaction', fid
        if fid:
            return '', fid
    return '', ''


def _prefer_https_origin(context, origin, fid=''):
    parsed = urlsplit(str(origin or '').strip())
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'http' or parsed.port is not None or not host or _is_reserved_host(host):
        return origin, None
    https_origin = f'https://{parsed.netloc}'
    known = KNOWN_SCHOOL_SITES.get(str(fid or ''), {})
    if known.get('school_origin') == https_origin and known.get('auth_origin'):
        return https_origin, None
    if context is not None and getattr(context, 'request', None) is not None:
        https_res = _probe_school_login_entry(context, https_origin, fid=fid)
        if https_res[0] or https_res[1] != f'{https_origin}/visit/interaction':
            return https_origin, https_res
        http_res = _probe_school_login_entry(context, origin, fid=fid)
        if http_res[0] or http_res[1] != f'{origin}/visit/interaction':
            return origin, http_res
        return https_origin, https_res
    return origin, None


def note_notice_url(url, context=None):
    parsed = urlsplit(str(url or '').strip())
    if not _is_chaoxing_candidate_url(parsed):
        return False
    raw_origin = f'{parsed.scheme}://{parsed.netloc}'
    host = (parsed.hostname or '').lower()
    cfg = load_config()
    fid = ''
    for k, v in parse_qs(parsed.query).items():
        if k.lower() == 'fid' and v and str(v[0]).strip().isdigit():
            fid = str(v[0]).strip()
            break
    existing_origin = cfg.get('school_origin') or ''
    existing_host = (urlsplit(existing_origin).hostname or '').lower()
    if cfg.get('mode') == 'school_site' and existing_host == host:
        if existing_origin.startswith('https://') or parsed.scheme == 'http':
            if fid and not cfg.get('fid'):
                with _CONFIG_LOCK:
                    latest = load_config()
                    if fid and not latest.get('fid'):
                        latest['fid'] = fid
                        save_config(latest)
            return False
    origin, cached_probe = _prefer_https_origin(context, raw_origin, fid=fid or cfg.get('fid') or '')
    if cfg.get('mode') == 'school_site' and cfg.get('school_origin') == origin:
        if fid and not cfg.get('fid'):
            with _CONFIG_LOCK:
                latest = load_config()
                if fid and not latest.get('fid'):
                    latest['fid'] = fid
                    save_config(latest)
        return False
    eff_fid = fid or cfg.get('fid') or ''
    known = KNOWN_SCHOOL_SITES.get(eff_fid, {})
    if known.get('school_origin') == origin and known.get('auth_origin'):
        a_origin = known['auth_origin']
        login_url = f'{a_origin}/login?fid={eff_fid}&refer={quote(origin + "/visit/interaction", safe="")}'
    elif cached_probe is not None:
        a_origin, login_url = cached_probe
    else:
        a_origin, login_url = _probe_school_login_entry(context, origin, fid=eff_fid)
    with _CONFIG_LOCK:
        latest = load_config()
        latest_origin = latest.get('school_origin') or ''
        latest_host = (urlsplit(latest_origin).hostname or '').lower()
        if latest.get('mode') == 'school_site' and latest_host == host and (
            latest_origin == origin or (latest_origin.startswith('https://') and origin.startswith('http://'))
        ):
            if fid and not latest.get('fid'):
                latest['fid'] = fid
                save_config(latest)
            return False
        latest['mode'] = 'school_site'
        latest['school_origin'] = origin
        if a_origin:
            latest['auth_origin'] = a_origin
        if login_url:
            latest['login_url'] = login_url
        if fid and not latest.get('fid'):
            latest['fid'] = fid
        latest['asset_hosts'] = _derive_asset_hosts(origin, latest.get('asset_hosts'))
        latest['discovered_at'] = datetime.now().isoformat(timespec='seconds')
        save_config(latest)
    return True


def discover_school(context, state=None, username=None):
    cfg = sync_account(username) if username else load_config()
    if username:
        cfg['account'] = collapse_whitespace(username)

    uid = cfg.get('uid') or ''
    fid = cfg.get('fid') or ''
    school_name = cfg.get('school_name') or ''
    candidate_origin = cfg.get('school_origin') if cfg.get('mode') == 'school_site' else ''
    fetched_base = False

    if hasattr(context, 'cookies'):
        try:
            for cookie in context.cookies() or ():
                if not isinstance(cookie, dict):
                    continue
                c_name = str(cookie.get('name') or '').strip()
                c_val = str(cookie.get('value') or '').strip()
                if c_name.lower() == 'fid' and c_val.isdigit() and c_val != '0' and not fid:
                    fid = c_val
                elif c_name.lower() in {'uid', '_uid'} and c_val.isdigit() and c_val != '0' and not uid:
                    uid = c_val
        except Exception:
            pass

    if not candidate_origin and fid in KNOWN_SCHOOL_SITES:
        known = KNOWN_SCHOOL_SITES[fid]
        candidate_origin = known['school_origin']
        school_name = school_name or known.get('school_name', '')

    req = getattr(context, 'request', None)
    if req and hasattr(req, 'get') and not (candidate_origin and fid and school_name and CHAOXING_BASE_URL == _DEFAULT_CHAOXING_BASE_URL):
        try:
            sep = '&' if '?' in CHAOXING_BASE_URL else '?'
            resp = req.get(f'{CHAOXING_BASE_URL}{sep}t={int(time.time() * 1000)}', timeout=12000)
            try:
                status = getattr(resp, 'status', None)
                body = resp.text() if hasattr(resp, 'text') else ''
                url = getattr(resp, 'url', '')
                url = url if isinstance(url, str) else ''
                if isinstance(status, int) and 200 <= status < 300 and isinstance(body, str) and body.strip() and 'passport2.chaoxing.com' not in url.lower():
                    fetched_base = True
                    fid = fid or _extract_fid_from_text(body) or _extract_fid_from_text(url)
                    uid = uid or _extract_uid_from_text(body)
                    school_name = school_name or _extract_school_name_from_text(body)
                    if not candidate_origin:
                        origins = _extract_candidate_origins_from_text(body)
                        if origins:
                            candidate_origin = origins[0]
            finally:
                if hasattr(resp, 'dispose'):
                    resp.dispose()
        except Exception:
            pass

    if not candidate_origin and isinstance(state, dict):
        for item in (state.get('assignments') or {}).values():
            if not isinstance(item, dict):
                continue
            for field in ('entry_url', 'work_url', 'list_url', 'answer_url', 'course_url', 'detail_url'):
                parsed = urlsplit(str(item.get(field) or '').strip())
                if _is_chaoxing_candidate_url(parsed):
                    candidate_origin = f'{parsed.scheme}://{parsed.netloc}'
                    fid = fid or _extract_fid_from_text(str(item.get(field) or ''))
                    break
            if candidate_origin:
                break

    if not candidate_origin and fid in KNOWN_SCHOOL_SITES:
        known = KNOWN_SCHOOL_SITES[fid]
        candidate_origin = known['school_origin']
        school_name = school_name or known.get('school_name', '')

    if not candidate_origin and req and hasattr(req, 'post') and fetched_base:
        try:
            resp = req.post(
                NOTICE_API_URL,
                form={'type': '0', 'notice_type': '0', 'lastValue': '', 'sort': '0'},
                headers={'Origin': 'https://notice.chaoxing.com', 'Referer': 'https://notice.chaoxing.com/pc/notice/myNotice', 'X-Requested-With': 'XMLHttpRequest'},
                timeout=10000,
            )
            try:
                payload = resp.json() if hasattr(resp, 'json') else {}
                notices = ((payload.get('notices') or {}).get('list') or []) if isinstance(payload, dict) else []
                for notice in notices:
                    n_url, n_fid = _parse_notice_url_and_fid(notice)
                    if n_fid and not fid:
                        fid = n_fid
                    if n_url:
                        parsed = urlsplit(n_url)
                        if _is_chaoxing_candidate_url(parsed):
                            candidate_origin = f'{parsed.scheme}://{parsed.netloc}'
                            break
            finally:
                if hasattr(resp, 'dispose'):
                    resp.dispose()
        except Exception:
            pass

    if not fetched_base and not candidate_origin and not fid and cfg.get('mode') == 'unknown':
        return cfg

    prev_fid = cfg.get('fid', '')
    cfg['uid'] = uid
    cfg['fid'] = fid
    cfg['school_name'] = school_name
    cfg['discovered_at'] = datetime.now().isoformat(timespec='seconds')

    if candidate_origin:
        candidate_origin, cached_probe = _prefer_https_origin(context, candidate_origin, fid=fid)
        known = KNOWN_SCHOOL_SITES.get(fid, {})
        if (cfg.get('mode') == 'school_site' and cfg.get('school_origin') == candidate_origin
                and cfg.get('auth_origin') and cfg.get('login_url') and (not fid or prev_fid == fid)):
            a_origin = cfg['auth_origin']
            login_url = cfg['login_url']
            if fid and 'fid=0' in login_url:
                login_url = re.sub(r'([?&])fid=0\b', rf'\g<1>fid={fid}', login_url, count=1)
        elif known.get('school_origin') == candidate_origin and known.get('auth_origin'):
            a_origin = known['auth_origin']
            login_url = f'{a_origin}/login?fid={fid or FALLBACK_FID}&refer={quote(candidate_origin + "/visit/interaction", safe="")}'
        elif cached_probe is not None:
            a_origin, login_url = cached_probe
        else:
            a_origin, login_url = _probe_school_login_entry(context, candidate_origin, fid=fid)
        if not a_origin and known.get('school_origin') == candidate_origin:
            a_origin = known.get('auth_origin', '')
            if a_origin and not login_url:
                login_url = f'{a_origin}/login?refer={quote(candidate_origin + "/visit/interaction", safe="")}'
        cfg['mode'] = 'school_site'
        cfg['school_origin'] = candidate_origin
        cfg['auth_origin'] = a_origin
        cfg['login_url'] = login_url or f'{candidate_origin}/visit/interaction'
        cfg['asset_hosts'] = _derive_asset_hosts(candidate_origin, known.get('asset_hosts', ()))
    else:
        cfg['mode'] = 'chaoxing_only'
        cfg['school_origin'] = ''
        cfg['auth_origin'] = ''
        cfg['login_url'] = ''
        cfg['asset_hosts'] = []

    return save_config(cfg)


def public_info():
    cfg = load_config()
    return {
        'mode': cfg['mode'],
        'fid': cfg['fid'],
        'school_name': cfg['school_name'],
        'school_origin': cfg['school_origin'],
        'has_school_site': has_school_site(),
    }
