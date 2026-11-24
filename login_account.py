"""Lanzador raíz de login_account.py para consola de Pelican / Pterodactyl."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent / "telegram-bot"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

if __name__ == "__main__":
    runpy.run_path(str(BOT_DIR / "login_account.py"), run_name="__main__")
