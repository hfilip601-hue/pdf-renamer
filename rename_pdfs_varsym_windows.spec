# -*- mode: python ; coding: utf-8 -*-
# Windows PyInstaller spec for rename_pdfs_varsym
# This spec bundles mdbtools executables (mdb-export.exe, mdb-tables.exe, mdb-schema.exe)
# and their required DLLs for a fully standalone .exe

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

# Path to mdbtools Windows binaries (DLLs and executables)
# Expected structure (from Chocolatey or manual download):
# mdbtools/
#   bin/
#     mdb-export.exe
#     mdb-tables.exe
#     mdb-schema.exe
#   lib/
#     *.dll (required DLLs)
#   *.dll (some DLLs may also be in root)
# Get the directory containing this spec file
SPEC_DIR = os.path.dirname(os.path.abspath(sys.argv[0])) if '__file__' not in globals() else os.path.dirname(os.path.abspath(__file__))
MDBTOOLS_DIR = os.path.join(SPEC_DIR, 'mdbtools')

binaries = []
datas = []

if os.path.exists(MDBTOOLS_DIR):
    # Add mdbtools executables from bin/
    bin_dir = os.path.join(MDBTOOLS_DIR, 'bin')
    if os.path.exists(bin_dir):
        for exe in ['mdb-export.exe', 'mdb-tables.exe', 'mdb-schema.exe']:
            exe_path = os.path.join(bin_dir, exe)
            if os.path.exists(exe_path):
                binaries.append((exe_path, 'mdbtools'))
                print(f"  Adding executable: {exe}")

    # Add mdbtools DLLs from lib/
    lib_dir = os.path.join(MDBTOOLS_DIR, 'lib')
    if os.path.exists(lib_dir):
        for dll in os.listdir(lib_dir):
            if dll.endswith('.dll'):
                dll_path = os.path.join(lib_dir, dll)
                binaries.append((dll_path, 'mdbtools'))
                print(f"  Adding DLL from lib/: {dll}")

    # Add mdbtools DLLs from root (some installations put them here)
    for dll in os.listdir(MDBTOOLS_DIR):
        if dll.endswith('.dll'):
            dll_path = os.path.join(MDBTOOLS_DIR, dll)
            binaries.append((dll_path, 'mdbtools'))
            print(f"  Adding DLL from root: {dll}")

    # Also add as datas for runtime access via sys._MEIPASS
    datas.append((MDBTOOLS_DIR, 'mdbtools'))
    print(f"Bundling mdbtools from: {MDBTOOLS_DIR}")
else:
    print(f"WARNING: mdbtools directory not found at {MDBTOOLS_DIR}")
    print("The executable will require mdbtools to be installed on the target system.")

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
    target_arch='x86_64',
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico' if os.path.exists('icon.ico') else None,
)