#!/usr/bin/env python3
"""
Очистка телевизора TCL (Android TV / Google TV) от ненужных приложений через ADB.

Что делает скрипт:
  * подключается к телевизору по сети (adb connect IP:5555);
  * показывает, какие приложения из списка «мусора» реально установлены;
  * ОТКЛЮЧАЕТ их (pm disable-user) — это полностью обратимо;
  * по желанию удаляет их для текущего пользователя (pm uninstall --user 0) —
    тоже обратимо: системный APK остаётся в прошивке, а сброс к заводским
    настройкам или команда restore вернут приложение;
  * сохраняет журнал изменений, чтобы командой restore всё вернуть;
  * может ускорить анимации интерфейса.

Ничего не делается без подтверждения, по умолчанию — «пробный прогон» (--dry-run
выключается флагом --apply).

Примеры:
  python tcl_cleanup.py connect 192.168.1.50
  python tcl_cleanup.py list                 # что установлено из списка
  python tcl_cleanup.py list --all           # все пакеты TCL / сторонние
  python tcl_cleanup.py clean                # пробный прогон, покажет план
  python tcl_cleanup.py clean --apply        # отключить «безопасные» пакеты
  python tcl_cleanup.py clean --apply --level optional   # + необязательные
  python tcl_cleanup.py clean --apply --uninstall        # удалить, а не отключить
  python tcl_cleanup.py restore              # вернуть всё, что было изменено
  python tcl_cleanup.py speedup --apply      # ускорить анимации, почистить кэш
"""

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

LOG_FILE = Path(__file__).with_name("removed_packages.json")

# ---------------------------------------------------------------------------
# Списки пакетов.
#   safe     — реклама, телеметрия, дубли сервисов; на работу ТВ не влияют.
#   optional — приложения, которыми многие не пользуются (стриминги, игры и т.п.).
#              Отключайте, только если они вам точно не нужны.
# Названия пакетов отличаются между моделями и прошивками — скрипт трогает
# только то, что реально установлено на вашем телевизоре.
# ---------------------------------------------------------------------------
CANDIDATES = {
    "safe": {
        # TCL: аналитика, реклама, «оптимизаторы», собственные магазины
        "com.tcl.bi": "TCL: сбор статистики (BI)",
        "com.tcl.logger": "TCL: журналирование/телеметрия",
        "com.tcl.guard": "TCL Guard («оптимизатор», сам тратит ресурсы)",
        "com.tcl.appmarket2": "TCL App Store (дублирует Google Play)",
        "com.tcl.ad": "TCL: рекламный сервис",
        "com.tcl.advert": "TCL: рекламный сервис",
        "com.tcl.esticker": "TCL: рекламные баннеры/стикеры",
        "com.tcl.messagebox": "TCL: рекламные уведомления",
        "com.tcl.usercenter": "TCL: учётная запись TCL",
        "com.tcl.feedback": "TCL: отправка отзывов",
        "com.tcl.waterfall.overseas": "TCL Channel / рекламная витрина",
        # Google: обучалки, отзывы, тестовые сервисы
        "com.google.android.tvtutorials": "Google: обучающие подсказки",
        "com.google.android.feedback": "Google: отправка отзывов",
        "com.google.android.backdrop": "Google: заставка (Ambient mode)",
        "com.google.android.speech.pumpkin": "Google: офлайн-модель (если не нужна)",
        "com.android.printspooler": "Печать (на ТВ не нужна)",
        "com.android.dreams.basic": "Базовая заставка",
        "com.android.dreams.phototable": "Заставка «Фотостол»",
    },
    "optional": {
        "com.netflix.ninja": "Netflix",
        "com.amazon.amazonvideo.livingroom": "Prime Video",
        "com.disney.disneyplus": "Disney+",
        "com.google.android.youtube.tvmusic": "YouTube Music",
        "com.google.android.youtube.tvkids": "YouTube Kids",
        "com.google.android.play.games": "Google Play Игры",
        "com.google.android.videos": "Google Play Фильмы / Google TV",
        "com.google.android.katniss": "Google Ассистент (голосовой поиск!)",
        "com.google.android.tvrecommendations": "Рекомендации на главном экране",
        "com.google.android.marvin.talkback": "TalkBack (для незрячих)",
        "com.tcl.weather": "TCL: погода",
        "com.tcl.gallery": "TCL: галерея",
        "com.tcl.smartalexa": "Alexa от TCL",
        "com.tcl.tvweishi": "TCL: сервис удалённого управления/помощи",
    },
}

# Эти пакеты не трогаем НИКОГДА, даже если пользователь их укажет:
# без них телевизор может перестать загружаться или реагировать на пульт.
PROTECTED_PREFIXES = (
    "android",
    "com.android.systemui",
    "com.android.settings",
    "com.android.tv.settings",
    "com.android.providers.",
    "com.android.inputmethod",
    "com.android.packageinstaller",
    "com.android.vending",            # Google Play
    "com.google.android.gms",         # Google Play Services
    "com.google.android.gsf",
    "com.google.android.tvlauncher",  # главный экран Android TV
    "com.google.android.apps.tv.launcherx",  # главный экран Google TV
    "com.google.android.leanbacklauncher",
    "com.google.android.inputmethod",
    "com.tcl.tv",                     # ТВ-тюнер / каналы
    "com.tcl.settings",
    "com.tcl.launcher",
    "com.tcl.inputsource",
    "com.tcl.systemserver",
    "com.tcl.common",
)


# ---------------------------------------------------------------------------
# Работа с adb
# ---------------------------------------------------------------------------
def adb(*args, check=True):
    """Запустить команду adb и вернуть stdout."""
    if not shutil.which("adb"):
        sys.exit("Не найден adb. Установите Android Platform Tools: "
                 "https://developer.android.com/tools/releases/platform-tools")
    res = subprocess.run(["adb", *args], capture_output=True, text=True)
    if check and res.returncode != 0:
        sys.exit(f"Ошибка adb {' '.join(args)}:\n{res.stderr or res.stdout}")
    return res.stdout.strip()


def shell(cmd):
    return adb("shell", cmd, check=False)


def ensure_device():
    out = adb("devices")
    lines = [line for line in out.splitlines()[1:] if line.strip()]
    if not lines:
        sys.exit("Телевизор не подключён. Выполните: python tcl_cleanup.py connect <IP>")
    if any("unauthorized" in line for line in lines):
        sys.exit("Подтвердите на экране телевизора запрос «Разрешить отладку по USB/сети?»")


def installed_packages(disabled=False):
    flag = "-d" if disabled else ""
    out = shell(f"pm list packages {flag}")
    return {line.split(":", 1)[1].strip() for line in out.splitlines() if ":" in line}


def is_protected(pkg):
    return any(pkg == p or pkg.startswith(p + ".") or (p.endswith(".") and pkg.startswith(p))
               for p in PROTECTED_PREFIXES)


def load_log():
    if LOG_FILE.exists():
        return json.loads(LOG_FILE.read_text(encoding="utf-8"))
    return []


def save_log(entries):
    LOG_FILE.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------
def cmd_connect(args):
    target = args.ip if ":" in args.ip else f"{args.ip}:5555"
    print(adb("connect", target))
    print("Если на ТВ появился запрос на отладку — нажмите «Разрешить» и повторите команду.")


def cmd_list(args):
    ensure_device()
    pkgs = installed_packages()
    disabled = installed_packages(disabled=True)
    model = shell("getprop ro.product.model")
    android = shell("getprop ro.build.version.release")
    print(f"Устройство: {model}, Android {android}. Пакетов: {len(pkgs)}\n")

    for level, items in CANDIDATES.items():
        print(f"== {level} ==")
        for pkg, desc in items.items():
            if pkg in pkgs:
                state = "отключён" if pkg in disabled else "активен"
                print(f"  [{state:8}] {pkg:45} {desc}")
        print()

    if args.all:
        known = {p for items in CANDIDATES.values() for p in items}
        third_party = installed_packages_third_party()
        print("== Остальные пакеты TCL и сторонние приложения (проверьте сами) ==")
        for pkg in sorted(pkgs - known):
            if pkg.startswith("com.tcl.") or pkg in third_party:
                mark = "  [защищён]" if is_protected(pkg) else ""
                print(f"  {pkg}{mark}")


def installed_packages_third_party():
    out = shell("pm list packages -3")
    return {line.split(":", 1)[1].strip() for line in out.splitlines() if ":" in line}


def cmd_clean(args):
    ensure_device()
    pkgs = installed_packages()
    disabled = installed_packages(disabled=True)

    levels = ["safe"] if args.level == "safe" else ["safe", "optional"]
    plan = [p for lvl in levels for p in CANDIDATES[lvl] if p in pkgs]
    plan += [p for p in (args.extra or []) if p in pkgs]
    plan = [p for p in dict.fromkeys(plan)
            if not is_protected(p) and (args.uninstall or p not in disabled)]

    if not plan:
        print("Нечего чистить: подходящих пакетов не найдено (или уже отключены).")
        return

    action = "удалить для пользователя 0" if args.uninstall else "отключить"
    print(f"План ({action}):")
    for p in plan:
        desc = CANDIDATES["safe"].get(p) or CANDIDATES["optional"].get(p) or "указан вручную"
        print(f"  - {p:45} {desc}")

    if not args.apply:
        print("\nЭто пробный прогон. Добавьте --apply, чтобы выполнить.")
        return
    if not args.yes and input("\nПродолжить? [y/N] ").strip().lower() not in ("y", "yes", "д", "да"):
        print("Отменено.")
        return

    log = load_log()
    for p in plan:
        if args.uninstall:
            shell(f"am force-stop {p}")
            out = shell(f"pm uninstall -k --user 0 {p}")
        else:
            out = shell(f"pm disable-user --user 0 {p}")
        ok = "Success" in out or "disabled" in out.lower()
        print(f"  {'OK ' if ok else 'ERR'} {p}: {out}")
        if ok:
            log.append({"package": p, "action": "uninstall" if args.uninstall else "disable",
                        "time": datetime.now().isoformat(timespec="seconds")})
    save_log(log)
    print(f"\nГотово. Журнал: {LOG_FILE}. Перезагрузите ТВ: adb reboot")


def cmd_restore(args):
    ensure_device()
    log = load_log()
    targets = args.packages or [e["package"] for e in log]
    if not targets:
        print("Журнал пуст — восстанавливать нечего.")
        return
    for p in dict.fromkeys(targets):
        out1 = shell(f"cmd package install-existing --user 0 {p}")
        out2 = shell(f"pm enable --user 0 {p}")
        print(f"  {p}: {out1 or ''} {out2 or ''}".strip())
    save_log([e for e in log if e["package"] not in targets])
    print("Готово. Перезагрузите ТВ: adb reboot")


def cmd_speedup(args):
    ensure_device()
    steps = [
        ("Анимации окон x0.5", "settings put global window_animation_scale 0.5"),
        ("Анимации переходов x0.5", "settings put global transition_animation_scale 0.5"),
        ("Длительность анимаций x0.5", "settings put global animator_duration_scale 0.5"),
        ("Очистка кэша приложений", "pm trim-caches 999G"),
        ("Сжатие/оптимизация приложений (долго)", "cmd package bg-dexopt-job"),
    ]
    for title, cmd in steps:
        if args.apply:
            print(f"  {title}... {shell(cmd) or 'OK'}")
        else:
            print(f"  [пробный прогон] {title}: adb shell {cmd}")
    if not args.apply:
        print("\nДобавьте --apply, чтобы выполнить. Вернуть анимации: значения 1.0.")


def main():
    ap = argparse.ArgumentParser(description="Очистка телевизора TCL от лишних приложений через ADB")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("connect", help="подключиться к ТВ по сети")
    p.add_argument("ip", help="IP-адрес телевизора (Настройки → Сеть)")
    p.set_defaults(func=cmd_connect)

    p = sub.add_parser("list", help="показать найденные «мусорные» пакеты")
    p.add_argument("--all", action="store_true", help="также все пакеты TCL и сторонние")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("clean", help="отключить/удалить лишние пакеты")
    p.add_argument("--level", choices=["safe", "optional"], default="safe",
                   help="safe — только реклама/телеметрия; optional — плюс необязательные приложения")
    p.add_argument("--extra", nargs="*", help="дополнительные пакеты вручную")
    p.add_argument("--uninstall", action="store_true",
                   help="удалить для пользователя (освобождает место), а не просто отключить")
    p.add_argument("--apply", action="store_true", help="реально выполнить (иначе пробный прогон)")
    p.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждение")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("restore", help="вернуть отключённые/удалённые пакеты")
    p.add_argument("packages", nargs="*", help="какие пакеты (по умолчанию — все из журнала)")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("speedup", help="ускорить анимации и почистить кэш")
    p.add_argument("--apply", action="store_true", help="реально выполнить")
    p.set_defaults(func=cmd_speedup)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
