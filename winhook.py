"""Низкоуровневый хук клавиатуры и синтетический ввод для Windows.

Модуль использует только стандартную библиотеку (ctypes), никаких pip-зависимостей.
Хук WH_KEYBOARD_LL позволяет перехватывать события ручки (AULA F75 отправляет
мультимедийные клавиши) и при необходимости не пропускать их в систему.
"""

from __future__ import annotations

import ctypes
import os
import threading
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Iterable

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)

CLSCTX_ALL = 0x17
COINIT_APARTMENTTHREADED = 0x2
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106
SPI_SETDESKWALLPAPER = 0x0014
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02
DESKTOP_WALLPAPER_POSITION_FILL = 4

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
HC_ACTION = 0

LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10

# Метка в dwExtraInfo у всех синтетических нажатий этой утилиты.
# Нужна, чтобы хук отличал свои SendInput-события от чужих инжектированных:
# фирменный софт AULA (OemDrv.exe) тоже эмулирует мультимедиа-клавиши ручки,
# и такие события обязательно нужно перехватывать.
INJECT_TAG = 0x41554C41  # 'AULA'

INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000

ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("ki", _KEYBDINPUT),
        ("mi", _MOUSEINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", _INPUTUNION),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int,
    HOOKPROC,
    ctypes.c_void_p,
    wintypes.DWORD,
]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.CallNextHookEx.argtypes = [
    ctypes.c_void_p,
    ctypes.c_int,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
]
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = [
    wintypes.DWORD,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT

kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = ctypes.c_void_p
kernel32.CreateEventW.argtypes = [
    ctypes.c_void_p,
    wintypes.BOOL,
    wintypes.BOOL,
    wintypes.LPCWSTR,
]
kernel32.CreateEventW.restype = ctypes.c_void_p
kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenEventW.restype = ctypes.c_void_p
kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenMutexW.restype = ctypes.c_void_p
kernel32.SetEvent.argtypes = [ctypes.c_void_p]
kernel32.SetEvent.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.restype = wintypes.BOOL

user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short

user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL


def is_key_down(vk: int) -> bool:
    """Нажата ли клавиша прямо сейчас (для второго слоя действий)."""
    try:
        return bool(user32.GetAsyncKeyState(int(vk)) & 0x8000)
    except Exception:  # noqa: BLE001
        return False


@dataclass(frozen=True)
class KeyEvent:
    """Одно событие клавиатуры, полученное хуком."""

    vk: int
    scan: int
    is_down: bool
    flags: int


class KeyboardHook:
    """Глобальный низкоуровневый хук клавиатуры.

    on_event вызывается в потоке хука, поэтому колбэк должен быть очень быстрым
    (например, queue.put). blocked — клавиши, которые нужно «съесть».
    interest=None означает «отдавать все события», иначе только указанные VK.
    """

    def __init__(
        self,
        on_event: Callable[[KeyEvent], None],
        blocked: Iterable[int] = (),
        interest: Iterable[int] | None = None,
    ) -> None:
        self._on_event = on_event
        self._blocked = frozenset(blocked)
        self._interest = None if interest is None else frozenset(interest)
        self._thread = threading.Thread(
            target=self._run, name="aula-knob-hook", daemon=True
        )
        self._ready = threading.Event()
        self._stopping = False
        self._thread_id = 0
        self._hook = None
        self._proc = None
        self._error: BaseException | None = None

    def start(self, timeout: float = 5.0) -> None:
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("не удалось установить хук клавиатуры (таймаут)")
        if self._error is not None:
            raise RuntimeError(f"не удалось установить хук клавиатуры: {self._error}")

    def stop(self) -> None:
        self._stopping = True
        thread_id = self._thread_id
        if thread_id:
            user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout=2.0)

    def set_blocked(self, blocked: Iterable[int]) -> None:
        self._blocked = frozenset(blocked)

    def set_interest(self, interest: Iterable[int] | None) -> None:
        self._interest = None if interest is None else frozenset(interest)

    # ------------------------------------------------------------------ внутри

    def _run(self) -> None:
        try:
            self._thread_id = int(kernel32.GetCurrentThreadId())
            self._proc = HOOKPROC(self._hook_proc)
            module = kernel32.GetModuleHandleW(None)
            self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, module, 0)
            if not self._hook:
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException as exc:  # noqa: BLE001 - пробрасываем в start()
            self._error = exc
            self._ready.set()
            return

        self._ready.set()
        if self._stopping:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

        msg = wintypes.MSG()
        while True:
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

    def _hook_proc(self, code: int, wparam: int, lparam: int) -> int:
        if code == HC_ACTION:
            try:
                info = ctypes.cast(lparam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                vk = int(info.vkCode)
                # Свои синтетические нажатия помечаем dwExtraInfo и игнорируем,
                # чтобы не было рекурсии. Всё остальное — физические клавиши и
                # инжект сторонних программ (например, софта AULA) — обрабатываем.
                if int(info.dwExtraInfo) == INJECT_TAG:
                    return user32.CallNextHookEx(
                        self._hook, code, wparam, lparam
                    )
                if self._interest is None or vk in self._interest:
                    self._on_event(
                        KeyEvent(
                            vk=vk,
                            scan=int(info.scanCode),
                            is_down=wparam in (WM_KEYDOWN, WM_SYSKEYDOWN),
                            flags=int(info.flags),
                        )
                    )
                if vk in self._blocked:
                    return 1
            except Exception:  # noqa: BLE001 - хук обязан всегда отвечать
                pass
        return user32.CallNextHookEx(self._hook, code, wparam, lparam)


# --------------------------------------------------------------------- VK-таблица

VK_CODES: dict[str, int] = {
    "ctrl": 0x11,
    "control": 0x11,
    "lctrl": 0xA2,
    "left ctrl": 0xA2,
    "rctrl": 0xA3,
    "right ctrl": 0xA3,
    "shift": 0x10,
    "lshift": 0xA0,
    "left shift": 0xA0,
    "rshift": 0xA1,
    "right shift": 0xA1,
    "alt": 0x12,
    "lalt": 0xA4,
    "left alt": 0xA4,
    "ralt": 0xA5,
    "right alt": 0xA5,
    "win": 0x5B,
    "windows": 0x5B,
    "lwin": 0x5B,
    "rwin": 0x5C,
    "cmd": 0x5B,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "spacebar": 0x20,
    "tab": 0x09,
    "backspace": 0x08,
    "bksp": 0x08,
    "insert": 0x2D,
    "ins": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "page up": 0x21,
    "pageup": 0x21,
    "pgup": 0x21,
    "page down": 0x22,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "caps lock": 0x14,
    "capslock": 0x14,
    "num lock": 0x90,
    "numlock": 0x90,
    "scroll lock": 0x91,
    "scrolllock": 0x91,
    "print screen": 0x2C,
    "printscreen": 0x2C,
    "prtsc": 0x2C,
    "pause": 0x13,
    "break": 0x13,
    "apps": 0x5D,
    "context menu": 0x5D,
    "volume up": 0xAF,
    "vol up": 0xAF,
    "volume down": 0xAE,
    "vol down": 0xAE,
    "volume mute": 0xAD,
    "mute": 0xAD,
    "next track": 0xB0,
    "next": 0xB0,
    "prev track": 0xB1,
    "previous track": 0xB1,
    "prev": 0xB1,
    "stop": 0xB2,
    "play/pause": 0xB3,
    "play pause": 0xB3,
    "playpause": 0xB3,
    "media play pause": 0xB3,
    "browser back": 0xA6,
    "browser forward": 0xA7,
    "browser refresh": 0xA8,
    "browser stop": 0xA9,
    "browser search": 0xAA,
    "browser favorites": 0xAB,
    "browser home": 0xAC,
    "launch mail": 0xB4,
    "launch media": 0xB5,
    "launch app1": 0xB6,
    "launch app2": 0xB7,
    "calculator": 0xB7,
    "multiply": 0x6A,
    "add": 0x6B,
    "plus": 0x6B,
    "separator": 0x6C,
    "subtract": 0x6D,
    "minus": 0x6D,
    "decimal": 0x6E,
    "divide": 0x6F,
    ";": 0xBA,
    "=": 0xBB,
    ",": 0xBC,
    "-": 0xBD,
    ".": 0xBE,
    "/": 0xBF,
    "`": 0xC0,
    "[": 0xDB,
    "\\": 0xDC,
    "]": 0xDD,
    "'": 0xDE,
}

for _i in range(1, 25):
    VK_CODES[f"f{_i}"] = 0x6F + _i

for _i in range(10):
    VK_CODES[f"numpad {_i}"] = 0x60 + _i
    VK_CODES[f"num {_i}"] = 0x60 + _i

_EXTENDED_VKS = frozenset(
    {
        0x21,
        0x22,
        0x23,
        0x24,
        0x25,
        0x26,
        0x27,
        0x28,
        0x2C,
        0x2D,
        0x2E,
        0x5B,
        0x5C,
        0x5D,
        0x6F,
        0xA3,
        0xA5,
        0xA6,
        0xA7,
        0xA8,
        0xA9,
        0xAA,
        0xAB,
        0xAC,
        0xAD,
        0xAE,
        0xAF,
        0xB0,
        0xB1,
        0xB2,
        0xB3,
        0xB4,
        0xB5,
        0xB6,
        0xB7,
    }
)

_VK_NAMES = {
    0x08: "backspace",
    0x09: "tab",
    0x0D: "enter",
    0x10: "shift",
    0x11: "ctrl",
    0x12: "alt",
    0x13: "pause",
    0x14: "caps lock",
    0x1B: "esc",
    0x20: "space",
    0x21: "page up",
    0x22: "page down",
    0x23: "end",
    0x24: "home",
    0x25: "left",
    0x26: "up",
    0x27: "right",
    0x28: "down",
    0x2C: "print screen",
    0x2D: "insert",
    0x2E: "delete",
    0x5B: "win",
    0x5C: "rwin",
    0x5D: "apps",
    0x90: "num lock",
    0x91: "scroll lock",
    0xA0: "lshift",
    0xA1: "rshift",
    0xA2: "lctrl",
    0xA3: "rctrl",
    0xA4: "lalt",
    0xA5: "ralt",
    0xAD: "volume mute",
    0xAE: "volume down",
    0xAF: "volume up",
    0xB0: "next track",
    0xB1: "prev track",
    0xB2: "stop",
    0xB3: "play/pause",
    0xB4: "launch mail",
    0xB5: "launch media",
    0xB6: "launch app1",
    0xB7: "launch app2",
}


def key_name(vk: int) -> str:
    """Человекочитаемое имя VK-кода (для режима --learn)."""
    if vk in _VK_NAMES:
        return _VK_NAMES[vk]
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x60 <= vk <= 0x69:
        return f"numpad {vk - 0x60}"
    if 0x70 <= vk <= 0x87:
        return f"f{vk - 0x6F}"
    return f"vk:0x{vk:02X}"


@lru_cache(maxsize=None)
def _parse_key_cached(name: str) -> int:
    if name.startswith("vk:"):
        value = int(name[3:], 0)
        if not 0 <= value <= 0xFF:
            raise ValueError(f"VK-код вне диапазона: {name!r}")
        return value
    if name in VK_CODES:
        return VK_CODES[name]
    if len(name) == 1:
        char = name.upper()
        if "A" <= char <= "Z" or "0" <= char <= "9":
            return ord(char)
    raise KeyError(name)


def parse_key(name: str) -> int:
    """Превращает имя клавиши ('volume up', 'ctrl', 'vk:0xAF') в VK-код."""
    if not isinstance(name, str):
        raise ValueError("имя клавиши должно быть строкой")
    normalized = " ".join(name.strip().lower().replace("_", " ").split())
    if not normalized:
        raise ValueError("пустое имя клавиши")
    try:
        return _parse_key_cached(normalized)
    except KeyError:
        raise ValueError(f"неизвестная клавиша: {name!r}") from None


def is_extended(vk: int) -> bool:
    return vk in _EXTENDED_VKS


# --------------------------------------------------------------- синтетический ввод


def _key_input(vk: int, *, is_up: bool) -> _INPUT:
    flags = 0
    if is_extended(vk):
        flags |= KEYEVENTF_EXTENDEDKEY
    if is_up:
        flags |= KEYEVENTF_KEYUP
    scan = int(user32.MapVirtualKeyW(vk, 0))
    return _INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUTUNION(
            ki=_KEYBDINPUT(
                wVk=vk,
                wScan=scan,
                dwFlags=flags,
                time=0,
                dwExtraInfo=INJECT_TAG,
            )
        ),
    )


def _unicode_input(code_unit: int, *, is_up: bool) -> _INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if is_up else 0)
    return _INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUTUNION(
            ki=_KEYBDINPUT(
                wVk=0,
                wScan=code_unit,
                dwFlags=flags,
                time=0,
                dwExtraInfo=INJECT_TAG,
            )
        ),
    )


def _send_all(inputs: list[_INPUT]) -> None:
    if not inputs:
        return
    chunk = 64
    for start in range(0, len(inputs), chunk):
        part = inputs[start : start + chunk]
        array = (_INPUT * len(part))(*part)
        sent = user32.SendInput(len(part), array, ctypes.sizeof(_INPUT))
        if sent != len(part):
            raise OSError(ctypes.get_last_error(), "SendInput не сработал")


def press_key(vk: int) -> None:
    _send_all([_key_input(vk, is_up=False), _key_input(vk, is_up=True)])


def send_hotkey(combo: str) -> None:
    """Отправляет сочетание, например 'ctrl+shift+s' или 'volume up'."""
    parts = [part.strip() for part in combo.split("+") if part.strip()]
    if not parts:
        raise ValueError("пустое сочетание клавиш")
    vks = [parse_key(part) for part in parts]
    inputs: list[_INPUT] = []
    for vk in vks:
        inputs.append(_key_input(vk, is_up=False))
    for vk in reversed(vks):
        inputs.append(_key_input(vk, is_up=True))
    _send_all(inputs)


def type_text(text: str) -> None:
    """Печатает текст как есть (через KEYEVENTF_UNICODE)."""
    if not text:
        return
    data = text.encode("utf-16-le")
    inputs: list[_INPUT] = []
    for index in range(0, len(data), 2):
        unit = int.from_bytes(data[index : index + 2], "little")
        inputs.append(_unicode_input(unit, is_up=False))
        inputs.append(_unicode_input(unit, is_up=True))
    _send_all(inputs)


def scroll(delta: int, *, horizontal: bool = False) -> None:
    """Прокручивает колесо на delta «щелчков» (плюс — вверх/вправо)."""
    if not delta:
        return
    flags = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    value = (int(delta) * 120) & 0xFFFFFFFF
    item = _INPUT(
        type=INPUT_MOUSE,
        u=_INPUTUNION(
            mi=_MOUSEINPUT(
                dx=0,
                dy=0,
                mouseData=value,
                dwFlags=flags,
                time=0,
                dwExtraInfo=0,
            )
        ),
    )
    _send_all([item])


def focus_window(needle: str) -> bool:
    """Разворачивает и активирует окно, в заголовке которого есть needle."""
    needle_lower = needle.lower()
    found: list[int] = []

    def _callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = int(user32.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if needle_lower in buffer.value.lower():
            found.append(int(hwnd))
            return False
        return True

    callback = WNDENUMPROC(_callback)
    user32.EnumWindows(callback, 0)
    if not found:
        return False
    hwnd = found[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if user32.SetForegroundWindow(hwnd):
        return True
    # SetForegroundWindow может отказать фоновому процессу.
    switch = getattr(user32, "SwitchToThisWindow", None)
    if switch is not None:
        switch.argtypes = [wintypes.HWND, wintypes.BOOL]
        switch.restype = None
        switch(hwnd, True)
        return True
    return False


# ------------------------------------------------------------------- именованные объекты


def create_mutex(name: str):
    """Возвращает handle мьютекса или None, если объект уже существует."""
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def create_event(name: str):
    handle = kernel32.CreateEventW(None, True, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def mutex_exists(name: str) -> bool:
    handle = kernel32.OpenMutexW(0x00100000, False, name)  # SYNCHRONIZE
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def event_is_set(handle) -> bool:
    return kernel32.WaitForSingleObject(handle, 0) == 0


def set_event(handle) -> bool:
    return bool(kernel32.SetEvent(handle))


def signal_event(name: str) -> bool:
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, name)
    if not handle:
        return False
    try:
        return bool(kernel32.SetEvent(handle))
    finally:
        kernel32.CloseHandle(handle)


def close_handle(handle) -> None:
    if handle:
        kernel32.CloseHandle(handle)


# ------------------------------------------------------------------ обои рабочего стола


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


_CLSID_DESKTOP_WALLPAPER = _guid("C2CF3110-460E-4FC1-B9D0-8A1C0C9CC4BD")
_IID_IDESKTOP_WALLPAPER = _guid("B92B56A9-8B55-4E14-9A89-0199BBB6F93B")

ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
ole32.CoInitializeEx.restype = ctypes.c_long
ole32.CoUninitialize.argtypes = []
ole32.CoUninitialize.restype = None
ole32.CoCreateInstance.argtypes = [
    ctypes.POINTER(_GUID),
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(_GUID),
    ctypes.POINTER(ctypes.c_void_p),
]
ole32.CoCreateInstance.restype = ctypes.c_long

user32.SystemParametersInfoW.argtypes = [
    wintypes.UINT,
    wintypes.UINT,
    ctypes.c_void_p,
    wintypes.UINT,
]
user32.SystemParametersInfoW.restype = wintypes.BOOL


def _com(ptr, index, restype, argtypes, *args):
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    address = vtable[index]
    if not address:
        raise OSError("пустой метод COM")
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(address)(ptr, *args)


def _release_com(ptr) -> None:
    if ptr:
        try:
            _com(ptr, 2, ctypes.c_ulong, [])
        except Exception:  # noqa: BLE001
            pass


def set_desktop_wallpaper(image_path: str) -> None:
    """Ставит картинку обоями рабочего стола.

    Использует IDesktopWallpaper (PNG/JPG/BMP/WEBP) с откатом на
    SystemParametersInfoW, если COM-интерфейс недоступен.
    """
    path = str(image_path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    absolute = os.path.abspath(path)

    initialized = False
    result = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if result >= 0:
        initialized = True
    elif result != RPC_E_CHANGED_MODE:
        raise OSError(f"CoInitializeEx: HRESULT 0x{result & 0xFFFFFFFF:08X}")

    try:
        desktop = ctypes.c_void_p()
        created = ole32.CoCreateInstance(
            ctypes.byref(_CLSID_DESKTOP_WALLPAPER),
            None,
            CLSCTX_ALL,
            ctypes.byref(_IID_IDESKTOP_WALLPAPER),
            ctypes.byref(desktop),
        )
        if created >= 0:
            try:
                hr = _com(
                    desktop,
                    3,
                    ctypes.c_long,
                    [wintypes.LPCWSTR, wintypes.LPCWSTR],
                    None,
                    absolute,
                )
                if hr >= 0:
                    _com(
                        desktop,
                        10,
                        ctypes.c_long,
                        [ctypes.c_int],
                        DESKTOP_WALLPAPER_POSITION_FILL,
                    )
                    return
            finally:
                _release_com(desktop)
    finally:
        if initialized:
            ole32.CoUninitialize()

    if not user32.SystemParametersInfoW(
        SPI_SETDESKWALLPAPER,
        0,
        ctypes.c_wchar_p(absolute),
        SPIF_UPDATEINIFILE | SPIF_SENDCHANGE,
    ):
        raise ctypes.WinError(ctypes.get_last_error())

