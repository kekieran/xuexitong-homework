"""Prepare a project-local Windows environment; never reuse a moved venv."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
MARKER = '.homework-environment.json'
MIN_PYTHON = (3, 10)
MAX_PYTHON = (3, 14)


def requirements_hash(root: Path) -> str:
    return hashlib.sha256((root / 'requirements.txt').read_bytes()).hexdigest()


def bundle_hash(root: Path) -> str:
    wheels = root / 'vendor' / 'wheels'
    files = sorted(wheels.glob('*.whl'))
    if not files:
        raise RuntimeError('Offline dependency bundle is missing.')
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode('utf-8'))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def environment_ready(root: Path) -> bool:
    environment = root / '.venv'
    interpreter = environment / 'Scripts' / 'python.exe'
    try:
        marker = json.loads((environment / MARKER).read_text(encoding='utf-8'))
        if (marker.get('project') != str(root.resolve())
                or marker.get('requirements') != requirements_hash(root)
                or marker.get('bundle') != bundle_hash(root)):
            return False
        if not Path(marker['base_executable']).is_file():
            return False
        code = (
            'import sys,json,importlib.metadata; import playwright.sync_api; '
            'print(json.dumps({"version":list(sys.version_info[:2]),'
            '"prefix":sys.prefix,"base":sys.base_prefix,'
            '"playwright":importlib.metadata.version("playwright")}))'
        )
        result = subprocess.run([str(interpreter), '-I', '-c', code], capture_output=True, text=True, timeout=30, check=True)
        info = json.loads(result.stdout)
        return (MIN_PYTHON <= tuple(info['version']) <= MAX_PYTHON
                and Path(info['prefix']).resolve() == environment.resolve()
                and info['prefix'] != info['base']
                and info['playwright'] == '1.55.0')
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return False


@contextmanager
def setup_lock(root: Path):
    # The OS releases this lock even if the installer is terminated.
    import msvcrt
    with (root / '.environment-setup.lock').open('a+b') as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError('Another installation/startup is preparing this project. Please wait and try again.') from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def sibling_backup(root: Path, label: str) -> Path:
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    return root / f'.venv-{label}-{stamp}-{os.getpid()}'


def move_environment(source: Path, destination: Path, root: Path) -> None:
    # Only this project's direct environment folders may ever be moved.
    if source.is_symlink() or source.resolve().parent != root.resolve() or destination.resolve().parent != root.resolve():
        raise RuntimeError('Environment path is outside this project; refusing to move it.')
    # Recently exited Python/antivirus readers may briefly retain a Windows
    # handle. Retry only sharing/access errors; keep a genuinely busy env intact.
    for attempt in range(5):
        try:
            source.rename(destination)
            return
        except OSError as exc:
            if getattr(exc, 'winerror', None) not in (5, 32, 33):
                raise
            if attempt == 4:
                raise RuntimeError('The Python environment is still in use. Exit the assistant and wait a few seconds, then run this script again. Existing files were not deleted.') from exc
            time.sleep(0.25 * (2 ** attempt))


def remove_environment(path: Path, root: Path, label: str) -> None:
    if (path.is_symlink() or path.resolve().parent != root.resolve()
            or not path.name.startswith(f'.venv-{label}-')):
        raise RuntimeError('Environment cleanup path is outside this project.')
    shutil.rmtree(path)


def prepare_environment(root: Path, base_executable: Path) -> Path:
    environment = root / '.venv'
    interpreter = environment / 'Scripts' / 'python.exe'
    wheel_directory = root / 'vendor' / 'wheels'
    if environment_ready(root):
        return interpreter
    backup = None
    if environment.exists():
        backup = sibling_backup(root, 'backup')
        move_environment(environment, backup, root)
        print(f'Previous environment preserved: {backup.name}', flush=True)
    try:
        print('Creating local Python environment...', flush=True)
        subprocess.run([str(base_executable), '-I', '-m', 'venv', str(environment)], check=True)
        # Use python -m pip: there are no embedded moved-path pip launchers.
        print('Installing bundled dependencies...', flush=True)
        subprocess.run([str(interpreter), '-I', '-m', 'pip', 'install',
                        '--disable-pip-version-check', '--no-index',
                        '--find-links', str(wheel_directory), '-r', str(root / 'requirements.txt')], check=True)
        (environment / MARKER).write_text(json.dumps({
            'project': str(root.resolve()),
            'requirements': requirements_hash(root),
            'bundle': bundle_hash(root),
            'base_executable': str(base_executable.resolve()),
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        if not environment_ready(root):
            raise RuntimeError('The newly installed environment failed validation.')
        if backup is not None:
            remove_environment(backup, root, 'backup')
    except BaseException:
        if environment.exists():
            failed = sibling_backup(root, 'failed')
            move_environment(environment, failed, root)
            remove_environment(failed, root, 'failed')
            print('Incomplete environment removed.', flush=True)
        if backup is not None:
            move_environment(backup, environment, root)
            print('Previous environment restored.', flush=True)
        raise
    return interpreter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('install', 'start', 'check', 'login', 'chrome'))
    args = parser.parse_args()
    if os.name != 'nt':
        print('These one-click scripts support Windows only.', file=sys.stderr)
        return 1
    base = Path(getattr(sys, '_base_executable', sys.executable)).resolve()
    # Entry scripts use the base interpreter, so no process pins the old venv.
    if Path(sys.prefix).resolve() == (ROOT / '.venv').resolve():
        print('Please use a one-click .cmd entry point to prepare this environment.', file=sys.stderr)
        return 1
    try:
        with setup_lock(ROOT):
            interpreter = prepare_environment(ROOT, base)
        if args.mode == 'start':
            subprocess.Popen([str(interpreter.with_name('pythonw.exe')), str(ROOT / 'src' / 'homework_app.py')], cwd=ROOT)
            print('Assistant started.', flush=True)
        elif args.mode in ('check', 'login', 'chrome'):
            target = ([str(ROOT / 'src' / 'homework_runtime.py'), '--chrome'] if args.mode == 'chrome'
                      else [str(ROOT / 'src' / 'homework_reminder.py'), '--login'] if args.mode == 'login'
                      else [str(ROOT / 'src' / 'homework_reminder.py'), '--check', '--visible'])
            return subprocess.call([str(interpreter), *target], cwd=ROOT)
        else:
            print('Environment ready. Browser data and application data stay in this project.', flush=True)
        return 0
    except Exception as exc:
        print(f'Setup failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
