"""Make "offline" a guarantee instead of a promise.

When the offline engine is selected, the app blocks every outgoing internet
connection from its own process (IPv4 and IPv6). Local sockets such as the
X11 display connection still work. The one exception is a model download you
approve, which runs inside ``allow_network()``, fetches public model files
only, and never sends screen content.
"""

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager

_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_getaddrinfo = socket.getaddrinfo
_state = threading.local()
_enabled = False


class NetworkBlocked(ConnectionRefusedError):
    pass


def _allowed() -> bool:
    return not _enabled or getattr(_state, "allow", 0) > 0


def _is_internet(sock: socket.socket) -> bool:
    return sock.family in (socket.AF_INET, socket.AF_INET6)


def _guarded_connect(self, address):
    if _is_internet(self) and not _allowed():
        raise NetworkBlocked(f"Scan-screen is in offline mode: blocked connection to {address!r}")
    return _original_connect(self, address)


def _guarded_connect_ex(self, address):
    if _is_internet(self) and not _allowed():
        raise NetworkBlocked(f"Scan-screen is in offline mode: blocked connection to {address!r}")
    return _original_connect_ex(self, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if not _allowed() and host not in (None, "localhost", "127.0.0.1", "::1"):
        raise NetworkBlocked(f"Scan-screen is in offline mode: blocked lookup of {host!r}")
    return _original_getaddrinfo(host, *args, **kwargs)


def enforce_offline() -> None:
    """Block internet access for the rest of this process (except allow_network())."""
    global _enabled
    _enabled = True
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.getaddrinfo = _guarded_getaddrinfo


def disable() -> None:
    """Undo enforce_offline() (used when switching to an online engine, and in tests)."""
    global _enabled
    _enabled = False
    socket.socket.connect = _original_connect
    socket.socket.connect_ex = _original_connect_ex
    socket.getaddrinfo = _original_getaddrinfo


def is_offline() -> bool:
    return _enabled


@contextmanager
def allow_network():
    """Temporarily allow connections from the current thread only (model downloads)."""
    _state.allow = getattr(_state, "allow", 0) + 1
    try:
        yield
    finally:
        _state.allow -= 1
