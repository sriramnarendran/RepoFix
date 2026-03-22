"""Port conflict detection and resolution."""
from __future__ import annotations
import socket
import psutil
from repofix.output import display

class PortConflictError(Exception):

    def __init__(self, port: int):
        super().__init__(f'Port {port} is already in use')
        self.port = port

def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(('0.0.0.0', port))
            return False
        except OSError:
            return True

def find_free_port(start: int=3000, end: int=9999) -> int:
    for port in range(start, end):
        if not is_port_in_use(port):
            return port
    raise RuntimeError(f'No free port found in range {start}–{end}')

def get_pids_on_port(port: int) -> list[int]:
    pids: list[int] = []
    try:
        for conn in psutil.net_connections(kind='tcp'):
            if conn.laddr.port == port and conn.pid:
                pids.append(conn.pid)
    except (psutil.AccessDenied, AttributeError):
        pass
    return list(set(pids))

def kill_port(port: int) -> bool:
    """Kill all processes listening on the given port. Returns True if any were killed."""
    pids = get_pids_on_port(port)
    if not pids:
        return False
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            display.warning(f'Killing process [bold]{proc.name()}[/bold] (PID {pid}) on port {port}')
            proc.terminate()
            proc.wait(timeout=3)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
            try:
                psutil.Process(pid).kill()
            except Exception:
                pass
    return True

def resolve_port(desired_port: int, auto_approve: bool=False, mode: str='auto') -> int:
    """
    Check if desired_port is free. If not, kill the occupying process or
    pick the next free port, depending on mode.

    Returns the port that should be used.
    """
    if not is_port_in_use(desired_port):
        return desired_port
    pids = get_pids_on_port(desired_port)
    pid_str = ', '.join((str(p) for p in pids)) if pids else 'unknown'
    display.warning(f'Port [bold]{desired_port}[/bold] is in use (PIDs: {pid_str})')
    if mode == 'assist' and (not auto_approve):
        answer = display.prompt_confirm(f'Kill process(es) on port {desired_port} and reuse it?')
        if answer:
            kill_port(desired_port)
            return desired_port
        new_port = find_free_port(desired_port + 1)
        display.info(f'Using port [bold]{new_port}[/bold] instead')
        return new_port
    else:
        kill_port(desired_port)
        if not is_port_in_use(desired_port):
            display.success(f'Port {desired_port} is now free')
            return desired_port
        new_port = find_free_port(desired_port + 1)
        display.info(f'Using port [bold]{new_port}[/bold] instead')
        return new_port
