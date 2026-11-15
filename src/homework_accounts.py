"""Per-account directory isolation, one-time legacy migration and account switching."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time

import homework_runtime as runtime
import homework_shared as shared

ACCOUNTS_FILE_NAME = 'accounts.json'
ACCOUNTS_DIR_NAME = 'accounts'
LEGACY_BACKUP_DIR_NAME = 'legacy_backup'
LEGACY_FILES = (
    'state.json',
    'answer_drafts.json',
    'assignment_overrides.json',
    'browser_session.json',
    'login_credentials.bin',
    'school_config.json',
    'sync.log',
    'latest_homework.txt',
    'reminder.log',
    'latest_assignments.txt',
)
LEGACY_DIRS = ('homework_content', 'answer_attachments', 'browser_data')
BACKUP_FILES = ('state.json', 'answer_drafts.json', 'assignment_overrides.json', 'school_config.json')

_LOCK = threading.RLock()
_ISOLATED = False
_BASE_DATA_DIR: Path | None = None


def base_data_dir(explicit: Path | str | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    return _BASE_DATA_DIR or Path(runtime.DATA_DIR)


def is_isolated() -> bool:
    return _ISOLATED


def set_isolated_for_test(enabled: bool, base_dir: Path | str | None = None) -> None:
    global _ISOLATED, _BASE_DATA_DIR
    with _LOCK:
        _ISOLATED = bool(enabled)
        _BASE_DATA_DIR = Path(base_dir) if base_dir is not None else None


def accounts_file(base_dir: Path | str | None = None) -> Path:
    return base_data_dir(base_dir) / ACCOUNTS_FILE_NAME


def accounts_root(base_dir: Path | str | None = None) -> Path:
    return base_data_dir(base_dir) / ACCOUNTS_DIR_NAME


def _normalize_username(username: str | None) -> str:
    return shared.collapse_whitespace(str(username or '')).strip()


def _normalize_alias(alias: str | None) -> str:
    return shared.collapse_whitespace(str(alias or '')).strip()[:40]


def _effective_label(meta: dict, *, fallback: str = '当前账号') -> str:
    return (
        _normalize_alias(meta.get('alias'))
        or _normalize_username(meta.get('username'))
        or _normalize_username(meta.get('school_name'))
        or (f"用户 {meta['uid']}" if _normalize_uid(meta.get('uid')) else fallback)
    )


def _normalize_uid(uid: str | None) -> str:
    text = shared.collapse_whitespace(str(uid or '')).strip()
    return text if text.isdigit() else ''


def _safe_dir_name(name: str) -> str:
    cleaned = re.sub(r'[^a-zA-Z0-9_-]', '_', str(name or '').strip())
    return cleaned[:48] or 'default'


def make_account_id(username: str = '', uid: str = '') -> str:
    norm_u = _normalize_username(username)
    norm_id = _normalize_uid(uid)
    if norm_u:
        digest = hashlib.sha256(norm_u.lower().encode('utf-8')).hexdigest()[:12]
        return f'acct_{digest}'
    if norm_id:
        return f'uid_{norm_id}'
    return 'default'


def _validated_account_dir(dir_name: str, base_dir: Path | str | None = None) -> Path:
    root = accounts_root(base_dir).resolve()
    safe_name = _safe_dir_name(dir_name)
    candidate = root / safe_name
    if candidate.is_symlink():
        raise ValueError('账号目录不允许使用符号链接')
    resolved = candidate.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError('账号目录路径无效')
    return resolved


def _read_registry(base_dir: Path | str | None = None) -> dict:
    path = accounts_file(base_dir)
    data = shared.read_json_file(path, {'version': 1, 'active_id': None, 'accounts': {}}, corrupted='fallback')
    if not isinstance(data, dict):
        data = {'version': 1, 'active_id': None, 'accounts': {}}
    accounts = data.get('accounts')
    if not isinstance(accounts, dict):
        accounts = {}
    cleaned = {}
    for acct_id, meta in accounts.items():
        if not isinstance(meta, dict):
            continue
        safe_id = _safe_dir_name(str(meta.get('id') or acct_id))
        dir_name = _safe_dir_name(str(meta.get('dir_name') or safe_id))
        cleaned[safe_id] = {
            'id': safe_id,
            'dir_name': dir_name,
            'username': _normalize_username(meta.get('username')),
            'uid': _normalize_uid(meta.get('uid')),
            'alias': _normalize_alias(meta.get('alias')),
            'label': _normalize_username(meta.get('label')),
            'school_name': _normalize_username(meta.get('school_name')),
            'fid': _normalize_uid(meta.get('fid')),
            'mode': _normalize_username(meta.get('mode')) or 'unknown',
            'last_sync_at': str(meta.get('last_sync_at') or ''),
            'updated_at': str(meta.get('updated_at') or ''),
        }
        cleaned[safe_id]['label'] = _effective_label(
            cleaned[safe_id],
            fallback='默认账号' if safe_id == 'default' else '当前账号',
        )
    active = data.get('active_id')
    if active not in cleaned:
        active = next(iter(cleaned), None)
    return {'version': 1, 'active_id': active, 'accounts': cleaned}


def _write_registry(data: dict, base_dir: Path | str | None = None) -> None:
    path = accounts_file(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    shared.write_json_file(path, data)


def _cookie_uid_from_session(session_path: Path) -> str:
    payload = shared.read_json_file(session_path, {}, corrupted='fallback')
    cookies = payload.get('cookies') if isinstance(payload, dict) else None
    if not isinstance(cookies, list):
        return ''
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '')
        if name in {'_uid', 'UID'}:
            val = _normalize_uid(item.get('value'))
            if val:
                return val
    return ''


def _cookie_uid_from_context(context) -> str:
    if not context or not hasattr(context, 'cookies'):
        return ''
    try:
        cookies = context.cookies()
    except Exception:
        return ''
    for item in cookies or ():
        if not isinstance(item, dict):
            continue
        if item.get('name') in {'_uid', 'UID'}:
            val = _normalize_uid(item.get('value'))
            if val:
                return val
    return ''


def _has_chaoxing_session_cookies(session_path: Path) -> bool:
    payload = shared.read_json_file(session_path, {}, corrupted='fallback')
    cookies = payload.get('cookies') if isinstance(payload, dict) else None
    return bool(isinstance(cookies, list) and cookies)


def _close_cdp_browser_if_profile(target_profile: Path) -> None:
    """Close the per-account CDP browser cleanly without closing the assistant UI window."""
    try:
        import homework_engine as engine
        from playwright.sync_api import sync_playwright
    except Exception:
        return
    try:
        resolved_target = Path(target_profile).resolve()
    except OSError:
        return
    closed_browser = False
    try:
        with sync_playwright() as p:
            browser = runtime.connect_browser_direct(p, engine.CHAOXING_CDP_URL, timeout=450)
            session = browser.new_browser_cdp_session()
            try:
                cmd = session.send('SystemInfo.getInfo').get('commandLine', '')
                args = runtime._windows_argv(cmd) if os.name == 'nt' else cmd.split()
                profile = next((v.split('=', 1)[1] for v in args if v.startswith('--user-data-dir=')), None)
                if profile and Path(profile).resolve() == resolved_target:
                    targets = session.send('Target.getTargets').get('targetInfos', [])
                    pages = [t for t in targets if t.get('type') == 'page']
                    has_local_ui = any(
                        str(t.get('url') or '').startswith(('http://127.0.0.1:', 'http://localhost:'))
                        for t in pages
                    )
                    if has_local_ui:
                        for item in pages:
                            page_url = str(item.get('url') or '')
                            if not page_url.startswith(('http://127.0.0.1:', 'http://localhost:')):
                                try:
                                    session.send('Target.closeTarget', {'targetId': item.get('targetId')})
                                except Exception:
                                    pass
                    else:
                        session.send('Browser.close')
                        closed_browser = True
            finally:
                try:
                    session.detach()
                except Exception:
                    pass
    except Exception:
        pass
    if closed_browser and hasattr(runtime, 'cdp_endpoint_alive'):
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and runtime.cdp_endpoint_alive(engine.CHAOXING_CDP_URL, timeout=0.15):
            time.sleep(0.05)


def apply_active_paths(acct_id: str | None = None, *, base_dir: Path | str | None = None,
                       close_old_browser: bool = False) -> Path:
    """Bind per-account paths on loaded modules while keeping global settings at base_dir."""
    global _ISOLATED, _BASE_DATA_DIR
    with _LOCK:
        root_dir = base_data_dir(base_dir)
        _BASE_DATA_DIR = root_dir
        _ISOLATED = True
        registry = _read_registry(root_dir)
        target_id = acct_id or registry.get('active_id') or 'default'
        meta = registry['accounts'].get(target_id)
        dir_name = meta['dir_name'] if meta else target_id
        acct_dir = _validated_account_dir(dir_name, root_dir)
        acct_dir.mkdir(parents=True, exist_ok=True)
        content_dir = acct_dir / 'homework_content'
        profile_dir = acct_dir / 'browser_data'
        upload_dir = acct_dir / 'answer_attachments'

        old_profile = getattr(runtime, 'PROFILE_DIR', None)
        if close_old_browser and old_profile and Path(old_profile).resolve() != profile_dir.resolve():
            _close_cdp_browser_if_profile(Path(old_profile))

        runtime.PROFILE_DIR = profile_dir

        engine = sys.modules.get('homework_engine')
        if engine is not None:
            engine.DATA_DIR = acct_dir
            engine.STATE_FILE = acct_dir / 'state.json'
            engine.DRAFTS_FILE = acct_dir / 'answer_drafts.json'
            engine.OVERRIDES_FILE = acct_dir / 'assignment_overrides.json'
            engine.LOG_FILE = acct_dir / 'reminder.log'
            engine.LATEST_FILE = acct_dir / 'latest_assignments.txt'
            engine.CONTENT_DIR = content_dir
            engine.PROFILE_DIR = profile_dir
            engine._OVERRIDES_CACHE = (None, {})
            engine.CONNECTED_BROWSER = None
            engine.LOGIN_TARGET_ID = None
            engine.LOGIN_TARGET_PLATFORM = None

        auth = sys.modules.get('homework_auth')
        if auth is None:
            try:
                import homework_auth as auth
            except ImportError:
                auth = None
        if auth is not None:
            auth.CREDENTIAL_FILE = acct_dir / 'login_credentials.bin'
            auth._AUTO_FAILED = False
            if hasattr(auth, '_PREFLIGHT_PAGES') and isinstance(auth._PREFLIGHT_PAGES, dict):
                auth._PREFLIGHT_PAGES.clear()

        school = sys.modules.get('homework_school')
        if school is None:
            try:
                import homework_school as school
            except ImportError:
                school = None
        if school is not None:
            school.SCHOOL_FILE = acct_dir / 'school_config.json'

        app = sys.modules.get('homework_app')
        if app is not None:
            app.DATA_DIR = acct_dir
            app.UPLOAD_DIR = upload_dir

        return acct_dir


def _move_legacy_into_account(root_dir: Path, acct_dir: Path) -> None:
    acct_dir.mkdir(parents=True, exist_ok=True)
    has_legacy = any((root_dir / name).exists() for name in LEGACY_FILES + LEGACY_DIRS)
    if not has_legacy:
        return
    backup_dir = root_dir / LEGACY_BACKUP_DIR_NAME
    if not backup_dir.exists():
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            for name in BACKUP_FILES:
                src = root_dir / name
                if src.is_file():
                    shutil.copy2(src, backup_dir / name)
        except OSError:
            pass
    for name in LEGACY_FILES:
        src = root_dir / name
        dst = acct_dir / name
        if src.exists() and not src.is_symlink() and not dst.exists():
            try:
                shutil.move(str(src), str(dst))
            except OSError:
                try:
                    shutil.copy2(src, dst)
                    src.unlink(missing_ok=True)
                except OSError:
                    pass
    for name in LEGACY_DIRS:
        src = root_dir / name
        dst = acct_dir / name
        if src.is_dir() and not src.is_symlink() and not dst.exists():
            try:
                shutil.move(str(src), str(dst))
            except OSError:
                # Chromium cache or an open log handle can lock browser_data; skip cleanly.
                pass


def _read_root_credentials(root_dir: Path) -> str:
    cred_path = root_dir / 'login_credentials.bin'
    if not cred_path.exists():
        return ''
    auth = sys.modules.get('homework_auth')
    if auth is None:
        try:
            import homework_auth as auth
        except Exception:
            return ''
    prev = getattr(auth, 'CREDENTIAL_FILE', None)
    try:
        auth.CREDENTIAL_FILE = cred_path
        creds = auth.load_credentials()
        return _normalize_username(creds[0]) if creds else ''
    except Exception:
        return ''
    finally:
        if prev is not None:
            auth.CREDENTIAL_FILE = prev


def init_accounts(base_dir: Path | str | None = None) -> dict:
    """Initialize isolated account storage and migrate pre-isolation data once."""
    with _LOCK:
        root_dir = base_data_dir(base_dir)
        root_dir.mkdir(parents=True, exist_ok=True)
        reg_path = accounts_file(root_dir)
        if not reg_path.exists():
            school_cfg = shared.read_json_file(root_dir / 'school_config.json', {}, corrupted='fallback')
            if not isinstance(school_cfg, dict):
                school_cfg = {}
            username = _read_root_credentials(root_dir) or _normalize_username(school_cfg.get('account'))
            uid = _normalize_uid(school_cfg.get('uid')) or _cookie_uid_from_session(root_dir / 'browser_session.json')
            school_name = _normalize_username(school_cfg.get('school_name'))
            fid = _normalize_uid(school_cfg.get('fid'))
            mode = _normalize_username(school_cfg.get('mode')) or 'unknown'
            acct_id = make_account_id(username=username, uid=uid)
            acct_dir = _validated_account_dir(acct_id, root_dir)
            _move_legacy_into_account(root_dir, acct_dir)
            if not uid:
                uid = _cookie_uid_from_session(acct_dir / 'browser_session.json')
            state_data = shared.read_json_file(acct_dir / 'state.json', {}, corrupted='fallback')
            last_sync = str(state_data.get('last_success') or '') if isinstance(state_data, dict) else ''
            now = shared.iso_timestamp()
            registry = {
                'version': 1,
                'active_id': acct_id,
                'accounts': {
                    acct_id: {
                        'id': acct_id,
                        'dir_name': acct_id,
                        'username': username,
                        'uid': uid,
                        'alias': '',
                        'label': username or school_name or ('默认账号' if acct_id == 'default' else acct_id),
                        'school_name': school_name,
                        'fid': fid,
                        'mode': mode,
                        'last_sync_at': last_sync,
                        'updated_at': now,
                    }
                },
            }
            _write_registry(registry, root_dir)
        else:
            registry = _read_registry(root_dir)
            if not registry['accounts']:
                now = shared.iso_timestamp()
                registry = {
                    'version': 1,
                    'active_id': 'default',
                    'accounts': {
                        'default': {
                            'id': 'default', 'dir_name': 'default', 'username': '', 'uid': '', 'alias': '',
                            'label': '默认账号', 'school_name': '', 'fid': '', 'mode': 'unknown',
                            'last_sync_at': '', 'updated_at': now,
                        }
                    },
                }
                _write_registry(registry, root_dir)
        apply_active_paths(registry['active_id'], base_dir=root_dir, close_old_browser=False)
        return _read_registry(root_dir)


def _dir_has_user_data(acct_dir: Path) -> bool:
    state = shared.read_json_file(acct_dir / 'state.json', {}, corrupted='fallback')
    if isinstance(state, dict) and state.get('assignments'):
        return True
    drafts = shared.read_json_file(acct_dir / 'answer_drafts.json', {}, corrupted='fallback')
    if isinstance(drafts, dict) and drafts:
        return True
    if (acct_dir / 'login_credentials.bin').exists() or _has_chaoxing_session_cookies(acct_dir / 'browser_session.json'):
        return True
    return False


def _drop_empty_dir(acct_dir: Path, root_dir: Path) -> None:
    try:
        resolved = acct_dir.resolve()
        root = accounts_root(root_dir).resolve()
        if resolved != root and resolved.is_relative_to(root) and resolved.is_dir():
            shutil.rmtree(resolved, ignore_errors=True)
    except OSError:
        pass


def prepare_login(username: str | None = None, *, alias: str | None = None, base_dir: Path | str | None = None) -> dict:
    """Select or create an isolated profile before logging in as `username`."""
    with _LOCK:
        root_dir = base_data_dir(base_dir)
        if not accounts_file(root_dir).exists():
            init_accounts(root_dir)
        registry = _read_registry(root_dir)
        prev_active = registry.get('active_id')
        norm_u = _normalize_username(username)
        norm_alias = _normalize_alias(alias) if alias is not None else ''
        if not norm_u:
            return {'prev_active_id': prev_active, 'active_id': prev_active, 'created_id': None}

        existing_id = next(
            (aid for aid, m in registry['accounts'].items() if _normalize_username(m.get('username')).lower() == norm_u.lower()),
            None,
        )
        if existing_id:
            if existing_id != prev_active:
                registry['active_id'] = existing_id
                _write_registry(registry, root_dir)
                apply_active_paths(existing_id, base_dir=root_dir, close_old_browser=True)
            return {'prev_active_id': prev_active, 'active_id': existing_id, 'created_id': None}

        target_id = make_account_id(username=norm_u)
        while target_id in registry['accounts'] and _normalize_username(registry['accounts'][target_id].get('username')).lower() != norm_u.lower():
            target_id = target_id + '_2'

        # Promote an untouched empty 'default' slot in place so we never leave a ghost default row.
        if prev_active == 'default' and 'default' in registry['accounts']:
            cur_meta = registry['accounts']['default']
            cur_dir = _validated_account_dir(cur_meta['dir_name'], root_dir)
            if not cur_meta.get('username') and not cur_meta.get('uid') and not _dir_has_user_data(cur_dir):
                new_dir = _validated_account_dir(target_id, root_dir)
                dir_name = target_id
                try:
                    if cur_dir.exists() and not new_dir.exists():
                        cur_dir.rename(new_dir)
                except OSError:
                    dir_name = cur_meta['dir_name']
                registry['accounts'].pop('default', None)
                registry['accounts'][target_id] = {
                    'id': target_id,
                    'dir_name': dir_name,
                    'username': norm_u,
                    'uid': '',
                    'alias': norm_alias,
                    'label': norm_alias or norm_u,
                    'school_name': '',
                    'fid': '',
                    'mode': 'unknown',
                    'last_sync_at': '',
                    'updated_at': shared.iso_timestamp(),
                }
                registry['active_id'] = target_id
                _write_registry(registry, root_dir)
                apply_active_paths(target_id, base_dir=root_dir, close_old_browser=True)
                return {'prev_active_id': None, 'active_id': target_id, 'created_id': target_id}

        target_dir = _validated_account_dir(target_id, root_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        registry['accounts'][target_id] = {
            'id': target_id,
            'dir_name': target_id,
            'username': norm_u,
            'uid': '',
            'alias': norm_alias,
            'label': norm_alias or norm_u,
            'school_name': '',
            'fid': '',
            'mode': 'unknown',
            'last_sync_at': '',
            'updated_at': shared.iso_timestamp(),
        }
        registry['active_id'] = target_id
        _write_registry(registry, root_dir)
        apply_active_paths(target_id, base_dir=root_dir, close_old_browser=True)
        return {'prev_active_id': prev_active, 'active_id': target_id, 'created_id': target_id}


def finalize_login(prep: dict | None = None, *, context=None, username: str = '', alias: str | None = None,
                   ok: bool = True, base_dir: Path | str | None = None) -> dict:
    """Update uid/school metadata or roll back a failed brand-new stub."""
    with _LOCK:
        root_dir = base_data_dir(base_dir)
        registry = _read_registry(root_dir)
        prep = prep or {}
        created_id = prep.get('created_id')
        prev_active = prep.get('prev_active_id')
        active_id = registry.get('active_id')

        if not ok:
            if created_id and created_id == active_id and created_id in registry['accounts']:
                created_dir = _validated_account_dir(registry['accounts'][created_id]['dir_name'], root_dir)
                # Keep only if a valid session already landed; otherwise roll back to prior account.
                if not _has_chaoxing_session_cookies(created_dir / 'browser_session.json'):
                    state = shared.read_json_file(created_dir / 'state.json', {}, corrupted='fallback')
                    if not (isinstance(state, dict) and state.get('assignments')):
                        registry['accounts'].pop(created_id, None)
                        fallback = prev_active if prev_active in registry['accounts'] else next(iter(registry['accounts']), None)
                        registry['active_id'] = fallback
                        _write_registry(registry, root_dir)
                        _drop_empty_dir(created_dir, root_dir)
                        if fallback:
                            apply_active_paths(fallback, base_dir=root_dir, close_old_browser=False)
            return _read_registry(root_dir)

        sync_active_metadata(context=context, username=username, base_dir=root_dir)
        registry = _read_registry(root_dir)
        active_id = registry.get('active_id')
        if alias is not None and active_id and active_id in registry['accounts']:
            meta = registry['accounts'][active_id]
            meta['alias'] = _normalize_alias(alias)
            meta['label'] = _effective_label(meta)
            meta['updated_at'] = shared.iso_timestamp()
            _write_registry(registry, root_dir)
        if created_id and created_id == active_id and active_id in registry['accounts']:
            active_meta = registry['accounts'][active_id]
            uid = _normalize_uid(active_meta.get('uid'))
            if uid:
                duplicate_id = next(
                    (aid for aid, m in registry['accounts'].items() if aid != active_id and _normalize_uid(m.get('uid')) == uid),
                    None,
                )
                if duplicate_id:
                    # Merge credentials/session acquired during username login into existing UID profile.
                    src_dir = _validated_account_dir(active_meta['dir_name'], root_dir)
                    dst_meta = registry['accounts'][duplicate_id]
                    dst_dir = _validated_account_dir(dst_meta['dir_name'], root_dir)
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    for fname in ('login_credentials.bin', 'browser_session.json', 'school_config.json'):
                        s_file = src_dir / fname
                        if s_file.is_file():
                            try:
                                shutil.copy2(s_file, dst_dir / fname)
                            except OSError:
                                pass
                    if alias is not None:
                        dst_meta['alias'] = _normalize_alias(alias)
                    elif active_meta.get('alias') or not dst_meta.get('alias'):
                        dst_meta['alias'] = _normalize_alias(active_meta.get('alias') or dst_meta.get('alias'))
                    if active_meta.get('username'):
                        dst_meta['username'] = active_meta['username']
                    dst_meta['label'] = _effective_label(dst_meta)
                    for field in ('school_name', 'fid', 'mode'):
                        if active_meta.get(field) and active_meta.get(field) != 'unknown':
                            dst_meta[field] = active_meta[field]
                    dst_meta['updated_at'] = shared.iso_timestamp()
                    registry['accounts'].pop(active_id, None)
                    registry['active_id'] = duplicate_id
                    _write_registry(registry, root_dir)
                    _drop_empty_dir(src_dir, root_dir)
                    apply_active_paths(duplicate_id, base_dir=root_dir, close_old_browser=True)
        return _read_registry(root_dir)


def sync_active_metadata(*, context=None, username: str = '', base_dir: Path | str | None = None) -> None:
    """Update active account entry with current username, uid, school and last sync."""
    with _LOCK:
        root_dir = base_data_dir(base_dir)
        if not accounts_file(root_dir).exists():
            return
        registry = _read_registry(root_dir)
        active_id = registry.get('active_id')
        if not active_id or active_id not in registry['accounts']:
            return
        meta = registry['accounts'][active_id]
        acct_dir = _validated_account_dir(meta['dir_name'], root_dir)
        norm_u = _normalize_username(username)
        if not norm_u:
            auth = sys.modules.get('homework_auth')
            if auth is not None:
                try:
                    info = auth.credential_info()
                    norm_u = _normalize_username(info.get('username'))
                except Exception:
                    pass
        if norm_u:
            meta['username'] = norm_u
        uid = _cookie_uid_from_context(context) or _cookie_uid_from_session(acct_dir / 'browser_session.json')
        school = sys.modules.get('homework_school')
        if school is not None:
            try:
                scfg = school.load_config()
                uid = uid or _normalize_uid(scfg.get('uid'))
                if scfg.get('school_name'):
                    meta['school_name'] = _normalize_username(scfg.get('school_name'))
                if scfg.get('fid'):
                    meta['fid'] = _normalize_uid(scfg.get('fid'))
                if scfg.get('mode') and scfg.get('mode') != 'unknown':
                    meta['mode'] = scfg['mode']
                if not meta.get('username') and scfg.get('account'):
                    meta['username'] = _normalize_username(scfg.get('account'))
            except Exception:
                pass
        elif not meta.get('school_name') and (meta.get('username') or uid):
            meta['school_name'] = '深圳职业技术大学'
        if uid:
            meta['uid'] = uid
        engine = sys.modules.get('homework_engine')
        state_path = (engine.STATE_FILE if engine is not None else acct_dir / 'state.json')
        state_data = shared.read_json_file(state_path, {}, corrupted='fallback')
        if isinstance(state_data, dict) and state_data.get('last_success'):
            meta['last_sync_at'] = str(state_data['last_success'])
        meta['label'] = _effective_label(meta)
        meta['updated_at'] = shared.iso_timestamp()
        _write_registry(registry, root_dir)


def rename_account(target_id: str, label: str | None = None, *, base_dir: Path | str | None = None) -> dict:
    """Set or clear a custom display alias for a saved account."""
    with _LOCK:
        root_dir = base_data_dir(base_dir)
        if not accounts_file(root_dir).exists():
            init_accounts(root_dir)
        registry = _read_registry(root_dir)
        clean_id = _safe_dir_name(target_id)
        if clean_id not in registry['accounts']:
            raise ValueError('找不到指定的账号记录')
        meta = registry['accounts'][clean_id]
        meta['alias'] = _normalize_alias(label)
        meta['label'] = _effective_label(
            meta,
            fallback='默认账号' if clean_id == 'default' else '当前账号',
        )
        meta['updated_at'] = shared.iso_timestamp()
        _write_registry(registry, root_dir)
        public = public_accounts(base_dir=root_dir)
        return next(
            (item for item in public['accounts'] if item['id'] == clean_id),
            {'id': clean_id, 'alias': meta['alias'], 'label': meta['label']},
        )


def has_saved_credentials(username: str | None = None, account_id: str | None = None, *,
                          base_dir: Path | str | None = None) -> bool:
    """Check whether saved encrypted credentials exist for the target or active account."""
    with _LOCK:
        root_dir = base_data_dir(base_dir)
        if not accounts_file(root_dir).exists():
            return False
        registry = _read_registry(root_dir)
        target_id = None
        if account_id:
            clean = _safe_dir_name(account_id)
            if clean in registry['accounts']:
                target_id = clean
        elif username:
            norm_u = _normalize_username(username).lower()
            target_id = next(
                (aid for aid, m in registry['accounts'].items() if _normalize_username(m.get('username')).lower() == norm_u),
                None,
            )
        else:
            target_id = registry.get('active_id')
        if not target_id or target_id not in registry['accounts']:
            return False
        acct_dir = _validated_account_dir(registry['accounts'][target_id]['dir_name'], root_dir)
        return (acct_dir / 'login_credentials.bin').is_file()


def switch_account(target_id: str, *, base_dir: Path | str | None = None) -> dict:
    """Switch active account to an existing saved profile under lock."""
    with _LOCK:
        root_dir = base_data_dir(base_dir)
        registry = _read_registry(root_dir)
        clean_id = _safe_dir_name(target_id)
        if clean_id not in registry['accounts']:
            raise ValueError('找不到指定的账号记录')
        already_active = registry.get('active_id') == clean_id
        registry['active_id'] = clean_id
        _write_registry(registry, root_dir)
        apply_active_paths(clean_id, base_dir=root_dir, close_old_browser=not already_active)
        return dict(registry['accounts'][clean_id])


def remove_account(target_id: str, *, base_dir: Path | str | None = None) -> dict:
    """Delete an account profile directory cleanly and fall back safely if active."""
    with _LOCK:
        root_dir = base_data_dir(base_dir)
        registry = _read_registry(root_dir)
        clean_id = _safe_dir_name(target_id)
        if clean_id not in registry['accounts']:
            raise ValueError('找不到指定的账号记录')
        meta = registry['accounts'].pop(clean_id)
        was_active = registry.get('active_id') == clean_id
        acct_dir = _validated_account_dir(meta['dir_name'], root_dir)
        if was_active:
            old_profile = acct_dir / 'browser_data'
            _close_cdp_browser_if_profile(old_profile)
        _drop_empty_dir(acct_dir, root_dir)
        if acct_dir.exists():
            # Clean all sensitive files if browser locked an unrelated cache file.
            for fname in LEGACY_FILES:
                (acct_dir / fname).unlink(missing_ok=True)
        if not registry['accounts']:
            now = shared.iso_timestamp()
            registry['accounts']['default'] = {
                'id': 'default', 'dir_name': 'default', 'username': '', 'uid': '', 'alias': '',
                'label': '默认账号', 'school_name': '', 'fid': '', 'mode': 'unknown',
                'last_sync_at': '', 'updated_at': now,
            }
        if was_active or registry.get('active_id') not in registry['accounts']:
            next_id = next(iter(registry['accounts']))
            registry['active_id'] = next_id
            _write_registry(registry, root_dir)
            apply_active_paths(next_id, base_dir=root_dir, close_old_browser=False)
        else:
            _write_registry(registry, root_dir)
        return {'removed_id': clean_id, 'active_id': registry['active_id'], 'switched': was_active}


def public_accounts(*, base_dir: Path | str | None = None) -> dict:
    """Return UI-safe metadata for all saved accounts."""
    with _LOCK:
        root_dir = base_data_dir(base_dir)
        if not _ISOLATED and not accounts_file(root_dir).exists():
            return {'active_id': None, 'accounts': []}
        registry = _read_registry(root_dir)
        active_id = registry.get('active_id')
        items = []
        for aid, meta in registry['accounts'].items():
            try:
                acct_dir = _validated_account_dir(meta['dir_name'], root_dir)
            except ValueError:
                continue
            state_data = shared.read_json_file(acct_dir / 'state.json', {}, corrupted='fallback')
            items.append({
                'id': aid,
                'username': meta.get('username') or '',
                'alias': meta.get('alias') or '',
                'label': _effective_label(meta, fallback='默认账号' if aid == 'default' else '当前账号'),
                'school_name': meta.get('school_name') or '',
                'fid': meta.get('fid') or '',
                'mode': meta.get('mode') or 'unknown',
                'has_uid': bool(meta.get('uid')),
                'saved_credentials': (acct_dir / 'login_credentials.bin').is_file(),
                'has_session': _has_chaoxing_session_cookies(acct_dir / 'browser_session.json'),
                'last_sync_at': meta.get('last_sync_at') or (state_data.get('last_success') if isinstance(state_data, dict) else '') or '',
                'active': aid == active_id,
            })
        return {'active_id': active_id, 'accounts': items}


def archive_quarantined_record(key: str, record: dict, draft: dict | None = None,
                               *, base_dir: Path | str | None = None) -> None:
    """Keep a recoverable copy of a removed cross-account assignment in account dir."""
    with _LOCK:
        engine = sys.modules.get('homework_engine')
        acct_dir = Path(engine.DATA_DIR) if engine is not None else base_data_dir(base_dir)
        q_file = acct_dir / 'quarantined_legacy.json'
        payload = shared.read_json_file(q_file, {'version': 1, 'records': {}}, corrupted='fallback')
        if not isinstance(payload, dict) or not isinstance(payload.get('records'), dict):
            payload = {'version': 1, 'records': {}}
        payload['records'][key] = {
            'quarantined_at': shared.iso_timestamp(),
            'record': record,
            'draft': draft or {},
        }
        shared.write_json_file(q_file, payload)
