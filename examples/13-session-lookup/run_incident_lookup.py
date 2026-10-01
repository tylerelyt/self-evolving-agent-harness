#!/usr/bin/env python3
"""旧入口兼容：需显式 --live；旧回放当前不可用。"""
from demo import main
if __name__ == '__main__':
    raise SystemExit(main())
