"""Lanzador raíz para despliegues directos desde GitHub en Pelican / Pterodactyl.

Permite dejar PY_FILE=bot.py y REQUIREMENTS_FILE=requirements.txt en panel.plugged.host
sin mover carpetas después de clonar el repositorio desde GitHub.
"""

from __future__ import annotations

import asyncio
import os
import runpy
import sys
from pathlib import Path

# Crear event loop explícito antes de importar hydrogram (compatible Python 3.11–3.14 sin warnings)
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)

ROOT_DIR = Path(__file__).resolve().parent
BOT_DIR = ROOT_DIR / "telegram-bot"

if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

os.environ.setdefault("DATA_DIR", str(ROOT_DIR / "data"))
os.environ.setdefault("WORK_DIR", str(ROOT_DIR / "temp_work"))
os.environ.setdefault("LINKS_DIR", str(ROOT_DIR / "temp_links"))

if __name__ == "__main__":
    runpy.run_path(str(BOT_DIR / "bot.py"), run_name="__main__")
