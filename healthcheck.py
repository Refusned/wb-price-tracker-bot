"""Docker HEALTHCHECK: бот жив, если heartbeat-файл обновлялся недавно.

Планировщик (`WbUpdateScheduler`) трогает `data/.heartbeat` в конце каждого
цикла обновления. Если файл устарел больше, чем на несколько интервалов
поллинга, фоновый цикл завис — контейнер помечается unhealthy.

`start-period` в Dockerfile даёт боту время на первый цикл, поэтому отсутствие
файла на старте не приводит к ложному рестарту в течение grace-периода.
"""
from __future__ import annotations

import os
import sys
import time


def main() -> int:
    path = os.getenv("HEARTBEAT_PATH", "data/.heartbeat")
    try:
        interval = int(os.getenv("WB_POLL_INTERVAL_SECONDS", "600"))
    except ValueError:
        interval = 600
    max_age = max(interval * 3, 300) + 120

    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        # Файл ещё не создан — нормально в start-period; вне его — unhealthy.
        return 1

    return 0 if age <= max_age else 1


if __name__ == "__main__":
    sys.exit(main())
