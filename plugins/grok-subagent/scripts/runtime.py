"""Process-scoped launch settings and conservative Windows process identity checks."""
import ctypes
from ctypes import wintypes
import os

VERSION = "0.3.1"
TESTED_GROK = "1.0.13"


def launch_environment(mode):
    env = dict(os.environ, GROK_DISABLE_AUTOUPDATER="1", GROK_SUBAGENTS="0")
    if mode == "native":
        env.update(GROK_CURSOR_MCPS_ENABLED="0", GROK_CLAUDE_MCPS_ENABLED="0")
    return env


def process_identity(pid):
    """Never infer that access-denied or unsupported process queries mean dead."""
    if type(pid) is not int or pid <= 0 or os.name != "nt":
        return {"state": "unknown"}
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return {"state": "dead" if ctypes.get_last_error() == 87 else "unknown"}
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return {"state": "unknown"}
        if code.value != 259:
            return {"state": "dead"}
        creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return {"state": "unknown"}
        return {"state": "alive", "birth": str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)}
    finally:
        kernel.CloseHandle(handle)


def original_process_state(pid, birth):
    observed = process_identity(pid)
    if observed["state"] == "alive" and birth and observed.get("birth") != birth:
        return "dead"
    return observed["state"]
