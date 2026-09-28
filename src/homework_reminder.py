"""Compatibility entry point for the existing Windows launchers."""
import argparse
import sys
import homework_engine as backend

def main():
    parser = argparse.ArgumentParser(description='学习通作业检查与提醒')
    parser.add_argument('--login', action='store_true')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--visible', action='store_true')
    args = parser.parse_args()
    if args.login:
        return backend.login_mode()
    try:
        result = backend.check_once(headless=not args.visible)
        if result == 0:
            pending = backend.active_pending(backend.load_state())
            if pending:
                backend.notify('学习通未提交作业提醒', backend.render_summary(pending))
        else:
            backend.notify('学习通作业检查', '部分信息暂未更新，请打开助手检查登录状态。')
        return result
    except Exception as exc:
        backend.notify('学习通作业检查', backend.safe_error(exc))
        return 1

if __name__ == '__main__':
    sys.exit(main())
