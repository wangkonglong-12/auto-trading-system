"""PyInstaller 固定入口；工作进程由应用分派，不执行任意模块。"""

from multiprocessing import freeze_support

if __name__ == "__main__":
    freeze_support()
    from auto_trading_system.apps.desktop.__main__ import main

    raise SystemExit(main())
