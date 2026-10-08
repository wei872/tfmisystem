import sys
import os
sys.setrecursionlimit(10000)
from PyInstaller.utils.hooks import collect_submodules
# -*- mode: python ; coding: utf-8 -*-

# 自动获取当前虚拟环境的 site-packages 路径
sp_path = [p for p in sys.path if 'site-packages' in p and '.venv' in p][0]

# 把 lib/ 挂到 sys.path，下面的 collect_submodules('gxipy') 才找得到大恒的包
_project_root = os.path.dirname(os.path.abspath(SPEC)) if 'SPEC' in dir() else os.getcwd()
_lib_dir = os.path.join(_project_root, 'lib')
if _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

# 强制将 cupy 和 cupy_backends 整个文件夹打包进去
extra_datas = []
for pkg in ['cupy', 'cupy_backends']:
    pkg_path = os.path.join(sp_path, pkg)
    if os.path.exists(pkg_path):
        extra_datas.append((pkg_path, pkg))

# ------------------------------------------------------------------
# 大恒 Galaxy SDK 的原生 DLL **不**打包进 exe。
# gxipy/gxwrapper.py 在运行时会读取环境变量 GALAXY_GENICAM_ROOT 并用
# os.add_dll_directory() 把 GxIAPI.dll / DxImageProc.dll 所在目录挂进搜索路径，
# 所以现场机器必须安装大恒 Galaxy 相机驱动（含 GenICam 运行时）。
# 这里只需要把纯 Python 的 gxipy 包收进去（见下面的 pathex / hiddenimports）。
# ------------------------------------------------------------------
extra_binaries = []

# 打包 config.yaml：settings.py 现在是 fail-fast 的（找不到配置直接报错），
# 且优先在 exe 同级目录 / _MEIPASS 下查找，必须随包分发。
if os.path.exists('config.yaml'):
    extra_datas.append(('config.yaml', '.'))


a = Analysis(
    ['main_yolo.py'],
    # gxipy 放在 lib/ 下，内部用的是绝对导入(from gxipy.xxx import *)，
    # 所以把 lib/ 作为搜索根，让 PyInstaller 能把它当顶层包分析
    pathex=['lib'],
    binaries=extra_binaries,
    datas=extra_datas,
    hiddenimports=[
        *collect_submodules('fastrlock'),  # <-- 新增这一行收集 fastrlock
        # 大恒 gxipy：这些模块之间靠 `from gxipy.xxx import *` 互相引用，
        # PyInstaller 的静态分析经常漏掉，显式全量收集最稳妥
        *collect_submodules('gxipy'),
    ],
    hookspath=[],
    excludes=[
        # ===== 解决隔离环境崩溃的核心 =====
        'onnx.reference',
        'onnx.reference_implementation',
        'onnxscript',
        'onnx_graphsurgeon',
        'onnxconverter_common',
        'onnxslim',
        'polygraphy',
        'pulp',
        'functorch',
        'IPython',
        'ipykernel',
        'ipywidgets',
        'jupyter',
        'jupyterlab',
        'notebook',
        'nbformat',
        'tkinter',
        'pygments',
        'networkx',
        'modelopt',
        'nvidia_modelopt',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='main_yolo',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    a.zipfiles,
    name='main_yolo'
)