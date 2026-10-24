"""A tiny stdlib-only DNS client used to generate traffic against a Pi-hole.

It builds RFC 1035 queries, sends them over UDP (falling back to TCP when the
answer is truncated, or on request) and parses the response header, the
question and the answer records well enough to report the rcode and the
answers. It is deliberately small: no DNSSEC validation, no EDNS options
beyond an OPT record advertising a larger UDP payload.

Example::

    >>> from helpers import dnsgen
    >>> reply = dnsgen.query("example.com", "AAAA", server="127.0.0.1", port=18153)
    >>> reply.rcode_name, [a.data for a in reply.answers]
    ('NOERROR', ['2606:2800:21f:cb07:6820:80da:af6b:8b2c'])

:func:`query_many` sends a whole batch concurrently and never raises for an
individual query; failures are returned as :class:`DnsQueryError` values.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import random
import socket
import struct
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "QTYPES",
    "RCODES",
    "DnsQueryError",
    "DnsResponse",
    "ResourceRecord",
    "build_query",
    "encode_name",
    "parse_response",
    "qtype_code",
    "qtype_name",
    "query",
    "query_many",
    "reverse_name",
]

#: Query type mnemonics → numeric codes (the ones useful for traffic generation).
QTYPES: dict[str, int] = {
    "A": 1,
    "NS": 2,
    "CNAME": 5,
    "SOA": 6,
    "PTR": 12,
    "HINFO": 13,
    "MX": 15,
    "TXT": 16,
    "AAAA": 28,
    "SRV": 33,
    "NAPTR": 35,
    "DS": 43,
    "RRSIG": 46,
    "DNSKEY": 48,
    "SVCB": 64,
    "HTTPS": 65,
    "ANY": 255,
    "CAA": 257,
}

#: Response codes → mnemonic.
RCODES: dict[int, str] = {
    0: "NOERROR",
    1: "FORMERR",
    2: "SERVFAIL",
    3: "NXDOMAIN",
    4: "NOTIMP",
    5: "REFUSED",
    6: "YXDOMAIN",
    7: "YXRRSET",
    8: "NXRRSET",
    9: "NOTAUTH",
    10: "NOTZONE",
}

_QTYPE_NAMES = {code: name for name, code in QTYPES.items()}
_CLASS_IN = 1
_TYPE_OPT = 41
_HEADER = struct.Struct(">HHHHHH")
_MAX_LABEL = 63
_MAX_NAME = 253
_NULL_ANSWERS = frozenset({"0.0.0.0", "::"})  # noqa: S104 - data, not a bind address


class DnsQueryError(Exception):
    """A query could not be completed (timeout, socket error, malformed reply)."""

    def __init__(self, name: str, qtype: str, reason: str) -> None:
        super().__init__(f"{name} {qtype}: {reason}")
        self.name = name
        self.qtype = qtype
        self.reason = reason


@dataclass(frozen=True)
class ResourceRecord:
    """One resource record from the answer section."""

    name: str
    type: int
    ttl: int
    data: str
    """Human-readable rdata for common types (A, AAAA, CNAME, PTR, NS, MX, TXT),
    otherwise the hex-encoded raw rdata."""

    @property
    def type_name(self) -> str:
        """Mnemonic of :attr:`type` (``"TYPE<n>"`` when unknown)."""
        return qtype_name(self.type)


@dataclass(frozen=True)
class DnsResponse:
    """The interesting parts of a DNS reply."""

    txid: int
    flags: int
    rcode: int
    question: tuple[str, int] | None
    answers: tuple[ResourceRecord, ...] = field(default_factory=tuple)
    authority_count: int = 0
    additional_count: int = 0

    @property
    def rcode_name(self) -> str:
        """Mnemonic of :attr:`rcode` (e.g. ``"NXDOMAIN"``)."""
        return RCODES.get(self.rcode, f"RCODE{self.rcode}")

    @property
    def truncated(self) -> bool:
        """Whether the TC bit is set."""
        return bool(self.flags & 0x0200)

    @property
    def is_blocked_null(self) -> bool:
        """Heuristic: Pi-hole's default NULL blocking reply (``0.0.0.0`` / ``::``)."""
        return any(a.data in _NULL_ANSWERS for a in self.answers)


def qtype_code(qtype: str | int) -> int:
    """Return the numeric code for a query type mnemonic (or pass an int through)."""
    if isinstance(qtype, int):
        if not 0 < qtype < 65536:
            raise ValueError(f"query type out of range: {qtype}")
        return qtype
    key = qtype.upper()
    if key in QTYPES:
        return QTYPES[key]
    if key.startswith("TYPE") and key[4:].isdigit():
        return qtype_code(int(key[4:]))
    raise ValueError(f"unknown query type: {qtype!r}")


def qtype_name(code: int) -> str:
    """Return the mnemonic for a numeric query type (``"TYPE<n>"`` when unknown)."""
    return _QTYPE_NAMES.get(code, f"TYPE{code}")


def encode_name(name: str) -> bytes:
    """Encode a domain name in DNS wire format (uncompressed, IDNA for non-ASCII)."""
    name = name.rstrip(".")
    if not name:
        return b"\x00"
    try:
        ascii_name = name.encode("ascii")
    except UnicodeEncodeError:
        ascii_name = name.encode("idna")
    if len(ascii_name) > _MAX_NAME:
        raise ValueError(f"domain name too long: {name!r}")
    out = bytearray()
    for label in ascii_name.split(b"."):
        if not label or len(label) > _MAX_LABEL:
            raise ValueError(f"invalid label in domain name: {name!r}")
        out.append(len(label))
        out += label
    out.append(0)
    return bytes(out)


def reverse_name(address: str) -> str:
    """Return the ``in-addr.arpa`` / ``ip6.arpa`` name for an IP address."""
    return ipaddress.ip_address(address).reverse_pointer


def build_query(
    name: str,
    qtype: str | int = "A",
    *,
    txid: int | None = None,
    recursion_desired: bool = True,
    edns_payload: int | None = 1232,
) -> bytes:
    """Build a query packet.

    ``edns_payload`` adds an EDNS(0) OPT record advertising that UDP payload
    size (``None`` to send a plain RFC 1035 query).
    """
    if txid is None:
        txid = random.randrange(65536)
    flags = 0x0100 if recursion_desired else 0
    arcount = 1 if edns_payload else 0
    packet = _HEADER.pack(txid, flags, 1, 0, 0, arcount)
    packet += encode_name(name) + struct.pack(">HH", qtype_code(qtype), _CLASS_IN)
    if edns_payload:
        packet += b"\x00" + struct.pack(">HHIH", _TYPE_OPT, edns_payload, 0, 0)
    return packet


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    """Read a (possibly compressed) name; return it and the offset after it."""
    labels: list[str] = []
    jumped = False
    end = offset
    hops = 0
    while True:
        if offset >= len(data):
            raise ValueError("name runs past end of packet")
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("truncated compression pointer")
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                end = offset + 2
            jumped = True
            hops += 1
            if hops > 64:
                raise ValueError("compression loop")
            offset = pointer
            continue
        if length == 0:
            if not jumped:
                end = offset + 1
            break
        offset += 1
        labels.append(data[offset : offset + length].decode("ascii", "replace"))
        offset += length
    return ".".join(labels), end


def _format_rdata(data: bytes, rtype: int, start: int, length: int) -> str:
    rdata = data[start : start + length]
    if rtype == 1 and length == 4:
        return str(ipaddress.IPv4Address(rdata))
    if rtype == 28 and length == 16:
        return str(ipaddress.IPv6Address(rdata))
    if rtype in (2, 5, 12):  # NS, CNAME, PTR
        return _read_name(data, start)[0]
    if rtype == 15 and length >= 3:  # MX
        preference = struct.unpack(">H", rdata[:2])[0]
        return f"{preference} {_read_name(data, start + 2)[0]}"
    if rtype == 16:  # TXT
        parts: list[str] = []
        pos = 0
        while pos < len(rdata):
            size = rdata[pos]
            parts.append(rdata[pos + 1 : pos + 1 + size].decode("utf-8", "replace"))
            pos += 1 + size
        return "".join(parts)
    return rdata.hex()


def parse_response(data: bytes) -> DnsResponse:
    """Parse a reply packet. Raises :class:`ValueError` on malformed data."""
    if len(data) < _HEADER.size:
        raise ValueError("packet shorter than a DNS header")
    txid, flags, qdcount, ancount, nscount, arcount = _HEADER.unpack_from(data)
    if not flags & 0x8000:
        raise ValueError("packet is not a response")
    offset = _HEADER.size
    question: tuple[str, int] | None = None
    for _ in range(qdcount):
        qname, offset = _read_name(data, offset)
        (qtype,) = struct.unpack_from(">H", data, offset)
        offset += 4
        question = question or (qname, qtype)
    answers: list[ResourceRecord] = []
    for _ in range(ancount):
        rname, offset = _read_name(data, offset)
        rtype, _rclass, ttl, rdlength = struct.unpack_from(">HHIH", data, offset)
        offset += 10
        if offset + rdlength > len(data):
            raise ValueError("record data runs past end of packet")
        answers.append(
            ResourceRecord(rname, rtype, ttl, _format_rdata(data, rtype, offset, rdlength))
        )
        offset += rdlength
    return DnsResponse(
        txid=txid,
        flags=flags,
        rcode=flags & 0x000F,
        question=question,
        answers=tuple(answers),
        authority_count=nscount,
        additional_count=arcount,
    )


def _family(server: str) -> socket.AddressFamily:
    try:
        return socket.AF_INET6 if ipaddress.ip_address(server).version == 6 else socket.AF_INET
    except ValueError:
        return socket.AF_INET


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ConnectionError("connection closed mid-message")
        buf += chunk
    return bytes(buf)


def _exchange_tcp(packet: bytes, server: str, port: int, timeout: float) -> bytes:
    with socket.create_connection((server, port), timeout=timeout) as sock:
        sock.sendall(struct.pack(">H", len(packet)) + packet)
        (size,) = struct.unpack(">H", _recv_exact(sock, 2))
        return _recv_exact(sock, size)


def _exchange_udp(packet: bytes, server: str, port: int, timeout: float, txid: int) -> bytes:
    with socket.socket(_family(server), socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(packet, (server, port))
        while True:  # ignore stray datagrams with a different transaction id
            data = sock.recv(65535)
            if len(data) >= 2 and struct.unpack(">H", data[:2])[0] == txid:
                return data


def query(
    name: str,
    qtype: str | int = "A",
    *,
    server: str = "127.0.0.1",
    port: int = 53,
    timeout: float = 2.0,
    retries: int = 1,
    tcp: bool = False,
) -> DnsResponse:
    """Send one query and return the parsed reply.

    UDP is retried ``retries`` times on timeout; a truncated UDP reply is
    re-asked over TCP. Raises :class:`DnsQueryError` when no reply arrives.
    """
    qtype_label = qtype if isinstance(qtype, str) else qtype_name(qtype)
    last_error = "no attempt made"
    for _attempt in range(retries + 1):
        txid = random.randrange(65536)
        packet = build_query(name, qtype, txid=txid)
        try:
            if tcp:
                raw = _exchange_tcp(packet, server, port, timeout)
            else:
                raw = _exchange_udp(packet, server, port, timeout, txid)
                reply = parse_response(raw)
                if reply.truncated:
                    raw = _exchange_tcp(packet, server, port, timeout)
            return parse_response(raw)
        except TimeoutError:
            last_error = f"timed out after {timeout:g}s"
        except (OSError, ValueError, struct.error) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    raise DnsQueryError(name, qtype_label, last_error)


def query_many(
    queries: Iterable[tuple[str, str | int]],
    *,
    server: str = "127.0.0.1",
    port: int = 53,
    timeout: float = 2.0,
    retries: int = 1,
    concurrency: int = 8,
) -> list[DnsResponse | DnsQueryError]:
    """Send many ``(name, qtype)`` queries concurrently, preserving order.

    Never raises for individual queries: a failed query yields its
    :class:`DnsQueryError` in the result list.
    """
    batch: Sequence[tuple[str, str | int]] = list(queries)

    def one(item: tuple[str, str | int]) -> DnsResponse | DnsQueryError:
        try:
            return query(
                item[0], item[1], server=server, port=port, timeout=timeout, retries=retries
            )
        except DnsQueryError as exc:
            return exc

    if concurrency <= 1:
        return [one(item) for item in batch]
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        return list(pool.map(one, batch))
