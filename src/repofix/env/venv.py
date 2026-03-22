"""Virtual environment management for user repos.

For Python repos, a .venv is created inside the repo directory so that
all dependency installation and execution is fully isolated from the
user's system Python.

For Node/Go/Rust/etc., the runtime's native isolation already applies
(node_modules, Go module cache, Cargo target/), so no venv is needed.
"""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path
from repofix.detection.stack import StackInfo
from repofix.output import display
_VENV_DIR = '.venv'

def needs_venv(stack: StackInfo) -> bool:
    """Return True for runtimes where venv-style isolation is needed."""
    return stack.runtime.lower() in ('python', 'pip')

def setup(repo_path: Path, stack: StackInfo, venv_dir_name: str=_VENV_DIR) -> dict[str, str]:
    """
    Ensure an isolated environment exists for the repo.
    Returns extra env-var overrides to inject when running commands.

    For Python: creates/reuses the venv at *venv_dir_name* inside the repo and
    returns PATH + VIRTUAL_ENV overrides.  Pass a branch-specific name such as
    '.venv-feature-x' to keep each branch's dependencies fully isolated.

    For Node/Go/Rust/Ruby/etc.: returns lightweight env tweaks only.
    """
    runtime = stack.runtime.lower()
    if runtime in ('python', 'pip'):
        return _setup_python_venv(repo_path, venv_dir_name)
    if runtime in ('node', 'npm'):
        return _setup_node_env(repo_path)
    if runtime == 'ruby':
        return _setup_ruby_env(repo_path)
    return {}

def venv_path(repo_path: Path, venv_dir_name: str=_VENV_DIR) -> Path:
    return repo_path / venv_dir_name

def venv_bin(repo_path: Path, venv_dir_name: str=_VENV_DIR) -> Path:
    return venv_path(repo_path, venv_dir_name) / 'bin'

def venv_python(repo_path: Path, venv_dir_name: str=_VENV_DIR) -> Path:
    return venv_bin(repo_path, venv_dir_name) / 'python'

def venv_pip(repo_path: Path, venv_dir_name: str=_VENV_DIR) -> Path:
    return venv_bin(repo_path, venv_dir_name) / 'pip'

def venv_exists(repo_path: Path, venv_dir_name: str=_VENV_DIR) -> bool:
    return venv_python(repo_path, venv_dir_name).exists()

def _setup_python_venv(repo_path: Path, venv_dir_name: str=_VENV_DIR) -> dict[str, str]:
    venv_dir = venv_path(repo_path, venv_dir_name)
    if venv_exists(repo_path, venv_dir_name):
        display.info(f'Reusing existing venv at [bold]{venv_dir.relative_to(repo_path)}[/bold]')
    else:
        display.step(f'Creating Python venv at [bold]{venv_dir_name}[/bold]…')
        try:
            subprocess.run([sys.executable, '-m', 'venv', str(venv_dir)], check=True, capture_output=True, text=True)
            display.success('Virtual environment created')
        except subprocess.CalledProcessError as exc:
            display.warning(f'Could not create venv: {exc.stderr.strip()} — falling back to system Python')
            return {}
    return _venv_activation_env(repo_path, venv_dir_name)

def _venv_activation_env(repo_path: Path, venv_dir_name: str=_VENV_DIR) -> dict[str, str]:
    """
    Return env vars that replicate `source .venv/bin/activate`.

    Prepending the venv's bin/ to PATH means every subsequent shell command
    (pip, python, uvicorn, gunicorn, flask, django-admin …) automatically
    resolves to the venv's copy — no command string changes required.
    """
    bin_dir = str(venv_bin(repo_path, venv_dir_name))
    current_path = os.environ.get('PATH', '')
    return {'VIRTUAL_ENV': str(venv_path(repo_path, venv_dir_name)), 'PATH': f'{bin_dir}{os.pathsep}{current_path}', 'PIP_NO_USER_INSTALL': '1', 'PYTHONNOUSERSITE': '1'}

def _setup_node_env(repo_path: Path) -> dict[str, str]:
    """
    node_modules/.bin is already on PATH when scripts are run via npm/yarn/pnpm.
    npm_config_prefix is the repo so ``npm install -g`` lands in ./bin (not
    /usr/local), avoiding EACCES on typical Linux setups.
    """
    prefix_bin = str(repo_path / 'bin')
    local_bin = str(repo_path / 'node_modules' / '.bin')
    current_path = os.environ.get('PATH', '')
    return {'npm_config_prefix': str(repo_path), 'PATH': f'{prefix_bin}{os.pathsep}{local_bin}{os.pathsep}{current_path}'}

def machine_npm_global_prefix_writable(repo_path: Path) -> bool:
    """
    True if npm's default global prefix (no ``npm_config_prefix`` override) is
    writable by the current user, so ``npm install -g`` can succeed without sudo.
    """
    import subprocess
    try:
        proc = subprocess.run(['npm', 'config', 'get', 'prefix'], cwd=str(repo_path), capture_output=True, text=True, timeout=60, env={**os.environ})
        if proc.returncode != 0:
            return False
        raw = (proc.stdout or '').strip()
        if not raw:
            return False
        prefix = Path(raw).expanduser().resolve()
        lib_modules = prefix / 'lib' / 'node_modules'
        if lib_modules.is_dir():
            return os.access(lib_modules, os.W_OK)
        return os.access(prefix, os.W_OK)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False

def _setup_ruby_env(repo_path: Path) -> dict[str, str]:
    """
    Direct Bundler to install gems into vendor/bundle inside the repo
    instead of the system gem directory.
    """
    vendor_dir = repo_path / 'vendor' / 'bundle'
    return {'BUNDLE_PATH': str(vendor_dir), 'BUNDLE_BIN': str(repo_path / 'bin'), 'GEM_HOME': str(vendor_dir)}
