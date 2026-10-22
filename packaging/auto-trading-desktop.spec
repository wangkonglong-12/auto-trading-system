# Windows one-file 构建。只收纳本项目和运行依赖；界面来自本次独立构建目录。
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

project = Path(SPECPATH).parent
sys.path.insert(0, str(project / 'src'))
assets = Path(os.environ['ATS_EXE_ASSETS'])
if not assets.is_absolute() or not (assets / 'node.html').is_file():
    raise ValueError('An explicit verified frontend build is required')

analysis = Analysis(
    [str(project / 'scripts/windows/desktop-entry.py')],
    pathex=[str(project / 'src')],
    binaries=[],
    datas=[(str(assets), 'auto_trading_system/apps/local_console/static')]
        + collect_data_files('tzdata'),
    hiddenimports=collect_submodules('auto_trading_system') + ['win32timezone'],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['pytest', 'mypy', 'ruff', 'tkinter.test'],
    noarchive=False,
)
archive = PYZ(analysis.pure)
exe = EXE(
    archive, analysis.scripts, analysis.binaries, analysis.datas, [],
    name='AutoTradingSystem-0.1.0-dev56',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False,
    version=str(project / 'packaging/windows-version.txt'),
)
