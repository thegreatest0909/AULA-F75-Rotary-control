"""Громкость приложений Windows через Core Audio API (без сторонних пакетов).

Позволяет получить список активных аудиосессий, менять громкость и mute
конкретного приложения, не затрагивая остальные, а также системную громкость.
"""

from __future__ import annotations

import ctypes
import os
import threading
import uuid
from ctypes import POINTER, byref, c_float, c_int, c_uint, c_void_p, wintypes
from dataclasses import dataclass, field

ole32 = ctypes.WinDLL("ole32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

HRESULT = ctypes.c_long
CLSCTX_ALL = 0x17
COINIT_MULTITHREADED = 0x0
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


CLSID_MMDEVICE_ENUMERATOR = _guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDEVICE_ENUMERATOR = _guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAUDIO_SESSION_MANAGER2 = _guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
IID_IAUDIO_SESSION_CONTROL2 = _guid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
IID_ISIMPLE_AUDIO_VOLUME = _guid("87CE5498-68D6-44E5-9215-6DA47EF883D8")
IID_IAUDIO_ENDPOINT_VOLUME = _guid("5CDF2C82-841E-4546-9722-0CF74078229A")

ole32.CoInitializeEx.argtypes = [c_void_p, wintypes.DWORD]
ole32.CoInitializeEx.restype = HRESULT
ole32.CoCreateInstance.argtypes = [
    POINTER(GUID),
    c_void_p,
    wintypes.DWORD,
    POINTER(GUID),
    POINTER(c_void_p),
]
ole32.CoCreateInstance.restype = HRESULT
ole32.CoTaskMemFree.argtypes = [c_void_p]
ole32.CoTaskMemFree.restype = None

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = c_void_p
kernel32.CloseHandle.argtypes = [c_void_p]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    c_void_p,
    wintypes.DWORD,
    wintypes.LPWSTR,
    POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL


class AudioError(Exception):
    """Ошибка Core Audio."""


_tls = threading.local()
_process_names: dict[int, str] = {}


def _ensure_com() -> None:
    if getattr(_tls, "ready", False):
        return
    result = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    if result < 0:
        raise AudioError(f"CoInitializeEx: HRESULT 0x{result & 0xFFFFFFFF:08X}")
    _tls.ready = True


def _call(ptr: c_void_p, index: int, restype, argtypes, *args):
    vtable = ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents
    address = vtable[index]
    if not address:
        raise AudioError("пустой метод COM")
    function = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(address)
    return function(ptr, *args)


def _check(result: int, what: str) -> int:
    if result < 0:
        raise AudioError(f"{what}: HRESULT 0x{result & 0xFFFFFFFF:08X}")
    return result


def _release(ptr: c_void_p | None) -> None:
    if ptr is not None and ptr.value:
        try:
            _call(ptr, 2, ctypes.c_ulong, [])
        except Exception:  # noqa: BLE001
            pass


def _query(ptr: c_void_p, iid: GUID) -> c_void_p | None:
    out = c_void_p()
    result = _call(
        ptr, 0, HRESULT, [POINTER(GUID), POINTER(c_void_p)], byref(iid), byref(out)
    )
    if result < 0:
        return None
    return out


def _default_device() -> c_void_p:
    enumerator = c_void_p()
    result = ole32.CoCreateInstance(
        byref(CLSID_MMDEVICE_ENUMERATOR),
        None,
        CLSCTX_ALL,
        byref(IID_IMMDEVICE_ENUMERATOR),
        byref(enumerator),
    )
    _check(result, "CoCreateInstance(MMDeviceEnumerator)")
    try:
        device = c_void_p()
        # IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender, eConsole, &device)
        result = _call(
            enumerator,
            4,
            HRESULT,
            [c_int, c_int, POINTER(c_void_p)],
            0,
            0,
            byref(device),
        )
        _check(result, "GetDefaultAudioEndpoint")
        return device
    finally:
        _release(enumerator)


def _activate(device: c_void_p, iid: GUID) -> c_void_p:
    interface = c_void_p()
    result = _call(
        device,
        3,
        HRESULT,
        [POINTER(GUID), wintypes.DWORD, c_void_p, POINTER(c_void_p)],
        byref(iid),
        CLSCTX_ALL,
        None,
        byref(interface),
    )
    _check(result, "IMMDevice::Activate")
    return interface


def _session_enum() -> tuple[c_void_p, c_void_p]:
    """Возвращает (session_enumerator, device) — вызывающий освобождает оба."""
    device = _default_device()
    manager = c_void_p()
    try:
        activated = _activate(device, IID_IAUDIO_SESSION_MANAGER2)
        manager = activated
        enumerator = c_void_p()
        result = _call(
            manager, 5, HRESULT, [POINTER(c_void_p)], byref(enumerator)
        )
        _check(result, "GetSessionEnumerator")
        return enumerator, device
    except BaseException:
        _release(manager)
        _release(device)
        raise


def _process_name(pid: int) -> str:
    if not pid:
        return ""
    if pid in _process_names:
        return _process_names[pid]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    name = ""
    if handle:
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buffer))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, byref(size)):
                name = os.path.basename(buffer.value)
        finally:
            kernel32.CloseHandle(handle)
    _process_names[pid] = name
    return name


@dataclass
class AppAudio:
    app: str
    title: str
    volume: int
    mute: bool
    sessions: int = 1
    pid: int = 0
    system: bool = False

    def to_dict(self) -> dict:
        return {
            "app": self.app,
            "title": self.title,
            "volume": self.volume,
            "mute": self.mute,
            "sessions": self.sessions,
            "pid": self.pid,
            "system": self.system,
        }


@dataclass
class _Session:
    control: c_void_p = field(repr=False)
    control2: c_void_p | None = field(default=None, repr=False)
    volume: c_void_p | None = field(default=None, repr=False)
    pid: int = 0
    app: str = ""
    system: bool = False
    level: float = 0.0
    mute: bool = False


def _read_sessions() -> list[_Session]:
    _ensure_com()
    enumerator, device = _session_enum()
    sessions: list[_Session] = []
    try:
        count = c_int()
        _check(
            _call(enumerator, 3, HRESULT, [POINTER(c_int)], byref(count)),
            "GetCount",
        )
        for index in range(count.value):
            control = c_void_p()
            result = _call(
                enumerator,
                4,
                HRESULT,
                [c_int, POINTER(c_void_p)],
                index,
                byref(control),
            )
            if result < 0:
                continue
            session = _Session(control=control)
            keep = False
            try:
                session.control2 = _query(control, IID_IAUDIO_SESSION_CONTROL2)
                session.volume = _query(control, IID_ISIMPLE_AUDIO_VOLUME)
                if session.volume is None:
                    continue
                if session.control2 is not None:
                    pid = wintypes.DWORD(0)
                    if (
                        _call(
                            session.control2,
                            14,
                            HRESULT,
                            [POINTER(wintypes.DWORD)],
                            byref(pid),
                        )
                        >= 0
                    ):
                        session.pid = int(pid.value)
                    session.system = (
                        _call(session.control2, 15, HRESULT, []) == 0
                    )
                session.app = "System Sounds" if session.system else _process_name(session.pid)
                if not session.app:
                    continue
                level = c_float()
                _check(
                    _call(
                        session.volume,
                        4,
                        HRESULT,
                        [POINTER(c_float)],
                        byref(level),
                    ),
                    "GetMasterVolume",
                )
                muted = wintypes.BOOL()
                _check(
                    _call(
                        session.volume,
                        6,
                        HRESULT,
                        [POINTER(wintypes.BOOL)],
                        byref(muted),
                    ),
                    "GetMute",
                )
                session.level = float(level.value)
                session.mute = bool(muted.value)
                sessions.append(session)
                keep = True
            except Exception:  # noqa: BLE001 - одна битая сессия не должна ломать список
                pass
            finally:
                if not keep:
                    _release(session.volume)
                    _release(session.control2)
                    _release(control)
        return sessions
    finally:
        _release(enumerator)
        _release(device)


def _close_sessions(sessions: list[_Session]) -> None:
    for session in sessions:
        _release(session.volume)
        _release(session.control2)
        _release(session.control)


def list_apps() -> list[dict]:
    """Список приложений с активными аудиосессиями, сгруппированный по процессу."""
    sessions = _read_sessions()
    try:
        grouped: dict[str, dict] = {}
        for session in sessions:
            key = session.app.lower()
            entry = grouped.get(key)
            if entry is None:
                grouped[key] = {
                    "app": session.app,
                    "title": session.app.rsplit(".", 1)[0],
                    "levels": [session.level],
                    "mutes": [session.mute],
                    "sessions": 1,
                    "pid": session.pid,
                    "system": session.system,
                }
            else:
                entry["levels"].append(session.level)
                entry["mutes"].append(session.mute)
                entry["sessions"] += 1
        result = []
        for entry in grouped.values():
            levels = entry.pop("levels")
            mutes = entry.pop("mutes")
            entry["volume"] = int(round(sum(levels) / len(levels) * 100))
            entry["mute"] = all(mutes)
            result.append(AppAudio(**entry).to_dict())
        result.sort(key=lambda item: (item["system"], item["app"].lower()))
        return result
    finally:
        _close_sessions(sessions)


def _norm_app(app: str) -> str:
    name = os.path.basename(str(app).strip().strip('"')).lower()
    return name[:-4] if name.endswith(".exe") else name


def _matches(session: _Session, needle: str) -> bool:
    return needle in session.app.lower()


def _sessions_for(app: str) -> list[_Session]:
    needle = _norm_app(app)
    if not needle:
        return []
    sessions = _read_sessions()
    matched: list[_Session] = []
    for session in sessions:
        if _matches(session, needle):
            matched.append(session)
        else:
            _release(session.volume)
            _release(session.control2)
            _release(session.control)
    return matched


def set_app_volume(app: str, level: int) -> int:
    """Ставит громкость (0..100) всем сессиям приложения. Возвращает число сессий."""
    sessions = _sessions_for(app)
    try:
        value = max(0.0, min(1.0, float(level) / 100.0))
        for session in sessions:
            _check(
                _call(
                    session.volume,
                    3,
                    HRESULT,
                    [c_float, POINTER(GUID)],
                    value,
                    None,
                ),
                "SetMasterVolume",
            )
        return len(sessions)
    finally:
        _close_sessions(sessions)


def adjust_app_volume(app: str, delta: int) -> tuple[int, int | None]:
    """Меняет громкость приложения на delta процентных пунктов.

    Возвращает (количество сессий, новая средняя громкость в процентах).
    """
    sessions = _sessions_for(app)
    try:
        levels: list[int] = []
        for session in sessions:
            value = max(0.0, min(1.0, session.level + delta / 100.0))
            _check(
                _call(
                    session.volume,
                    3,
                    HRESULT,
                    [c_float, POINTER(GUID)],
                    value,
                    None,
                ),
                "SetMasterVolume",
            )
            levels.append(int(round(value * 100)))
        if not levels:
            return 0, None
        return len(levels), int(round(sum(levels) / len(levels)))
    finally:
        _close_sessions(sessions)


def get_app_volume(app: str) -> int | None:
    sessions = _sessions_for(app)
    try:
        if not sessions:
            return None
        return int(round(sum(item.level for item in sessions) / len(sessions) * 100))
    finally:
        _close_sessions(sessions)


def set_app_mute(app: str, state: bool | None = None) -> tuple[int, bool | None]:
    """Mute приложения. state=None — переключить.

    Возвращает (количество сессий, новое состояние mute).
    """
    sessions = _sessions_for(app)
    try:
        if not sessions:
            return 0, None
        target = (not all(item.mute for item in sessions)) if state is None else bool(state)
        for session in sessions:
            _check(
                _call(
                    session.volume,
                    5,
                    HRESULT,
                    [wintypes.BOOL, POINTER(GUID)],
                    target,
                    None,
                ),
                "SetMute",
            )
        return len(sessions), target
    finally:
        _close_sessions(sessions)


def _endpoint_volume() -> c_void_p:
    _ensure_com()
    device = _default_device()
    try:
        return _activate(device, IID_IAUDIO_ENDPOINT_VOLUME)
    finally:
        _release(device)


def get_master_volume() -> tuple[int, bool]:
    volume = _endpoint_volume()
    try:
        level = c_float()
        _check(
            _call(volume, 9, HRESULT, [POINTER(c_float)], byref(level)),
            "GetMasterVolumeLevelScalar",
        )
        muted = wintypes.BOOL()
        _check(
            _call(volume, 15, HRESULT, [POINTER(wintypes.BOOL)], byref(muted)),
            "GetMute",
        )
        return int(round(level.value * 100)), bool(muted.value)
    finally:
        _release(volume)


def set_master_volume(level: int) -> None:
    volume = _endpoint_volume()
    try:
        value = max(0.0, min(1.0, float(level) / 100.0))
        _check(
            _call(
                volume,
                7,
                HRESULT,
                [c_float, POINTER(GUID)],
                value,
                None,
            ),
            "SetMasterVolumeLevelScalar",
        )
    finally:
        _release(volume)


def set_master_mute(state: bool | None = None) -> bool:
    volume = _endpoint_volume()
    try:
        muted = wintypes.BOOL()
        _check(
            _call(volume, 15, HRESULT, [POINTER(wintypes.BOOL)], byref(muted)),
            "GetMute",
        )
        target = (not bool(muted.value)) if state is None else bool(state)
        _check(
            _call(
                volume,
                14,
                HRESULT,
                [wintypes.BOOL, POINTER(GUID)],
                target,
                None,
            ),
            "SetMute",
        )
        return target
    finally:
        _release(volume)
