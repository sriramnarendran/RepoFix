"""Process execution with real-time log streaming and capture."""

from __future__ import annotations

import os
import select
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from repofix.output import display

LogLine = tuple[str, str]  # (source, text) where source is "stdout" | "stderr"


@dataclass
class ExecutionResult:
    command: str
    exit_code: int
    stdout_lines: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)
    all_lines: list[LogLine] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0

    @property
    def full_output(self) -> str:
        return "\n".join(line for _, line in self.all_lines)

    @property
    def combined_text(self) -> str:
        return "\n".join(self.stdout_lines + self.stderr_lines)


class ExecutionError(Exception):
    def __init__(self, message: str, result: ExecutionResult):
        super().__init__(message)
        self.result = result


def run_command(
    command: str | list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    stream: bool = True,
    debug: bool = False,
    on_line: Callable[[str, str], None] | None = None,
    timeout: int | None = None,
) -> ExecutionResult:
    """
    Run a shell command, streaming its output in real time.

    Args:
        command:  Shell command string or argv list.
        cwd:      Working directory.
        env:      Extra environment variables merged with current env.
        stream:   Print output to terminal while running.
        debug:    Show extra diagnostic info.
        on_line:  Optional callback(source, line) called for each output line.
        timeout:  Optional timeout in seconds.
    """
    merged_env = {**os.environ}
    if env:
        merged_env.update(env)

    cmd_str = command if isinstance(command, str) else " ".join(command)
    if debug:
        display.muted(f"$ {cmd_str}")

    result = ExecutionResult(command=cmd_str, exit_code=-1)

    try:
        proc = subprocess.Popen(
            command,
            shell=isinstance(command, str),
            cwd=cwd,
            env=merged_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise ExecutionError(f"Command not found: {cmd_str}", result) from exc

    def _read_stream(pipe, source: str) -> None:
        assert pipe is not None
        for raw_line in iter(pipe.readline, ""):
            line = raw_line.rstrip("\n")
            result.all_lines.append((source, line))
            if source == "stdout":
                result.stdout_lines.append(line)
            else:
                result.stderr_lines.append(line)
            if stream:
                display.log_line(line, source)
            if on_line:
                on_line(source, line)
        pipe.close()

    stdout_thread = threading.Thread(target=_read_stream, args=(proc.stdout, "stdout"), daemon=True)
    stderr_thread = threading.Thread(target=_read_stream, args=(proc.stderr, "stderr"), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise ExecutionError(f"Command timed out after {timeout}s: {cmd_str}", result)
    finally:
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

    result.exit_code = proc.returncode
    return result


def run_long_lived(
    command: str | list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    debug: bool = False,
    on_line: Callable[[str, str], None] | None = None,
    log_file: Path | None = None,
) -> "LongLivedProcess":
    """
    Launch a long-lived process (e.g. a dev server) and return a handle
    that can be inspected and terminated later.

    Args:
        log_file: If given, all output is also written (tee'd) to this file.
    """
    merged_env = {**os.environ}
    if env:
        merged_env.update(env)

    cmd_str = command if isinstance(command, str) else " ".join(command)
    if debug:
        display.muted(f"$ {cmd_str} (long-lived)")

    proc = subprocess.Popen(
        command,
        shell=isinstance(command, str),
        cwd=cwd,
        env=merged_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    return LongLivedProcess(proc, cmd_str, on_line=on_line, debug=debug, log_file=log_file)


class LongLivedProcess:
    """Wraps a long-running subprocess with streaming log capture."""

    def __init__(
        self,
        proc: subprocess.Popen,
        command: str,
        on_line: Callable[[str, str], None] | None = None,
        debug: bool = False,
        log_file: Path | None = None,
    ):
        self._proc = proc
        self.command = command
        self._debug = debug
        self._on_line = on_line
        self._log_fh = open(log_file, "a", buffering=1) if log_file else None  # noqa: SIM115
        self.stdout_lines: list[str] = []
        self.stderr_lines: list[str] = []
        self.all_lines: list[LogLine] = []
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._start_readers()

    def _start_readers(self) -> None:
        for pipe, source in ((self._proc.stdout, "stdout"), (self._proc.stderr, "stderr")):
            t = threading.Thread(target=self._read, args=(pipe, source), daemon=True)
            t.start()
            self._threads.append(t)

    def _read(self, pipe, source: str) -> None:
        assert pipe is not None
        for raw_line in iter(pipe.readline, ""):
            if self._stop_event.is_set():
                break
            line = raw_line.rstrip("\n")
            self.all_lines.append((source, line))
            if source == "stdout":
                self.stdout_lines.append(line)
            else:
                self.stderr_lines.append(line)
            display.log_line(line, source)
            if self._log_fh:
                self._log_fh.write(f"[{source}] {line}\n")
                self._log_fh.flush()
            if self._on_line:
                self._on_line(source, line)
        pipe.close()

    @property
    def pid(self) -> int:
        return self._proc.pid

    def is_running(self) -> bool:
        return self._proc.poll() is None

    def exit_code(self) -> int | None:
        return self._proc.returncode

    def wait_until_done(self) -> int:
        """Block until the process exits. Returns the exit code."""
        code = self._proc.wait()
        for t in self._threads:
            t.join(timeout=5)
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None
        return code

    def terminate(self) -> None:
        self._stop_event.set()
        if self.is_running():
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._log_fh:
            self._log_fh.close()
            self._log_fh = None

    @property
    def full_output(self) -> str:
        return "\n".join(line for _, line in self.all_lines)


def run_interactive(
    command: str,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> int:
    """
    Run a command with stdin / stdout / stderr passed directly to the terminal.

    Use this for interactive setup wizards (e.g. ``npx cli setup``, ``npm run setup``)
    that require user input.  Unlike ``run_command``, output is NOT captured.

    Returns the process exit code.
    """
    merged_env = {**os.environ}
    if env:
        merged_env.update(env)

    result = subprocess.run(command, shell=True, cwd=cwd, env=merged_env)
    return result.returncode
