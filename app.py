#!/usr/bin/env python3
"""AULA F75 Knob — нативное приложение с прежним тёмным «стеклянным» меню.

Интерфейс — тот же HTML/CSS, что и раньше, но он открывается во встроенном
окне WebView2 (pywebview), а не в браузере: никакого локального HTTP-сервера,
портов и адресов. Обмен с Python идёт через встроенный мост pywebview.

Запуск: python app.py [--minimized] [--smoke] [--stop]
"""

from __future__ import annotations

import argparse
import winreg
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
APP_VERSION = "1.0.6"
REPO_URL = "https://github.com/thegreatest0909/AULA-F75-Rotary-control"
FROZEN = bool(getattr(sys, "frozen", False))
BASE_DIR = Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))

DATA_DIR = (Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME) if FROZEN else BASE_DIR
CONFIG_PATH = DATA_DIR / "config.json"
UI_INDEX = RESOURCE_DIR / "ui" / "index.html"
APP_MUTEX_NAME = r"Local\AulaF75KnobApp.Mutex"
APP_QUIT_EVENT = r"Local\AulaF75KnobApp.Quit"
APP_SHOW_EVENT = r"Local\AulaF75KnobApp.Show"


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


def _reg_value(key: Any, name: str, default: Any) -> Any:
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return default


def promote_tray_icon() -> bool:
    """Разрешает показ значка в трее (Windows 11 по умолчанию скрывает новые)."""
    try:
        root = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Control Panel\NotifyIconSettings",
            0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        )
    except OSError:
        return False
    changed = False
    try:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            try:
                sub = winreg.OpenKey(root, name, 0, winreg.KEY_READ | winreg.KEY_WRITE)
            except OSError:
                continue
            try:
                tooltip = _reg_value(sub, "InitialTooltip", "")
                executable = str(_reg_value(sub, "ExecutablePath", "")).lower()
                if tooltip != APP_NAME and "aula f75 knob" not in executable:
                    continue
                if _reg_value(sub, "IsPromoted", 0) != 1:
                    winreg.SetValueEx(sub, "IsPromoted", 0, winreg.REG_DWORD, 1)
                    changed = True
            finally:
                winreg.CloseKey(sub)
    finally:
        winreg.CloseKey(root)
    return changed

STARTUP_LINK_NAME = "AULA F75 Knob.lnk"


def startup_link() -> Path:
    appdata = Path(os.environ.get("APPDATA", str(Path.home())))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / STARTUP_LINK_NAME


def autostart_enabled() -> bool:
    try:
        return startup_link().exists()
    except OSError:
        return False


def _startup_target(minimized: bool) -> tuple[str, str, str]:
    if FROZEN:
        return (
            sys.executable,
            "--minimized" if minimized else "",
            str(Path(sys.executable).parent),
        )
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    target = str(pythonw if pythonw.exists() else sys.executable)
    arguments = f'"{BASE_DIR / "app.py"}"' + (" --minimized" if minimized else "")
    return target, arguments, str(BASE_DIR)


def set_autostart(enabled: bool, minimized: bool = True) -> None:
    link = startup_link()
    if not enabled:
        try:
            link.unlink()
        except FileNotFoundError:
            pass
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    target, arguments, workdir = _startup_target(minimized)
    import subprocess

    script = (
        f'$s=(New-Object -ComObject WScript.Shell).CreateShortcut("{link}");'
        f"$s.TargetPath='{target}';$s.Arguments='{arguments}';"
        f"$s.WorkingDirectory='{workdir}';$s.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        capture_output=True,
    )


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
            apps = data.get("apps")
            if apps is None:
                apps = data.get("app") or []
            return self._core.set_target(
                apps,
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
        if route == "/api/wallpaper/builtin":
            return self._core.set_builtin_wallpaper(data.get("file"))
        if route == "/api/wallpaper/desktop":
            return self._core.desktop_wallpaper()
        if route == "/api/targets":
            if (method or "GET").upper() == "POST":
                return self._core.set_targets(data.get("targets", []))
            config = self._core.state().get("config") or {}
            return {"ok": True, "targets": config.get("targets", [])}
        if route == "/api/settings":
            if self._app is None:
                raise core.ApiError("Приложение ещё не готово")
            if (method or "GET").upper() == "POST":
                return {"ok": True, "settings": self._app.apply_settings(data)}
            return {"ok": True, "settings": self._app.get_settings()}
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

    def set_builtin_wallpaper(self, name: str | None) -> dict[str, Any]:
        try:
            return self._core.set_builtin_wallpaper(name)
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
        threading.Thread(target=self._watch_signals, name="app-watch", daemon=True).start()
        if self.smoke:
            threading.Thread(target=self._smoke_quit, name="smoke-quit", daemon=True).start()

    def _smoke_quit(self) -> None:
        time.sleep(3.0)
        self.request_quit()

    def _watch_signals(self) -> None:
        """Следит за сигналами: показать окно (второй запуск) или выйти."""
        try:
            quit_event = winhook.create_event(APP_QUIT_EVENT)
            show_event = winhook.create_event(APP_SHOW_EVENT)
        except Exception:  # noqa: BLE001
            return
        try:
            while not self._quitting:
                if winhook.event_is_set(quit_event):
                    self.request_quit()
                    return
                if winhook.event_is_set(show_event):
                    winhook.reset_event(show_event)
                    self.show()
                time.sleep(0.15)
        finally:
            winhook.close_handle(quit_event)
            winhook.close_handle(show_event)

    def _on_closing(self) -> bool:
        if self._quitting:
            return True
        if bool(self.api._core.ui.get("close_to_tray", True)):
            self.hide()
            return False
        self._shutdown_services()
        return True

    def _shutdown_services(self) -> None:
        self._quitting = True
        try:
            self.api._core.service.stop()
        except Exception:  # noqa: BLE001
            pass
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:  # noqa: BLE001
                pass
            self.tray = None

    def request_quit(self) -> None:
        if self._quitting:
            return
        self._shutdown_services()
        threading.Timer(0.25, self._destroy_window).start()

    def _destroy_window(self) -> None:
        try:
            self.window.destroy()
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- settings

    def get_settings(self) -> dict[str, Any]:
        ui = self.api._core.ui
        config = self.api._core.state().get("config") or {}
        knob = config.get("knob", {}) if isinstance(config, dict) else {}
        return {
            "autostart": autostart_enabled(),
            "start_minimized": bool(ui.get("start_minimized", True)),
            "close_to_tray": bool(ui.get("close_to_tray", True)),
            "switch_modifier": str(knob.get("switch_modifier", "ctrl+alt")),
            "version": APP_VERSION,
        }

    def apply_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        ui = self.api._core.ui
        if "start_minimized" in data:
            ui["start_minimized"] = bool(data["start_minimized"])
        if "close_to_tray" in data:
            ui["close_to_tray"] = bool(data["close_to_tray"])
        self.api._core._save_ui()
        if "autostart" in data or "start_minimized" in data:
            enabled = bool(data.get("autostart", autostart_enabled()))
            set_autostart(enabled, bool(ui.get("start_minimized", True)))
        if "switch_modifier" in data:
            config = self.api._core.state().get("config") or {}
            if isinstance(config, dict):
                knob = config.setdefault("knob", {})
                knob["switch_modifier"] = str(data["switch_modifier"])
                self.api._core.save_config(config)
        return self.get_settings()

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
            self._tray_image = Image.open(icon_path)
        except Exception as exc:  # noqa: BLE001
            self.api._core.journal.info(f"Иконка трея недоступна: {exc}")
            return
        self._tray_pystray = pystray
        self._start_tray()
        # Windows 11 по умолчанию прячет новые значки в «скрытые». Включаем показ.
        threading.Thread(target=self._promote_tray, name="tray-promote", daemon=True).start()

    def _start_tray(self) -> None:
        if self._tray_image is None or self._tray_pystray is None:
            return
        try:
            menu = self._tray_pystray.Menu(
                self._tray_pystray.MenuItem("Открыть", lambda _i, _it: self.show(), default=True),
                self._tray_pystray.MenuItem("Скрыть", lambda _i, _it: self.hide()),
                self._tray_pystray.Menu.SEPARATOR,
                self._tray_pystray.MenuItem("Выход", lambda _i, _it: self.request_quit()),
            )
            self.tray = self._tray_pystray.Icon("aula-f75-knob", self._tray_image, APP_NAME, menu)
            threading.Thread(target=self.tray.run, name="tray", daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            self.api._core.journal.info(f"Трей недоступен: {exc}")
            self.tray = None

    def _promote_tray(self) -> None:
        """Делает значок видимым в трее Windows 11 (иначе он в «скрытых»)."""
        changed = False
        # Windows может создать запись о значке с задержкой — ждём до ~25 секунд.
        for _ in range(25):
            time.sleep(1.0)
            if self._quitting:
                return
            try:
                changed = promote_tray_icon()
            except Exception:  # noqa: BLE001
                continue
            if changed:
                break
        if not changed or self._quitting:
            return
        # Пересоздаём значок, чтобы Проводник перечитал настройку видимости.
        time.sleep(0.3)
        old = self.tray
        self.tray = None
        if old is not None:
            try:
                old.stop()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.2)
        self._start_tray()


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


def show_running() -> bool:
    """Просит уже запущенный экземпляр показать окно."""
    try:
        handle = winhook.create_event(APP_SHOW_EVENT)
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
        show_running()
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
