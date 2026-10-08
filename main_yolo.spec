import sys
import os
sys.setrecursionlimit(10000)
from PyInstaller.utils.hooks import collect_submodules
# -*- mode: python ; coding: utf-8 -*-

# 自动获取当前虚拟环境的 site-packages 路径
sp_path = [p for p in sys.path if 'site-packages' in p and '.venv' in p][0]

# 强制将 cupy 和 cupy_backends 整个文件夹打包进去
extra_datas = []
for pkg in ['cupy', 'cupy_backends']:
    pkg_path = os.path.join(sp_path, pkg)
    if os.path.exists(pkg_path):
        extra_datas.append((pkg_path, pkg))

import glob
# 1. 尝试找系统默认安装路径下的海康 Win64 DLL
mv_dll_dir = r'C:\Program Files (x86)\Common Files\MVS\Runtime\Win64_x64'
mv_dlls = glob.glob(os.path.join(mv_dll_dir, '*.dll'))

# 2. 如果你的 DLL 不在默认路径，而是在你项目的 lib 文件夹里，用下面这行代替
# mv_dlls = glob.glob('lib/**/*.dll', recursive=True)

extra_binaries = [(dll, '.') for dll in mv_dlls]

# 打包 config.yaml：settings.py 现在是 fail-fast 的（找不到配置直接报错），
# 且优先在 exe 同级目录 / _MEIPASS 下查找，必须随包分发。
if os.path.exists('config.yaml'):
    extra_datas.append(('config.yaml', '.'))


a = Analysis(
    ['main_yolo.py'],
    pathex=['lib/MvImport'],  # <-- 新增：告诉 PyInstaller 去这里找海康模块
    binaries=extra_binaries,
    datas=extra_datas,
    hiddenimports=[
        *collect_submodules('fastrlock'),  # <-- 新增这一行收集 fastrlock
        'CameraParams_const',
        'CameraParams_header',
        'PixelType_header',
        'PixelType_const',
        'MvErrorDefine_const',
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