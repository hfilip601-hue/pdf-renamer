# -*- mode: python ; coding: utf-8 -*-
# Linux PyInstaller spec for rename_pdfs_varsym
# Bundles mdbtools executables and libraries for a fully standalone executable

from PyInstaller.utils.hooks import collect_submodules
import os
import sys

# Hidden imports for the application
hiddenimports = [
    'pandas',
    'pypdf',
    'tkinter',
    'tkinter.ttk',
    'tkinter.filedialog',
    'tkinter.messagebox',
    'tkinter.scrolledtext',
]
hiddenimports += collect_submodules('pandas')

# Path to mdbtools Linux binaries
# Expected structure (from package manager):
# mdbtools/
#   bin/
#     mdb-export, mdb-tables, mdb-schema
#   lib/
#     *.so (required shared libraries)
SPEC_DIR = os.path.dirname(os.path.abspath(sys.argv[0])) if '__file__' not in globals() else os.path.dirname(os.path.abspath(__file__))
MDBTOOLS_DIR = os.path.join(SPEC_DIR, 'mdbtools')

binaries = []
datas = []

if os.path.exists(MDBTOOLS_DIR):
    # Add mdbtools executables from bin/ to 'mdbtools' (root of bundle)
    bin_dir = os.path.join(MDBTOOLS_DIR, 'bin')
    if os.path.exists(bin_dir):
        for exe in ['mdb-export', 'mdb-tables', 'mdb-schema']:
            exe_path = os.path.join(bin_dir, exe)
            if os.path.exists(exe_path):
                binaries.append((exe_path, 'mdbtools'))
                print(f"  Adding executable: {exe}")

    # Add mdbtools shared libraries from lib/ to 'mdbtools' (same dir as executables)
    lib_dir = os.path.join(MDBTOOLS_DIR, 'lib')
    if os.path.exists(lib_dir):
        for so in os.listdir(lib_dir):
            if so.endswith('.so') or '.so.' in so:
                so_path = os.path.join(lib_dir, so)
                binaries.append((so_path, 'mdbtools'))
                print(f"  Adding library from lib/: {so}")

    # Add mdbtools libraries from root (some installations put them here)
    for so in os.listdir(MDBTOOLS_DIR):
        if so.endswith('.so') or '.so.' in so:
            so_path = os.path.join(MDBTOOLS_DIR, so)
            binaries.append((so_path, 'mdbtools'))
            print(f"  Adding library from root: {so}")

    # Also add entire mdbtools tree as datas for fallback access via sys._MEIPASS
    datas.append((MDBTOOLS_DIR, 'mdbtools'))
    print(f"Bundling mdbtools from: {MDBTOOLS_DIR}")
else:
    # Fallback: try system paths
    for exe in ['mdb-export', 'mdb-tables', 'mdb-schema']:
        system_path = shutil.which(exe) if 'shutil' in sys.modules else None
        if system_path:
            binaries.append((system_path, 'mdbtools'))
            print(f"  Adding system executable: {exe}")
    print(f"WARNING: mdbtools directory not found at {MDBTOOLS_DIR}")
    print("Using system mdbtools if available.")

import shutil

a = Analysis(
    ['rename_pdfs_varsym.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'scipy', 'PIL', 'cv2', 'torch',
        'tensorflow', 'jupyter', 'notebook', 'IPython',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='rename_pdfs_varsym',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
