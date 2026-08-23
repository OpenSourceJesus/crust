"""hostsim_net.py - connect a simulated fleet to real network services.

A :class:`Fleet` on its own is a closed world. This joins it to one that is
not: a TCP service on the other end of a socket, which might be a telemetry
collector, a fleet controller under test, a message broker, or the rest of a
CI pipeline. Neither side needs to know the other is simulated.

    from hostsim import Fleet, Sim
    from hostsim_net import SocketBridge

    bridge = SocketBridge.connect("localhost", 9000, name="telemetry")
    fleet = Fleet([Sim(so, name="axis-x")], endpoints=[bridge])
    fleet.start()
    for _ in range(1000):
        fleet.step_ms(1)          # boards run, then messages cross the wire

A bridge is just another participant in routing: it exposes the same
``link_pop_all`` and ``link_push`` a board does, so a router addresses it the
same way and nothing else has to know it is a socket.

**Framing.** Messages on the link are whole messages, and a TCP stream is not,
so each is sent as a four-byte big-endian length followed by the payload. A
peer that speaks something else needs its own codec -- pass ``codec=`` rather
than reaching into the buffers.

**Time.** The fleet runs on virtual time and the socket does not. A bridge is
therefore polled, never blocked on: whatever has arrived by the time a step
ends is delivered at the next one, and the simulation never waits for the
network. That makes runs reproducible in a way they would not be if a slow
peer could stall the clock -- but it does mean a bridged run is no longer
deterministic in the way a closed fleet is, because what arrives depends on
real timing. Assert on what was exchanged, not on exactly when.
"""

import socket
import struct
import threading

#: Four-byte big-endian length prefix.
LENGTH = struct.Struct(">I")

#: Refuse anything larger, rather than trying to allocate it. Matches NET_MTU
#: in hostsim/hostsim.c; a longer message could not be delivered to a board
#: anyway.
MAX_MESSAGE = 512


class LengthPrefixed:
    """Default codec: four-byte big-endian length, then payload."""

    @staticmethod
    def encode(message):
        return LENGTH.pack(len(message)) + message

    @staticmethod
    def decode(buffer):
        """Return (messages, remaining_buffer)."""
        messages = []
        while len(buffer) >= LENGTH.size:
            (size,) = LENGTH.unpack_from(buffer)
            if size > MAX_MESSAGE:
                raise ValueError(
                    "framed message of %d bytes exceeds the %d-byte limit; "
                    "the peer is probably not speaking this framing"
                    % (size, MAX_MESSAGE))
            if len(buffer) < LENGTH.size + size:
                break
            start = LENGTH.size
            messages.append(bytes(buffer[start:start + size]))
            buffer = buffer[start + size:]
        return messages, buffer


class Newline:
    """Alternative codec for line-oriented peers."""

    @staticmethod
    def encode(message):
        return message.rstrip(b"\n") + b"\n"

    @staticmethod
    def decode(buffer):
        messages = []
        while b"\n" in buffer:
            line, _, buffer = buffer.partition(b"\n")
            if line:
                messages.append(bytes(line))
        return messages, buffer


class SocketBridge:
    """One endpoint joining a fleet to a socket.

    Constructed through :meth:`connect` (dial out) or :meth:`listen` (accept
    one peer). Both are non-blocking once established.
    """

    def __init__(self, sock, name="socket", codec=LengthPrefixed):
        self.name = name
        self.codec = codec
        self._sock = sock
        self._sock.setblocking(False)
        self._inbound = bytearray()
        self._pending = []          # decoded, waiting for the fleet
        self._outbound = bytearray()
        self.closed = False
        #: Counters a test can assert on.
        self.sent = 0
        self.received = 0

    # -- construction ------------------------------------------------
    @classmethod
    def connect(cls, host, port, name=None, codec=LengthPrefixed,
                timeout=5.0):
        """Dial an existing service."""
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return cls(sock, name=name or "%s:%d" % (host, port), codec=codec)

    @classmethod
    def listen(cls, port=0, host="127.0.0.1", name=None,
               codec=LengthPrefixed, timeout=5.0):
        """Wait for one peer to connect, then bridge to it.

        Returns (bridge, port) so a caller that passed port 0 can find out
        which port was chosen.
        """
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(1)
        chosen = server.getsockname()[1]
        server.settimeout(timeout)
        try:
            peer, address = server.accept()
        finally:
            server.close()
        peer.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return cls(peer, name=name or "peer:%d" % address[1],
                   codec=codec), chosen

    # -- the endpoint interface a Fleet uses -------------------------
    def link_push(self, message):
        """Queue a message for the peer. Sent on the next poll."""
        if self.closed:
            return False
        if isinstance(message, str):
            message = message.encode()
        self._outbound.extend(self.codec.encode(message))
        self.sent += 1
        return True

    def link_pop_all(self):
        """Everything the peer has sent since the last call."""
        messages, self._pending = self._pending, []
        return messages

    def poll(self):
        """Move bytes both ways. Called by :class:`Fleet` after each step."""
        if self.closed:
            return
        self._flush()
        self._fill()

    def _flush(self):
        while self._outbound:
            try:
                n = self._sock.send(self._outbound)
            except BlockingIOError:
                return              # peer is not reading; try again next step
            except OSError:
                self.close()
                return
            if n == 0:
                return
            del self._outbound[:n]

    def _fill(self):
        while True:
            try:
                chunk = self._sock.recv(4096)
            except BlockingIOError:
                break
            except OSError:
                self.close()
                return
            if not chunk:
                self.close()        # peer hung up
                return
            self._inbound.extend(chunk)

        if self._inbound:
            messages, self._inbound = self.codec.decode(self._inbound)
            if not isinstance(self._inbound, bytearray):
                self._inbound = bytearray(self._inbound)
            self._pending.extend(messages)
            self.received += len(messages)

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self._sock.close()
            except OSError:
                pass

    def __repr__(self):
        return "<SocketBridge %s sent=%d received=%d%s>" % (
            self.name, self.sent, self.received,
            " closed" if self.closed else "")


class EchoService(threading.Thread):
    """A trivial peer, for tests and for trying the bridge out.

    Accepts one connection and echoes each framed message back with a prefix,
    which is enough to show that a real socket round trip happened rather than
    a message being handed back locally.
    """

    def __init__(self, prefix=b"ack:", codec=LengthPrefixed, host="127.0.0.1"):
        super().__init__(daemon=True)
        self.prefix = prefix
        self.codec = codec
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((host, 0))
        self._server.listen(1)
        self.port = self._server.getsockname()[1]
        self.messages = []
        self._stop = threading.Event()

    def run(self):
        self._server.settimeout(5.0)
        try:
            peer, _ = self._server.accept()
        except OSError:
            return
        finally:
            self._server.close()

        peer.settimeout(0.2)
        buffer = bytearray()
        with peer:
            while not self._stop.is_set():
                try:
                    chunk = peer.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buffer.extend(chunk)
                messages, buffer = self.codec.decode(buffer)
                if not isinstance(buffer, bytearray):
                    buffer = bytearray(buffer)
                for message in messages:
                    self.messages.append(message)
                    try:
                        peer.sendall(self.codec.encode(
                            self.prefix + message))
                    except OSError:
                        return

    def stop(self):
        self._stop.set()
