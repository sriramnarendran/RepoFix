# PyInstaller spec — run: ./scripts/build_binary.sh
# Build in a fresh venv (./scripts/build_binary.sh) so global torch/ml packages are not bundled.
# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"

datas = []
binaries = []
hiddenimports = []

# Do NOT collect_all("pydantic") — it drags huge optional stacks from site-packages.
for pkg in ("typer", "rich", "httpx", "git"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

hiddenimports += [
    "pydantic",
    "pydantic_core",
    "pydantic.deprecated",
    "google.genai",
    "google.genai.types",
    "yaml",
    "toml",
    "psutil",
    "certifi",
    "sqlite3",
    "repofix",
]

# Hard block common accidental imports from a polluted build environment
_ml_junk = (
    "torch",
    "torchvision",
    "torchaudio",
    "tensorflow",
    "transformers",
    "sklearn",
    "scipy",
    "matplotlib",
    "pandas",
    "tensorboard",
    "triton",
    "nvidia",
)

a = Analysis(
    [str(SRC / "repofix" / "cli.py")],
    pathex=[str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=list(_ml_junk),
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="repofix",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
