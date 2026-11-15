#!/usr/bin/env python3
"""AULA F75 Knob — превращает ручку клавиатуры в универсальный контроллер.

Утилита ставит низкоуровневый хук клавиатуры, ловит события ручки AULA F75
(по умолчанию это volume up/down и mute) и выполняет любые заданные действия:
запуск программ, горячие клавиши, открытие папок и ссылок, медиа, прокрутка,
фокус окна и т.д. Работает без драйверов и без AULA-софта.
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if sys.platform != "win32":
    sys.exit("AULA F75 Knob пока поддерживает только Windows.")

import actions
import winhook

APP_NAME = "AULA F75 Knob"
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"
MUTEX_NAME = r"Local\AulaF75Knob.Mutex"
QUIT_EVENT_NAME = r"Local\AulaF75Knob.Quit"

BINDING_NAMES = (
    "rotate_left",
    "rotate_right",
    "press",
    "hold",
    "held_rotate_left",
    "held_rotate_right",
)

DEFAULT_CONFIG: dict[str, Any] = {
    "knob": {
        "suppress": True,
        "hold_ms": 500,
        "merge_rotation_ms": 0,
        "second_layer_modifier": "shift",
        "rotate_left_keys": ["volume down"],
        "rotate_right_keys": ["volume up"],
        "press_keys": ["volume mute", "play/pause"],
    },
    "cooldown_ms": 0,
    "log_file": None,
    "bindings": {
        "rotate_left": {"type": "volume", "delta": -1},
        "rotate_right": {"type": "volume", "delta": 1},
        "press": {"type": "media", "action": "play_pause"},
        "hold": {"type": "media", "action": "mute"},
        "held_rotate_left": {"type": "media", "action": "prev"},
        "held_rotate_right": {"type": "media", "action": "next"},
    },
}


class ConfigError(Exception):
    """Ошибка в config.json."""


class Log:
    def __init__(
        self,
        file_path: str | None = None,
        verbose: bool = False,
        sink: Any = None,
    ) -> None:
        self.verbose = verbose
        self.sink = sink
        self.path: Path | None = None
        if file_path:
            self.path = Path(file_path).expanduser()
            if not self.path.is_absolute():
                self.path = BASE_DIR / self.path
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, message: str, *, force: bool = False) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}"
        if force or self.verbose:
            print(line, flush=True)
        if self.path is not None:
            try:
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
            except OSError:
                pass
        if self.sink is not None:
            try:
                self.sink(line)
            except Exception:  # noqa: BLE001 - лог не должен ломать сервис
                pass


@dataclass(frozen=True)
class Settings:
    suppress: bool
    hold_ms: int
    merge_rotation_ms: int
    cooldown_ms: int
    rotate_left_keys: tuple[str, ...]
    rotate_right_keys: tuple[str, ...]
    press_keys: tuple[str, ...]
    bindings: dict[str, Any]
    log_file: str | None = None
    # Клавиша-модификатор для второго слоя (held_rotate_*), например "shift".
    # "none" — второй слой только при реально зажатой ручке (если прошивка
    # вообще присылает удержание).
    second_layer_modifier: str = "shift"

    @classmethod
    def from_dict(cls, data: Any) -> "Settings":
        if not isinstance(data, dict):
            raise ConfigError("корень config.json должен быть объектом {...}")

        knob = data.get("knob", {})
        if not isinstance(knob, dict):
            raise ConfigError("раздел 'knob' должен быть объектом")

        suppress = bool(knob.get("suppress", True))
        hold_ms = _int_field(knob, "hold_ms", 500, 150, 5000)
        merge_ms = _int_field(knob, "merge_rotation_ms", 0, 0, 2000)
        cooldown_ms = _int_field(data, "cooldown_ms", 0, 0, 10000)

        rotate_left = _key_list(knob, "rotate_left_keys", ["volume down"])
        rotate_right = _key_list(knob, "rotate_right_keys", ["volume up"])
        press = _key_list(knob, "press_keys", ["volume mute", "play/pause"])
        second_layer_modifier = _modifier(knob)

        bindings = data.get("bindings", {})
        if not isinstance(bindings, dict) or not bindings:
            raise ConfigError("раздел 'bindings' должен быть непустым объектом")

        unknown = sorted(set(bindings) - set(BINDING_NAMES))
        if unknown:
            raise ConfigError(
                f"неизвестные привязки: {', '.join(unknown)}. "
                f"Доступно: {', '.join(BINDING_NAMES)}"
            )

        errors: list[str] = []
        for name, action in bindings.items():
            errors.extend(actions.validate_action(action, f"bindings.{name}"))
        if errors:
            raise ConfigError("\n  ".join(["ошибки в действиях:", *errors]))

        log_file = data.get("log_file")
        if log_file is not None and not isinstance(log_file, str):
            raise ConfigError("'log_file' должен быть строкой или null")

        settings = cls(
            suppress=suppress,
            hold_ms=hold_ms,
            merge_rotation_ms=merge_ms,
            cooldown_ms=cooldown_ms,
            rotate_left_keys=rotate_left,
            rotate_right_keys=rotate_right,
            press_keys=press,
            bindings=dict(bindings),
            log_file=log_file or None,
            second_layer_modifier=second_layer_modifier,
        )
        settings.role_by_vk()  # проверяем конфликты и имена клавиш
        return settings

    @classmethod
    def load(cls, path: Path) -> "Settings":
        try:
            text = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            raise ConfigError(
                f"файл конфигурации не найден: {path}\n"
                "Создайте его командой: python aula_knob.py --init"
            ) from None
        except OSError as exc:
            raise ConfigError(f"не удалось прочитать {path}: {exc}") from None
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"JSON сломан ({exc.lineno}:{exc.colno}): {exc.msg}") from None
        return cls.from_dict(data)

    def role_by_vk(self) -> dict[int, str]:
        mapping: dict[int, str] = {}
        groups = (
            (self.rotate_left_keys, "rotate_left"),
            (self.rotate_right_keys, "rotate_right"),
            (self.press_keys, "press"),
        )
        for keys, role in groups:
            for key in keys:
                vk = winhook.parse_key(key)
                previous = mapping.get(vk)
                if previous and previous != role:
                    raise ConfigError(
                        f"клавиша {key!r} одновременно назначена на "
                        f"{previous} и {role}"
                    )
                mapping[vk] = role
        return mapping

    def summary(self) -> str:
        lines = [
            f"Подавление системных клавиш: {'да' if self.suppress else 'нет'}",
            f"Порог удержания: {self.hold_ms} мс",
            f"Объединение поворотов: {self.merge_rotation_ms} мс",
            "Второй слой (held_rotate_*): "
            + (
                "только зажатая ручка"
                if self.second_layer_modifier == "none"
                else f"вращение с зажатым {self.second_layer_modifier}"
            ),
            f"Пауза между срабатываниями: {self.cooldown_ms} мс",
            "Клавиши ручки:",
            f"  влево:  {', '.join(self.rotate_left_keys)}",
            f"  вправо: {', '.join(self.rotate_right_keys)}",
            f"  нажатие: {', '.join(self.press_keys)}",
            "Привязки:",
        ]
        for name in BINDING_NAMES:
            if name in self.bindings:
                lines.append(f"  {name}: {actions.describe(self.bindings[name])}")
        return "\n".join(lines)


def _int_field(
    data: dict[str, Any],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"'{name}' должен быть целым числом")
    if not minimum <= value <= maximum:
        raise ConfigError(f"'{name}' должен быть от {minimum} до {maximum}")
    return value


def _key_list(knob: dict[str, Any], name: str, default: list[str]) -> tuple[str, ...]:
    raw = knob.get(name, default)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"knob.{name}: нужен непустой список имён клавиш")
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ConfigError(f"knob.{name}: каждый элемент должен быть строкой")
        try:
            winhook.parse_key(item)
        except ValueError as exc:
            raise ConfigError(
                f"knob.{name}: {exc}. Запустите 'python aula_knob.py --learn', "
                "покрутите ручку и подставьте увиденное имя (или vk:0xXX)"
            ) from None
        result.append(item)
    return tuple(result)


def _modifier(knob: dict[str, Any]) -> str:
    raw = knob.get("second_layer_modifier", "shift")
    if raw is None:
        return "none"
    if not isinstance(raw, str):
        raise ConfigError("'second_layer_modifier' должен быть строкой")
    name = raw.strip().lower()
    if name in ("", "none", "нет", "off"):
        return "none"
    try:
        winhook.parse_key(name)
    except ValueError as exc:
        raise ConfigError(f"knob.second_layer_modifier: {exc}") from None
    return name


def _modifier_vk(name: str) -> int:
    if not name or name == "none":
        return 0
    try:
        return winhook.parse_key(name)
    except ValueError:
        return 0


class Controller:
    def __init__(
        self,
        config_path: Path,
        settings: Settings,
        verbose: bool = False,
        log_sink: Any = None,
    ) -> None:
        self.config_path = config_path
        self.settings = settings
        self.verbose = verbose
        self.log = Log(settings.log_file, verbose, log_sink)
        self._log_sink = log_sink
        self.roles = settings.role_by_vk()
        self._second_layer_vk = _modifier_vk(settings.second_layer_modifier)
        self._events: queue.Queue[winhook.KeyEvent] = queue.Queue()
        self._action_queue: queue.Queue[tuple[str, Any] | None] = queue.Queue()
        self._hook: winhook.KeyboardHook | None = None
        self._stopping = threading.Event()
        self._stop_requested = threading.Event()
        self._quit_event: Any = None
        self._last_fire: dict[str, float] = {}
        self._pressed = False
        self._press_started = 0.0
        self._hold_deadline = float("inf")
        self._hold_fired = False
        self._rotated_while_held = False
        self._last_rotate = 0.0
        self._last_direction: str | None = None
        self._last_config_check = 0.0
        self._config_mtime = 0.0
        self._reload_error: str | None = None

    # ------------------------------------------------------------------ запуск

    def run(self) -> int:
        mutex = winhook.create_mutex(MUTEX_NAME)
        if mutex is None:
            self.log(
                "Утилита уже запущена (или остался зависший процесс). "
                "Остановить: python aula_knob.py --stop",
                force=True,
            )
            return 1
        quit_event = winhook.create_event(QUIT_EVENT_NAME)
        self._quit_event = quit_event
        try:
            self._hook = winhook.KeyboardHook(
                on_event=self._events.put,
                blocked=self.roles if self.settings.suppress else (),
                interest=set(self.roles),
            )
            self._hook.start()
            self._config_mtime = self._mtime()
            worker = threading.Thread(
                target=self._action_loop, name="aula-knob-actions", daemon=True
            )
            worker.start()
            self.log(f"{APP_NAME} запущен. Конфиг: {self.config_path}", force=True)
            self.log("Крутите ручку. Остановить: Ctrl+C или --stop.")
            try:
                self._loop(quit_event)
            except KeyboardInterrupt:
                self.log("Остановка по Ctrl+C.")
            finally:
                self._stopping.set()
                self._action_queue.put(None)
                if self._hook:
                    self._hook.stop()
                self.log(f"{APP_NAME} остановлен.", force=True)
        finally:
            winhook.close_handle(quit_event)
            winhook.close_handle(mutex)
        return 0

    def _loop(self, quit_event) -> None:
        while True:
            try:
                event = self._events.get(timeout=0.02)
            except queue.Empty:
                event = None
            if event is not None:
                self._handle(event)
            self._tick()
            if winhook.event_is_set(quit_event) or self._stop_requested.is_set():
                self.log("Получен сигнал остановки.")
                return
            self._maybe_reload()

    def request_stop(self) -> None:
        """Останавливает цикл из другого потока (используется меню)."""
        self._stop_requested.set()
        event = self._quit_event
        if event:
            try:
                winhook.set_event(event)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ события

    def _handle(self, event: winhook.KeyEvent) -> None:
        role = self.roles.get(event.vk)
        if role is None:
            return
        if role in ("rotate_left", "rotate_right"):
            # Один щелчок ручки приходит парой keydown+keyup — реагируем только
            # на нажатие, иначе громкость меняется дважды за щелчок.
            if event.is_down:
                self._rotate(role)
        elif event.is_down:
            self._press_down()
        else:
            self._press_up()

    def _rotate(self, role: str) -> None:
        now = time.monotonic()
        merge = self.settings.merge_rotation_ms / 1000.0
        if (
            merge
            and self._last_direction == role
            and now - self._last_rotate < merge
        ):
            return
        self._last_rotate = now
        self._last_direction = role

        if self._pressed or self._second_layer_held():
            self._rotated_while_held = True
            binding = f"held_{role}"
            if binding not in self.settings.bindings:
                binding = role
        else:
            binding = role
        self._fire(binding, now)

    def _second_layer_held(self) -> bool:
        """Зажат ли модификатор второго слоя (Shift по умолчанию).

        Нужен потому, что фирменный драйвер AULA присылает нажатие ручки как
        мгновенный тап и удержание до софта не доходит — «зажатая ручка» на
        F75 не определяется. Модификатор же виден всегда.
        """
        vk = self._second_layer_vk
        return bool(vk) and winhook.is_key_down(vk)

    def _press_down(self) -> None:
        if self._pressed:
            return
        self._pressed = True
        self._press_started = time.monotonic()
        self._hold_fired = False
        self._rotated_while_held = False
        if "hold" in self.settings.bindings:
            self._hold_deadline = self._press_started + self.settings.hold_ms / 1000.0
        else:
            self._hold_deadline = float("inf")

    def _press_up(self) -> None:
        if not self._pressed:
            return
        self._pressed = False
        now = time.monotonic()
        if self._hold_fired or self._rotated_while_held:
            return
        if now >= self._hold_deadline:
            self._hold_fired = True
            self._fire("hold", now)
        else:
            self._fire("press", now)

    def _tick(self) -> None:
        if (
            self._pressed
            and not self._hold_fired
            and not self._rotated_while_held
            and time.monotonic() >= self._hold_deadline
        ):
            self._hold_fired = True
            self._fire("hold")

    def _fire(self, binding: str, now: float | None = None) -> None:
        action = self.settings.bindings.get(binding)
        if action is None:
            return
        now = time.monotonic() if now is None else now
        cooldown = self.settings.cooldown_ms
        if isinstance(action, dict):
            raw = action.get("cooldown_ms")
            if isinstance(raw, int) and not isinstance(raw, bool):
                cooldown = max(0, raw)
        if cooldown and now - self._last_fire.get(binding, 0.0) < cooldown / 1000.0:
            return
        self._last_fire[binding] = now
        self.log(f"{binding}: {actions.describe(action)}")
        self._action_queue.put((binding, action))

    # ------------------------------------------------------------- действия/конфиг

    def _action_loop(self) -> None:
        while True:
            item = self._action_queue.get()
            if item is None:
                return
            name, action = item
            try:
                actions.execute(action, log=self.log)
            except Exception as exc:  # noqa: BLE001 - одно действие не должно ронять утилиту
                self.log(f"Ошибка действия {name}: {exc}", force=True)

    def _mtime(self) -> float:
        try:
            return self.config_path.stat().st_mtime
        except OSError:
            return 0.0

    def _maybe_reload(self) -> None:
        now = time.monotonic()
        if now - self._last_config_check < 1.0:
            return
        self._last_config_check = now
        mtime = self._mtime()
        if not mtime or mtime == self._config_mtime:
            return
        try:
            new_settings = Settings.load(self.config_path)
        except Exception as exc:  # noqa: BLE001 - показываем ошибку, старый конфиг живёт
            message = str(exc)
            if message != self._reload_error:
                self.log(f"Конфиг не применён: {message}", force=True)
                self._reload_error = message
            return
        self._config_mtime = mtime
        self._reload_error = None
        self.settings = new_settings
        self.log = Log(new_settings.log_file, self.verbose, self._log_sink)
        self.roles = new_settings.role_by_vk()
        self._second_layer_vk = _modifier_vk(new_settings.second_layer_modifier)
        if self._hook:
            self._hook.set_blocked(self.roles if new_settings.suppress else ())
            self._hook.set_interest(set(self.roles))
        self._reset_state()
        self.log("Конфиг перезагружен.", force=True)

    def _reset_state(self) -> None:
        self._pressed = False
        self._hold_fired = False
        self._rotated_while_held = False
        self._hold_deadline = float("inf")
        self._last_fire.clear()
        self._last_direction = None


# ------------------------------------------------------------------------- CLI


def run_learn() -> int:
    print("Режим обучения. Покрутите и нажмите ручку — ниже появятся VK-коды.")
    print("Эти имена можно вписывать в config.json (knob.*_keys).")
    print("Выход: Ctrl+C.\n")

    def on_event(event: winhook.KeyEvent) -> None:
        direction = "down" if event.is_down else "up  "
        name = winhook.key_name(event.vk)
        injected = "  (injected)" if event.flags & winhook.LLKHF_INJECTED else ""
        print(
            f"{direction}  VK=0x{event.vk:02X} ({event.vk:3d})  "
            f"scan=0x{event.scan:02X}  flags=0x{event.flags:02X}  {name}{injected}",
            flush=True,
        )

    hook = winhook.KeyboardHook(on_event=on_event)
    hook.start()
    try:
        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nВыход из режима обучения.")
    finally:
        hook.stop()
    return 0


def write_default_config(path: Path, force: bool = False) -> int:
    if path.exists() and not force:
        print(f"Файл уже существует: {path}")
        print("Перезаписать: python aula_knob.py --init --force")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Создан конфиг: {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aula_knob",
        description=(
            "AULA F75 Knob — назначает любые действия на вращение и нажатие "
            "ручки клавиатуры AULA F75."
        ),
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="путь к config.json (по умолчанию рядом со скриптом)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="логировать действия")
    parser.add_argument("--learn", action="store_true", help="показать VK-коды ручки")
    parser.add_argument("--check", action="store_true", help="проверить конфиг и выйти")
    parser.add_argument("--init", action="store_true", help="создать config.json по умолчанию")
    parser.add_argument("--force", action="store_true", help="перезаписать конфиг при --init")
    parser.add_argument("--test", metavar="BINDING", help="выполнить привязку один раз")
    parser.add_argument("--do", metavar="ACTION", help="выполнить действие, например keys:win+d")
    parser.add_argument("--stop", action="store_true", help="остановить запущенную утилиту")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()

    if args.stop:
        if winhook.signal_event(QUIT_EVENT_NAME):
            print("Сигнал остановки отправлен.")
            return 0
        print("Запущенная утилита не найдена.")
        return 1

    if args.init:
        return write_default_config(config_path, force=args.force)

    try:
        settings = Settings.load(config_path)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    if args.check:
        print(f"Конфиг в порядке: {config_path}\n")
        print(settings.summary())
        return 0

    if args.do:
        try:
            actions.execute(args.do, log=print)
        except Exception as exc:  # noqa: BLE001
            print(f"Ошибка действия: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.test:
        if args.test not in settings.bindings:
            available = ", ".join(name for name in BINDING_NAMES if name in settings.bindings)
            print(f"Нет привязки {args.test!r}. Есть: {available}", file=sys.stderr)
            return 2
        try:
            actions.execute(settings.bindings[args.test], log=print)
        except Exception as exc:  # noqa: BLE001
            print(f"Ошибка действия: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.learn:
        return run_learn()

    return Controller(config_path, settings, verbose=args.verbose).run()


if __name__ == "__main__":
    raise SystemExit(main())
