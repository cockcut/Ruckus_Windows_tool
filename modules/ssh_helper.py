# -*- coding: utf-8 -*-
"""
Ruckus AP SSH Helper - Expect 스타일 자동화
Paramiko 기반, Windows 네이티브 동작
"""

import os
import paramiko
import time
import re
import socket
from pathlib import Path
from typing import Tuple, Optional, List

# OpenSSL 3 + Python 3.12 가 SHA1 서명을 거부함
os.environ.setdefault("OPENSSL_ENABLE_SHA1_SIGNATURES", "1")

FW_DIR = Path(__file__).resolve().parent.parent / "firmware"


def _match_bl7_for_model(model: str) -> str:
    if not model:
        return ""
    key = model.upper().replace("-", "")
    hits = []
    if FW_DIR.is_dir():
        for f in sorted(FW_DIR.glob("*.bl7")):
            stem = f.stem.split("_", 1)[0].upper().replace("-", "")
            if stem == key or key.startswith(stem) or stem.startswith(key):
                hits.append(f.name)
    return hits[0] if hits else ""



def _enable_legacy_algorithms():
    """
    Paramiko 신버전 + OpenSSL3 에서 Ruckus AP ssh-rsa/SHA1 핸드셰이크 실패 대응
    """
    try:
        from paramiko.rsakey import RSAKey
        if hasattr(paramiko.Transport, "_key_info"):
            paramiko.Transport._key_info["ssh-rsa"] = RSAKey
        if hasattr(paramiko.Transport, "_preferred_keys"):
            preferred = list(getattr(paramiko.Transport, "_preferred_keys", ()) or ())
            if "ssh-rsa" not in preferred:
                preferred.append("ssh-rsa")
            paramiko.Transport._preferred_keys = tuple(preferred)
        if hasattr(paramiko.Transport, "_preferred_pubkeys"):
            preferred = list(getattr(paramiko.Transport, "_preferred_pubkeys", ()) or ())
            for k in ("ssh-rsa", "rsa-sha2-256", "rsa-sha2-512"):
                if k not in preferred:
                    preferred.append(k)
            paramiko.Transport._preferred_pubkeys = tuple(preferred)
        disabled = getattr(paramiko.Transport, "disabled_algorithms", None)
        if isinstance(disabled, dict):
            for key in ("keys", "pubkeys", "host-keys"):
                if key in disabled and isinstance(disabled[key], (list, tuple, set)):
                    disabled[key] = [x for x in disabled[key] if x != "ssh-rsa"]

        # SHA1 서명 검증이 OpenSSL3 에서 실패하면 호스트키 검증만 통과
        orig = paramiko.Transport._verify_key
        if not getattr(orig, "_ruckus_patched", False):
            def _verify_key_relaxed(self, host_key, sig):
                try:
                    return orig(self, host_key, sig)
                except Exception:
                    return True
            _verify_key_relaxed._ruckus_patched = True
            paramiko.Transport._verify_key = _verify_key_relaxed
    except Exception:
        pass


_enable_legacy_algorithms()


# ---------------------------------------------------------------------------
# 기본 계정 정책
# ---------------------------------------------------------------------------
# 공장 초기화 AP: super / sp-admin  → 강제 변경 → STANDARD_PASSWORD
# 이미 변경된 AP: sp-admin 실패 시 STANDARD_PASSWORD 로 재시도
DEFAULT_USER = "super"
DEFAULT_PASSWORD = "sp-admin"
STANDARD_PASSWORD = "Ruckus!234"


# 구형 네트워크 장비 호환 알고리즘
_LEGACY_KEX = (
    "curve25519-sha256",
    "curve25519-sha256@libssh.org",
    "ecdh-sha2-nistp256",
    "ecdh-sha2-nistp384",
    "ecdh-sha2-nistp521",
    "diffie-hellman-group-exchange-sha256",
    "diffie-hellman-group14-sha256",
    "diffie-hellman-group-exchange-sha1",
    "diffie-hellman-group14-sha1",
    "diffie-hellman-group1-sha1",
)
_LEGACY_KEYS = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "rsa-sha2-512",
    "rsa-sha2-256",
    "ssh-rsa",
)
_LEGACY_CIPHERS = (
    "aes128-ctr",
    "aes192-ctr",
    "aes256-ctr",
    "aes128-cbc",
    "aes192-cbc",
    "aes256-cbc",
    "3des-cbc",
)
_LEGACY_MACS = (
    "hmac-sha2-256",
    "hmac-sha2-512",
    "hmac-sha1",
    "hmac-md5",
)


class RuckusSSH:
    """Ruckus AP(rkscli) 전용 SSH 세션 래퍼"""

    def __init__(self, timeout: float = 10.0, debug: bool = False, verbose: bool = True):
        self.timeout = timeout
        self._fw_progress = False
        self._fw_pct = -10
        self.debug = debug
        self.verbose = verbose or debug
        self.client: Optional[paramiko.SSHClient] = None
        self.shell = None
        self.transport: Optional[paramiko.Transport] = None
        self.last_output = ""
        self.ip = ""
        self.user = ""
        self._password = ""

    def _v(self, msg: str):
        """항상 보이는 단계 로그 (verbose)"""
        if self.verbose:
            print(msg)

    def _log(self, msg: str):
        if self.debug:
            print(f"[DEBUG] {msg}")

    @staticmethod
    def _progress_pct(text: str) -> Optional[int]:
        found = re.findall(r"\]\s*(\d{1,3})\s*", text or "")
        if not found:
            return None
        try:
            n = int(found[-1])
        except ValueError:
            return None
        return n if 0 <= n <= 100 else None

    @staticmethod
    def _looks_progress(text: str) -> bool:
        if not text:
            return False
        if "[" in text and "]" in text and ("=" in text or ">" in text):
            return True
        return bool(re.search(r"\]\s*\d{1,3}\s*", text))

    def _note_fw_progress(self, text: str) -> None:
        if not self._fw_progress:
            return
        pct = self._progress_pct(text)
        if pct is None:
            return
        if pct >= self._fw_pct + 10 or (pct == 100 and self._fw_pct < 100):
            self._fw_pct = pct - (pct % 10) if pct < 100 else 100
            print(f"    [FW] 다운로드 {self._fw_pct}%")

    def _apply_legacy_security(self, transport: paramiko.Transport):
        """start_client 전에 호출해야 함 — 지원하는 항목만 설정"""
        def _filter(wanted, available):
            if not available:
                return list(wanted)
            return [x for x in wanted if x in available]

        try:
            sec = transport.get_security_options()
            kex_ok = _filter(_LEGACY_KEX, getattr(transport, "_kex_info", {}))
            keys_ok = _filter(_LEGACY_KEYS, getattr(transport, "_key_info", {}))
            ciph_ok = _filter(_LEGACY_CIPHERS, getattr(transport, "_cipher_info", {}))
            mac_ok = _filter(_LEGACY_MACS, getattr(transport, "_mac_info", {}))
            if kex_ok:
                sec.kex = kex_ok
            if keys_ok:
                sec.key_types = keys_ok
            if ciph_ok:
                sec.ciphers = ciph_ok
            if mac_ok:
                sec.digests = mac_ok
            self._v("      레거시 KEX/Cipher/MAC 알고리즘 적용")
        except Exception as e:
            self._v(f"      (보안옵션 설정 일부 실패: {e})")

    def _ki_handler(self, username: str, password: str):
        def handler(title, instructions, prompt_list):
            self._v(f"      [KI] title={title!r}")
            if instructions:
                self._v(f"      [KI] instructions={instructions!r}")
            responses = []
            for i, (prompt, show) in enumerate(prompt_list):
                pl = (prompt or "").lower()
                self._v(f"      [KI] prompt[{i}]={prompt!r} show={show}")
                if any(k in pl for k in ("password", "passwd", "passcode")):
                    responses.append(password)
                    self._v("      [KI] → password 응답")
                elif any(k in pl for k in ("login", "user", "account", "name")):
                    responses.append(username)
                    self._v("      [KI] → username 응답")
                else:
                    val = password if responses else username
                    responses.append(val)
                    self._v(f"      [KI] → 기본 응답 ({'password' if responses else 'username'})")
            return responses

        return handler

    def _discover_auth_methods(self, transport: paramiko.Transport, username: str) -> List[str]:
        """서버가 허용하는 인증 방식 확인"""
        methods: List[str] = []
        try:
            transport.auth_none(username)
            if transport.is_authenticated():
                methods.append("none(already-ok)")
        except paramiko.BadAuthenticationType as e:
            methods = list(e.allowed_types or [])
            self._v(f"      서버 허용 인증: {methods}")
        except paramiko.AuthenticationException as e:
            self._v(f"      auth_none: {e}")
            # 일부 구현은 allowed_types를 여기 담음
            allowed = getattr(e, "allowed_types", None)
            if allowed:
                methods = list(allowed)
                self._v(f"      서버 허용 인증: {methods}")
        except Exception as e:
            self._v(f"      auth_none 예외: {type(e).__name__}: {e}")
        return methods

    def _try_ssh_auth(self, transport: paramiko.Transport, username: str, password: str,
                      preferred: Optional[List[str]] = None) -> bool:
        if transport.is_authenticated():
            self._v("      이미 인증됨")
            return True

        order = preferred or ["password", "keyboard-interactive", "none"]
        # 중복 제거, 알려진 것만
        tried = set()

        for method in order:
            if method in tried:
                continue
            tried.add(method)

            if method in ("password", "passsword"):
                self._v("      [인증] password 방식 시도...")
                try:
                    transport.auth_password(username, password)
                    if transport.is_authenticated():
                        self._v("      [인증] password 성공")
                        return True
                    self._v("      [인증] password - 인증 안 됨")
                except paramiko.BadAuthenticationType as e:
                    self._v(f"      [인증] password 미지원. 허용: {e.allowed_types}")
                    for m in (e.allowed_types or []):
                        if m not in tried:
                            order.append(m)
                except paramiko.AuthenticationException as e:
                    self._v(f"      [인증] password 거부: {e}")
                except Exception as e:
                    self._v(f"      [인증] password 오류: {type(e).__name__}: {e}")

            elif method in ("keyboard-interactive", "keyboard_interactive"):
                self._v("      [인증] keyboard-interactive 방식 시도...")
                try:
                    transport.auth_interactive(username, self._ki_handler(username, password))
                    if transport.is_authenticated():
                        self._v("      [인증] keyboard-interactive 성공")
                        return True
                    self._v("      [인증] keyboard-interactive - 인증 안 됨")
                except paramiko.BadAuthenticationType as e:
                    self._v(f"      [인증] keyboard-interactive 미지원. 허용: {e.allowed_types}")
                except paramiko.AuthenticationException as e:
                    self._v(f"      [인증] keyboard-interactive 거부: {e}")
                except Exception as e:
                    self._v(f"      [인증] keyboard-interactive 오류: {type(e).__name__}: {e}")

            elif method == "none":
                self._v("      [인증] none 방식 시도...")
                try:
                    transport.auth_none(username)
                    if transport.is_authenticated():
                        self._v("      [인증] none 성공")
                        return True
                except Exception as e:
                    self._v(f"      [인증] none: {type(e).__name__}: {e}")

            else:
                self._v(f"      [인증] 미구현 방식 스킵: {method}")

        return transport.is_authenticated()

    def _open_shell(self, transport: paramiko.Transport):
        self._v("      PTY + interactive shell 요청...")
        self.transport = transport
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client._transport = transport
        chan = transport.open_session()
        chan.get_pty(term="vt100", width=200, height=50)
        chan.invoke_shell()
        chan.settimeout(0.0)
        self.shell = chan
        self._v("      셸 채널 열림")

    def _enter_ap_mode(self, initial: str = "") -> Tuple[bool, str]:
        """Unleashed: Wizard no → enable → ap-mode"""
        out = initial or ""
        self._v("      → Unleashed CLI 진입 (wizard/enable/ap-mode)")

        # Setup Wizard
        if re.search(r"(Setup Wizard|Would you like to start)", out, re.IGNORECASE):
            self._v("      → Setup Wizard? no")
            self.send("no")
            ok, out = self.expect(
                r"(ruckus>|ruckus#|rkscli|Would you like to start)",
                timeout=12,
            )
            self._v(f"      wizard 후:\n{out[-400:]}")

        # ruckus> → enable
        if re.search(r"ruckus>", out, re.IGNORECASE) and not re.search(
            r"ruckus\(ap-mode\)#|ruckus#", out, re.IGNORECASE
        ):
            self._v("      → enable")
            self.send("enable")
            ok, out = self.expect(
                r"(ruckus#|rkscli|password|Password|privileged user|force\}|ruckus>)",
                timeout=8,
            )
            self._v(f"      enable 후:\n{out[-400:]}")
            if re.search(r"password", out, re.IGNORECASE) and not re.search(
                r"ruckus#|privileged", out, re.IGNORECASE
            ):
                self._v("      → enable password 전송")
                self.send(self._password or STANDARD_PASSWORD)
                ok, out = self.expect(r"(ruckus#|rkscli|denied|privileged user|ruckus>)", timeout=8)
            if re.search(r"privileged user|use \{force\}|use \{force\} option", out, re.IGNORECASE) or (
                re.search(r"force", out, re.IGNORECASE)
                and re.search(r"already logged in", out, re.IGNORECASE)
            ):
                self._v("      → enable force (다른 privileged 세션 있음)")
                self.send("enable force")
                ok, out = self.expect(r"(ruckus#|rkscli|denied|privileged user|ruckus>)", timeout=10)
                self._v(f"      enable force 후:\n{out[-400:]}")

        # ruckus# → ap-mode (already in ap-mode skip)
        if re.search(r"ruckus#", out, re.IGNORECASE) and not re.search(
            r"ruckus\(ap-mode\)#", out, re.IGNORECASE
        ):
            self._v("      → ap-mode")
            self.send("ap-mode")
            ok, out = self.expect(r"(ruckus\(ap-mode\)#|rkscli)", timeout=8)
            self._v(f"      ap-mode 후:\n{out[-400:]}")

        if re.search(r"rkscli", out, re.IGNORECASE):
            return True, "rkscli"
        if re.search(r"ruckus\(ap-mode\)#", out, re.IGNORECASE):
            return True, "ap-mode"
        return False, f"ap-mode 진입 실패: {out[-180:]!r}"

    def _shell_login(self, username: str, password: str,

                     new_password: Optional[str] = None) -> Tuple[bool, str]:
        self._v("")
        self._v("---- 셸 로그인 단계 ----")
        time.sleep(0.8)

        ok, out = self.expect(
            r"(rkscli|ruckus>|ruckus#|Please login|Login:|login:|password|Password|New password|Confirm password)",
            timeout=10,
        )
        self._v(f"      초기 수신:\n{'-'*40}\n{out}\n{'-'*40}")

        if re.search(r"rkscli", out, re.IGNORECASE):
            self._v("      → 이미 rkscli")
            return True, "rkscli"

        if re.search(r"(Setup Wizard|Would you like to start|ruckus>|ruckus#)", out, re.IGNORECASE):
            ok, msg = self._enter_ap_mode(out)
            if ok:
                return True, msg

        if re.search(r"(Please login|Login:|login:)", out, re.IGNORECASE):
            self._v(f"      → username 전송: {username}")
            self.send(username)
            ok, out = self.expect(r"(password|Password|rkscli)", timeout=10)
            self._v(f"      username 후 수신:\n{'-'*40}\n{out}\n{'-'*40}")

        if re.search(r"password", out, re.IGNORECASE) and not re.search(
            r"(rkscli|New password)", out, re.IGNORECASE
        ):
            self._v("      → password 전송")
            self.send(password)
            ok, out = self.expect(
                r"(rkscli|Login incorrect|Invalid|denied|New password|Confirm password|modify the password|Please login)",
                timeout=12,
            )
            self._v(f"      password 후 수신:\n{'-'*40}\n{out}\n{'-'*40}")

        if re.search(r"(Login incorrect|Invalid|Access denied)", out, re.IGNORECASE):
            return False, "장비 로그인 실패 (아이디/비밀번호 확인)"

        if re.search(r"(New password|modify the password|Change password|Confirm password)", out, re.IGNORECASE):
            if not new_password:
                return False, (
                    "초기화 AP가 새 비밀번호를 요구합니다. 2차 기본 계정 비밀번호를 입력하세요."
                )
            self._v("      → 강제 비밀번호 변경 진행")
            if not re.search(r"New password", out, re.IGNORECASE):
                ok, out = self.expect(r"New password", timeout=8)
                self._v(f"      New password 대기: {out[:200]!r}")
            self._v("      → New password 전송")
            self.send(new_password)
            ok, out = self.expect(r"(Confirm password|confirm|re-enter|again)", timeout=8)
            self._v(f"      Confirm 대기:\n{out[:300]}")
            self._v("      → Confirm password 전송")
            self.send(new_password)
            ok, out = self.expect(
                r"(rkscli|Login incorrect|Please login|Successfully completed|success|changed|OK|password)",
                timeout=10,
            )
            self._v(f"      변경 후 수신:\n{'-'*40}\n{out}\n{'-'*40}")

            if re.search(r"rkscli", out, re.IGNORECASE):
                self._password = new_password
                return True, "rkscli"

            # 비밀번호 변경 성공 후 다시 Please login 이 나옴 → 새 비밀번호로 재로그인
            if re.search(
                r"(Please login|Successfully completed|Login:|login:)",
                out,
                re.IGNORECASE,
            ):
                self._v("      → 비밀번호 변경 완료. 새 비밀번호로 재로그인...")
                self._password = new_password
                if not re.search(r"(Please login|Login:|login:)", out, re.IGNORECASE):
                    ok, out = self.expect(r"(Please login|Login:|login:|rkscli)", timeout=10)
                    self._v(f"      재로그인 프롬프트:\n{out[:300]}")
                if re.search(r"rkscli", out, re.IGNORECASE):
                    return True, "rkscli"
                if re.search(r"(Please login|Login:|login:)", out, re.IGNORECASE):
                    self._v(f"      → username 재전송: {username}")
                    self.send(username)
                    ok, out = self.expect(r"(password|Password|rkscli)", timeout=10)
                    self._v(f"      username 후:\n{'-'*40}\n{out}\n{'-'*40}")
                if re.search(r"password", out, re.IGNORECASE) and not re.search(r"rkscli", out, re.IGNORECASE):
                    self._v("      → 새 password 전송")
                    self.send(new_password)
                    ok, out = self.expect(
                        r"(rkscli|Login incorrect|Invalid|denied|Please login)",
                        timeout=12,
                    )
                    self._v(f"      재로그인 후:\n{'-'*40}\n{out}\n{'-'*40}")
                if re.search(r"rkscli", out, re.IGNORECASE):
                    return True, "rkscli"
                if re.search(r"(Login incorrect|Invalid|Access denied)", out, re.IGNORECASE):
                    return False, "새 비밀번호로 재로그인 실패"
                return False, f"재로그인 후 rkscli 미수신: {out[:150]!r}"

            return False, "비밀번호 변경 후 rkscli 진입 실패"

        if re.search(r"rkscli", out, re.IGNORECASE):
            return True, "rkscli"

        if re.search(r"(Setup Wizard|Would you like to start|ruckus>|ruckus#)", out, re.IGNORECASE):
            ok, msg = self._enter_ap_mode(out)
            if ok:
                return True, msg

        self._v("      → 빈 줄 전송 후 재확인")
        self.send("")
        ok, out2 = self.expect(r"(rkscli|Please login|password|Setup Wizard|ruckus>|ruckus#)", timeout=5)
        self._v(f"      재확인 수신: {out2[:300]!r}")
        if re.search(r"rkscli", out2, re.IGNORECASE):
            return True, "rkscli"
        if re.search(r"(Setup Wizard|Would you like to start|ruckus>|ruckus#)", out2, re.IGNORECASE):
            ok, msg = self._enter_ap_mode(out2)
            if ok:
                return True, msg

        return False, f"rkscli/ap-mode 프롬프트를 받지 못함. 수신: {out[:180]!r}"

    def connect(self, ip: str, username: str = None, password: str = None,
                port: int = 22, new_password: Optional[str] = None,
                standard_password: Optional[str] = None,
                try_factory: bool = True) -> Tuple[bool, str]:
        """
        비밀번호 정책:
          1) CSV/입력 비밀번호
          2) try_factory 이면 공장 sp-admin (강제변경 프롬프트까지 들어가기 위함)
          강제변경 새 비밀번호 = new_password (2차 상자, 상수 사용 안 함)
        """
        username = username or DEFAULT_USER

        passwords_to_try: List[str] = []
        ordered = [password]
        if try_factory:
            ordered.append(DEFAULT_PASSWORD)
        for p in ordered:
            if p and p not in passwords_to_try:
                passwords_to_try.append(p)

        self.ip = ip
        self.user = username
        self._password = passwords_to_try[0] if passwords_to_try else DEFAULT_PASSWORD

        last_err = "인증 실패"
        total = len(passwords_to_try)

        self._v("")
        self._v(f"계정 정책: user={username}")
        self._v(f"  기본(공장) 비밀번호: {DEFAULT_PASSWORD}")
        self._v(f"  강제변경/2차 비밀번호: {standard_password or new_password or '(없음)'}")
        self._v(f"  시도 순서: {['*' * len(p) for p in passwords_to_try]}")

        for idx, pw in enumerate(passwords_to_try):
            transport = None
            try:
                self._v("")
                self._v("=" * 56)
                self._v(f" 접속 시도 {idx + 1}/{total}: {username}@{ip}:{port}")
                label = (
                    "CSV/입력" if password and pw == password
                    else "공장 기본(sp-admin)" if pw == DEFAULT_PASSWORD
                    else "2차 기본" if standard_password and pw == standard_password
                    else "사용자 지정"
                )
                self._v(f" 비밀번호: {label} / 길이 {len(pw)}")
                self._v("=" * 56)

                # 1) TCP
                self._v("[1/5] TCP 연결...")
                sock = socket.create_connection((ip, port), timeout=self.timeout)
                self._v(f"      TCP OK  peer={sock.getpeername()}")

                # 2) SSH handshake
                self._v("[2/5] SSH 핸드셰이크 (start_client)...")
                transport = paramiko.Transport(sock)
                transport.banner_timeout = 10
                transport.auth_timeout = 10
                self._apply_legacy_security(transport)
                transport.start_client(timeout=self.timeout)
                self._v(f"      원격 SSH 버전: {transport.remote_version}")
                try:
                    self._v(f"      로컬 버전: {transport.local_version}")
                except Exception:
                    pass

                # 3) 인증 방식 탐색
                self._v("[3/5] 서버 인증 방식 확인 (auth_none)...")
                methods = self._discover_auth_methods(transport, username)
                if transport.is_authenticated():
                    self._v("      auth_none 만으로 인증 완료")
                else:
                    self._v("[4/5] SSH 인증 시도...")
                    order = []
                    for m in methods:
                        if m not in order:
                            order.append(m)
                    for m in ("password", "keyboard-interactive", "none"):
                        if m not in order:
                            order.append(m)
                    authed = self._try_ssh_auth(transport, username, pw, preferred=order)
                    if not authed:
                        last_err = "SSH 인증 실패 (password / keyboard-interactive)"
                        self._v(f"      → 실패: {last_err}")
                        try:
                            transport.close()
                        except Exception:
                            pass
                        continue

                # 5) 셸 + CLI 로그인
                #    공장 비밀번호로 들어왔을 때만 강제 변경용 new_password 전달
                change_to = (new_password or standard_password or "") or None
                self._v("[5/5] 셸 오픈 및 CLI 로그인...")
                if change_to:
                    self._v(f"      (공장 비밀번호 → 변경 시 사용: 표준 비밀번호)")
                self._open_shell(transport)
                ok, msg = self._shell_login(username, pw, new_password=change_to)
                if ok:
                    self._v("")
                    self._v("**** 로그인 성공 (rkscli) ****")
                    return True, msg

                last_err = msg
                self._v(f"      → 셸 로그인 실패: {msg}")
                # 로그인 실패면 다음 비밀번호로 재시도
                self.close()

            except socket.timeout:
                last_err = f"SSH 연결 타임아웃 ({ip}:{port})"
                self._v(f"      ERROR: {last_err}")
                break
            except socket.error as e:
                last_err = f"SSH 연결 실패 (네트워크): {e}"
                self._v(f"      ERROR: {last_err}")
                break
            except paramiko.SSHException as e:
                last_err = f"SSH 오류: {e}"
                self._v(f"      ERROR: {type(e).__name__}: {e}")
                try:
                    if transport:
                        transport.close()
                except Exception:
                    pass
            except Exception as e:
                last_err = f"SSH 연결 실패: {e}"
                self._v(f"      ERROR: {type(e).__name__}: {e}")
                try:
                    if transport:
                        transport.close()
                except Exception:
                    pass

        self._v("")
        self._v(f"최종 실패: {last_err}")
        return False, last_err

    def send(self, command: str, add_newline: bool = True):
        if not self.shell:
            return
        data = command + ("\n" if add_newline else "")
        self._log(f"SEND >> {command!r}")
        if self.verbose and self.debug:
            self._v(f"      SEND >> {command!r}")
        try:
            self.shell.send(data)
        except Exception as e:
            self._log(f"send error: {e}")

    def expect(self, pattern: str, timeout: Optional[float] = None) -> Tuple[bool, str]:
        if timeout is None:
            timeout = self.timeout
        if not self.shell:
            return False, ""

        buffer = ""
        start = time.time()
        compiled = re.compile(pattern, re.DOTALL | re.IGNORECASE)

        while time.time() - start < timeout:
            try:
                if self.shell.recv_ready():
                    chunk = self.shell.recv(4096).decode("utf-8", errors="ignore")
                    buffer += chunk
                    if self._fw_progress and self._looks_progress(chunk):
                        self._note_fw_progress(chunk)
                    else:
                        self._log(f"RECV << {chunk[:220]!r}")
                    if compiled.search(buffer):
                        self.last_output = buffer
                        return True, buffer
                else:
                    time.sleep(0.05)
            except socket.timeout:
                time.sleep(0.05)
            except Exception as e:
                self._log(f"recv error: {e}")
                break

        self.last_output = buffer
        return False, buffer

    def run(self, command: str, success_pattern: str = r"(OK[\s\S]*?rkscli|ruckus\(ap-mode\)#|rkscli)",
            timeout: Optional[float] = None) -> Tuple[bool, str]:
        print(f"    [CMD] {command}")
        self.send(command)
        ok, out = self.expect(success_pattern, timeout=timeout)
        # 응답 요약 (너무 길면 앞부분만)
        preview = out.replace("\r", "").strip()
        if self._fw_progress:
            lines = [ln for ln in preview.splitlines() if ln.strip() and not self._looks_progress(ln)]
            preview = "\n".join(lines[-8:])
        elif len(preview) > 400:
            preview = preview[:400] + " ..."
        if preview:
            for line in preview.splitlines()[-12:]:
                if self._looks_progress(line):
                    continue
                print(f"    [OUT] {line}")
        print(f"    [CMD] → {'OK' if ok else 'TIMEOUT/FAIL'}")
        return ok, out

    def get_boarddata(self) -> Tuple[Optional[str], Optional[str]]:
        ok, out = self.run("get boarddata", success_pattern=r"(rkscli|ruckus\(ap-mode\)#)", timeout=8)
        serial = mac = None
        m = re.search(r"Serial#:\s*([0-9A-Fa-f:]+)", out)
        if m:
            serial = m.group(1).strip()
        m = re.search(r"base\s+([0-9A-Fa-f:]+)", out, re.IGNORECASE)
        if m:
            mac = m.group(1).strip()
        return serial, mac

    def get_model(self) -> str:
        ok, out = self.run("get boarddata", success_pattern=r"(rkscli|ruckus\(ap-mode\)#)", timeout=8)
        for pat in (
            r"Model:\s*([A-Za-z0-9\-]+)",
            r"Product:\s*([A-Za-z0-9\-]+)",
            r"Board\s*Type:\s*([A-Za-z0-9\-]+)",
        ):
            m = re.search(pat, out, re.IGNORECASE)
            if m:
                return m.group(1).strip()
        ok, out = self.run("get version", success_pattern=r"(rkscli|ruckus\(ap-mode\)#)", timeout=8)
        m = re.search(r"Ruckus\s+([A-Za-z0-9\-]+)", out)
        if m:
            return m.group(1).strip()
        return ""

    def get_version_info(self) -> Tuple[str, str]:
        ok, out = self.run("get version", success_pattern=r"(rkscli|ruckus\(ap-mode\)#)", timeout=8)
        model = ver = ""
        m = re.search(r"Ruckus\s+(\S+)", out)
        if m:
            model = m.group(1).strip()
        m = re.search(r"Version:\s*(\S+)", out)
        if m:
            ver = m.group(1).strip()
        return model, ver

    def wait_fw_update_result(self, first_out: str = "", timeout: float = 180) -> Tuple[str, str]:
        """
        원본 expect 와 동일:
          No update + OK          → SAME
          fw(숫자) : Completed    → DONE
          fail/error              → FAIL
        In progress 후 프롬프트가 먼저 나와도 같은 세션에서 Completed 를 계속 기다린다.
        반환: (SAME|DONE|FAIL|TIMEOUT, 수신텍스트)
        """
        buf = first_out or ""
        deadline = time.time() + timeout
        same_re = re.compile(r"No update.*OK", re.IGNORECASE | re.DOTALL)
        done_re = re.compile(r"fw\(\d+\)\s*:\s*Completed", re.IGNORECASE)
        fail_re = re.compile(
            r"("
            r"fw\(\d+\)\s*:\s*Fail"
            r"|Control File Download Error"
            r"|could not get control file"
            r"|tftp:\s*timeout"
            r"|update failed"
            r"|Download Error"
            r"|not found"
            r"|cannot"
            r")",
            re.IGNORECASE,
        )

        def classify(text: str) -> Optional[str]:
            if re.search(r"needs a reboot", text, re.IGNORECASE):
                return "REBOOT_NEEDED"
            if same_re.search(text):
                return "SAME"
            if done_re.search(text):
                return "DONE"
            if fail_re.search(text):
                return "FAIL"
            return None

        hit = classify(buf)
        if hit:
            print(f"    [FW] {hit}")
            return hit, buf

        print(f"    [FW] Completed / No update 대기 (최대 {int(timeout)}초)")
        while time.time() < deadline:
            remain = int(deadline - time.time())
            try:
                ok, extra = self.expect(
                    r"(No update[\s\S]*?OK|fw\(\d+\)\s*:\s*Completed|fw\(\d+\)\s*:\s*Fail|"
                    r"needs a reboot|Control File Download Error|tftp:\s*timeout|"
                    r"could not get control file|cannot connect to remote host)",
                    timeout=min(15, max(3, remain)),
                )
            except Exception as e:
                print(f"    [FW] 세션 종료: {e}")
                return "FAIL", buf + f"\n세션 종료: {e}"
            if extra:
                buf += extra
                self._note_fw_progress(extra)
                preview = extra.replace("\r", "").strip()
                if preview:
                    for line in preview.splitlines()[-6:]:
                        if line.strip() and not self._looks_progress(line):
                            print(f"    [FW] {line}")
            hit = classify(buf)
            if hit:
                print(f"    [FW] {hit}")
                return hit, buf
            if not ok and remain <= 3:
                break
        return "TIMEOUT", buf or "fw 완료 대기 시간 초과"

    def close(self):
        try:
            if self.shell:
                self.shell.close()
        except Exception:
            pass
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass
        try:
            if self.transport:
                self.transport.close()
        except Exception:
            pass
        self.shell = None
        self.client = None
        self.transport = None


def process_ap(
    ip: str,
    user: str,
    password: str,
    operation: str,
    new_ip: str = "",
    subnet: str = "",
    gw: str = "",
    sz: str = "",
    hostname: str = "",
    new_password: str = "",
    debug: bool = False,
    fw_host: str = "",
    fw_port: str = "69",
    fw_proto: str = "tftp",
    fw_file: str = "",
    fw_factory: bool = False,
    fw_change_ip: bool = True,
    provision_tag: str = "",
    standard_password: str = "",
    fallback_user: str = "",
    try_factory: bool = True,
) -> dict:
    result = {
        "ip": ip,
        "user": user,
        "password": password,
        "serial": "",
        "mac": "",
        "model": "",
        "status": "FAIL",
        "message": "",
    }

    ssh = RuckusSSH(timeout=10, debug=debug, verbose=debug)
    print(f"  → 접속 중: {ip} ...")

    std_user = (fallback_user or "").strip()
    std_pw = (standard_password or "").strip()
    change_pw = (new_password or std_pw).strip()
    ok, msg = ssh.connect(
        ip,
        username=user or DEFAULT_USER,
        password=password or None,
        new_password=change_pw,
        standard_password=std_pw,
        try_factory=try_factory,
    )
    csv_failed_no_change = (not ok) and ("비밀번호 변경이 필요" not in (msg or ""))
    if csv_failed_no_change and std_pw:
        retry_user = std_user or (user or DEFAULT_USER)
        print(f"  → 2차 계정으로 재시도: {retry_user}")
        ssh = RuckusSSH(timeout=10, debug=debug, verbose=debug)
        ok, msg = ssh.connect(
            ip,
            username=retry_user,
            password=std_pw,
            new_password=change_pw,
            standard_password=std_pw,
            try_factory=False,
        )
        if ok:
            result["user"] = retry_user
            result["password"] = std_pw
    if not ok:
        result["message"] = msg
        print(f"  × {ip}: {msg}")
        return result

    print(f"  ✓ {ip}: 로그인 성공")

    serial, mac = ssh.get_boarddata()
    result["serial"] = serial or ""
    result["mac"] = mac or ""
    if serial:
        print(f"    Serial: {serial}, MAC: {mac}")

    try:
        if operation == "connect_sz":
            if not sz:
                result["message"] = "SZ IP가 비어 있음"
            else:
                ssh.run("set scg enable")
                ok, out = ssh.run(f"set scg ip {sz}")
                if ok:
                    ok2, out2 = ssh.run("get scg", success_pattern=r"(rkscli|ruckus\(ap-mode\)#)")
                    result["status"] = "OK" if sz in out2 else "FAIL"
                    result["message"] = (
                        f"SZ IP를 {sz} 로 설정 완료" if sz in out2 else "설정은 됐으나 확인 실패"
                    )
                else:
                    result["message"] = "set scg ip 실패"

        elif operation == "changeip":
            if not (new_ip and subnet and gw):
                result["message"] = "new_ip / subnet / gw 필요"
            else:
                ok, _ = ssh.run(f"set ipaddr wan {new_ip} {subnet} {gw}")
                result["status"] = "OK" if ok else "FAIL"
                result["message"] = f"IP 변경 → {new_ip}" if ok else "set ipaddr 실패"

        elif operation == "devicename":
            if not hostname:
                result["message"] = "hostname 필요"
            else:
                ok, _ = ssh.run(f"set device-name {hostname}")
                result["status"] = "OK" if ok else "FAIL"
                result["message"] = f"hostname → {hostname}" if ok else "set device-name 실패"

        elif operation == "sz_devicename_changeip":
            msgs = []
            if sz:
                ssh.run("set scg enable")
                ok, _ = ssh.run(f"set scg ip {sz}")
                msgs.append(f"SZ:{'OK' if ok else 'FAIL'}")
            if hostname:
                ok, _ = ssh.run(f"set device-name {hostname}")
                msgs.append(f"Name:{'OK' if ok else 'FAIL'}")
            if new_ip and subnet and gw:
                ok, _ = ssh.run(f"set ipaddr wan {new_ip} {subnet} {gw}")
                msgs.append(f"IP:{'OK' if ok else 'FAIL'}")
            result["status"] = "OK" if msgs and all("OK" in m for m in msgs) else "PARTIAL"
            result["message"] = " / ".join(msgs) if msgs else "작업 항목 없음"

        elif operation == "reboot":
            ssh.run("reboot", success_pattern=r"(OK|rkscli)", timeout=5)
            result["status"] = "OK"
            result["message"] = "reboot 명령 전송 완료"

        elif operation == "factory_reset":
            ok, _ = ssh.run("set factory", timeout=12)
            if ok:
                ssh.run("reboot", success_pattern=r"(OK|rkscli)", timeout=5)
                result["status"] = "OK"
                result["message"] = "공장초기화 + reboot 명령 전송"
            else:
                result["message"] = "set factory 실패"

        elif operation == "fw_upgrade":
            model = ssh.get_model()
            result["model"] = model or ""
            if model:
                print(f"    Model: {model}")
            if (not fw_file) or str(fw_file).upper().startswith("AUTO") or str(fw_file).startswith("자동"):
                fw_file = _match_bl7_for_model(model)
                print(f"    auto-match model={model} file={fw_file or '(없음)'}")
            if not fw_host:
                result["message"] = "TFTP/HTTP 서버 IP 없음"
            elif not fw_file:
                result["message"] = f"펌웨어 파일 없음 (model={model or '?'})"
            else:
                steps = [
                    f"fw set proto {fw_proto or 'tftp'}",
                    f"fw set port {fw_port or '69'}",
                    f"fw set host {fw_host}",
                    f"fw set control {fw_file}",
                ]
                failed = None
                for cmd in steps:
                    ok, out = ssh.run(cmd, timeout=12)
                    if not ok:
                        failed = cmd
                        break
                if failed:
                    result["message"] = f"실패: {failed}"
                else:
                    print("    [CMD] fw update (No update / Completed 대기, 최대 180초)")
                    ssh._fw_progress = True
                    ssh._fw_pct = -10
                    try:
                        ok, out = ssh.run(
                            "fw update",
                            success_pattern=r"(No update[\s\S]*?OK|fw\(\d+\)\s*:\s*Completed|fw\(\d+\)\s*:\s*Fail|In progress|rkscli|ruckus\(ap-mode\)#)",
                            timeout=30,
                        )
                        kind, wait_out = ssh.wait_fw_update_result(first_out=out or "", timeout=180)
                    finally:
                        ssh._fw_progress = False
                    if kind == "SAME":
                        result["status"] = "OK"
                        result["message"] = "동일한버전입니다. 다음장비로 이동합니다."
                    elif kind == "DONE":
                        print("    [FW] 업그레이드 완료, set factory + reboot")
                        time.sleep(1)
                        extra = []
                        if fw_factory:
                            okf, _ = ssh.run("set factory", timeout=12)
                            extra.append("factory:" + ("OK" if okf else "FAIL"))
                            time.sleep(2)
                        try:
                            ssh.run("reboot", success_pattern=r"(OK|rkscli)", timeout=8)
                            extra.append("reboot")
                        except Exception:
                            extra.append("reboot(끊김)")
                        result["status"] = "OK"
                        result["message"] = "업그레이드 완료, AP를 초기화 후 재부팅합니다." + (
                            " / " + " ".join(extra) if extra else ""
                        )
                    elif kind == "REBOOT_NEEDED":
                        print("    [FW] needs a reboot — reboot 후 다음 장비")
                        try:
                            ssh.run("reboot", success_pattern=r"(OK|rkscli)", timeout=8)
                        except Exception:
                            pass
                        result["message"] = "업그레이드 오류. AP 재부팅후 업그레이드를 다시 시도해주세요."
                    elif kind == "FAIL":
                        low = (wait_out or "").lower()
                        if "tftp" in low and "timeout" in low:
                            reason = "TFTP timeout"
                        elif "control file" in low:
                            reason = "Control File Download Error"
                        else:
                            reason = "fw update 실패"
                        print(f"    [FW] {reason} — 다음 장비로 이동")
                        result["message"] = f"{reason} — 다음 장비로 이동"
                    else:
                        result["message"] = "fw update 완료 대기 시간 초과: " + (wait_out.replace("\r", " ")[-180:])

        elif operation == "fw_upgrade_sz":
            model, cur_ver = ssh.get_version_info()
            if not model:
                model = ssh.get_model()
            result["model"] = model or ""
            print(f"    Model: {model or '?'}  Version: {cur_ver or '?'}")
            fw_ver = (fw_file or "").strip()
            if not fw_host:
                result["message"] = "SZ API IP 없음"
            elif not fw_ver:
                result["message"] = "펌웨어 버전 없음"
            elif not model:
                result["message"] = "AP 모델명 확인 실패"
            else:
                control = f"wsg/firmware/{model}_{fw_ver}.rcks"
                print(f"    control: {control}")
                tag = (provision_tag or "").strip()
                steps = [
                    "fw set proto HTTPS",
                    "fw set port 443",
                    f"fw set host {fw_host}",
                    f"fw set control {control}",
                ]
                failed = None
                for cmd in steps:
                    ok, out = ssh.run(cmd, timeout=12)
                    if not ok:
                        failed = cmd
                        break
                if failed:
                    result["message"] = f"실패: {failed}"
                else:
                    print("    [CMD] fw update (SZ HTTPS, 최대 180초)")
                    ssh._fw_progress = True
                    ssh._fw_pct = -10
                    try:
                        ok, out = ssh.run(
                            "fw update",
                            success_pattern=r"(No update[\s\S]*?OK|fw\(\d+\)\s*:\s*Completed|fw\(\d+\)\s*:\s*Fail|needs a reboot|In progress|rkscli|ruckus\(ap-mode\)#)",
                            timeout=30,
                        )
                        kind, wait_out = ssh.wait_fw_update_result(first_out=out or "", timeout=180)
                    finally:
                        ssh._fw_progress = False
                    if kind == "SAME":
                        if tag:
                            tag_cmd = f'set provisioning-tag "{tag}"' if " " in tag else f"set provisioning-tag {tag}"
                            ssh.run(tag_cmd, timeout=10)
                        if sz:
                            ssh.run("set scg enable", timeout=10)
                            ssh.run(f"set scg ip {sz}", timeout=10)
                        result["status"] = "OK"
                        result["message"] = f"{ip} 동일한버전입니다. 다음장비로 이동합니다."
                    elif kind == "REBOOT_NEEDED":
                        try:
                            ssh.run("reboot", success_pattern=r"(OK|rkscli)", timeout=8)
                        except Exception:
                            pass
                        result["message"] = f"{ip} 업그레이드 오류. AP 재부팅후 업그레이드를 다시 시도해주세요."
                    elif kind == "FAIL":
                        result["message"] = (
                            f"{ip} SmartZone({fw_host})에 접속하거나 펌웨어 파일을 찾는데 문제가 발생했습니다."
                        )
                    elif kind == "DONE":
                        print("    [FW] 완료. SZ/hostname/IP 설정")
                        time.sleep(1)
                        parts = [f"fw={fw_ver}"]
                        if tag:
                            tag_cmd = f'set provisioning-tag "{tag}"' if " " in tag else f"set provisioning-tag {tag}"
                            ok, _ = ssh.run(tag_cmd, timeout=10)
                            parts.append(f"tag:{tag}:{'OK' if ok else 'FAIL'}")
                        if sz:
                            ssh.run("set scg enable", timeout=10)
                            ok, _ = ssh.run(f"set scg ip {sz}", timeout=10)
                            parts.append(f"SZ:{sz}:{'OK' if ok else 'FAIL'}")
                        if hostname:
                            ok, _ = ssh.run(f"set device-name {hostname}", timeout=10)
                            parts.append(f"Name:{hostname}:{'OK' if ok else 'FAIL'}")
                        if fw_change_ip and new_ip and subnet and gw:
                            ok, _ = ssh.run(f"set ipaddr wan {new_ip} {subnet} {gw}", timeout=8)
                            parts.append(f"IP:{new_ip}:{'OK' if ok else 'TIMEOUT'}")
                            do_reboot = (ip == new_ip)
                        else:
                            parts.append("IP변경안함")
                            do_reboot = True
                        if do_reboot:
                            try:
                                ssh.run("reboot", success_pattern=r"(OK|rkscli)", timeout=8)
                                parts.append("reboot")
                            except Exception:
                                parts.append("reboot(끊김)")
                            if fw_change_ip and new_ip:
                                result["message"] = (
                                    f"{ip}의 펌웨어 업그레이드(vers.: {fw_ver}) | SZ - {fw_host} | "
                                    f"IP- {new_ip} {subnet} {gw} 설정 변경 후 재부팅 명령 실행 완료."
                                )
                            else:
                                result["message"] = (
                                    f"{ip}의 펌웨어 업그레이드(vers.: {fw_ver}) | SZ - {fw_host} | "
                                    f"IP 변경 없음, 재부팅 명령 실행 완료."
                                )
                        else:
                            parts.append("reboot건너뜀")
                            result["message"] = (
                                f"{ip}의 펌웨어 업그레이드(vers.: {fw_ver})| SZ - {fw_host} | "
                                f"IP- {new_ip} {subnet} {gw} 설정 변경 완료. 재부팅 건너뜀."
                            )
                        result["status"] = "OK"
                        print("    " + " / ".join(parts))
                    else:
                        result["message"] = f"{ip} 펌웨어 업데이트가 180초 내에 완료되지 않았습니다."

        else:
            result["message"] = f"지원하지 않는 operation: {operation}"

    except Exception as e:
        result["message"] = f"작업 중 예외: {e}"
    finally:
        ssh.close()

    icon = "✓" if result["status"] == "OK" else "△" if result["status"] == "PARTIAL" else "×"
    print(f"  {icon} {ip}: {result['message']}")
    return result
