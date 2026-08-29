# -*- coding: utf-8 -*-
"""
Ruckus Unleashed 통계 API 클라이언트
원본 api-u/script_ap-wlan-stats.sh 로직 이식
"""

from __future__ import annotations

import csv
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _attr(elem: Optional[ET.Element], name: str, default: str = "") -> str:
    if elem is None:
        return default
    val = elem.get(name)
    if val is None or val == "":
        return default
    return str(val)


def _dpsk_duration(seconds: int) -> str:
    fixed = {
        15552000: "6개월", 7776000: "3개월", 5184000: "2개월",
        2592000: "1개월", 1209600: "2주", 604800: "1주", 86400: "1일",
    }
    if seconds in fixed:
        return fixed[seconds]
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}일")
    if hours:
        parts.append(f"{hours}시간")
    if minutes:
        parts.append(f"{minutes}분")
    return " ".join(parts) if parts else "0초"


def _find_child(parent: ET.Element, tag_endswith: str) -> Optional[ET.Element]:
    """네임스페이스 무시하고 태그 끝부분으로 자식 검색"""
    for child in parent.iter():
        if child.tag == tag_endswith or str(child.tag).endswith(tag_endswith):
            if child is not parent:
                return child
    return None


class UnleashedAPI:
    def __init__(self, host: str, username: str, password: str, timeout: int = 60):
        self.host = host.strip()
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = False
        self.csrf_token: Optional[str] = None
        self.login_url: Optional[str] = None
        self.cmdstat_url: Optional[str] = None
        self.raw_xml: str = ""

    def _base(self) -> str:
        h = self.host
        if not h.startswith("http"):
            h = f"https://{h}"
        return h.rstrip("/")

    @staticmethod
    def _extract_csrf(resp: requests.Response) -> Optional[str]:
        """응답 헤더/쿠키/본문에서 CSRF 토큰 추출 (원본 curl awk 대응)"""
        if resp is None:
            return None
        # 헤더: X-CSRF-Token, HTTP_X_CSRF_TOKEN 등
        for k, v in resp.headers.items():
            kl = k.lower().replace("_", "-")
            if "csrf" in kl and v:
                return v.strip()
        # 쿠키
        try:
            for c in resp.cookies:
                if "csrf" in c.name.lower() and c.value:
                    return c.value.strip()
        except Exception:
            pass
        # 본문
        text = resp.text or ""
        patterns = [
            r'X-CSRF-Token["\s:=]+([A-Za-z0-9_\-+=/.]{8,})',
            r'csrf[_-]?token["\s:=]+([A-Za-z0-9_\-+=/.]{8,})',
            r'name=["\']csrf[_-]?token["\'][^>]*value=["\']([^"\']+)',
            r'value=["\']([^"\']+)["\'][^>]*name=["\']csrf[_-]?token["\']',
            r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)',
        ]
        for pat in patterns:
            m = re.search(pat, text, re.I)
            if m:
                return m.group(1).strip()
        return None

    def login(self, log=None) -> Tuple[bool, str]:
        """
        원본 script_ap-wlan-stats.sh 와 동일한 흐름:
          1) curl https://IP -k -L -I  → 최종 로그인 URL
          2) curl LOGIN -d username/password/ok=Log In -i -c cookie
             → 헤더 X-CSRF-Token (또는 HTTP_X_CSRF_TOKEN)
          3) cmdstat = dirname(login)/_cmdstat.jsp
        """
        def _log(msg: str):
            if log:
                log(msg)

        try:
            base = self._base()

            # 1) 로그인 URL 스니핑 (HEAD + 리다이렉트, 실패 시 GET)
            login_url = None
            try:
                r0 = self.session.head(
                    base, timeout=self.timeout, allow_redirects=True,
                )
                login_url = r0.url
            except requests.RequestException:
                pass
            if not login_url or login_url.rstrip("/").endswith(self.host.split("://")[-1].split("/")[0]):
                r0 = self.session.get(base, timeout=self.timeout, allow_redirects=True)
                login_url = r0.url

            # 로그인 페이지가 루트면 /admin/login.jsp 후보 추가
            candidates = [login_url]
            parsed0 = urlparse(login_url)
            origin = f"{parsed0.scheme}://{parsed0.netloc}"
            for path in ("/admin/login.jsp", "/login.jsp"):
                u = origin + path
                if u not in candidates:
                    candidates.append(u)

            _log(f"로그인 URL 후보: {candidates}")

            token = None
            used_login = None
            last_status = None
            last_headers_dbg = ""

            for cand in candidates:
                # 2) 로그인 POST — 리다이렉트 전 응답 헤더도 확인
                r1 = self.session.post(
                    cand,
                    data={
                        "username": self.username,
                        "password": self.password,
                        "ok": "Log In",
                    },
                    timeout=self.timeout,
                    allow_redirects=False,  # 1차: 토큰이 302 응답 헤더에 있을 수 있음
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Referer": cand,
                    },
                )
                last_status = r1.status_code
                token = self._extract_csrf(r1)
                # history 없음 (redirects=False)

                if not token and r1.status_code in (301, 302, 303, 307, 308):
                    loc = r1.headers.get("Location") or r1.headers.get("location")
                    if loc:
                        next_url = urljoin(cand, loc)
                        r2 = self.session.get(
                            next_url, timeout=self.timeout, allow_redirects=True,
                        )
                        token = self._extract_csrf(r2)
                        if not token:
                            for h in r2.history:
                                token = self._extract_csrf(h)
                                if token:
                                    break
                        last_status = r2.status_code

                if not token:
                    # 리다이렉트 허용 재시도
                    r3 = self.session.post(
                        cand,
                        data={
                            "username": self.username,
                            "password": self.password,
                            "ok": "Log In",
                        },
                        timeout=self.timeout,
                        allow_redirects=True,
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Referer": cand,
                        },
                    )
                    last_status = r3.status_code
                    for resp in list(r3.history) + [r3]:
                        token = self._extract_csrf(resp)
                        if token:
                            break
                    if not token:
                        # 쿠키에서 재탐색
                        for c in self.session.cookies:
                            if "csrf" in c.name.lower() and c.value:
                                token = c.value.strip()
                                break
                    # 디버그용 헤더 요약
                    hdrs = {k: v for k, v in r3.headers.items()
                            if any(x in k.lower() for x in ("csrf", "set-cookie", "location", "cookie"))}
                    last_headers_dbg = str(hdrs)[:300]

                if token:
                    used_login = cand
                    break
                _log(f"  후보 실패: {cand} (HTTP {last_status})")

            if not token:
                _log(f"헤더 요약: {last_headers_dbg or '(없음)'}")
                _log(f"쿠키: {list(self.session.cookies.keys())}")
                return False, (
                    "로그인 실패: X-CSRF-Token 을 받지 못했습니다. "
                    "ID/비밀번호 또는 Unleashed 버전을 확인하세요."
                )

            self.login_url = used_login or login_url
            parsed = urlparse(self.login_url)
            dir_path = parsed.path.rsplit("/", 1)[0] + "/" if "/" in parsed.path else "/"
            # admin 경로 보정
            if "/admin" not in dir_path and parsed.path:
                # login.jsp 가 /admin 밖이면 cmdstat 은 보통 /admin/_cmdstat.jsp
                self.cmdstat_url = f"{parsed.scheme}://{parsed.netloc}/admin/_cmdstat.jsp"
            else:
                self.cmdstat_url = f"{parsed.scheme}://{parsed.netloc}{dir_path}_cmdstat.jsp"

            self.csrf_token = token
            _log(f"CSRF 토큰 수신 (길이 {len(token)}), cmdstat={self.cmdstat_url}")
            return True, f"로그인 성공 (cmdstat={self.cmdstat_url})"

        except requests.RequestException as e:
            return False, f"연결 실패: {e}"

    def fetch_stats_xml(self) -> Tuple[bool, str]:
        """AP+WLAN 통합 통계 XML 요청"""
        if not self.csrf_token or not self.cmdstat_url:
            return False, "로그인이 필요합니다."

        body = "<ajax-request action='getstat' comp='stamgr'><ap LEVEL='2'/></ajax-request>"
        try:
            r = self.session.post(
                self.cmdstat_url,
                data=body,
                headers={
                    "X-CSRF-Token": self.csrf_token,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=self.timeout,
            )
            text = r.text or ""
            if not text.strip():
                return False, "통계 XML 이 비어 있습니다."
            if "Unauthorized" in text or "The document has moved" in text:
                return False, "인증 실패 (세션/토큰 만료 또는 잘못된 계정)"
            self.raw_xml = text
            return True, f"XML 수신 ({len(text)} bytes)"
        except requests.RequestException as e:
            return False, f"통계 요청 실패: {e}"

    def parse_aps(self) -> List[dict]:
        """//ap 요소 파싱"""
        if not self.raw_xml:
            return []
        try:
            root = ET.fromstring(self.raw_xml)
        except ET.ParseError:
            # 가끔 HTML 래핑 → ap 태그만 추출 시도
            m = re.search(r"(<ap[\s\S]+</ap>)", self.raw_xml, re.I)
            if not m:
                return []
            try:
                root = ET.fromstring(f"<root>{m.group(1)}</root>")
            except ET.ParseError:
                return []

        aps = []
        for ap in root.iter():
            tag = str(ap.tag).split("}")[-1].lower()
            if tag != "ap":
                continue
            mac = _attr(ap, "mac")
            if not mac:
                continue

            eth0 = "NULL"
            for lan in ap.iter():
                if str(lan.tag).split("}")[-1].lower() in ("lan-port", "lanport"):
                    if _attr(lan, "Interface") == "eth0" or _attr(lan, "if-descr") == "eth0":
                        eth0 = _attr(lan, "Physical", "NULL") or "NULL"
                        break

            ch_2g = ch_5g = ch_6g = "NULL"
            for radio in ap.iter():
                if str(radio.tag).split("}")[-1].lower() != "radio":
                    continue
                band = (_attr(radio, "radio-band") or "").lower()
                ch = _attr(radio, "channel", "NULL") or "NULL"
                if "2.4" in band or band in ("2g", "2.4g"):
                    ch_2g = ch
                elif "5" in band:
                    ch_5g = ch
                elif "6" in band:
                    ch_6g = ch

            aps.append({
                "mac": mac,
                "ap-name": _attr(ap, "ap-name") or _attr(ap, "device-name"),
                "model": _attr(ap, "model"),
                "ip": _attr(ap, "ip"),
                "netmask": _attr(ap, "netmask"),
                "gateway": _attr(ap, "gateway"),
                "serial-number": _attr(ap, "serial-number") or _attr(ap, "serial"),
                "firmware-version": _attr(ap, "firmware-version") or _attr(ap, "version"),
                "num-sta": _attr(ap, "num-sta") or _attr(ap, "num-station"),
                "eth0_Physical": eth0,
                "eth1_Physical": "",
                "2G_ch": ch_2g,
                "5G_ch": ch_5g,
                "6G_ch": ch_6g,
            })
        return aps

    def parse_wlans(self) -> List[dict]:
        """//vap 요소 파싱"""
        if not self.raw_xml:
            return []
        try:
            root = ET.fromstring(self.raw_xml)
        except ET.ParseError:
            try:
                root = ET.fromstring(f"<root>{self.raw_xml}</root>")
            except ET.ParseError:
                return []

        rows = []
        for vap in root.iter():
            tag = str(vap.tag).split("}")[-1].lower()
            if tag != "vap":
                continue
            bssid = _attr(vap, "bssid")
            if not bssid:
                continue
            rows.append({
                "BSSID": bssid,
                "SSID": _attr(vap, "ssid"),
                "Radio_Band": _attr(vap, "radio-band"),
                "AP_mac": _attr(vap, "ap"),
                "Radio_Type": _attr(vap, "ieee80211-radio-type"),
                "VAP_Up_Status": _attr(vap, "vap-up"),
                "Channel": _attr(vap, "channel"),
            })
        return rows

    def collect_all(self, log=print) -> dict:
        ok, msg = self.login(log=log)
        log(msg)
        if not ok:
            return {"ok": False, "error": msg}

        ok, msg = self.fetch_stats_xml()
        log(msg)
        if not ok:
            return {"ok": False, "error": msg}

        aps = self.parse_aps()
        wlans = self.parse_wlans()
        log(f"AP {len(aps)} 대, WLAN/VAP {len(wlans)} 개")
        return {
            "ok": True,
            "host": self.host,
            "aps": aps,
            "wlans": wlans,
            "raw_xml": self.raw_xml,
        }

    def _post_xml(self, xml_body: str) -> Tuple[bool, str]:
        if not self.csrf_token or not self.cmdstat_url:
            return False, "로그인이 필요합니다."
        try:
            r = self.session.post(
                self.cmdstat_url,
                data=xml_body,
                headers={
                    "X-CSRF-Token": self.csrf_token,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=self.timeout,
            )
            text = r.text or ""
            if "Unauthorized" in text or "The document has moved" in text:
                return False, "인증 실패 (세션 만료)"
            return True, text
        except requests.RequestException as e:
            return False, str(e)

    def _parse_xml(self, text: str) -> Optional[ET.Element]:
        cleaned = re.sub(r"<\?xml[^>]*\?>", "", text or "", flags=re.I)
        cleaned = re.sub(r"<!DOCTYPE[^>]*>", "", cleaned, flags=re.I)
        for cand in (cleaned, f"<root>{cleaned}</root>"):
            try:
                return ET.fromstring(cand)
            except ET.ParseError:
                continue
        return None

    def fetch_dpsk_wlans(self) -> List[dict]:
        ok, text = self._post_xml(
            "<ajax-request action='getstat' comp='stamgr'><wlan LEVEL='1'/></ajax-request>"
        )
        if not ok:
            return []
        root = self._parse_xml(text)
        if root is None:
            return []
        out = []
        for wlan in root.iter():
            if str(wlan.tag).split("}")[-1].lower() != "wlan":
                continue
            wpa = None
            for ch in list(wlan):
                if str(ch.tag).split("}")[-1].lower() == "wpa":
                    wpa = ch
                    break
            if wpa is None or (_attr(wpa, "dynamic-psk") or "").lower() != "enabled":
                continue
            out.append({
                "id": _attr(wlan, "id"),
                "name": _attr(wlan, "name"),
                "ssid": _attr(wlan, "ssid"),
                "vlan_id": _attr(wpa, "dvlan-id") or _attr(wlan, "vlan-id"),
                "dpsk_len": _attr(wpa, "dynamic-psk-len"),
                "start_point": _attr(wpa, "start-point"),
                "limit_dpsk": _attr(wpa, "limit-dpsk"),
                "limit_dpsk_val": _attr(wpa, "limit-dpsk-val"),
                "shared_dpsk": _attr(wpa, "shared-dpsk"),
                "shared_dpsk_num": _attr(wpa, "shared-dpsk-num"),
            })
        return out

    def fetch_dpsk_list(self, wlan_map: Optional[Dict[str, dict]] = None) -> List[dict]:
        wlan_map = wlan_map or {}
        ok, text = self._post_xml(
            '<ajax-request action="getstat" comp="stamgr"><dpsklist/></ajax-request>'
        )
        if not ok:
            return []
        root = self._parse_xml(text)
        if root is None:
            return []
        rows = []
        min_ts = 946684800
        now = __import__("time").time()
        for dpsk in root.iter():
            if str(dpsk.tag).split("}")[-1].lower() != "dpsk":
                continue
            wid = _attr(dpsk, "wlansvc-id")
            info = wlan_map.get(wid) or {}
            try:
                expire_ts = int(_attr(dpsk, "expire") or 0)
            except ValueError:
                expire_ts = 0
            try:
                next_rekey = int(_attr(dpsk, "next-rekey") or 0)
            except ValueError:
                next_rekey = 0
            try:
                last_rekey = int(_attr(dpsk, "last-rekey") or 0)
            except ValueError:
                last_rekey = 0
            start_raw = info.get("start_point") or ""
            if expire_ts == 0:
                status = "활성 (무제한)"
            elif start_raw == "first-use" and next_rekey < min_ts:
                status = "활성 (미사용)"
            elif next_rekey > now:
                status = "활성"
            else:
                status = "만료"
            if start_raw == "first-use":
                start_disp = "사용시"
            elif start_raw == "creation-time":
                start_disp = "생성시"
            else:
                start_disp = start_raw or "N/A"
            def yn(raw):
                raw = (raw or "").lower()
                if raw == "enabled":
                    return "Y"
                if raw == "disabled":
                    return "N"
                return raw or "N/A"
            wname = info.get("name") or "Unknown"
            rows.append({
                "id": _attr(dpsk, "id"),
                "wlan_id": wid,
                "wlan": f"{wname} (ID:{wid})" if wid else wname,
                "dpsk_len": info.get("dpsk_len") or "N/A",
                "shared_dpsk": yn(info.get("shared_dpsk")),
                "shared_num": info.get("shared_dpsk_num") or "N/A",
                "user": _attr(dpsk, "user"),
                "psk": _attr(dpsk, "passphrase"),
                "vlan": _attr(dpsk, "dvlan-id") or "0",
                "clients": _attr(dpsk, "cur-shared-num") or "0",
                "usage": _attr(dpsk, "usage") or "0",
                "mac": _attr(dpsk, "mac") or "00:00:00:00:00:00",
                "period": "무제한" if expire_ts == 0 else _dpsk_duration(expire_ts),
                "status": status,
                "start_point": start_disp,
                "limit_dpsk": yn(info.get("limit_dpsk")),
                "limit_num": info.get("limit_dpsk_val") or "N/A",
                "created": __import__("datetime").datetime.fromtimestamp(last_rekey).strftime("%Y/%m/%d %H:%M:%S") if last_rekey > min_ts else "N/A",
                "expires": __import__("datetime").datetime.fromtimestamp(next_rekey).strftime("%Y/%m/%d %H:%M:%S") if next_rekey > min_ts else "N/A",
            })
        return rows

    def create_dpsk(self, wlan_id: str, username: str = "", vlan_id: str = "", amount: int = 1) -> Tuple[bool, str]:
        amount = max(1, int(amount or 1))
        attrs = f'cmd="batch-dpsk" type="gen" num="{amount}" wlansvc-id="{wlan_id}"'
        if username:
            attrs += f' user="{username}"'
        if vlan_id and str(vlan_id) not in ("", "0"):
            attrs += f' dvlan-id="{vlan_id}"'
        xml = (
            '<ajax-request action="docmd" comp="system" updater="batch-dpsk" checkAbility="2">'
            f"<xcmd {attrs} />"
            "</ajax-request>"
        )
        ok, text = self._post_xml(xml)
        if not ok:
            return False, text
        if 'type="error"' in text or 'type="fault"' in text:
            return False, text[:400]
        return True, text

    def delete_dpsk(self, ids: List[str]) -> Tuple[bool, str]:
        if not ids:
            return True, "없음"
        tags = "".join(f'<dpsk id="{i}"></dpsk>' for i in ids)
        xml = f'<ajax-request action="docmd" comp="system"><xcmd cmd="delete-dpsk">{tags}</xcmd></ajax-request>'
        ok, text = self._post_xml(xml)
        if not ok:
            return False, text
        if 'type="error"' in text or 'type="fault"' in text:
            return False, text[:400]
        return True, text


def save_csv(path: Path, fieldnames: List[str], rows: List[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path
