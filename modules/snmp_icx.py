# -*- coding: utf-8 -*-
"""ICX SNMPv2c 조회 — 원본 snmp/index.php (snmpget/snmpbulkwalk) 이식."""
from __future__ import annotations

import random
import re
import socket
from typing import Dict, List, Optional, Tuple

OID_SYSDESCR = "1.3.6.1.2.1.1.1.0"
OID_SYSNAME = "1.3.6.1.2.1.1.5.0"
OID_IFDESCR = "1.3.6.1.2.1.2.2.1.2"
OID_ARP = "1.3.6.1.2.1.4.35.1.4"


def _ber_len(n: int) -> bytes:
    if n < 128:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _enc_int(n: int) -> bytes:
    if n == 0:
        body = b"\x00"
    else:
        length = max(1, (n.bit_length() + 8) // 8)
        body = n.to_bytes(length, "big", signed=True)
        if n > 0 and body[0] & 0x80:
            body = b"\x00" + body
    return b"\x02" + _ber_len(len(body)) + body


def _enc_null() -> bytes:
    return b"\x05\x00"


def _enc_octet(data) -> bytes:
    if isinstance(data, str):
        data = data.encode("latin-1", "replace")
    return b"\x04" + _ber_len(len(data)) + data


def _enc_oid(oid: str) -> bytes:
    parts = [int(x) for x in oid.strip(".").split(".") if x]
    if len(parts) < 2:
        raise ValueError("bad oid")
    body = bytes([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        if p < 128:
            body += bytes([p])
        else:
            stack = [p & 0x7F]
            p >>= 7
            while p:
                stack.append(0x80 | (p & 0x7F))
                p >>= 7
            body += bytes(reversed(stack))
    return b"\x06" + _ber_len(len(body)) + body


def _enc_seq(tag: int, *items: bytes) -> bytes:
    body = b"".join(items)
    return bytes([tag]) + _ber_len(len(body)) + body


def _dec_len(buf: bytes, i: int) -> Tuple[int, int]:
    first = buf[i]
    i += 1
    if first < 128:
        return first, i
    n = first & 0x7F
    val = int.from_bytes(buf[i:i + n], "big")
    return val, i + n


def _dec_tlv(buf: bytes, i: int) -> Tuple[int, bytes, int]:
    tag = buf[i]
    ln, j = _dec_len(buf, i + 1)
    return tag, buf[j:j + ln], j + ln


def _dec_int(body: bytes) -> int:
    if not body:
        return 0
    return int.from_bytes(body, "big", signed=True)


def _dec_oid(body: bytes) -> str:
    if not body:
        return ""
    first = body[0]
    parts = [first // 40, first % 40]
    val = 0
    for b in body[1:]:
        val = (val << 7) | (b & 0x7F)
        if not (b & 0x80):
            parts.append(val)
            val = 0
    return ".".join(str(p) for p in parts)


def _build_pdu(pdu_tag: int, community: str, req_id: int, oids: List[str],
               extra_ints: Tuple[int, int] = (0, 0)) -> bytes:
    varbinds = [_enc_seq(0x30, _enc_oid(o), _enc_null()) for o in oids]
    pdu = _enc_seq(
        pdu_tag,
        _enc_int(req_id),
        _enc_int(extra_ints[0]),
        _enc_int(extra_ints[1]),
        _enc_seq(0x30, *varbinds),
    )
    return _enc_seq(0x30, _enc_int(1), _enc_octet(community), pdu)


def _parse_varbinds(buf: bytes) -> List[Tuple[str, object]]:
    out = []
    i = 0
    while i < len(buf):
        tag, body, i = _dec_tlv(buf, i)
        if tag != 0x30:
            continue
        j = 0
        oid = ""
        val: object = None
        while j < len(body):
            t, b, j = _dec_tlv(body, j)
            if t == 0x06:
                oid = _dec_oid(b)
            elif t == 0x04:
                val = b
            elif t == 0x02:
                val = _dec_int(b)
            elif t == 0x05:
                val = None
            elif t in (0x43, 0x41, 0x42, 0x46, 0x47):
                val = int.from_bytes(b, "big") if b else 0
            else:
                val = b
        if oid:
            out.append((oid, val))
    return out


def _parse_response(pkt: bytes) -> List[Tuple[str, object]]:
    tag, body, _ = _dec_tlv(pkt, 0)
    if tag != 0x30:
        return []
    i = 0
    pdu_body = b""
    while i < len(body):
        t, b, i = _dec_tlv(body, i)
        if t in (0xA0, 0xA1, 0xA2, 0xA3, 0xA5):
            pdu_body = b
    # request-id, err, erridx, varbind-seq
    j = 0
    vb = b""
    seen = 0
    while j < len(pdu_body):
        t, b, j = _dec_tlv(pdu_body, j)
        seen += 1
        if seen == 4 and t == 0x30:
            vb = b
    return _parse_varbinds(vb)


class SnmpV2c:
    def __init__(self, host: str, community: str, port: int = 161, timeout: float = 3.0):
        self.host = host
        self.community = community
        self.port = port
        self.timeout = timeout

    def _send(self, payload: bytes) -> bytes:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        try:
            sock.sendto(payload, (self.host, self.port))
            data, _ = sock.recvfrom(65535)
            return data
        finally:
            sock.close()

    def get(self, oid: str) -> object:
        req = random.randint(1, 0x7FFFFFFF)
        pkt = _build_pdu(0xA0, self.community, req, [oid])
        binds = _parse_response(self._send(pkt))
        return binds[0][1] if binds else None

    def walk(self, oid: str, max_rep: int = 20) -> List[Tuple[str, object]]:
        start = oid.strip(".")
        prefix = start
        cur = start
        out: List[Tuple[str, object]] = []
        seen = set()
        for _ in range(2000):
            req = random.randint(1, 0x7FFFFFFF)
            pkt = _build_pdu(0xA5, self.community, req, [cur], (0, max_rep))
            try:
                binds = _parse_response(self._send(pkt))
            except socket.timeout:
                break
            if not binds:
                break
            progressed = False
            for o, v in binds:
                if o in seen:
                    continue
                if not o.startswith(prefix):
                    return out
                seen.add(o)
                out.append((o, v))
                cur = o
                progressed = True
            if not progressed:
                break
        return out


def _as_str(val) -> str:
    if val is None:
        return ""
    if isinstance(val, bytes):
        try:
            return val.decode("utf-8")
        except Exception:
            return val.decode("latin-1", "replace")
    return str(val)


def _fmt_mac(val) -> str:
    if isinstance(val, bytes) and len(val) >= 6:
        return ":".join(f"{b:02X}" for b in val[:6])
    text = _as_str(val).strip()
    if not text:
        return ""
    parts = re.split(r"[:\-\s]", text)
    hexes = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        try:
            hexes.append(f"{int(p, 16):02X}")
        except ValueError:
            continue
    if len(hexes) >= 6:
        return ":".join(hexes[:6])
    return text


def _parse_sysdescr(text: str) -> Tuple[str, str]:
    m = re.search(r"Ruckus\s+Wireless,\s+Inc\.\s+(ICX\S+),\s+IronWare\s+Version\s+(\S+)", text, re.I)
    if not m:
        return "N/A", "N/A"
    model = m.group(1)
    version = re.sub(r"T\d+$", "", m.group(2))
    return model, version


def query_icx(host: str, community: str, timeout: float = 3.0) -> dict:
    cli = SnmpV2c(host, community, timeout=timeout)
    try:
        descr = _as_str(cli.get(OID_SYSDESCR))
    except Exception as e:
        return {"ok": False, "error": f"SNMP 연결 실패: {e}"}
    if not descr:
        return {"ok": False, "error": "응답이 없습니다. Community / IP / UDP 161 을 확인하세요."}
    model, version = _parse_sysdescr(descr)
    try:
        hostname = _as_str(cli.get(OID_SYSNAME)) or "N/A"
    except Exception:
        hostname = "N/A"
    ifaces: Dict[str, str] = {}
    try:
        for oid, val in cli.walk(OID_IFDESCR):
            idx = oid[len(OID_IFDESCR):].lstrip(".")
            ifaces[idx] = _as_str(val)
    except Exception:
        pass
    rows = []
    try:
        for oid, val in cli.walk(OID_ARP):
            rest = oid[len(OID_ARP):].lstrip(".")
            parts = rest.split(".")
            if len(parts) < 6:
                continue
            ifindex = parts[0]
            # type ipv4 = 1, then 4 octets
            ip = None
            if "1" in parts[1:2] and len(parts) >= 6:
                # ifIndex.type.a.b.c.d
                typ = parts[1]
                if typ == "1" and len(parts) >= 6:
                    ip = ".".join(parts[-4:])
            if not ip:
                # fallback last 4 numeric
                try:
                    ip = ".".join(parts[-4:])
                except Exception:
                    continue
            mac = _fmt_mac(val)
            iface = ifaces.get(ifindex, "Unknown")
            rows.append({
                "ip": ip,
                "mac": mac,
                "mac_lower": mac.lower(),
                "iface": iface,
                "ifindex": ifindex,
            })
    except Exception as e:
        return {"ok": False, "error": f"ARP 조회 실패: {e}", "hostname": hostname, "model": model, "version": version}
    return {
        "ok": True,
        "hostname": hostname or "N/A",
        "model": model,
        "version": version,
        "sysdescr": descr,
        "rows": rows,
    }
