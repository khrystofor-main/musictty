"""mpv's JSON IPC: a unix socket on Linux/macOS, a named pipe on Windows.

One JSON object per line. Replies carry our request_id; events (file-loaded, end-file,
client-message, ...) come in between and are queued for whoever reads them.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import sys
from typing import Any

# the playlist property with direct stream URLs easily exceeds asyncio's default 64 KiB per line
LINE_LIMIT = 16 * 1024 * 1024


class MpvError(Exception):
    """A command failed, or mpv went away."""


class NotRunning(MpvError):
    """Nothing listens at the IPC address."""


async def _open(address: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if sys.platform != "win32":
        return await asyncio.open_unix_connection(address, limit=LINE_LIMIT)
    # Windows: the default proactor event loop speaks named pipes
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=LINE_LIMIT, loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
    transport, _ = await loop.create_pipe_connection(lambda: protocol, address)
    return reader, asyncio.StreamWriter(transport, protocol, reader, loop)


class Mpv:
    """An IPC connection to a running mpv."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._pending: dict[int, asyncio.Future] = {}
        self._events: asyncio.Queue[dict | None] = asyncio.Queue()
        self._ids = itertools.count(1)
        self._closed = False
        self._reader_task = asyncio.create_task(self._read_loop())

    @classmethod
    async def connect(cls, address: str, timeout: float = 1.0) -> Mpv:
        try:
            reader, writer = await asyncio.wait_for(_open(address), timeout)
        except (OSError, TimeoutError) as e:
            raise NotRunning(address) from e
        return cls(reader, writer)

    @property
    def closed(self) -> bool:
        return self._closed

    async def _read_loop(self) -> None:
        try:
            while line := await self._reader.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "event" in msg:
                    self._events.put_nowait(msg)
                    continue
                future = self._pending.pop(msg.get("request_id"), None)
                if future and not future.done():
                    future.set_result(msg)
        except (OSError, ValueError, asyncio.IncompleteReadError):
            pass
        finally:
            self._closed = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(MpvError("mpv closed the connection"))
                    # its waiter may be cancelled at this very moment: don't warn about it
                    future.exception()
            self._pending.clear()
            self._events.put_nowait(None)

    async def command(self, *args: Any, timeout: float | None = None) -> Any:
        """Run a command and return its data.

        Positional form: command("playlist-next", "force"); named form: command({"name": ...}).
        """
        cmd = args[0] if len(args) == 1 and isinstance(args[0], dict) else list(args)
        name = cmd["name"] if isinstance(cmd, dict) else cmd[0]
        if self._closed:
            raise MpvError(f"{name}: mpv is not connected")
        request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message = {"command": cmd, "request_id": request_id}
        try:
            self._writer.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
            await self._writer.drain()
            reply = await asyncio.wait_for(future, timeout)
        except (OSError, TimeoutError) as e:
            raise MpvError(f"{name}: {e or 'timed out'}") from e
        finally:
            self._pending.pop(request_id, None)
        if reply.get("error") != "success":
            raise MpvError(f"{name}: {reply.get('error')}")
        return reply.get("data")

    async def get(self, name: str, default: Any = None, timeout: float | None = None) -> Any:
        """A property's value, or `default` if mpv has none right now."""
        try:
            return await self.command("get_property", name, timeout=timeout)
        except MpvError:
            if self._closed:
                raise
            return default

    async def set(self, name: str, value: Any) -> None:
        await self.command("set_property", name, value)

    async def next_event(self) -> dict | None:
        """The next event, or None once mpv is gone."""
        return await self._events.get()

    async def close(self) -> None:
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()
        self._reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._reader_task
