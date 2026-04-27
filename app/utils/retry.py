from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    retries: int = 3,
    base_delay: float = 0.5,
    exceptions: tuple[type[Exception], ...] = (Exception,),
    logger: logging.Logger | None = None,
    operation_name: str = "operation",
) -> T:
    attempt = 0
    while True:
        try:
            return await func()
        except exceptions as exc:
            attempt += 1
            if attempt >= retries:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            if logger:
                logger.warning(
                    "%s failed (attempt %s/%s): %s. Retry in %.2fs",
                    operation_name,
                    attempt,
                    retries,
                    exc,
                    delay,
                )
            await asyncio.sleep(delay)
