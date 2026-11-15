"""Выполнение действий, привязанных к ручке AULA F75."""

from __future__ import annotations

import os
import subprocess
import time
import webbrowser
from typing import Any, Callable

import audio
import winhook

Log = Callable[[str], None]


class ActionError(Exception):
    """Некорректное или невыполнимое действие."""


_MEDIA_KEYS = {
    "play_pause": "play/pause",
    "playpause": "play/pause",
    "play": "play/pause",
    "toggle": "play/pause",
    "next": "next track",
    "next_track": "next track",
    "prev": "prev track",
    "previous": "prev track",
    "prev_track": "prev track",
    "previous_track": "prev track",
    "stop": "stop",
    "mute": "volume mute",
}

_ACTION_TYPES = (
    "keys",
    "text",
    "command",
    "open",
    "url",
    "media",
    "volume",
    "mute",
    "wheel",
    "focus",
    "sleep",
    "none",
)


def parse_shorthand(text: str) -> dict[str, Any]:
    """Разбирает краткую запись вида 'keys:ctrl+s' или 'run:notepad'."""
    head, separator, tail = text.partition(":")
    if not separator:
        raise ActionError(
            f"не понимаю действие {text!r}; используйте 'тип:значение', "
            "например 'keys:ctrl+s'"
        )
    kind = head.strip().lower()
    value = tail.strip()
    if kind in ("keys", "key", "hotkey"):
        return {"type": "keys", "keys": value}
    if kind in ("text", "type"):
        return {"type": "text", "text": tail}
    if kind in ("run", "cmd", "command", "sh"):
        return {"type": "command", "cmd": tail}
    if kind in ("open", "start"):
        return {"type": "open", "target": value}
    if kind in ("url", "browser"):
        return {"type": "url", "url": value}
    if kind == "media":
        return {"type": "media", "action": value}
    if kind == "volume":
        return {"type": "volume", "delta": _to_int(value, "volume")}
    if kind == "wheel":
        return {"type": "wheel", "delta": _to_int(value, "wheel")}
    if kind in ("focus", "window"):
        return {"type": "focus", "title": tail}
    if kind == "sleep":
        return {"type": "sleep", "ms": _to_int(value, "sleep")}
    raise ActionError(f"неизвестный тип действия: {head!r}")


def _to_int(value: Any, where: str) -> int:
    if isinstance(value, bool):
        raise ActionError(f"{where}: ожидается целое число")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ActionError(f"{where}: ожидается целое число, получено {value!r}") from None


def _normalize(action: Any) -> Any:
    if isinstance(action, str):
        return parse_shorthand(action)
    return action


def execute(action: Any, log: Log | None = None) -> None:
    """Выполняет действие или список действий."""
    log = log or (lambda _message: None)
    if action is None:
        return
    if isinstance(action, (list, tuple)):
        for item in action:
            execute(item, log)
        return
    action = _normalize(action)
    if not isinstance(action, dict):
        raise ActionError(f"действие должно быть объектом или списком, а не {action!r}")
    kind = str(action.get("type", "none")).strip().lower()
    if kind in ("", "none", "nothing"):
        return
    if kind not in _ACTION_TYPES:
        raise ActionError(f"неизвестный тип действия: {kind!r}")

    if kind == "keys":
        _do_keys(action)
    elif kind == "text":
        winhook.type_text(str(action.get("text", "")))
    elif kind == "command":
        _do_command(action, log)
    elif kind == "open":
        _do_open(action)
    elif kind == "url":
        webbrowser.open(str(action["url"]), new=2)
    elif kind == "media":
        _do_media(action)
    elif kind == "volume":
        _do_volume(action, log)
    elif kind == "mute":
        _do_mute(action, log)
    elif kind == "wheel":
        horizontal = bool(action.get("horizontal", False))
        winhook.scroll(_to_int(action.get("delta"), "wheel.delta"), horizontal=horizontal)
    elif kind == "focus":
        title = str(action.get("title", ""))
        if not title:
            raise ActionError("focus: не задан title")
        if not winhook.focus_window(title):
            log(f"окно с заголовком {title!r} не найдено")
    elif kind == "sleep":
        time.sleep(max(0, _to_int(action.get("ms", 0), "sleep.ms")) / 1000.0)


def _do_keys(action: dict[str, Any]) -> None:
    raw = action.get("keys")
    if isinstance(raw, str):
        combos = [raw]
    elif isinstance(raw, (list, tuple)) and raw:
        combos = [str(item) for item in raw]
    else:
        raise ActionError("keys: нужно поле 'keys' (строка или список)")
    repeat = max(1, _to_int(action.get("repeat", 1), "keys.repeat"))
    interval = max(0, _to_int(action.get("interval_ms", 0), "keys.interval_ms")) / 1000.0
    for _ in range(repeat):
        for combo in combos:
            winhook.send_hotkey(combo)
            if interval:
                time.sleep(interval)


def _do_command(action: dict[str, Any], log: Log) -> None:
    cmd = action.get("cmd")
    if not cmd:
        raise ActionError("command: не задано поле 'cmd'")
    shell = action.get("shell")
    if shell is None:
        shell = isinstance(cmd, str)
    hidden = bool(action.get("hidden", True))
    cwd = action.get("cwd")
    if cwd:
        cwd = os.path.expandvars(os.path.expanduser(str(cwd)))
    wait = bool(action.get("wait", False))
    creation_flags = 0
    if hidden and os.name == "nt":
        creation_flags = subprocess.CREATE_NO_WINDOW
    try:
        process = subprocess.Popen(
            cmd,
            shell=bool(shell),
            cwd=cwd,
            creationflags=creation_flags,
        )
    except OSError as exc:
        raise ActionError(f"не удалось запустить {cmd!r}: {exc}") from None
    if wait:
        code = process.wait()
        log(f"команда завершилась с кодом {code}")


def _do_open(action: dict[str, Any]) -> None:
    target = action.get("target")
    if not target:
        raise ActionError("open: не задано поле 'target'")
    target = os.path.expandvars(os.path.expanduser(str(target)))
    arguments = action.get("args")
    try:
        if arguments:
            args = arguments if isinstance(arguments, (list, tuple)) else [arguments]
            subprocess.Popen([target, *[str(item) for item in args]])
        else:
            os.startfile(target)  # type: ignore[attr-defined]
    except OSError as exc:
        raise ActionError(f"не удалось открыть {target!r}: {exc}") from None


def _do_media(action: dict[str, Any]) -> None:
    name = str(action.get("action", "")).strip().lower()
    key = _MEDIA_KEYS.get(name)
    if not key:
        supported = ", ".join(sorted(_MEDIA_KEYS))
        raise ActionError(f"media: неизвестное действие {name!r}; доступно: {supported}")
    winhook.press_key(winhook.parse_key(key))


def _do_volume(action: dict[str, Any], log: Log) -> None:
    app = action.get("app")
    step = max(1, _to_int(action.get("step", 2), "volume.step"))
    if app:
        app = str(app).strip()
        level = action.get("level")
        if level is not None:
            count = audio.set_app_volume(app, _to_int(level, "volume.level"))
            if count:
                log(f"громкость {app}: {level}%")
            else:
                log(f"нет активной аудиосессии {app!r}")
            return
        delta = _to_int(action.get("delta", 0), "volume.delta")
        if not delta:
            return
        count, current = audio.adjust_app_volume(app, delta * step)
        if count:
            log(f"громкость {app}: {current}%")
        else:
            log(f"нет активной аудиосессии {app!r}")
        return

    level = action.get("level")
    if level is not None:
        try:
            audio.set_master_volume(_to_int(level, "volume.level"))
        except audio.AudioError as exc:
            raise ActionError(f"не удалось изменить системную громкость: {exc}") from None
        return

    delta = _to_int(action.get("delta", 0), "volume.delta")
    if not delta:
        return
    key = "volume up" if delta > 0 else "volume down"
    vk = winhook.parse_key(key)
    interval = max(0, _to_int(action.get("interval_ms", 0), "volume.interval_ms")) / 1000.0
    for _ in range(abs(delta)):
        winhook.press_key(vk)
        if interval:
            time.sleep(interval)


def _do_mute(action: dict[str, Any], log: Log) -> None:
    raw_state = action.get("state", "toggle")
    if isinstance(raw_state, bool):
        state: bool | None = raw_state
    else:
        name = str(raw_state).strip().lower()
        if name in ("toggle", "switch", "", "переключить"):
            state = None
        elif name in ("on", "true", "1", "mute", "вкл"):
            state = True
        elif name in ("off", "false", "0", "unmute", "выкл"):
            state = False
        else:
            raise ActionError(f"mute: неизвестное состояние {raw_state!r}")

    app = action.get("app")
    if app:
        app = str(app).strip()
        try:
            count, current = audio.set_app_mute(app, state)
        except audio.AudioError as exc:
            raise ActionError(f"не удалось переключить mute {app!r}: {exc}") from None
        if count:
            log(f"{app}: {'mute' if current else 'звук включён'}")
        else:
            log(f"нет активной аудиосессии {app!r}")
        return

    if state is None:
        winhook.press_key(winhook.parse_key("volume mute"))
    else:
        try:
            audio.set_master_mute(state)
        except audio.AudioError as exc:
            raise ActionError(f"не удалось переключить системный mute: {exc}") from None


def _do_focus_validate(action: dict[str, Any], where: str) -> list[str]:
    if not str(action.get("title", "")).strip():
        return [f"{where}: focus требует поле 'title'"]
    return []


def validate_action(action: Any, where: str) -> list[str]:
    """Проверяет действие, не выполняя его. Возвращает список ошибок."""
    try:
        action = _normalize(action)
    except ActionError as exc:
        return [f"{where}: {exc}"]
    if action is None:
        return []
    if isinstance(action, (list, tuple)):
        errors: list[str] = []
        for index, item in enumerate(action):
            errors.extend(validate_action(item, f"{where}[{index}]"))
        return errors
    if not isinstance(action, dict):
        return [f"{where}: ожидается объект действия, получено {action!r}"]
    kind = str(action.get("type", "none")).strip().lower()
    if kind in ("", "none", "nothing"):
        return []
    if kind not in _ACTION_TYPES:
        return [f"{where}: неизвестный тип {kind!r}; доступно: {', '.join(_ACTION_TYPES)}"]
    errors = []
    try:
        if kind == "keys":
            raw = action.get("keys")
            combos = [raw] if isinstance(raw, str) else list(raw or [])
            if not combos:
                errors.append(f"{where}: keys требует поле 'keys'")
            for combo in combos:
                try:
                    for part in str(combo).split("+"):
                        if part.strip():
                            winhook.parse_key(part)
                except ValueError as exc:
                    errors.append(f"{where}: {exc}")
        elif kind == "text":
            if not isinstance(action.get("text"), str):
                errors.append(f"{where}: text требует строковое поле 'text'")
        elif kind == "command":
            if not action.get("cmd"):
                errors.append(f"{where}: command требует поле 'cmd'")
        elif kind == "open":
            if not action.get("target"):
                errors.append(f"{where}: open требует поле 'target'")
        elif kind == "url":
            if not action.get("url"):
                errors.append(f"{where}: url требует поле 'url'")
        elif kind == "media":
            name = str(action.get("action", "")).strip().lower()
            if name not in _MEDIA_KEYS:
                errors.append(
                    f"{where}: неизвестное media-действие {name!r}; "
                    f"доступно: {', '.join(sorted(_MEDIA_KEYS))}"
                )
        elif kind == "volume":
            app = action.get("app")
            if app is not None and not str(app).strip():
                errors.append(f"{where}: volume.app не может быть пустым")
            step = _to_int(action.get("step", 2), "volume.step")
            if not 1 <= step <= 50:
                errors.append(f"{where}: volume.step должен быть от 1 до 50")
            if action.get("level") is not None:
                level = _to_int(action.get("level"), "volume.level")
                if not 0 <= level <= 100:
                    errors.append(f"{where}: volume.level должен быть от 0 до 100")
            _to_int(action.get("delta", 0), "volume.delta")
        elif kind == "mute":
            app = action.get("app")
            if app is not None and not str(app).strip():
                errors.append(f"{where}: mute.app не может быть пустым")
            state = action.get("state", "toggle")
            if not isinstance(state, bool):
                if str(state).strip().lower() not in (
                    "toggle",
                    "switch",
                    "on",
                    "true",
                    "1",
                    "mute",
                    "off",
                    "false",
                    "0",
                    "unmute",
                ):
                    errors.append(f"{where}: неизвестное mute.state {state!r}")
        elif kind == "wheel":
            _to_int(action.get("delta", 0), "wheel.delta")
        elif kind == "focus":
            errors.extend(_do_focus_validate(action, where))
        elif kind == "sleep":
            _to_int(action.get("ms", 0), "sleep.ms")
    except ActionError as exc:
        errors.append(f"{where}: {exc}")
    return errors


def describe(action: Any) -> str:
    """Короткое описание действия для лога."""
    action = _normalize(action)
    if action is None:
        return "ничего"
    if isinstance(action, (list, tuple)):
        return " + ".join(describe(item) for item in action)
    if not isinstance(action, dict):
        return repr(action)
    kind = str(action.get("type", "none")).strip().lower()
    if kind == "keys":
        return f"клавиши {action.get('keys')!r}"
    if kind == "text":
        text = str(action.get("text", ""))
        return f"текст {text[:30]!r}"
    if kind == "command":
        return f"команда {action.get('cmd')!r}"
    if kind == "open":
        return f"открыть {action.get('target')!r}"
    if kind == "url":
        return f"URL {action.get('url')!r}"
    if kind == "media":
        return f"медиа {action.get('action')!r}"
    if kind == "volume":
        app = action.get("app")
        suffix = f" для {app}" if app else ""
        if action.get("level") is not None:
            return f"громкость{suffix} = {action.get('level')}%"
        return f"громкость{suffix} {action.get('delta', 0):+}×{action.get('step', 2)}%"
    if kind == "mute":
        app = action.get("app")
        return f"mute {app or 'системы'}"
    if kind == "wheel":
        return f"колесо {action.get('delta')!r}"
    if kind == "focus":
        return f"окно {action.get('title')!r}"
    if kind == "sleep":
        return f"пауза {action.get('ms')} мс"
    return kind or "ничего"
