"""Launch the transaction helper; request UAC only when target folders need it."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .transactions import read_json


def needs_elevation(plan: dict) -> bool:
    if os.name != 'nt':
        return False
    parents = {(Path(plan['root']) / item['relative']).parent for item in plan['items']}
    for parent in parents:
        try:
            with tempfile.TemporaryFile(dir=parent, prefix='.splash-permission-'):
                pass
        except PermissionError:
            return True
    return False


def run_helper(plan_path: Path, recover: bool = False) -> None:
    helper = Path(__file__).with_name('helper.py').resolve()
    args = [str(helper), str(plan_path)] + (['--recover'] if recover else [])
    result = plan_path.with_suffix('.result.json')
    result.unlink(missing_ok=True)
    if needs_elevation(read_json(plan_path)):
        elevated_python(args)
    else:
        completed = subprocess.run([sys.executable, *args], capture_output=True, text=True)
        if not result.exists():
            raise RuntimeError(completed.stderr or 'Installer did not report a result')
    if not result.exists():
        raise RuntimeError('Installer stopped without a result. Use Recover interrupted operation.')
    outcome = read_json(result)
    if not outcome['ok']:
        raise RuntimeError(outcome['error'])


def elevated_python(args: list[str]) -> None:
    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ('cbSize', wintypes.DWORD), ('fMask', ctypes.c_ulong),
            ('hwnd', wintypes.HWND), ('lpVerb', wintypes.LPCWSTR),
            ('lpFile', wintypes.LPCWSTR), ('lpParameters', wintypes.LPCWSTR),
            ('lpDirectory', wintypes.LPCWSTR), ('nShow', ctypes.c_int),
            ('hInstApp', wintypes.HINSTANCE), ('lpIDList', ctypes.c_void_p),
            ('lpClass', wintypes.LPCWSTR), ('hkeyClass', wintypes.HKEY),
            ('dwHotKey', wintypes.DWORD), ('hIcon', wintypes.HANDLE),
            ('hProcess', wintypes.HANDLE),
        ]
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell.ShellExecuteExW.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040 | 0x00000100  # NOCLOSEPROCESS | NOASYNC
    info.lpVerb = 'runas'
    info.lpFile = sys.executable
    info.lpParameters = subprocess.list2cmdline(args)
    info.lpDirectory = str(Path(__file__).resolve().parent.parent)
    info.nShow = 0
    if not shell.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.get_last_error()
        if code == 1223:
            raise RuntimeError('Administrator prompt canceled. No changes installed; use Recover to discard the pending operation.')
        raise ctypes.WinError(code)
    try:
        if kernel.WaitForSingleObject(info.hProcess, 0xFFFFFFFF) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(info.hProcess)
