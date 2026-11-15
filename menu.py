#!/usr/bin/env python3
"""Красивое меню AULA F75 Knob: привязки, громкость приложений, обои.

Запускает локальный веб-интерфейс (127.0.0.1) и встроенный сервис ручки.
Открывается отдельным окном Edge/Chrome в режиме приложения, без адресной строки.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

if sys.platform != "win32":
    sys.exit("AULA F75 Knob поддерживает только Windows.")

import actions
import audio
import winhook
from aula_knob import (
    APP_NAME,
    BINDING_NAMES,
    DEFAULT_CONFIG,
    MUTEX_NAME,
    QUIT_EVENT_NAME,
    ConfigError,
    Controller,
    Settings,
)

BASE_DIR = Path(__file__).resolve().parent
UI_DIR = BASE_DIR / "ui"
WALLPAPER_DIR = BASE_DIR / "wallpapers"
UI_STATE_PATH = BASE_DIR / "ui.json"
MENU_QUIT_NAME = r"Local\AulaF75Menu.Quit"
MENU_MUTEX_NAME = r"Local\AulaF75Menu"

DEFAULT_UI = {
    "wallpaper": None,
    "dim": 42,
    "blur": 0,
    "accent": "violet",
}
ACCENTS = ("violet", "cyan", "lime", "rose", "amber")

MAX_BODY = 30 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class Journal:
    def __init__(self, limit: int = 600) -> None:
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
            lines = [
                {"seq": seq, "text": text}
                for seq, text in self._items
                if seq > cursor
            ]
            return {"cursor": self._seq, "lines": lines}


class ServiceHost:
    """Встроенный сервис ручки: запуск/остановка в фоновом потоке."""

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
        external = (not self._running) and winhook.mutex_exists(MUTEX_NAME)
        return {
            "running": self._running,
            "external": external,
            "error": self._error,
        }

    def start(self) -> bool:
        with self._lock:
            if self._running or (self._thread and self._thread.is_alive()):
                raise ApiError("Сервис уже запущен")
            if winhook.mutex_exists(MUTEX_NAME):
                raise ApiError(
                    "Ручка уже обслуживается отдельным процессом. "
                    "Остановите его и попробуйте снова."
                )
            self._error = None
            self._running = True
            self._thread = threading.Thread(
                target=self._run, name="aula-menu-service", daemon=True
            )
            self._thread.start()
        self.journal.info("Сервис ручки запускается")
        return True

    def _run(self) -> None:
        try:
            settings = Settings.load(self.config_path)
            controller = Controller(
                self.config_path,
                settings,
                verbose=False,
                log_sink=self.journal.append,
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

    def stop(self) -> bool:
        controller = self._controller
        thread = self._thread
        if controller is not None:
            controller.request_stop()
        if thread is not None:
            thread.join(timeout=4.0)
            if thread.is_alive():
                raise ApiError("Сервис не остановился за 4 секунды")
        if controller is None and winhook.mutex_exists(MUTEX_NAME):
            winhook.signal_event(QUIT_EVENT_NAME)
            for _ in range(30):
                if not winhook.mutex_exists(MUTEX_NAME):
                    break
                time.sleep(0.1)
        self.journal.info("Сервис ручки остановлен")
        return True


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def _image_ext(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    return None


def _open_app_window(url: str) -> None:
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        Path(program_files_x86) / "Microsoft/Edge/Application/msedge.exe",
        Path(program_files) / "Microsoft/Edge/Application/msedge.exe",
        Path(program_files) / "Google/Chrome/Application/chrome.exe",
        Path(program_files_x86) / "Google/Chrome/Application/chrome.exe",
        Path(local_appdata) / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            try:
                subprocess.Popen(
                    [
                        str(candidate),
                        f"--app={url}",
                        "--window-size=1280,860",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ]
                )
                return
            except OSError:
                continue
    webbrowser.open(url, new=2)


class Menu:
    def __init__(self, config_path: Path, verbose: bool = False) -> None:
        self.config_path = config_path
        self.verbose = verbose
        self.journal = Journal()
        self.service = ServiceHost(config_path, self.journal)
        WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
        self.ui = self._load_ui()
        self.server: ThreadingHTTPServer | None = None
        self._started_at = int(time.time())

    # ------------------------------------------------------------------ state

    def _load_ui(self) -> dict[str, Any]:
        data = _load_json(UI_STATE_PATH, {})
        result = dict(DEFAULT_UI)
        if isinstance(data, dict):
            if isinstance(data.get("wallpaper"), str):
                candidate = WALLPAPER_DIR / Path(data["wallpaper"]).name
                if candidate.is_file():
                    result["wallpaper"] = candidate.name
            for key in ("dim", "blur"):
                value = data.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    result[key] = max(0, min(90, value))
            if data.get("accent") in ACCENTS:
                result["accent"] = data["accent"]
        return result

    def _save_ui(self) -> None:
        _write_json(UI_STATE_PATH, self.ui)

    def _wallpaper_url(self) -> str | None:
        name = self.ui.get("wallpaper")
        if not name:
            return None
        path = WALLPAPER_DIR / name
        if not path.is_file():
            return None
        try:
            version = int(path.stat().st_mtime)
        except OSError:
            version = self._started_at
        return f"/wallpaper?v={version}"

    def state(self) -> dict[str, Any]:
        config = _load_json(self.config_path, {})
        return {
            "ok": True,
            "config": config,
            "config_path": str(self.config_path),
            "ui": self.ui,
            "wallpaper_url": self._wallpaper_url(),
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

    # ------------------------------------------------------------------ actions

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

    def set_target(
        self,
        app: str | None,
        step: int = 2,
        mute_on_press: bool = False,
    ) -> dict[str, Any]:
        app = (app or "").strip()
        step = max(1, min(50, int(step)))
        config = _load_json(self.config_path, {})
        if not isinstance(config, dict):
            raise ApiError("config.json повреждён")
        bindings = config.setdefault("bindings", {})
        if not isinstance(bindings, dict):
            raise ApiError("config.json: bindings должен быть объектом")

        if app:
            left = {"type": "volume", "delta": -1, "step": step, "app": app}
            right = {"type": "volume", "delta": 1, "step": step, "app": app}
        else:
            left = {"type": "volume", "delta": -1}
            right = {"type": "volume", "delta": 1}
        bindings["rotate_left"] = left
        bindings["rotate_right"] = right
        if mute_on_press:
            current_press = bindings.get("press")
            is_mute = isinstance(current_press, dict) and current_press.get("type") == "mute"
            if not is_mute:
                self.ui["press_backup"] = current_press or DEFAULT_CONFIG["bindings"]["press"]
                self._save_ui()
            bindings["press"] = (
                {"type": "mute", "app": app, "state": "toggle"}
                if app
                else {"type": "mute", "state": "toggle"}
            )
        else:
            backup = self.ui.pop("press_backup", None)
            if backup is not None:
                bindings["press"] = backup
                self._save_ui()
        self._validate_config(config)
        _write_json(self.config_path, config)
        self.journal.info(
            f"Ручка теперь меняет громкость: {app or 'системы'}"
        )
        return {"ok": True, "config": config}

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

    def apply_ui(self, data: dict[str, Any]) -> dict[str, Any]:
        if "dim" in data:
            self.ui["dim"] = max(0, min(90, int(data["dim"])))
        if "blur" in data:
            self.ui["blur"] = max(0, min(30, int(data["blur"])))
        if data.get("accent") in ACCENTS:
            self.ui["accent"] = data["accent"]
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
        for old in WALLPAPER_DIR.glob("bg.*"):
            try:
                old.unlink()
            except OSError:
                pass
        path = WALLPAPER_DIR / f"bg{ext}"
        path.write_bytes(body)
        self.ui["wallpaper"] = path.name
        self._save_ui()
        self.journal.info("Обои обновлены")
        return {"ok": True, "url": self._wallpaper_url(), "ui": self.ui}

    def clear_wallpaper(self) -> dict[str, Any]:
        for old in WALLPAPER_DIR.glob("bg.*"):
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
        path = WALLPAPER_DIR / Path(name).name
        if not path.is_file():
            raise ApiError("Файл обоев не найден")
        try:
            winhook.set_desktop_wallpaper(str(path))
        except OSError as exc:
            raise ApiError(f"Не удалось сменить обои рабочего стола: {exc}") from None
        self.journal.info("Обои поставлены на рабочий стол")
        return {"ok": True}

    def serve_wallpaper(self, handler: "Handler") -> None:
        name = self.ui.get("wallpaper")
        if not name:
            handler.send_error(404)
            return
        path = WALLPAPER_DIR / Path(name).name
        if not path.is_file():
            handler.send_error(404)
            return
        content_type = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
        }.get(path.suffix.lower(), "application/octet-stream")
        data = path.read_bytes()
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(data)

    # ------------------------------------------------------------------ server

    def serve(self, port: int, open_window: bool, auto_service: bool) -> int:
        mutex = winhook.create_mutex(MENU_MUTEX_NAME)
        if mutex is None:
            print(
                "Меню уже запущено. Закройте текущее окно или выполните stop.bat.",
                file=sys.stderr,
            )
            return 3

        if auto_service:
            try:
                self.service.start()
            except ApiError as exc:
                self.journal.info(str(exc))

        httpd = None
        last_error: OSError | None = None
        for candidate in range(port, port + 20):
            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
                break
            except OSError as exc:
                last_error = exc
        if httpd is None:
            print(f"Не удалось занять порт: {last_error}", file=sys.stderr)
            return 1
        httpd.daemon_threads = True
        httpd.menu = self  # type: ignore[attr-defined]
        self.server = httpd
        url = f"http://127.0.0.1:{httpd.server_address[1]}/"
        self.journal.info(f"{APP_NAME}: меню доступно на {url}")
        print(f"{APP_NAME}: меню открыто на {url}", flush=True)

        quit_event = winhook.create_event(MENU_QUIT_NAME)
        stop_watcher = threading.Thread(
            target=self._watch_quit, args=(quit_event,), daemon=True
        )
        stop_watcher.start()

        if open_window:
            threading.Timer(0.5, _open_app_window, args=(url,)).start()

        try:
            httpd.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            self.journal.info("Остановка по Ctrl+C")
        finally:
            httpd.server_close()
            try:
                self.service.stop()
            except Exception:  # noqa: BLE001
                pass
            winhook.close_handle(quit_event)
            winhook.close_handle(mutex)
        return 0

    def _watch_quit(self, quit_event) -> None:
        while not winhook.event_is_set(quit_event):
            time.sleep(0.2)
        if self.server is not None:
            self.server.shutdown()


class Handler(BaseHTTPRequestHandler):
    server_version = "AulaKnobMenu/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def menu(self) -> Menu:
        return self.server.menu  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    # ------------------------------------------------------------------ helpers

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str = "application/json; charset=utf-8",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > MAX_BODY:
            raise ApiError("Слишком большое тело запроса", 413)
        return self.rfile.read(length)

    def _json_body(self) -> dict[str, Any]:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError("Некорректный JSON") from None
        if not isinstance(data, dict):
            raise ApiError("Ожидается JSON-объект")
        return data

    def _static(self, filename: str, content_type: str) -> None:
        path = UI_DIR / filename
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self._send(200, data, content_type)

    # ------------------------------------------------------------------ routes

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                self._static("index.html", "text/html; charset=utf-8")
            elif path == "/style.css":
                self._static("style.css", "text/css; charset=utf-8")
            elif path == "/app.js":
                self._static("app.js", "application/javascript; charset=utf-8")
            elif path == "/api/state":
                self._json(self.menu.state())
            elif path == "/api/audio":
                self._json(self.menu.audio_state())
            elif path == "/api/log":
                query = parse_qs(parsed.query)
                cursor = int(query.get("since", ["0"])[0] or 0)
                self._json({"ok": True, **self.menu.journal.since(cursor)})
            elif path == "/wallpaper":
                self.menu.serve_wallpaper(self)
            elif path == "/favicon.ico":
                self._send(204, b"")
            else:
                self._json({"ok": False, "error": "Не найдено"}, 404)
        except ApiError as exc:
            self._json({"ok": False, "error": str(exc)}, exc.status)
        except Exception as exc:  # noqa: BLE001
            self.menu.journal.info(f"Ошибка GET {path}: {exc}")
            traceback.print_exc()
            self._json({"ok": False, "error": str(exc)}, 500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/config":
                data = self._json_body()
                self._json(self.menu.save_config(data))
            elif path == "/api/target":
                data = self._json_body()
                self._json(
                    self.menu.set_target(
                        data.get("app"),
                        int(data.get("step", 2)),
                        bool(data.get("mute_on_press", False)),
                    )
                )
            elif path == "/api/test":
                data = self._json_body()
                self._json(self.menu.test_binding(str(data.get("binding", ""))))
            elif path == "/api/app-volume":
                data = self._json_body()
                app = str(data.get("app", "")).strip()
                level = int(data.get("level", 0))
                if not app:
                    raise ApiError("Не указано приложение")
                count = audio.set_app_volume(app, max(0, min(100, level)))
                if not count:
                    raise ApiError(f"Нет активной аудиосессии {app!r}")
                self._json({"ok": True, "count": count})
            elif path == "/api/app-mute":
                data = self._json_body()
                app = str(data.get("app", "")).strip()
                if not app:
                    raise ApiError("Не указано приложение")
                state = data.get("state")
                count, current = audio.set_app_mute(
                    app,
                    None if state is None else bool(state),
                )
                if not count:
                    raise ApiError(f"Нет активной аудиосессии {app!r}")
                self._json({"ok": True, "mute": current})
            elif path == "/api/master-volume":
                data = self._json_body()
                level = max(0, min(100, int(data.get("level", 0))))
                audio.set_master_volume(level)
                self._json({"ok": True})
            elif path == "/api/master-mute":
                data = self._json_body()
                state = data.get("state")
                current = audio.set_master_mute(
                    None if state is None else bool(state)
                )
                self._json({"ok": True, "mute": current})
            elif path == "/api/service":
                data = self._json_body()
                action = str(data.get("action", "")).strip()
                if action == "start":
                    self.menu.service.start()
                elif action == "stop":
                    self.menu.service.stop()
                else:
                    raise ApiError("Неизвестное действие сервиса")
                self._json({"ok": True, "service": self.menu.service.status()})
            elif path == "/api/ui":
                data = self._json_body()
                self._json(self.menu.apply_ui(data))
            elif path == "/api/wallpaper":
                body = self._read_body()
                self._json(self.menu.upload_wallpaper(body))
            elif path == "/api/wallpaper/clear":
                self._json(self.menu.clear_wallpaper())
            elif path == "/api/wallpaper/desktop":
                self._json(self.menu.desktop_wallpaper())
            elif path == "/api/quit":
                self._json({"ok": True})
                if self.menu.server is not None:
                    threading.Thread(
                        target=self.menu.server.shutdown, daemon=True
                    ).start()
            else:
                self._json({"ok": False, "error": "Не найдено"}, 404)
        except ApiError as exc:
            self._json({"ok": False, "error": str(exc)}, exc.status)
        except audio.AudioError as exc:
            self._json({"ok": False, "error": f"Аудио: {exc}"}, 500)
        except Exception as exc:  # noqa: BLE001
            self.menu.journal.info(f"Ошибка POST {path}: {exc}")
            traceback.print_exc()
            self._json({"ok": False, "error": str(exc)}, 500)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="menu",
        description="Меню AULA F75 Knob: привязки, громкость приложений, обои.",
    )
    parser.add_argument(
        "--config",
        default=str(BASE_DIR / "config.json"),
        help="путь к config.json",
    )
    parser.add_argument("--port", type=int, default=8765, help="начальный порт меню")
    parser.add_argument(
        "--no-open", action="store_true", help="не открывать окно браузера"
    )
    parser.add_argument(
        "--no-service",
        action="store_true",
        help="не запускать сервис ручки автоматически",
    )
    parser.add_argument(
        "--stop", action="store_true", help="остановить запущенное меню и сервис"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.stop:
        menu_signal = winhook.signal_event(MENU_QUIT_NAME)
        knob_signal = winhook.signal_event(QUIT_EVENT_NAME)
        if menu_signal or knob_signal:
            print("Сигнал остановки отправлен.")
            return 0
        print("Запущенное меню не найдено.")
        return 1

    config_path = Path(args.config).expanduser().resolve()
    try:
        Settings.load(config_path)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    menu = Menu(config_path)
    return menu.serve(args.port, not args.no_open, not args.no_service)


if __name__ == "__main__":
    raise SystemExit(main())
