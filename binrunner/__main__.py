"""`python -m binrunner` 入口。

按惯例保持极薄：逻辑在 binrunner.cli，此处仅转发，
以免 import 该模块时（如单测）触发命令行解析等副作用。
"""
import sys

from binrunner.cli import main

if __name__ == "__main__":
    sys.exit(main())
