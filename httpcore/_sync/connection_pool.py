import ssl
import sys
from time import time
from types import TracebackType
from typing import Iterable, Iterator, List, Optional, Type, Dict, Set, final
from collections import defaultdict
from functools import partial

from .._exceptions import ConnectionNotAvailable, UnsupportedProtocol, PoolTimeout, LockNotAvailable
from .._models import Origin, Request, Response
from .._ssl import default_ssl_context
from .._synchronization import Event, Lock, BoundedSemaphore
from ..backends.sync import SyncBackend
from ..backends.base import NetworkBackend
from .connection import HTTPConnection
from .interfaces import ConnectionInterface, RequestInterface

"""
class RequestStatus:
    def __init__(self, request: Request):
        self.request = request
        self.connection: Optional[ConnectionInterface] = None
        self._connection_acquired = Event()

    def set_connection(self, connection: ConnectionInterface) -> None:
        assert self.connection is None
        self.connection = connection
        self._connection_acquired.set()

    def unset_connection(self) -> None:
        assert self.connection is not None
        self.connection = None
        self._connection_acquired = Event()

    def wait_for_connection(
        self, timeout: float = None
    ) -> ConnectionInterface:
        self._connection_acquired.wait(timeout=timeout)
        assert self.connection is not None
        return self.connection
"""

class ConnectionPool(RequestInterface):
    """
    A connection pool for making HTTP requests.
    """

    def __init__(
        self,
        ssl_context: ssl.SSLContext = None,
        max_connections: Optional[int] = 10,
        max_keepalive_connections: int = None,
        keepalive_expiry: float = None,
        http1: bool = True,
        http2: bool = False,
        retries: int = 0,
        local_address: str = None,
        uds: str = None,
        network_backend: NetworkBackend = None,
    ) -> None:
        """
        A connection pool for making HTTP requests.

        Parameters:
            ssl_context: An SSL context to use for verifying connections.
                If not specified, the default `httpcore.default_ssl_context()`
                will be used.
            max_connections: The maximum number of concurrent HTTP connections that
                the pool should allow. Any attempt to send a request on a pool that
                would exceed this amount will block until a connection is available.
            max_keepalive_connections: The maximum number of idle HTTP connections
                that will be maintained in the pool.
            keepalive_expiry: The duration in seconds that an idle HTTP connection
                may be maintained for before being expired from the pool.
            http1: A boolean indicating if HTTP/1.1 requests should be supported
                by the connection pool. Defaults to True.
            http2: A boolean indicating if HTTP/2 requests should be supported by
                the connection pool. Defaults to False.
            retries: The maximum number of retries when trying to establish a
                connection.
            local_address: Local address to connect from. Can also be used to connect
                using a particular address family. Using `local_address="0.0.0.0"`
                will connect using an `AF_INET` address (IPv4), while using
                `local_address="::"` will connect using an `AF_INET6` address (IPv6).
            uds: Path to a Unix Domain Socket to use instead of TCP sockets.
            network_backend: A backend instance to use for handling network I/O.
        """
        if ssl_context is None:
            ssl_context = default_ssl_context()

        self._ssl_context = ssl_context

        self._max_connections = (
            sys.maxsize if max_connections is None else max_connections
        )
        self._max_keepalive_connections = (
            sys.maxsize
            if max_keepalive_connections is None
            else max_keepalive_connections
        )
        self._max_keepalive_connections = min(
            self._max_connections, self._max_keepalive_connections
        )

        self._keepalive_expiry = keepalive_expiry
        self._http1 = http1
        self._http2 = http2
        self._retries = retries
        self._local_address = local_address
        self._uds = uds

        self._pool_lock = Lock()
        self._network_backend = (
            SyncBackend() if network_backend is None else network_backend
        )
        self._connection_semaphore = BoundedSemaphore(self._max_connections, exc_class = PoolTimeout)
        self._keepalive_semaphore = BoundedSemaphore(self._max_keepalive_connections)
        self._origin_locks: Dict[Origin, BoundedSemaphore] = defaultdict(partial(BoundedSemaphore, 1, exc_class = PoolTimeout))
        self._pool: Dict[Origin, List[ConnectionInterface]] = defaultdict(list)
       
    
    @property
    def connections(self) -> List[ConnectionInterface]:
        """
        Return a list of the connections currently in the pool.

        For example:

        ```python
        >>> pool.connections
        [
            <HTTPConnection ['https://example.com:443', HTTP/1.1, ACTIVE, Request Count: 6]>,
            <HTTPConnection ['https://example.com:443', HTTP/1.1, IDLE, Request Count: 9]> ,
            <HTTPConnection ['http://example.com:80', HTTP/1.1, IDLE, Request Count: 1]>,
        ]
        ```
        """
        return list([connection for origin_connections in reversed(self._pool.values()) for connection in origin_connections])
    

    @property
    def _origin_locks_copy(self):
        with self._pool_lock:
            return dict(self._origin_locks)

    
    def _connections_for_origin(self, origin: Origin) -> List[ConnectionInterface]:
        """
        Return a list of the connections currently in the pool for the origin.
        """
        return self._pool[origin]
    

    def _get_or_add_connection(self, origin: Origin, timeout: float):
        origin_lock = self._get_or_add_origin_lock(origin)
        origin_lock.acquire(timeout = timeout)
        try:
            connection = self._get_connection_from_pool(origin)
            if connection is not None:
                return connection
            #if none found, add connection
            connection = self.create_connection(origin)
            self._add_to_pool(connection, timeout)
            return connection
        finally:
            origin_lock.release()

    
    def _get_or_add_origin_lock(self, origin: Origin) -> BoundedSemaphore:
        with self._pool_lock:
            return self._origin_locks[origin]
    

    def _remove_origin_lock(self, origin: Origin) -> None:
        with self._pool_lock:
            self._origin_locks.pop(origin)
    
    
    def _get_connection_from_pool(self, origin: Origin) -> Optional[ConnectionInterface]:
        self._close_and_remove_expired_connections_for_origin(origin)
        origin_connections = self._connections_for_origin(origin)
        for idx, connection in enumerate(origin_connections):
            if connection.is_available():
                origin_connections.pop(idx)
                origin_connections.insert(0, connection)
                return connection
    
    
    def create_connection(self, origin: Origin) -> ConnectionInterface:
        return HTTPConnection(
            origin=origin,
            ssl_context=self._ssl_context,
            keepalive_expiry=self._keepalive_expiry,
            http1=self._http1,
            http2=self._http2,
            retries=self._retries,
            local_address=self._local_address,
            uds=self._uds,
            network_backend=self._network_backend,
        )


    def _add_to_pool(self, connection: ConnectionInterface, timeout: float) -> None:
        #if connection pool is full, reactively try cleaning up expired & idle connections across pool and try again
        if self._try_add_connection_to_pool(connection):
            return
        #cleanup expired across pool and try again
        self._close_and_remove_all_expired_connections()
        if self._try_add_connection_to_pool(connection):
            return
        #remove idle connections
        self._close_and_remove_idle_connections()
        if self._try_add_connection_to_pool(connection):
            return
        self._acquire_connection_semaphores(timeout, True)
        self._connections_for_origin(connection.origin).insert(0, connection)

    
    def _try_add_connection_to_pool(self, connection: ConnectionInterface) -> None:
        if self._acquire_connection_semaphores(None, False):
            self._connections_for_origin(connection.origin).insert(0, connection)
            return True
        return False

    
    def _remove_from_pool(self, connection: ConnectionInterface) -> None:
        connections_for_origin = self._connections_for_origin(connection.origin)
        if connection in connections_for_origin:
            connections_for_origin.remove(connection)
            self._release_connection_semaphores()
            if not connections_for_origin:
                del connections_for_origin
                self._remove_origin_lock(connection.origin)

    
    def _close_and_remove_all_expired_connections(self) -> None:
        for origin, origin_lock in self._origin_locks_copy.items():
            if origin_lock.acquire(blocking = False):
                self._close_and_remove_expired_connections_for_origin(origin)
                origin_lock.release()
    

    def _close_and_remove_all_connections(self) -> None:
        for origin, origin_lock in self._origin_locks_copy.items():
            if origin_lock.acquire(blocking = False):
                self._close_and_remove_connections_for_origin(origin)
                origin_lock.release()
            else:
                raise LockNotAvailable(f"Couldn't acquire lock for origin {origin}")
    
    
    def _close_and_remove_idle_connections(self) -> None:
        for origin, origin_lock in self._origin_locks_copy.items():
            if not self._is_pool_full_or_keepalive_reached():
                return
            if origin_lock.acquire(blocking = False):
                while self._is_pool_full_or_keepalive_reached() and self._close_and_remove_lru_idle_connection_for_origin(origin):
                    continue
                origin_lock.release()                
    

    def _is_pool_full_or_keepalive_reached(self) -> bool:
        return self._is_pool_full() or self._has_keepalive_reached_maximum()

    
    #using semapahore to check if pool is full
    def _is_pool_full(self) -> bool:
        if self._connection_semaphore.acquire(blocking = False):
            self._connection_semaphore.release()
            return False
        return True

    
    #keep alive check
    def _has_keepalive_reached_maximum(self) -> bool:
        if self._keepalive_semaphore.acquire(blocking = False):
            self._keepalive_semaphore.release()
            return False
        return True

    
    def _close_and_remove_expired_connections_for_origin(self, origin: Origin) -> None:
        self._close_and_remove_expired_connections(self._connections_for_origin(origin))

    
    def _close_and_remove_expired_connections(self, connections: List[ConnectionInterface]) -> None:
        for connection in reversed(list(connections)):
            if connection.has_expired():
                connection.close()
            if connection.is_closed():
                self._remove_from_pool(connection)


    def _close_and_remove_connections_for_origin(self, origin: Origin) -> None:
        self._close_and_remove_connections(self._connections_for_origin(origin))


    def _close_and_remove_connections(self, connections: List[ConnectionInterface]) -> None:
        for connection in reversed(list(connections)):
            connection.close()
            self._remove_from_pool(connection)


    def _close_and_remove_lru_idle_connection_for_origin(self, origin: Origin) -> None:
        return self._close_and_remove_lru_idle_connection(self._connections_for_origin(origin))


    def _close_and_remove_lru_idle_connection(self, connections: List[ConnectionInterface]) -> None:
        if(len(connections)) == 0:
            return False
        for connection in reversed(list(connections)):
            if connection.is_idle():
                connection.close()
                self._remove_from_pool(connection)
                return True
        return False
    

    def _acquire_connection_semaphores(self, timeout: float, blocking: bool) -> bool:
        rv = self._connection_semaphore.acquire(timeout = timeout, blocking = blocking)
        self._keepalive_semaphore.acquire(blocking = False)
        return rv


    def _release_connection_semaphores(self) -> None:
        self._connection_semaphore.release()
        #Keep alive sempahore release count can be > number of acquires as each removal of connection from connection pool
        #(regardless of within or outside keep alive max limit) 
        #will call keepalive semapahore release. So ignoring exception during release
        try:
            self._keepalive_semaphore.release()
        except ValueError:
            pass


    def handle_request(self, request: Request) -> Response:
        """
        Send an HTTP request, and return an HTTP response.
        This is the core implementation that is called into by `.request()` or `.stream()`.
        """
        scheme = request.url.scheme.decode()
        if scheme == "":
            raise UnsupportedProtocol(
                "Request URL is missing an 'http://' or 'https://' protocol."
            )
        if scheme not in ("http", "https"):
            raise UnsupportedProtocol(
                f"Request URL has an unsupported protocol '{scheme}://'."
            )

        while True:
            timeouts = request.extensions.get("timeout", {})
            timeout = timeouts.get("pool", None)
            origin = request.url.origin
            connection = self._get_or_add_connection(origin, timeout)
            try:
                response = connection.handle_request(request)
            except ConnectionNotAvailable:
                # The ConnectionNotAvailable exception is a special case, that
                # indicates we need to retry the request on a new connection.
                #
                # The most common case where this can occur is when multiple
                # requests are queued waiting for a single connection, which
                # might end up as an HTTP/2 connection, but which actually ends
                # up as HTTP/1.1.
                connection = self._get_or_add_connection(origin, timeout)
            except Exception as exc:
                self.response_closed(connection)
                raise exc
            else:
                break

        # When we return the response, we wrap the stream in a special class
        # that handles notifying the connection pool once the response
        # has been released.
        assert isinstance(response.stream, Iterable)
        return Response(
            status=response.status,
            headers=response.headers,
            content=ConnectionPoolByteStream(response.stream, self, request, connection),
            extensions=response.extensions,
        )

    def response_closed(self, connection: ConnectionInterface) -> None:
        """
        This method acts as a callback once the request/response cycle is complete.

        It is called into from the `ConnectionPoolByteStream.close()` method.
        """
        assert connection is not None

        #remove closed connection if able to acquire lock
        if connection.is_closed():
            origin_lock = self._get_or_add_origin_lock(connection.origin)
            if origin_lock.acquire(blocking = False):
                self._remove_from_pool(connection)
                origin_lock.release()

        # Housekeeping
        self._close_and_remove_all_expired_connections()
        self._close_and_remove_idle_connections()


    def close(self) -> None:
        """
        Close any connections in the pool.
        """
        self._close_and_remove_all_connections()
        self._pool = defaultdict(list)

    def __enter__(self) -> "ConnectionPool":
        return self

    def __exit__(
        self,
        exc_type: Type[BaseException] = None,
        exc_value: BaseException = None,
        traceback: TracebackType = None,
    ) -> None:
        self.close()



class ConnectionPoolByteStream:
    """
    A wrapper around the response byte stream, that additionally handles
    notifying the connection pool when the response has been closed.
    """

    def __init__(
        self,
        stream: Iterable[bytes],
        pool: ConnectionPool,
        request: Request,
        connection: ConnectionInterface
    ) -> None:
        self._stream = stream
        self._pool = pool
        self._request = request
        self._connection = connection

    def __iter__(self) -> Iterator[bytes]:
        for part in self._stream:
            yield part

    def close(self) -> None:
        try:
            if hasattr(self._stream, "close"):
                self._stream.close()  # type: ignore
        finally:
            self._pool.response_closed(self._connection)