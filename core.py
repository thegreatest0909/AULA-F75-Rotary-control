"""Ядро приложения AULA F75 Knob: состояние, сервис ручки, громкость, обои.

Без HTTP и браузера: этим модулем пользуется нативное окно (app.py).
Пути к конфигу, настройкам интерфейса и обоям берутся из папки рядом с
config.json — так установленная программа хранит их в %APPDATA%.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any

if __import__("sys").platform != "win32":
    raise SystemExit("AULA F75 Knob поддерживает только Windows.")

import actions
import audio
import winhook
from aula_knob import (
    BINDING_NAMES,
    DEFAULT_CONFIG,
    ConfigError,
    Controller,
    Settings,
)

DEFAULT_UI = {
    "wallpaper": None,
    "dim": 42,
    "blur": 0,
    "accent": "violet",
    "close_to_tray": True,
}
ACCENTS = ("violet", "cyan", "lime", "rose", "amber")

MAX_BODY = 30 * 1024 * 1024

_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)

_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _as_apps(value: Any) -> list[str]:
    """Приводит app/apps к списку имён приложений."""
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _action_apps(action: Any) -> list[str]:
    """Список приложений действия: apps (список) или app (одно)."""
    if not isinstance(action, dict):
        return []
    apps = _as_apps(action.get("apps"))
    if apps:
        return apps
    return _as_apps(action.get("app"))


def _mute_action(apps: list[str], state: Any = "toggle") -> dict[str, Any]:
    """Действие mute для одного или нескольких приложений."""
    action: dict[str, Any] = {"type": "mute", "state": state}
    if len(apps) == 1:
        action["app"] = apps[0]
    elif len(apps) > 1:
        action["apps"] = list(apps)
    return action


def _is_system_media_mute(action: Any) -> bool:
    """Системный медиа-mute (выключает звук всей Windows)."""
    return (
        isinstance(action, dict)
        and action.get("type") == "media"
        and str(action.get("action", "")).strip().lower() in ("mute", "mute_toggle")
    )


def _image_ext(data: bytes) -> str | None:
    for magic, ext in _IMAGE_MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


class Journal:
    def __init__(self, limit: int = 800) -> None:
        self._items: deque[tuple[int, str]] = deque(maxlen=limit)
        self._seq = 0
        self._lock = threading.Lock()

    def append(self, text: str) -> None:
        with self._lock:
            self._seq += 1
            self._items.append((self._seq, text))

    def info(self, text: str) -> None:
        self.append(f"{time.strftime('%H:%M:%S')}  {text}")

    def since(self, cursor: int) -> dict[str, Any]:
        with self._lock:
            lines = [{"seq": seq, "text": text} for seq, text in self._items if seq > cursor]
            return {"cursor": self._seq, "lines": lines}


class ServiceHost:
    """Сервис ручки в фоновом потоке приложения."""

    def __init__(self, config_path: Path, journal: Journal) -> None:
        self.config_path = config_path
        self.journal = journal
        self._thread: threading.Thread | None = None
        self._controller: Controller | None = None
        self._lock = threading.Lock()
        self._running = False
        self._error: str | None = None

    @property
    def running(self) -> bool:
        return self._running

    def status(self) -> dict[str, Any]:
        external = (not self._running) and winhook.mutex_exists("Local\\AulaF75Knob.Mutex")
        return {"running": self._running, "external": external, "error": self._error}

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            if winhook.mutex_exists("Local\\AulaF75Knob.Mutex"):
                raise RuntimeError("Ручка уже обслуживается другим процессом")
            self._error = None
            self._running = True
            self._thread = threading.Thread(target=self._run, name="aula-app-service", daemon=True)
            self._thread.start()
        self.journal.info("Сервис ручки запускается")

    def _run(self) -> None:
        try:
            settings = Settings.load(self.config_path)
            controller = Controller(
                self.config_path, settings, verbose=False, log_sink=self.journal.append
            )
            self._controller = controller
            code = controller.run()
            if code != 0:
                self._error = "Сервис не смог запуститься, подробности в журнале"
        except Exception as exc:  # noqa: BLE001
            self._error = str(exc)
            self.journal.info(f"Ошибка сервиса: {exc}")
        finally:
            self._controller = None
            self._running = False

    def stop(self) -> None:
        controller = self._controller
        if controller is not None:
            controller.request_stop()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=4.0)
        self.journal.info("Сервис ручки остановлен")


class Core:
    """Всё состояние приложения без привязки к UI."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.data_dir = config_path.parent
        self.wallpaper_dir = self.data_dir / "wallpapers"
        self.ui_path = self.data_dir / "ui.json"
        self.journal = Journal()
        self.service = ServiceHost(config_path, self.journal)
        self.wallpaper_dir.mkdir(parents=True, exist_ok=True)
        self.ui = self._load_ui()
        self._started_at = int(time.time())
        self._migrate_hold()

    def _migrate_hold(self) -> None:
        """Удержание не должно выключать звук всей системы.

        Если ручка уже управляет приложениями, системный медиа-mute на
        удержании заменяется на mute этих приложений (один раз для старых
        конфигов, дальше это делает set_target).
        """
        config = _load_json(self.config_path, {})
        if not isinstance(config, dict):
            return
        bindings = config.get("bindings")
        if not isinstance(bindings, dict):
            return
        apps = _action_apps(bindings.get("rotate_right")) or _action_apps(
            bindings.get("rotate_left")
        )
        if not apps or not _is_system_media_mute(bindings.get("hold")):
            return
        bindings["hold"] = _mute_action(apps)
        self.ui["hold_follows_target"] = True
        self._save_ui()
        _write_json(self.config_path, config)
        self.journal.info("Удержание перенастроено на mute выбранных приложений")

    # ------------------------------------------------------------------ ui state

    def _load_ui(self) -> dict[str, Any]:
        data = _load_json(self.ui_path, {})
        result = dict(DEFAULT_UI)
        if isinstance(data, dict):
            if isinstance(data.get("wallpaper"), str):
                candidate = self.wallpaper_dir / Path(data["wallpaper"]).name
                if candidate.is_file():
                    result["wallpaper"] = candidate.name
            for key in ("dim", "blur"):
                value = data.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    result[key] = max(0, min(90, value))
            if data.get("accent") in ACCENTS:
                result["accent"] = data["accent"]
            if isinstance(data.get("close_to_tray"), bool):
                result["close_to_tray"] = data["close_to_tray"]
            if isinstance(data.get("hold_follows_target"), bool):
                result["hold_follows_target"] = data["hold_follows_target"]
            if isinstance(data.get("press_follows_target"), bool):
                result["press_follows_target"] = data["press_follows_target"]
            if isinstance(data.get("press_backup"), (dict, list)):
                result["press_backup"] = data["press_backup"]
        return result

    def _save_ui(self) -> None:
        _write_json(self.ui_path, self.ui)

    def wallpaper_data_url(self) -> str | None:
        name = self.ui.get("wallpaper")
        if not name:
            return None
        path = self.wallpaper_dir / Path(name).name
        if not path.is_file():
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        mime = _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"

    # ------------------------------------------------------------------ state

    def state(self) -> dict[str, Any]:
        config = _load_json(self.config_path, {})
        return {
            "ok": True,
            "config": config,
            "config_path": str(self.config_path),
            "ui": self.ui,
            "wallpaper_url": self.wallpaper_data_url(),
            "service": self.service.status(),
            "bindings": list(BINDING_NAMES),
            "default_config": DEFAULT_CONFIG,
        }

    def audio_state(self) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": True, "apps": [], "master": None}
        try:
            result["apps"] = audio.list_apps()
        except Exception as exc:  # noqa: BLE001
            result["audio_error"] = str(exc)
        try:
            volume, mute = audio.get_master_volume()
            result["master"] = {"volume": volume, "mute": mute}
        except Exception as exc:  # noqa: BLE001
            result.setdefault("audio_error", str(exc))
        return result

    # ------------------------------------------------------------------ config

    def _validate_config(self, data: Any) -> Settings:
        try:
            return Settings.from_dict(data)
        except ConfigError as exc:
            raise ApiError(str(exc)) from None

    def save_config(self, data: Any) -> dict[str, Any]:
        self._validate_config(data)
        _write_json(self.config_path, data)
        self.journal.info("Конфиг сохранён")
        return {"ok": True, "config": data}

    def set_target(self, apps: Any, step: int = 2, mute_on_press: bool = False) -> dict[str, Any]:
        items = _as_apps(apps)
        step = max(1, min(50, int(step)))
        config = _load_json(self.config_path, {})
        if not isinstance(config, dict):
            raise ApiError("config.json повреждён")
        bindings = config.setdefault("bindings", {})
        if not isinstance(bindings, dict):
            raise ApiError("config.json: bindings должен быть объектом")

        def volume_action(delta: int) -> dict[str, Any]:
            action: dict[str, Any] = {"type": "volume", "delta": delta, "step": step}
            if len(items) == 1:
                action["app"] = items[0]
            elif len(items) > 1:
                action["apps"] = list(items)
            return action

        bindings["rotate_left"] = volume_action(-1)
        bindings["rotate_right"] = volume_action(1)
        if items:
            hold = bindings.get("hold")
            follows = self.ui.get("hold_follows_target")
            if _is_system_media_mute(hold) or (
                follows and isinstance(hold, dict) and hold.get("type") == "mute"
            ):
                # Удержание не должно выключать звук всей системы.
                bindings["hold"] = _mute_action(items)
                self.ui["hold_follows_target"] = True
                self._save_ui()
        elif self.ui.get("hold_follows_target"):
            hold = bindings.get("hold")
            if isinstance(hold, dict) and hold.get("type") == "mute" and _action_apps(hold):
                bindings["hold"] = {"type": "media", "action": "mute"}
            self.ui.pop("hold_follows_target", None)
            self._save_ui()
        if mute_on_press:
            current_press = bindings.get("press")
            if not self.ui.get("press_follows_target"):
                self.ui["press_backup"] = current_press or DEFAULT_CONFIG["bindings"]["press"]
                self.ui["press_follows_target"] = True
                self._save_ui()
            bindings["press"] = _mute_action(items)
        elif self.ui.get("press_follows_target"):
            backup = self.ui.pop("press_backup", None)
            bindings["press"] = backup or DEFAULT_CONFIG["bindings"]["press"]
            self.ui.pop("press_follows_target", None)
            self._save_ui()
        self._validate_config(config)
        _write_json(self.config_path, config)
        name = ", ".join(items) or "системы"
        self.journal.info(f"Ручка теперь меняет громкость: {name}")
        return {"ok": True, "config": config}

    def set_targets(self, targets: Any) -> dict[str, Any]:
        if not isinstance(targets, list):
            raise ApiError("targets должен быть списком")
        config = _load_json(self.config_path, {})
        if not isinstance(config, dict):
            raise ApiError("config.json повреждён")
        config["targets"] = targets
        self._validate_config(config)
        _write_json(self.config_path, config)
        self.journal.info(f"Наборов целей: {len(targets)}")
        return {"ok": True, "config": config, "targets": config.get("targets", [])}

    def test_binding(self, name: str) -> dict[str, Any]:
        if name not in BINDING_NAMES:
            raise ApiError(f"Неизвестная привязка: {name}")
        try:
            settings = Settings.load(self.config_path)
        except ConfigError as exc:
            raise ApiError(str(exc)) from None
        action = settings.bindings.get(name)
        if action is None:
            raise ApiError(f"Привязка {name} не задана")

        def run() -> None:
            try:
                actions.execute(action, log=self.journal.append)
            except Exception as exc:  # noqa: BLE001
                self.journal.info(f"Ошибка теста {name}: {exc}")

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}

    # ------------------------------------------------------------------ ui actions

    def apply_ui(self, data: dict[str, Any]) -> dict[str, Any]:
        if "dim" in data:
            self.ui["dim"] = max(0, min(90, int(data["dim"])))
        if "blur" in data:
            self.ui["blur"] = max(0, min(30, int(data["blur"])))
        if data.get("accent") in ACCENTS:
            self.ui["accent"] = data["accent"]
        if isinstance(data.get("close_to_tray"), bool):
            self.ui["close_to_tray"] = data["close_to_tray"]
        self._save_ui()
        return {"ok": True, "ui": self.ui}

    def upload_wallpaper(self, body: bytes) -> dict[str, Any]:
        if not body:
            raise ApiError("Пустой файл")
        if len(body) > MAX_BODY:
            raise ApiError("Файл больше 30 МБ")
        ext = _image_ext(body)
        if ext is None:
            raise ApiError("Поддерживаются PNG, JPG, WEBP, GIF и BMP")
        for old in self.wallpaper_dir.glob("bg.*"):
            try:
                old.unlink()
            except OSError:
                pass
        path = self.wallpaper_dir / f"bg{ext}"
        path.write_bytes(body)
        self.ui["wallpaper"] = path.name
        self._save_ui()
        self.journal.info("Обои обновлены")
        return {"ok": True, "url": self.wallpaper_data_url(), "ui": self.ui}

    def clear_wallpaper(self) -> dict[str, Any]:
        for old in self.wallpaper_dir.glob("bg.*"):
            try:
                old.unlink()
            except OSError:
                pass
        self.ui["wallpaper"] = None
        self._save_ui()
        return {"ok": True, "ui": self.ui}

    def desktop_wallpaper(self) -> dict[str, Any]:
        name = self.ui.get("wallpaper")
        if not name:
            raise ApiError("Сначала выберите обои для меню")
        path = self.wallpaper_dir / Path(name).name
        if not path.is_file():
            raise ApiError("Файл обоев не найден")
        try:
            winhook.set_desktop_wallpaper(str(path))
        except OSError as exc:
            raise ApiError(f"Не удалось сменить обои рабочего стола: {exc}") from None
        self.journal.info("Обои поставлены на рабочий стол")
        return {"ok": True}
