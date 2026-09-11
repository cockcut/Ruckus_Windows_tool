# -*- coding: utf-8 -*-
"""Ruckus DHCP Option 43 HEX generator (index.html 로직)."""

from __future__ import annotations

import re

SUBOPTION_SZ = "06"
SUBOPTION_UNLEASHED = "03"

CONTROLLER_CHOICES = (
    ("SmartZone (SZ) - Suboption 06", SUBOPTION_SZ),
    ("Unleashed - Suboption 03", SUBOPTION_UNLEASHED),
)

_IP_RE = re.compile(
    r"^(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$"
)


def parse_ips(text: str, limit: int = 4) -> list[str]:
    return [p for p in (text or "").strip().split() if p][:limit]


def _is_ipv4(s: str) -> bool:
    return bool(_IP_RE.match(s))


def to_hex_payload(s: str) -> str:
    return "".join(f"{ord(c):02x}" for c in s)


def generate(controller_type: str, ip_text: str) -> dict:
    """
    controller_type: '06' (SZ) or '03' (Unleashed)
    ip_text: space-separated IPv4, max 4
    """
    typ = (controller_type or "").strip().lower()
    if typ in ("sz", "smartzone", "06"):
        code = SUBOPTION_SZ
        label = "SZ"
    elif typ in ("ul", "unleashed", "03"):
        code = SUBOPTION_UNLEASHED
        label = "Unleashed"
    else:
        code = SUBOPTION_SZ if typ != "03" else SUBOPTION_UNLEASHED
        label = "SZ" if code == SUBOPTION_SZ else "Unleashed"

    raw = (ip_text or "").strip()
    if not raw:
        return {"ok": False, "error": "IP 주소를 입력해주세요.", "hex": "", "text": "IP 주소를 입력해주세요."}

    ips = parse_ips(raw, 4)
    bad = [ip for ip in ips if not _is_ipv4(ip)]
    if bad:
        return {
            "ok": False,
            "error": f"잘못된 IP: {' '.join(bad)}",
            "hex": "",
            "text": f"잘못된 IP: {' '.join(bad)}",
        }

    concat = ",".join(ips)
    length = len(concat)
    len_hex = f"{length:02x}"
    payload = to_hex_payload(concat)
    final_hex = f"{code}{len_hex}{payload}"
    cli = f"option 43 hex {final_hex}"

    lines = [
        f"* Wireless Controller Type: {label}",
        f"* Controller IP: {' '.join(ips)}",
        "",
        f"* 컨트롤러 종류에 따른 Sub-option: {code} (hex)",
        f"* IP 개수: {len(ips)}",
        f"* IP string length를 hex값 변환: 0x{len_hex} chars ({concat} -> length: {length})",
        f"* IP 전체 hex값 변환: {payload} (hex)",
        "",
        "* command for CLI: (아래 hex 코드를 복사하여 붙여넣으세요)",
        f"[ {cli} ]",
    ]
    return {
        "ok": True,
        "error": "",
        "type": code,
        "label": label,
        "ips": ips,
        "concat": concat,
        "length": length,
        "len_hex": len_hex,
        "payload": payload,
        "hex": final_hex,
        "cli": cli,
        "text": "\n".join(lines),
    }
