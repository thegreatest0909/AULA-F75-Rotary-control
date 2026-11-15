#!/usr/bin/env python3
"""AULA F75 Knob — нативное приложение с прежним тёмным «стеклянным» меню.

Интерфейс — тот же HTML/CSS, что и раньше, но он открывается во встроенном
окне WebView2 (pywebview), а не в браузере: никакого локального HTTP-сервера,
портов и адресов. Обмен с Python идёт через встроенный мост pywebview.

Запуск: python app.py [--minimized] [--smoke] [--stop]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    import webview
except Exception:  # noqa: BLE001 - понятное сообщение вместо стектрейса
    print(
        "Для запуска нужен пакет pywebview: python -m pip install pywebview",
        file=sys.stderr,
    )
    raise SystemExit(1) from None

import audio
import core
import winhook

APP_NAME = "AULA F75 Knob"
APP_VERSION = "1.0.1"
REPO_URL = "https://github.com/thegreatest0909/AULA-F75-Rotary-control"
FROZEN = bool(getattr(sys, "frozen", False))
BASE_DIR = Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))

DATA_DIR = (Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME) if FROZEN else BASE_DIR
CONFIG_PATH = DATA_DIR / "config.json"
UI_INDEX = RESOURCE_DIR / "ui" / "index.html"
APP_MUTEX_NAME = r"Local\AulaF75KnobApp.Mutex"
APP_QUIT_EVENT = r"Local\AulaF75KnobApp.Quit"


# --------------------------------------------------------------------- подготовка


def ensure_config() -> None:
    if CONFIG_PATH.exists():
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    template = RESOURCE_DIR / "config.json"
    data: Any = None
    if template.exists() and template.resolve() != CONFIG_PATH.resolve():
        try:
            data = json.loads(template.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            data = None
    if not isinstance(data, dict):
        from aula_knob import DEFAULT_CONFIG

        data = json.loads(json.dumps(DEFAULT_CONFIG))
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def message_box(text: str, title: str = APP_NAME) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, text, title, 0x00000040)
    except Exception:  # noqa: BLE001
        print(f"{title}: {text}")


# --------------------------------------------------------------------- мост JS ↔ Python


class Api:
    """Методы, доступные из JS через window.pywebview.api.*"""

    def __init__(self) -> None:
        self._core = core.Core(CONFIG_PATH)
        self._app: "Application | None" = None

    # ---------------------------------------------------------------- dispatch

    def request(self, method: str | None, path: str | None, body: Any) -> dict[str, Any]:
        try:
            return self._dispatch((method or "GET").upper(), path or "/", body)
        except core.ApiError as exc:
            return {"ok": False, "error": str(exc)}
        except audio.AudioError as exc:
            return {"ok": False, "error": f"Аудио: {exc}"}
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return {"ok": False, "error": str(exc)}

    def _dispatch(self, method: str, path: str, body: Any) -> dict[str, Any]:
        parsed = urlparse(path)
        route = parsed.path
        query = parse_qs(parsed.query)
        data = body if isinstance(body, dict) else {}

        if route == "/api/state":
            return self._core.state()
        if route == "/api/audio":
            return self._core.audio_state()
        if route == "/api/log":
            return self._core.journal.since(int(query.get("since", ["0"])[0] or 0))
        if route == "/api/config":
            return self._core.save_config(data)
        if route == "/api/target":
            return self._core.set_target(
                str(data.get("app") or ""),
                int(data.get("step", 2) or 2),
                bool(data.get("mute_on_press", False)),
            )
        if route == "/api/test":
            return self._core.test_binding(str(data.get("binding", "")))
        if route == "/api/app-volume":
            app = str(data.get("app", "")).strip()
            if not app:
                raise core.ApiError("Не указано приложение")
            count = audio.set_app_volume(app, max(0, min(100, int(data.get("level", 0)))))
            if not count:
                raise core.ApiError(f"Нет активной аудиосессии {app!r}")
            return {"ok": True, "count": count}
        if route == "/api/app-mute":
            app = str(data.get("app", "")).strip()
            if not app:
                raise core.ApiError("Не указано приложение")
            state = data.get("state")
            count, current = audio.set_app_mute(app, None if state is None else bool(state))
            if not count:
                raise core.ApiError(f"Нет активной аудиосессии {app!r}")
            return {"ok": True, "mute": current}
        if route == "/api/master-volume":
            audio.set_master_volume(max(0, min(100, int(data.get("level", 0)))))
            return {"ok": True}
        if route == "/api/master-mute":
            state = data.get("state")
            current = audio.set_master_mute(None if state is None else bool(state))
            return {"ok": True, "mute": current}
        if route == "/api/service":
            action = str(data.get("action", "")).strip()
            if action == "start":
                self._core.service.start()
            elif action == "stop":
                self._core.service.stop()
            else:
                raise core.ApiError("Неизвестное действие сервиса")
            return {"ok": True, "service": self._core.service.status()}
        if route == "/api/ui":
            return self._core.apply_ui(data)
        if route == "/api/wallpaper/clear":
            return self._core.clear_wallpaper()
        if route == "/api/wallpaper/desktop":
            return self._core.desktop_wallpaper()
        if route == "/api/quit":
            if self._app is not None:
                self._app.request_quit()
            return {"ok": True}
        raise core.ApiError("Не найдено", 404)

    # -------------------------------------------------------------- wallpaper

    def upload_wallpaper(self, filename: str | None, b64: str | None) -> dict[str, Any]:
        try:
            body = base64.b64decode(b64 or "")
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "Некорректные данные файла"}
        try:
            return self._core.upload_wallpaper(body)
        except core.ApiError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------- приложение


class Application:
    def __init__(self, api: Api, minimized: bool = False, smoke: bool = False) -> None:
        self.api = api
        api._app = self
        self.smoke = smoke
        self._quitting = False
        self.tray: Any = None
        if not UI_INDEX.exists():
            raise RuntimeError(f"Не найдена страница интерфейса: {UI_INDEX}")
        self.window = webview.create_window(
            APP_NAME,
            url=UI_INDEX.as_posix(),
            js_api=api,
            width=1180,
            height=800,
            min_size=(960, 640),
            background_color="#0b0e14",
            hidden=minimized,
            easy_drag=False,
        )
        self.window.events.closing += self._on_closing

    # ---------------------------------------------------------------- lifecycle

    def run(self) -> None:
        webview.start(self._on_start, debug=False)
        if self.smoke:
            print("SMOKE OK")

    def _on_start(self) -> None:
        try:
            self.api._core.service.start()
        except Exception as exc:  # noqa: BLE001
            self.api._core.journal.info(f"Сервис не запущен: {exc}")
        self._build_tray()
        threading.Thread(target=self._watch_quit_event, name="quit-watch", daemon=True).start()
        if self.smoke:
            threading.Thread(target=self._smoke_quit, name="smoke-quit", daemon=True).start()

    def _smoke_quit(self) -> None:
        time.sleep(3.0)
        self.request_quit()

    def _watch_quit_event(self) -> None:
        try:
            event = winhook.create_event(APP_QUIT_EVENT)
        except Exception:  # noqa: BLE001
            return
        try:
            while not self._quitting:
                if winhook.event_is_set(event):
                    self.request_quit()
                    return
                time.sleep(0.2)
        finally:
            winhook.close_handle(event)

    def _on_closing(self) -> bool:
        if self._quitting:
            return True
        self.hide()
        return False

    def request_quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        try:
            self.api._core.service.stop()
        except Exception:  # noqa: BLE001
            pass
        if self.tray is not None:
            try:
                self.tray.stop()
                self.tray = None
            except Exception:  # noqa: BLE001
                pass
        threading.Timer(0.25, self._destroy_window).start()

    def _destroy_window(self) -> None:
        try:
            self.window.destroy()
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- window

    def show(self) -> None:
        try:
            self.window.show()
            self.window.restore()
        except Exception:  # noqa: BLE001
            try:
                self.window.restore()
            except Exception:  # noqa: BLE001
                pass

    def hide(self) -> None:
        try:
            self.window.hide()
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- tray

    def _build_tray(self) -> None:
        try:
            import pystray
            from PIL import Image
        except Exception:  # noqa: BLE001
            self.api._core.journal.info("pystray/Pillow не установлены — работаю без трея")
            return
        icon_path = None
        for candidate in (RESOURCE_DIR / "assets" / "icon.png", RESOURCE_DIR / "assets" / "icon.ico"):
            if candidate.exists():
                icon_path = candidate
                break
        if icon_path is None:
            return
        try:
            image = Image.open(icon_path)
            menu = pystray.Menu(
                pystray.MenuItem("Открыть", lambda: self.show(), default=True),
                pystray.MenuItem("Скрыть", lambda: self.hide()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выход", lambda: self.request_quit()),
            )
            self.tray = pystray.Icon("aula-f75-knob", image, APP_NAME, menu)
            threading.Thread(target=self.tray.run, name="tray", daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            self.api._core.journal.info(f"Трей недоступен: {exc}")
            self.tray = None


# --------------------------------------------------------------------- запуск


def stop_running() -> bool:
    try:
        handle = winhook.create_event(APP_QUIT_EVENT)
    except Exception:  # noqa: BLE001
        return False
    try:
        return winhook.set_event(handle)
    finally:
        winhook.close_handle(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app", description=APP_NAME)
    parser.add_argument("--minimized", action="store_true", help="запуститься свёрнутым в трей")
    parser.add_argument("--smoke", action="store_true", help="открыть окно и закрыть через 3 секунды")
    parser.add_argument("--stop", action="store_true", help="остановить запущенное приложение")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    args = parser.parse_args(argv)

    if args.stop:
        if stop_running():
            print("Сигнал остановки отправлен.")
            return 0
        print("Приложение не запущено.")
        return 1

    ensure_config()

    mutex = winhook.create_mutex(APP_MUTEX_NAME)
    if mutex is None:
        if args.smoke:
            print("SMOKE SKIP: already running")
            return 0
        message_box("Приложение уже запущено. Окно можно открыть из трея.")
        return 0

    try:
        api = Api()
        application = Application(api, minimized=args.minimized, smoke=args.smoke)
        application.run()
    finally:
        winhook.close_handle(mutex)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
