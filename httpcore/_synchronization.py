import threading
from types import TracebackType
from typing import Type

import anyio

from ._exceptions import PoolTimeout, map_exceptions


class AsyncLock:
    def __init__(self) -> None:
        self._lock = anyio.Lock()

    async def __aenter__(self) -> "AsyncLock":
        await self._lock.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: Type[BaseException] = None,
        exc_value: BaseException = None,
        traceback: TracebackType = None,
    ) -> None:
        self._lock.release()


class AsyncEvent:
    def __init__(self) -> None:
        self._event = anyio.Event()

    def set(self) -> None:
        self._event.set()

    async def wait(self, timeout: float = None) -> None:
        exc_map: dict = {TimeoutError: PoolTimeout}
        with map_exceptions(exc_map):
            with anyio.fail_after(timeout):
                await self._event.wait()


class AsyncSemaphore:
    def __init__(self, bound: int) -> None:
        self._semaphore = anyio.Semaphore(initial_value=bound, max_value=bound)

    async def acquire(self) -> None:
        await self._semaphore.acquire()

    async def release(self) -> None:
        self._semaphore.release()


class Lock:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __enter__(self) -> "Lock":
        self._lock.acquire()
        return self

    def __exit__(
        self,
        exc_type: Type[BaseException] = None,
        exc_value: BaseException = None,
        traceback: TracebackType = None,
    ) -> None:
        self._lock.release()


class Event:
    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def wait(self, timeout: float = None) -> None:
        if not self._event.wait(timeout=timeout):
            raise PoolTimeout()  # pragma: nocover


class Semaphore:

    def __init__(self, bound: int, exc_class: type = None) -> None:
        self._semaphore = threading.Semaphore(value = bound)
        self._exc_class = exc_class

    def acquire(self, blocking: bool = True, timeout: float = None) -> bool:
        if self._exc_class is None:
            return self._semaphore.acquire(blocking = blocking, timeout = timeout)
        else :
            #in case blocking is set to False, dont throw an exception
            if not self._semaphore.acquire(blocking = blocking, timeout = timeout):
                if blocking:
                    raise self._exc_class()
                else:
                    return False
            return True

    __enter__ = acquire

    def release(self) -> None:
        self._semaphore.release()

    def __exit__(self, t, v, tb):
        self.release()


class BoundedSemaphore:

    def __init__(self, bound: int, exc_class: type = None) -> None:
        super().__init__(bound, exc_class = exc_class)
        self._semaphore = threading.BoundedSemaphore(value = bound)
