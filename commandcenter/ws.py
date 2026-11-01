"""WebSocket, server side, on top of the standard library HTTP server.

A terminal needs bytes to travel both ways, continuously, with no request boundary. That is a
WebSocket, and nothing else in the standard library does it.

Written by hand rather than taken from a package for a plain reason: the only WebSocket
library on this machine is asyncio-only, and the console's HTTP server is threaded. Mixing the
two models means a second port, a second event loop, and a second thing to keep running for
years. The server half of RFC 6455 is a hash and a frame reader, so this is the cheaper trade.

Frames are used with a convention that saves a lot of parsing:

    binary frame  raw terminal bytes, exactly as the pseudo-terminal produced them
    text frame    a JSON control message: resize, status, keystroke acknowledgement

Terminal output therefore never needs escaping, and the reader never needs to guess.
"""

import base64
import hashlib
import json
import socket
import struct
import threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

TEXT, BINARY, CLOSE, PING, PONG = 0x1, 0x2, 0x8, 0x9, 0xA


def accept_key(client_key):
    """The handshake answer: SHA1 of the client's key joined to a fixed string, base64.

    The value proves the server understood the WebSocket handshake rather than merely
    returning 101 to anything that asked, which is what stops a confused proxy from being
    mistaken for a WebSocket endpoint.
    """
    digest = hashlib.sha1((client_key + GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def is_upgrade(headers):
    return (headers.get("Upgrade", "").lower() == "websocket"
            and "upgrade" in headers.get("Connection", "").lower()
            and headers.get("Sec-WebSocket-Key"))


class WebSocket:
    """One connection. Reading happens on the calling thread; writing is locked.

    A terminal has one reader and several writers: the pseudo-terminal thread pushes output
    while the browser's keystrokes arrive on this connection's own thread. Only the write
    side needs the lock, because a half-written frame would corrupt the stream for good.
    """

    def __init__(self, conn):
        self.conn = conn
        self.lock = threading.Lock()
        self.closed = False
        # A terminal connection is idle for long stretches and must not be timed out.
        self.conn.settimeout(None)

    # -- handshake ---------------------------------------------------------------

    @classmethod
    def handshake(cls, handler):
        """Answer the upgrade on a BaseHTTPRequestHandler and take over its socket.

        After this the handler must not write anything else. It has no response to send: the
        connection is no longer speaking HTTP.
        """
        key = handler.headers.get("Sec-WebSocket-Key")
        lines = [
            "HTTP/1.1 101 Switching Protocols",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Accept: {accept_key(key)}",
            "", "",
        ]
        handler.wfile.write("\r\n".join(lines).encode())
        handler.wfile.flush()
        handler.close_connection = True
        return cls(handler.connection)

    # -- writing -----------------------------------------------------------------

    def _send(self, opcode, payload=b""):
        if self.closed:
            return False
        header = bytearray([0x80 | opcode])
        n = len(payload)
        # Three length encodings, chosen by size. Server frames are never masked, which is
        # the one asymmetry in the protocol: only clients mask.
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack("!H", n)
        else:
            header.append(127)
            header += struct.pack("!Q", n)
        try:
            with self.lock:
                self.conn.sendall(bytes(header) + payload)
            return True
        except OSError:
            self.closed = True
            return False

    def send_bytes(self, data):
        return self._send(BINARY, data)

    def send_json(self, obj):
        return self._send(TEXT, json.dumps(obj).encode())

    def ping(self):
        return self._send(PING)

    def close(self, code=1000, reason=b""):
        if not self.closed:
            self._send(CLOSE, struct.pack("!H", code) + reason)
            self.closed = True
            try:
                self.conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # -- reading -----------------------------------------------------------------

    def _recv_exactly(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("peer closed")
            buf += chunk
        return buf

    def recv(self):
        """Return (opcode, payload), or None when the peer has gone away.

        Continuation frames are joined here so callers never see a partial message. A browser
        splitting a keystroke across frames is legal and would otherwise be a rare bug that
        only appears under load.
        """
        payload, first_opcode = b"", None
        while True:
            try:
                b1, b2 = self._recv_exactly(2)
            except (ConnectionError, OSError):
                self.closed = True
                return None
            fin, opcode = b1 & 0x80, b1 & 0x0F
            masked, length = b2 & 0x80, b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exactly(8))[0]
            # A frame large enough to exhaust memory is either a bug or an attack, and the
            # console never needs one: keystrokes are tiny and resize messages smaller.
            if length > 4 << 20:
                self.close(1009, b"frame too large")
                return None
            mask = self._recv_exactly(4) if masked else None
            data = self._recv_exactly(length) if length else b""
            if mask:
                data = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))

            if opcode == CLOSE:
                self.closed = True
                return None
            if opcode == PING:
                self._send(PONG, data)
                continue
            if opcode == PONG:
                continue

            if first_opcode is None:
                first_opcode = opcode
            payload += data
            if fin:
                return first_opcode, payload
