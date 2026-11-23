"""FastAPI lifespan helper：启动 scheduler.run_forever() 后台 task + Ctrl-C grace shutdown。

由 main.py lifespan 调用：
    async with scheduler_lifespan(scheduler):
        yield
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any


log = logging.getLogger("scheduler_lifespan")


@asynccontextmanager
async def scheduler_lifespan(scheduler: Any):
    task = asyncio.create_task(scheduler.run_forever())
    try:
        yield
    finally:
        scheduler.stop()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("scheduler did not stop within 5s; cancelling task")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
