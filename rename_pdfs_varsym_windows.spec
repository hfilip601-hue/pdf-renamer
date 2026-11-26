# -*- mode: python ; coding: utf-8 -*-
# Windows PyInstaller spec for rename_pdfs_varsym
# This spec bundles:
# - mdbtools executables (mdb-export.exe, mdb-tables.exe, mdb-schema.exe) + DLLs
# - Tesseract OCR executable + tessdata language packs (ces, eng, slk)

from PyInstaller.utils.hooks import collect_submodules
import os
import sys

# Hidden imports for the application
hiddenimports = [
    'pandas',
    'pypdf',
    'pytesseract',
    'PIL',
    'PIL.Image',
    'tkinter',
    'tkinter.ttk',
    'tkinter.filedialog',
    'tkinter.messagebox',
    'tkinter.scrolledtext',
]
hiddenimports += collect_submodules('pandas')

# Path to mdbtools Windows binaries (DLLs and executables)
SPEC_DIR = os.path.dirname(os.path.abspath(sys.argv[0])) if '__file__' not in globals() else os.path.dirname(os.path.abspath(__file__))
MDBTOOLS_DIR = os.path.join(SPEC_DIR, 'mdbtools')
TESSERACT_DIR = os.path.join(SPEC_DIR, 'tesseract')

binaries = []
datas = []

# Bundle mdbtools
if os.path.exists(MDBTOOLS_DIR):
    datas.append((MDBTOOLS_DIR, 'mdbtools'))
    print(f"Bundling mdbtools from: {MDBTOOLS_DIR}")
    for root, dirs, files in os.walk(MDBTOOLS_DIR):
        for f in files:
            rel_root = os.path.relpath(root, MDBTOOLS_DIR)
            if rel_root == '.':
                print(f"  mdbtools/{f}")
            else:
                print(f"  mdbtools/{rel_root}/{f}")
else:
    print(f"WARNING: mdbtools directory not found at {MDBTOOLS_DIR}")

# Bundle Tesseract OCR
if os.path.exists(TESSERACT_DIR):
    datas.append((TESSERACT_DIR, 'tesseract'))
    print(f"Bundling Tesseract from: {TESSERACT_DIR}")
    for root, dirs, files in os.walk(TESSERACT_DIR):
        for f in files:
            rel_root = os.path.relpath(root, TESSERACT_DIR)
            if rel_root == '.':
                print(f"  tesseract/{f}")
            else:
                print(f"  tesseract/{rel_root}/{f}")
else:
    print(f"WARNING: Tesseract directory not found at {TESSERACT_DIR}")
    print("Receipt OCR will require Tesseract to be installed on the target system.")

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
    upx=False,
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