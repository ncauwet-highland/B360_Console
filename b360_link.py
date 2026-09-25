#!/usr/bin/env python3
"""Transport and session layer for talking to a B360/B960 over its CLI.

One `Board` owns one connection, and every command goes through a lock, because
the firmware cannot cope with two conversations at once:

  * There is exactly one TCP socket (socket 6, no accept backlog), so a second
    client simply cannot connect while the first holds it.
  * `cmd_src` is a single global set per-command in `process_commands()`, so a
    command arriving on USB part-way through an Ethernet reply reroutes the rest
    of that reply to `printf`. Mixing transports mid-transfer corrupts data.

Framing rules this layer exists to get right:

  * TCP has no line framing at all. Whatever bytes are in the WIZnet RX FIFO on
    a poll pass become one command line, so commands must be written one at a
    time with the reply read before the next write. Pipelining corrupts parsing.
  * A reply is "\\r\\n", then one token per command in input order, then "\\r\\n".
    Every token ends with "; " -- `OK; `, `ERR <msg>; `, `<value>; ` -- so the
    whole reply ends with "; \\r\\n". That is the terminator to look for.
  * A `WR` dump is thousands of CRLF-separated rows inside a single token, so a
    reader that stops at the first CRLF stops on row one. Do not do that.

Transports: TCP needs nothing but the standard library. Serial needs pyserial,
imported lazily so that its absence never breaks the TCP path.
"""

import socket
import select
import threading
import time

#: The command server. NOT 5008 -- that is PORT_UDP6. Some older tooling and
#: docs in this repo say 5008; include/wiznet_main.h:86 is the authority.
DEFAULT_TCP_PORT = 5007

#: USB CDC ignores the rate, but pyserial insists on one.
DEFAULT_BAUD = 115200

DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_TIMEOUT = 3.0

#: Settle before discarding whatever is still in flight, ahead of every command.
#: Long enough that the tail of the previous reply has landed -- a tail that
#: arrives late gets attributed to the next command -- and short enough not to
#: dominate a capture, which pays this once per command across dozens of them.
DRAIN_SETTLE_S = 0.02

#: End of a complete reply: the last token's "; " plus the frame's closing CRLF.
REPLY_END = b"; \r\n"

#: What "pressing Enter" sends. Selectable because the two transports disagree:
#:
#: Over TCP the whole write is taken as one line, so any of the three works and
#: CRLF is the conventional choice.
#:
#: Over USB it matters. read_to_process() (helper_funcs.c:1727) terminates on
#: '\r' OR '\n' with no pair suppression, so a CRLF ends the line twice -- the
#: second, empty line draws its own reply, and an empty line is answered with the
#: model name (custom_cmd.c:2307). So CRLF over USB produces a spurious extra
#: "B360; " after everything. CR alone is what PuTTY and TeraTerm send, and is
#: the default here for serial.
#: Command delimiters the firmware splits a multi-command line on (':' only when
#: the line has no MAC address in it). Trailing ones separate nothing, so they are
#: removed before the line ending is appended -- see command().
CMD_DELIMS = " \t;:"

EOL_CHOICES = {"crlf": "\r\n", "cr": "\r", "lf": "\n"}
DEFAULT_EOL_TCP = "crlf"
DEFAULT_EOL_SERIAL = "cr"


def eol_bytes(name):
    """Map an --eol name to the characters it sends. Unknown names fall back."""
    return EOL_CHOICES.get(str(name).lower().strip(), EOL_CHOICES["crlf"])


class LinkError(Exception):
    """Connection could not be opened, or died mid-conversation."""


class _Reader:
    """Shared read loop. Subclasses provide `_recv()` and `_send()`."""

    def read_reply(self, timeout=DEFAULT_TIMEOUT, idle_gap=0.20,
                   terminator=REPLY_END):
        """Collect one reply.

        Three ways to stop, in priority order: the terminator arrives; the link
        goes quiet for `idle_gap` with something already collected; or `timeout`
        expires. The idle-gap fallback matters because a few commands (the help
        menus, `ID`) do not end in a value token, and `ID` even emits "\\n\\r"
        rather than "\\r\\n".
        """
        buf = bytearray()
        deadline = time.monotonic() + timeout
        last_rx = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            chunk = self._recv(min(remaining, 0.02))
            if chunk:
                buf.extend(chunk)
                last_rx = time.monotonic()
                if terminator and buf.endswith(terminator):
                    break
            elif buf and last_rx is not None and (time.monotonic() - last_rx) >= idle_gap:
                break
        return bytes(buf)

    def drain(self, settle=DRAIN_SETTLE_S):
        """Discard anything still in flight before writing.

        The deliberate settle is the point: a reply whose tail has not arrived
        yet reads as clean, and that tail then lands at the head of the next
        reply and gets attributed to the wrong command.
        """
        time.sleep(settle)
        while self._recv(0.0):
            pass


class TcpTransport(_Reader):
    """Command socket over TCP. Standard library only."""

    def __init__(self, host, port=DEFAULT_TCP_PORT,
                 connect_timeout=DEFAULT_CONNECT_TIMEOUT):
        self.name = "tcp://%s:%d" % (host, port)
        self.peer = ""
        try:
            self._sock = socket.create_connection((host, port), timeout=connect_timeout)
        except socket.gaierror as exc:
            # Resolution, not connection -- the "close PuTTY" hint below would be
            # actively misleading here, and a .local name fails this way often
            # enough to deserve its own answer.
            hint = ""
            if host.lower().endswith(".local"):
                hint = ("\nA .local name needs mDNS. Two things break it: the "
                        "environment (WSL and most containers cannot, because "
                        "their multicast never reaches the LAN), and the record "
                        "expiring -- the board announces itself with a 120 s TTL, "
                        "so the name can work right after it boots and stop "
                        "working later. `ping %s` from the same shell is the quick "
                        "check; if that fails too, use the board's IP address "
                        "here." % host)
            raise LinkError("cannot resolve %s (%s: %s).%s"
                            % (host, exc.__class__.__name__, exc, hint))
        except OSError as exc:
            raise LinkError(
                "cannot connect to %s (%s: %s).\n"
                "If a terminal (PuTTY/TeraTerm) is already connected, close it -- "
                "the board has only one command socket."
                % (self.name, exc.__class__.__name__, exc))
        self._sock.setblocking(False)
        # Record the address that answered. The .local name is fixed to this
        # board -- the firmware builds it from the model and serial held in
        # EEPROM -- but the address behind it is a DHCP lease and does move, so
        # the capture says where the board was as well as what it is.
        try:
            self.peer = self._sock.getpeername()[0]
            if self.peer and self.peer != host:
                self.name = "tcp://%s:%d (%s)" % (host, port, self.peer)
        except OSError:
            pass

    def _recv(self, wait):
        try:
            ready, _, _ = select.select([self._sock], [], [], wait)
        except (OSError, ValueError):
            raise LinkError("connection to %s was closed" % self.name)
        if not ready:
            return b""
        try:
            data = self._sock.recv(4096)
        except BlockingIOError:
            return b""
        except OSError as exc:
            raise LinkError("read from %s failed: %s" % (self.name, exc))
        if data == b"":
            raise LinkError("%s closed the connection" % self.name)
        return data

    def peer_gone(self):
        """True if the peer has closed or reset. Sends nothing, consumes nothing.

        MSG_PEEK leaves any real data in the buffer for read_reply(), so this can
        run between commands without stealing a byte. It cannot see a board that
        was unplugged or powered off -- that leaves no FIN behind, and only a
        keepalive or a command would find out.
        """
        try:
            ready, _, _ = select.select([self._sock], [], [], 0)
            if not ready:
                return False
            return self._sock.recv(1, socket.MSG_PEEK) == b""
        except BlockingIOError:
            return False
        except OSError:
            return True

    def write(self, data):
        try:
            self._sock.sendall(data)
        except OSError as exc:
            raise LinkError("write to %s failed: %s" % (self.name, exc))

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


class SerialTransport(_Reader):
    """Command interface over the USB CDC port. Needs pyserial.

    pyserial rather than termios so this works on Windows, where the executable
    actually runs.
    """

    def __init__(self, port, baud=DEFAULT_BAUD,
                 connect_timeout=DEFAULT_CONNECT_TIMEOUT):
        try:
            import serial
        except ImportError:
            raise LinkError(
                "serial support needs pyserial (pip install pyserial); "
                "use TCP instead, or rebuild the executable with it bundled.")
        self.name = "serial://%s" % port
        try:
            self._port = serial.Serial(port, baudrate=baud, timeout=0,
                                       write_timeout=connect_timeout)
        except Exception as exc:
            raise LinkError("cannot open %s (%s: %s)"
                            % (self.name, exc.__class__.__name__, exc))

    def _recv(self, wait):
        n = self._port.in_waiting
        if not n:
            if wait:
                time.sleep(min(wait, 0.02))
            n = self._port.in_waiting
            if not n:
                return b""
        try:
            return self._port.read(n)
        except Exception as exc:
            raise LinkError("read from %s failed: %s" % (self.name, exc))

    def peer_gone(self):
        """True once the CDC port has left the OS. Sends nothing.

        Unplugging the board removes the port, which is visible without touching
        it. A board still enumerated but not answering looks fine here.
        """
        try:
            if not self._port.is_open:
                return True
        except Exception:
            return True
        want = self.name.split("://", 1)[-1].lower()
        try:
            import serial.tools.list_ports
            return not any(p.device.lower() == want
                           for p in serial.tools.list_ports.comports())
        except Exception:
            return False            #< cannot enumerate: do not cry wolf

    def write(self, data):
        try:
            self._port.write(data)
            self._port.flush()
        except Exception as exc:
            raise LinkError("write to %s failed: %s" % (self.name, exc))

    def close(self):
        try:
            self._port.close()
        except Exception:
            pass


def list_serial_ports():
    """Candidate CDC ports, RP2040 first. Empty list if pyserial is missing."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    found = []
    for p in list_ports.comports():
        found.append({"device": p.device,
                      "description": p.description or "",
                      "rp2040": getattr(p, "vid", None) == 0x2E8A})
    found.sort(key=lambda d: not d["rp2040"])
    return found


class Board:
    """One owned conversation with one board.

    Every command is serialized through `_lock`, so a waveform capture can never
    interleave with a command typed in the terminal.
    """

    def __init__(self, transport, eol=DEFAULT_EOL_TCP):
        self.transport = transport
        self._lock = threading.RLock()
        self.closed = False
        self.eol = eol_bytes(eol)

    @classmethod
    def tcp(cls, host, port=DEFAULT_TCP_PORT, connect_timeout=DEFAULT_CONNECT_TIMEOUT,
            eol=DEFAULT_EOL_TCP):
        return cls(TcpTransport(host, port, connect_timeout), eol)

    @classmethod
    def serial(cls, port, baud=DEFAULT_BAUD, connect_timeout=DEFAULT_CONNECT_TIMEOUT,
               eol=DEFAULT_EOL_SERIAL):
        return cls(SerialTransport(port, baud, connect_timeout), eol)

    @property
    def name(self):
        return self.transport.name

    def command(self, line, timeout=DEFAULT_TIMEOUT, idle_gap=0.20):
        """Send one command line and return its reply as text.

        One command per write, reply read before the next write -- see the
        module docstring on why pipelining corrupts parsing.

        An empty `line` sends just the line ending, which is what hitting Enter
        in a terminal does. That is not a no-op: the firmware answers a line with
        no command on it with its model name, so it doubles as an identity check.

        A trailing ';' is dropped, because over TCP it would otherwise draw a
        spurious extra reply. The firmware tokenises the line it receives on
        ";:", and the TCP path hands it the bytes exactly as they arrive --
        custom_tcps() passes the receive buffer straight to process_commands()
        with the line ending still on it (loopback.c:155). So "WG; WS;\\r\\n"
        splits into "WG", " WS" and "\\r\\n", and that third token reaches the
        parser's default case, which answers a line carrying no command with the
        model name (custom_cmd.c:2307) -- a stray "B360; " after the real
        replies. Over USB the same line is already fine: read_to_process() ends
        the line at the '\\r' and never stores it (helper_funcs.c:1731-1742), so
        the trailing ';' is just a trailing delimiter and strtok_r skips it.
        Typing the ';' or leaving it off now behaves the same on both links.
        """
        if self.closed:
            raise LinkError("link is closed")
        line = line.rstrip(CMD_DELIMS)
        with self._lock:
            self.transport.drain()
            self.transport.write((line + self.eol).encode("ascii", "replace"))
            raw = self.transport.read_reply(timeout=timeout, idle_gap=idle_gap)
        return raw.decode("ascii", errors="replace")

    def close(self):
        with self._lock:
            if not self.closed:
                self.closed = True
                self.transport.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def strip_frame(reply):
    """Reply text minus the frame CRLFs and the trailing '; '."""
    s = reply.strip("\r\n")
    if s.endswith("; "):
        s = s[:-2]
    elif s.endswith(";"):
        s = s[:-1]
    return s.strip()
