# -*- coding: utf-8 -*-
"""
간단한 읽기 전용 TFTP 서버 (RRQ only)
AP 펌웨어 배포용 — 외부 tftpd64 설정 불필요
"""

from __future__ import annotations

import os
import socket
import struct
import threading
from pathlib import Path
from typing import Optional


OPCODE_RRQ = 1
OPCODE_WRQ = 2
OPCODE_DATA = 3
OPCODE_ACK = 4
OPCODE_ERROR = 5
OPCODE_OACK = 6

ERR_NOT_FOUND = 1
ERR_ACCESS = 2
ERR_ILLEGAL = 4


class SimpleTftpServer:
    def __init__(self, root: Path, host: str = "0.0.0.0", port: int = 69, log=None):
        self.root = Path(root).resolve()
        self.host = host
        self.port = port
        self.log = log or (lambda m: None)
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.running = False

    def start(self) -> None:
        if self.running:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.bind((self.host, self.port))
        except OSError as e:
            self._sock.close()
            self._sock = None
            raise OSError(
                f"TFTP 포트 {self.port} 바인드 실패: {e}\n"
                "관리자 권한 또는 다른 TFTP(tftpd64)가 점유 중일 수 있습니다."
            ) from e
        self._sock.settimeout(1.0)
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.log(f"내장 TFTP 서버 시작: {self.host}:{self.port} root={self.root}")

    def stop(self) -> None:
        self._stop.set()
        self.running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
        self.log("내장 TFTP 서버 중지")

    def _loop(self) -> None:
        while not self._stop.is_set() and self._sock:
            try:
                data, addr = self._sock.recvfrom(516)
            except socket.timeout:
                continue
            except OSError as e:
                if self._is_client_reset(e):
                    continue
                break
            if not data or len(data) < 2:
                continue
            opcode = struct.unpack("!H", data[:2])[0]
            if opcode == OPCODE_RRQ:
                threading.Thread(
                    target=self._handle_rrq, args=(data, addr), daemon=True
                ).start()
            elif opcode == OPCODE_WRQ:
                self._send_error(addr, ERR_ACCESS, "Write not allowed")
            else:
                self._send_error(addr, ERR_ILLEGAL, "Unsupported")

    def _parse_rrq(self, data: bytes) -> tuple[str, str]:
        # RRQ: opcode(2) filename\0 mode\0 [options...]
        parts = data[2:].split(b"\x00")
        filename = parts[0].decode("utf-8", errors="replace") if parts else ""
        mode = parts[1].decode("utf-8", errors="replace").lower() if len(parts) > 1 else "octet"
        return filename, mode

    def _safe_path(self, filename: str) -> Optional[Path]:
        # 경로 조작 방지
        name = filename.replace("\\", "/").lstrip("/")
        if ".." in name.split("/"):
            return None
        path = (self.root / name).resolve()
        try:
            path.relative_to(self.root)
        except ValueError:
            return None
        return path if path.is_file() else None

    def _handle_rrq(self, data: bytes, addr) -> None:
        filename, mode = self._parse_rrq(data)
        self.log(f"RRQ from {addr[0]}: {filename}")
        path = self._safe_path(filename)
        if not path:
            self._send_error(addr, ERR_NOT_FOUND, f"File not found: {filename}")
            return
        # 전송용 소켓 (별도 포트)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5.0)
        try:
            with open(path, "rb") as f:
                block = 1
                while True:
                    chunk = f.read(512)
                    packet = struct.pack("!HH", OPCODE_DATA, block) + chunk
                    for _ in range(5):
                        try:
                            sock.sendto(packet, addr)
                            ack, ack_addr = sock.recvfrom(516)
                        except socket.timeout:
                            continue
                        except OSError as e:
                            if self._is_client_reset(e):
                                # AP가 전송 종료 후 UDP를 끊으면 Windows가 10054를 냄
                                return
                            raise
                        if ack_addr[0] != addr[0]:
                            continue
                        if len(ack) >= 4:
                            op, bn = struct.unpack("!HH", ack[:4])
                            if op == OPCODE_ACK and bn == block:
                                break
                            if op == OPCODE_ERROR:
                                self.log(f"클라이언트 오류: {filename}")
                                return
                    else:
                        self.log(f"전송 타임아웃: {filename} block={block}")
                        return
                    if len(chunk) < 512:
                        self.log(f"전송 완료: {filename}")
                        break
                    block = (block + 1) & 0xFFFF
        except OSError as e:
            if self._is_client_reset(e):
                return
            self.log(f"TFTP 전송 오류: {e}")
            try:
                self._send_error(addr, ERR_ACCESS, str(e))
            except OSError:
                pass
        finally:
            sock.close()

    @staticmethod
    def _is_client_reset(e: BaseException) -> bool:
        if isinstance(e, ConnectionResetError):
            return True
        win = getattr(e, "winerror", None)
        if win in (10054, 10053, 10060):
            return True
        err = getattr(e, "errno", None)
        return err in (10054, 10053, 10060, 104, 32)

    def _send_error(self, addr, code: int, msg: str) -> None:
        if not self._sock:
            return
        packet = struct.pack("!HH", OPCODE_ERROR, code) + msg.encode("utf-8", errors="replace") + b"\x00"
        try:
            self._sock.sendto(packet, addr)
        except OSError:
            pass
