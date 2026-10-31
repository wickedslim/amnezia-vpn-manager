from __future__ import annotations

import configparser
import getpass
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    token: str
    admin_id: int
    data_dir: Path
    legacy_dir: Path
    poll_seconds: int = 60

    @classmethod
    def load(cls) -> "Settings":
        legacy = ROOT / "files"
        ini = configparser.ConfigParser(interpolation=None)
        ini.read(legacy / "setting.ini")
        section = ini["setting"] if ini.has_section("setting") else {}
        token = os.environ.get("AWG_BOT_TOKEN") or section.get("bot_token", "")
        admin = os.environ.get("AWG_ADMIN_ID") or section.get("admin_id", "")
        if not token or not str(admin).isdigit():
            raise ValueError("Задайте AWG_BOT_TOKEN и AWG_ADMIN_ID или выполните bot_manager.py --init")
        return cls(token, int(admin), Path(os.environ.get("AWG_DATA_DIR", legacy / "v2")), legacy)


def initialize() -> None:
    path = ROOT / "files" / "setting.ini"
    if path.exists():
        print("Настройки уже существуют. Для новой версии повторный ввод токена не требуется.")
        return
    token = os.environ.get("AWG_BOT_TOKEN") or getpass.getpass("Токен Telegram-бота (ввод скрыт): ").strip()
    admin = os.environ.get("AWG_ADMIN_ID") or input("Telegram ID администратора: ").strip()
    if ":" not in token or not admin.isdigit():
        raise ValueError("Некорректный токен или ID администратора")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ini = configparser.ConfigParser(interpolation=None)
    ini["setting"] = {"bot_token": token, "admin_id": admin}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        ini.write(stream)
    print("Настройки сохранены. После запуска добавьте VPN-сервер через меню бота.")
