"""Task-owned locks allow existing callers and the executor to share ownership."""

import asyncio


class ReentrantAsyncLock:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._owner = None
        self._depth = 0

    def locked(self):
        return self._lock.locked()

    async def __aenter__(self):
        task = asyncio.current_task()
        if self._owner is not task:
            await self._lock.acquire()
            self._owner = task
        self._depth += 1
        return self

    async def __aexit__(self, *args):
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()
