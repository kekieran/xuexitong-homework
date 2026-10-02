"""重构等价性验证脚本（独立、可重复、只读为主）。

用途
----
在任何“提升可读性/消除重复/清理死代码”的重构前后各运行一次，用来证明：
1. 既有 unittest 用例（tests 下的四个模块）全部通过；
2. Python 源码与前端 JS 语法均可编译/解析；
3. 前端未发生注入式损坏（未出现 UTF-8 被按 GBK 解读产生的乱码字符）；
4. 真实 HTTP 服务的接口契约未变（用真实 Handler + ephemeral 端口，逐条比对状态码、
   Content-Type 与 JSON 顶层键）。

安全约束
--------
本脚本**不会**调用 `homework_app.main()`：main() 会启动 reminder/auth/maintenance
三个后台线程，其中 maintenance_loop 会在真实 data/ 目录上执行历史清理（可能删除
文件）、auth_watch_loop 会尝试联网登录。因此这里只实例化 Handler 并直接绑定端口，
不触发任何写数据或联网的循环。

用法
----
    <venv>/Scripts/python.exe scripts/verify_all.py
    <venv>/Scripts/python.exe scripts/verify_all.py --quick    # 跳过 HTTP 契约检查
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import shutil
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
UI = ROOT / 'ui'
TESTS = ('test_ai', 'test_auth', 'test_retention', 'test_workspace', 'test_shared')

# UTF-8 文本被按 GBK 解读后常见的典型乱码片段。出现即说明编码被破坏，
# 而这正是前端最容易在批量改写中受的伤（中文文案占 ui.js 相当比例）。
MOJIBAKE_MARKERS = ('锛', '鈥', '鏈', '璁剧疆', '宸插', '閰嶇疆', '淇濆瓨', '獚', '锟')

results: list[tuple[bool, str, str]] = []


def record(ok: bool, name: str, detail: str = '') -> bool:
    results.append((ok, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ''))
    return ok


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                          encoding='utf-8', errors='replace', **kwargs)


# --------------------------------------------------------------------------- #
# 1. 既有测试
# --------------------------------------------------------------------------- #
def check_unittests() -> None:
    existing = [name for name in TESTS if (ROOT / 'tests' / f'{name}.py').exists()]
    for name in existing:
        proc = run([sys.executable, '-m', 'unittest', f'tests.{name}'])
        tail = (proc.stderr or proc.stdout).strip().splitlines()
        summary = next((line for line in reversed(tail) if line.startswith('Ran ')), '')
        ok = proc.returncode == 0
        record(ok, f'unittest {name}', summary or f'exit={proc.returncode}')


# --------------------------------------------------------------------------- #
# 2. 语法
# --------------------------------------------------------------------------- #
def check_python_syntax() -> None:
    targets = sorted(
        path for folder in (SRC, ROOT / 'scripts', ROOT / 'tests')
        for path in folder.glob('*.py')
    )
    proc = run([sys.executable, '-m', 'compileall', '-q', *[str(p) for p in targets]])
    record(proc.returncode == 0, 'python compileall', f'{len(targets)} files, exit={proc.returncode}')


def check_js_syntax() -> None:
    node = shutil.which('node')
    if not node:
        record(True, 'js syntax', 'SKIPPED: node 不在 PATH 中')
        return
    for name in ('ui.js', 'ai.js', 'workspace.js'):
        path = UI / name
        if not path.exists():
            continue
        proc = run([node, '--check', str(path)])
        record(proc.returncode == 0, f'node --check {name}',
               (proc.stderr or '').strip().splitlines()[0] if proc.returncode else '')


# --------------------------------------------------------------------------- #
# 3. 前端静态等价性
# --------------------------------------------------------------------------- #
DECLARATION = re.compile(
    r'^\s*(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)', re.MULTILINE)


def declared_symbols(text: str) -> set[str]:
    return set(DECLARATION.findall(text))


def check_frontend_static() -> None:
    for name in ('ui.html', 'ui.js', 'ai.js', 'workspace.js', 'ui.css'):
        path = UI / name
        if not path.exists():
            record(False, f'frontend file {name}', '缺失')
            continue
        text = path.read_text(encoding='utf-8')  # 编码错误会在此抛出
        hits = [marker for marker in MOJIBAKE_MARKERS if marker in text]
        record(not hits, f'utf-8 integrity {name}', f'发现乱码片段 {hits}' if hits else 'ok')

    html = (UI / 'ui.html').read_text(encoding='utf-8')
    loaded = set(re.findall(r'<script[^>]+src="/([\w.]+)"', html))
    for name in ('ui.js', 'ai.js', 'workspace.js'):
        record(name in loaded, f'ui.html loads {name}', f'已加载: {sorted(loaded)}')

    exports = {'ai.js': 'window.HomeworkAI', 'workspace.js': 'window.HomeworkWorkspace'}
    for name, symbol in exports.items():
        text = (UI / name).read_text(encoding='utf-8')
        record(symbol in text, f'{name} 仍导出 {symbol}')

    # 全局顶层符号数量快照：用于人工比对重构前后是否发生意外改名/丢失。
    for name in ('ui.js', 'ai.js', 'workspace.js'):
        text = (UI / name).read_text(encoding='utf-8')
        record(True, f'{name} 顶层符号数', str(len(declared_symbols(text))))


# --------------------------------------------------------------------------- #
# 4. 真实 HTTP 接口契约（只读端点）
# --------------------------------------------------------------------------- #
def check_http_contract() -> None:
    sys.path.insert(0, str(SRC))
    try:
        import homework_app as app  # noqa: WPS433 (deliberate late import)
    except Exception as exc:  # pragma: no cover - 环境问题需要显式暴露
        record(False, 'import homework_app', repr(exc))
        return

    server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.2}, daemon=True).start()
    port = server.server_port
    token = app.TOKEN

    # 说明：这里刻意使用 http.client 而不是 urllib。urllib 会遵循系统代理设置，
    # 在本机（存在 127.0.0.1 代理）会把查询串弄丢，导致引导跳转误判为 403；
    # http.client 与浏览器一样直连，能真实反映 Handler 的行为。
    def fetch(path: str, cookie: str | None = None):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        headers = {'Host': f'127.0.0.1:{port}'}
        if cookie:
            headers['Cookie'] = cookie
        try:
            connection.request('GET', path, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def json_keys(body: bytes) -> set[str]:
        try:
            payload = json.loads(body.decode('utf-8'))
        except Exception:
            return set()
        return set(payload) if isinstance(payload, dict) else set()

    try:
        # 对照组：无 Cookie 必须被拒（安全边界不能被重构削弱）。
        status, _, _ = fetch('/api/state')
        record(status == 403, 'GET /api/state 无令牌被拒', f'status={status}')

        # 引导跳转应下发会话 Cookie。
        status, headers, _ = fetch(f'/?token={token}')
        set_cookie = headers.get('Set-Cookie', '')
        record(status == 303 and app.COOKIE in set_cookie, 'GET /?token 引导跳转',
               f'status={status}')
        cookie = set_cookie.split(';', 1)[0]

        # 只读端点：状态码 + JSON 顶层键契约。
        expectations = {
            '/': {'status': 200, 'type': 'text/html'},
            '/ui.js': {'status': 200, 'type': 'text/javascript'},
            '/workspace.js': {'status': 200, 'type': 'text/javascript'},
            '/ai.js': {'status': 200, 'type': 'text/javascript'},
            '/ui.css': {'status': 200, 'type': 'text/css'},
            '/api/state': {'status': 200, 'keys': {'dashboard', 'job'}},
            '/api/dashboard': {'status': 200, 'keys': {'version', 'counts', 'keys', 'questions'}},
            '/api/job': {'status': 200, 'keys': {'busy', 'action', 'message'}},
            '/api/ai/settings': {'status': 200},
        }
        for path, want in expectations.items():
            status, headers, body = fetch(path, cookie)
            ok = status == want['status']
            detail = f'status={status}'
            if 'type' in want:
                actual = headers.get('Content-Type', '')
                ok = ok and actual.startswith(want['type'])
                detail += f' type={actual}'
            if 'keys' in want:
                keys = json_keys(body)
                missing = want['keys'] - keys
                ok = ok and not missing
                detail += f' keys={sorted(keys)}'
                if missing:
                    detail += f' 缺少={sorted(missing)}'
            record(ok, f'GET {path}', detail)
    finally:
        server.shutdown()
        server.server_close()


def python_signatures() -> dict[str, list[str]]:
    """按 ast 导出每个模块的函数/类签名；不导入模块，因此无副作用。"""
    import ast
    snapshot: dict[str, list[str]] = {}
    for path in sorted(SRC.glob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        entries = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                arguments = ast.unparse(node.args)
                returns = f' -> {ast.unparse(node.returns)}' if node.returns else ''
                decorators = ','.join(ast.unparse(d) for d in node.decorator_list)
                prefix = f'[{decorators}] ' if decorators else ''
                entries.append(f'{prefix}def {node.name}({arguments}){returns}')
            elif isinstance(node, ast.ClassDef):
                entries.append(f'class {node.name}')
        snapshot[path.name] = sorted(entries)
    return snapshot


def frontend_symbols() -> dict[str, list[str]]:
    """按文本导出前端顶层声明名（只证名字面量集合，不证语义）。"""
    snapshot = {}
    for name in ('ui.js', 'ai.js', 'workspace.js'):
        path = UI / name
        if path.exists():
            snapshot[name] = sorted(declared_symbols(path.read_text(encoding='utf-8')))
    return snapshot


def snapshot_command(args) -> int:
    import json as json_module
    current = {'python': python_signatures(), 'frontend': frontend_symbols()}
    if args.snapshot:
        Path(args.snapshot).write_text(
            json_module.dumps(current, ensure_ascii=False, indent=1, sort_keys=True), encoding='utf-8')
        print(f'快照已写入 {args.snapshot}')
        for name, entries in current['python'].items():
            print(f'  {name}: {len(entries)} 个顶层定义')
        for name, entries in current['frontend'].items():
            print(f'  {name}: {len(entries)} 个顶层符号')
        return 0

    before = json_module.loads(Path(args.compare).read_text(encoding='utf-8-sig'))
    changed = 0
    for section in ('python', 'frontend'):
        old, new = before.get(section, {}), current[section]
        for name in sorted(set(old) | set(new)):
            gone = sorted(set(old.get(name, [])) - set(new.get(name, [])))
            added = sorted(set(new.get(name, [])) - set(old.get(name, [])))
            if gone or added:
                changed += 1
                print(f'--- {section}:{name}')
                for item in gone:
                    print(f'    消失: {item}')
                for item in added:
                    print(f'    新增: {item}')
    if not changed:
        print('结构快照无差异：顶层定义与前端符号集合完全一致。')
    else:
        print(f'\n共 {changed} 个文件存在结构差异（请逐条人工确认是否为预期的等价重构）。')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='重构等价性验证')
    parser.add_argument('--quick', action='store_true', help='跳过 HTTP 契约检查')
    parser.add_argument('--snapshot', metavar='FILE',
                        help='把 Python 函数/类签名与前端顶层符号写成 JSON 快照')
    parser.add_argument('--compare', metavar='FILE',
                        help='与快照比对：报告消失/新增的签名与前端符号（只读，不失败）')
    args = parser.parse_args()

    if args.snapshot or args.compare:
        return snapshot_command(args)

    print(f'== verify_all @ {ROOT} ==')
    check_python_syntax()
    check_js_syntax()
    check_frontend_static()
    check_unittests()
    if not args.quick:
        check_http_contract()

    failed = [name for ok, name, _ in results if not ok]
    print('\n== 汇总 ==')
    print(f'通过 {len(results) - len(failed)} / {len(results)}')
    if failed:
        print('失败项：' + '; '.join(failed))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
