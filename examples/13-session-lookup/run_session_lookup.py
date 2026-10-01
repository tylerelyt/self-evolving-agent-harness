#!/usr/bin/env python3
"""旧入口兼容：需显式 --live；无模型检查请用 self_check.py。"""
from demo import main
if __name__ == '__main__':
    raise SystemExit(main())
