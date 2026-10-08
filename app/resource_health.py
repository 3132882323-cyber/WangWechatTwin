"""Windows commit headroom, without changing OS settings or other processes."""
import sys


def available_commit_bytes():
    if sys.platform != 'win32':
        return None
    import ctypes
    class MemoryStatus(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in
            ('total_phys', 'avail_phys', 'total_page', 'avail_page', 'total_virtual', 'avail_virtual', 'extended')]
    state = MemoryStatus()
    state.length = ctypes.sizeof(state)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
        return None
    return state.avail_page


def under_pressure(available):
    return available is not None and available < 1024 * 1024 * 1024
