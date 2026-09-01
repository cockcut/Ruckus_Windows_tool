# -*- coding: utf-8 -*-
"""
Ruckus SmartZone Public API 클라이언트
원본 api-sz/index.php 로직 이식
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 컨트롤러 버전 → 시도할 API 버전 (높은 버전 우선)
CONTROLLER_API_MAP: Dict[str, List[str]] = {
    "7.2.0": ["v14_0", "v13_1", "v13_0", "v12_0"],
    "7.1.1": ["v13_1", "v13_0", "v12_0", "v11_1", "v11_0"],
    "7.1.0": ["v13_0", "v12_0", "v11_1", "v11_0"],
    "7.0.0": ["v12_0", "v11_1", "v11_0", "v10_0"],
    "6.1.2": ["v11_1", "v11_0", "v10_0", "v9_1", "v9_0"],
    "6.1.1": ["v11_1", "v11_0", "v10_0", "v9_1", "v9_0"],
    "6.1.0": ["v11_0", "v10_0", "v9_1", "v9_0"],
    "6.0.0": ["v10_0", "v9_1", "v9_0"],
    "5.2.0": ["v9_0", "v8_2", "v8_1", "v8_0", "v7_0", "v6_1", "v6_0"],
    "수동선택": [
        "v14_0", "v13_1", "v13_0", "v12_0", "v11_1", "v11_0",
        "v10_0", "v9_1", "v9_0", "v8_2", "v8_1", "v8_0", "v7_0", "v6_1", "v6_0",
    ],
}

# 원본 PHP와 동일한 기본 DOMAIN 필터 (전체 Administration Domain)
DEFAULT_DOMAIN_ID = "8b2081d5-9662-40d9-a3db-2a3cf4dde3f7"


def _na(val: Any, default: str = "N/A") -> str:
    if val is None or val == "":
        return default
    return str(val)


def _enc_has_psk(method: str) -> bool:
    m = (method or "").upper().replace("-", "").replace("_", "")
    if not m or m in ("NONE", "OPEN", "OWE", "WEP", "WEP64", "WEP128"):
        return False
    # WPA/WPA2/WPA23 혼합은 PSK passphrase 사용
    return ("WPA2" in m) or (m == "WPA") or ("WPA23" in m) or ("PSK" in m)


def _enc_has_sae(method: str) -> bool:
    m = (method or "").upper().replace("-", "").replace("_", "")
    return ("WPA3" in m) or ("SAE" in m) or ("WPA23" in m)


def _enc_is_mixed(method: str) -> bool:
    m = (method or "").upper().replace("-", "").replace("_", "")
    return "WPA23" in m


def _enc_changeable(wtype: str, method: str) -> bool:
    if wtype != "Standard_Open":
        return False
    m = (method or "").upper().replace("-", "").replace("_", "")
    return m in ("WPA2", "WPA3") or "WPA23" in m


class SmartZoneAPI:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        api_version: str,
        port: int = 8443,
        timeout: int = 60,
        domain_id: str = DEFAULT_DOMAIN_ID,
    ):
        self.host = host.strip()
        self.username = username
        self.password = password
        self.api_version = api_version
        self.port = port
        self.timeout = timeout
        self.domain_id = domain_id
        self.service_ticket: Optional[str] = None
        self.controller_version: str = ""
        self.cluster_name: str = ""
        self.base = f"https://{self.host}:{self.port}/wsg/api/public/{self.api_version}"
        self.switch_base = f"https://{self.host}:{self.port}/switchm/api/{self.api_version}"
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({"Content-Type": "application/json"})

    def _url(self, path: str, switch: bool = False) -> str:
        base = self.switch_base if switch else self.base
        sep = "&" if "?" in path else "?"
        if self.service_ticket and "serviceTicket=" not in path:
            path = f"{path}{sep}serviceTicket={self.service_ticket}"
        return f"{base}{path}"

    def _request(self, method: str, url: str, data: Any = None) -> Tuple[int, Any]:
        try:
            m = method.upper()
            payload = json.dumps(data) if data is not None else None
            if m == "POST":
                r = self.session.post(url, data=payload, timeout=self.timeout)
            elif m == "PATCH":
                r = self.session.patch(url, data=payload, timeout=self.timeout)
            elif m == "PUT":
                r = self.session.put(url, data=payload, timeout=self.timeout)
            elif m == "DELETE":
                r = self.session.delete(url, data=payload, timeout=self.timeout)
            else:
                r = self.session.get(url, timeout=self.timeout)
            try:
                body = r.json()
            except Exception:
                body = {"raw": (r.text or "")[:800]}
            return r.status_code, body
        except requests.RequestException as e:
            return 0, {"error": str(e)}

    def login(self) -> Tuple[bool, str]:
        url = f"{self.base}/serviceTicket"
        code, body = self._request("POST", url, {"username": self.username, "password": self.password})
        if isinstance(body, dict) and body.get("serviceTicket"):
            self.service_ticket = body["serviceTicket"]
            self.controller_version = _na(body.get("controllerVersion"), "")
            return True, f"로그인 성공 (controllerVersion={self.controller_version or 'N/A'})"
        err = body.get("error") if isinstance(body, dict) else body
        msg = body.get("message") if isinstance(body, dict) else None
        return False, f"로그인 실패 HTTP={code} {msg or err or body}"

    def fetch_cluster(self) -> str:
        code, body = self._request("GET", self._url("/cluster/state"))
        if isinstance(body, dict):
            self.cluster_name = _na(body.get("clusterName"), "")
        return self.cluster_name

    def _domain_filter_body(self, extra: Optional[dict] = None) -> dict:
        data = {
            "filters": [{"type": "DOMAIN", "value": self.domain_id}],
            "fullTextSearch": {"type": "AND", "value": ""},
            "limit": 10000,
        }
        if extra:
            data.update(extra)
        return data

    def fetch_aps(self) -> List[dict]:
        body = self._domain_filter_body({"attributes": ["*"]})
        code, resp = self._request("POST", self._url("/query/ap"), body)
        if isinstance(resp, dict) and isinstance(resp.get("list"), list):
            return resp["list"]
        return []

    def fetch_bssids(self) -> List[dict]:
        body = self._domain_filter_body()
        code, resp = self._request("POST", self._url("/query/ap/wlan"), body)
        if isinstance(resp, dict) and isinstance(resp.get("list"), list):
            return resp["list"]
        return []

    def fetch_zones(self) -> List[dict]:
        code, resp = self._request("GET", self._url("/rkszones"))
        if isinstance(resp, dict) and isinstance(resp.get("list"), list):
            return resp["list"]
        if isinstance(resp, list):
            return resp
        return []

    def fetch_zone(self, zone_id: str) -> dict:
        code, resp = self._request("GET", self._url(f"/rkszones/{zone_id}"))
        return resp if isinstance(resp, dict) else {}

    def zone_current_fw(self, zone: dict) -> str:
        if not isinstance(zone, dict):
            return ""
        for key in (
            "firmwareVersion",
            "apFirmwareVersion",
            "apFirmware",
            "zoneFirmwareVersion",
            "version",
        ):
            val = zone.get(key)
            if isinstance(val, dict):
                val = val.get("firmwareVersion") or val.get("version") or ""
            if val:
                return str(val).strip()
        return ""

    def fetch_ap_firmware(self, zone_id: str) -> List[str]:
        code, resp = self._request("GET", self._url(f"/rkszones/{zone_id}/apFirmware"))
        items = []
        if isinstance(resp, dict) and isinstance(resp.get("list"), list):
            items = resp["list"]
        elif isinstance(resp, list):
            items = resp
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            ver = it.get("firmwareVersion") or it.get("version") or ""
            if ver and it.get("supported", True):
                out.append(str(ver))
        return out

    def fetch_ap_rules(self) -> List[dict]:
        code, resp = self._request("GET", self._url("/apRules"))
        if isinstance(resp, dict) and isinstance(resp.get("list"), list):
            return resp["list"]
        if isinstance(resp, list):
            return resp
        return []

    def fetch_ap_rule(self, rule_id: str) -> dict:
        code, resp = self._request("GET", self._url(f"/apRules/{rule_id}"))
        return resp if isinstance(resp, dict) else {}

    def create_ap_rule(self, payload: dict) -> Tuple[bool, Any]:
        code, resp = self._request("POST", self._url("/apRules"), payload)
        ok = code in (200, 201, 204)
        return ok, resp

    def delete_ap_rule(self, rule_id: str) -> Tuple[bool, Any]:
        code, resp = self._request("DELETE", self._url(f"/apRules/{rule_id}"))
        ok = code in (200, 201, 204)
        return ok, resp

    def fetch_dpsk_list(self, zone_id: str) -> List[dict]:
        code, resp = self._request("GET", self._url(f"/rkszones/{zone_id}/dpsk"))
        if isinstance(resp, dict) and isinstance(resp.get("list"), list):
            return resp["list"]
        if isinstance(resp, list):
            return resp
        return []

    def fetch_dpsk_wlans(self, zone_id: str) -> List[dict]:
        out = []
        for w in self.fetch_wlans_in_zone(zone_id):
            wid = w.get("id")
            if not wid:
                continue
            code, detail = self.fetch_wlan(zone_id, wid)
            if not isinstance(detail, dict):
                continue
            dpsk = detail.get("dpsk") or {}
            if dpsk.get("dpskEnabled"):
                out.append({
                    "id": detail.get("id") or wid,
                    "name": detail.get("name") or w.get("name") or "",
                    "ssid": detail.get("ssid") or w.get("ssid") or "",
                })
        return out

    def fetch_user_roles(self) -> List[dict]:
        for path in ("/identityUserRoles", "/identityUserRole", "/query/identityUserRole"):
            code, resp = self._request("GET", self._url(path))
            items = []
            if isinstance(resp, dict) and isinstance(resp.get("list"), list):
                items = resp["list"]
            elif isinstance(resp, list):
                items = resp
            out = []
            for it in items:
                if isinstance(it, dict) and (it.get("id") or it.get("name")):
                    out.append({"id": it.get("id") or "", "name": it.get("name") or it.get("id") or ""})
            if out:
                return out
        return []

    def create_dpsk(
        self,
        zone_id: str,
        wlan_id: str,
        username: str = "",
        group: bool = False,
        vlan_id: Optional[int] = None,
        amount: int = 1,
        passphrase: str = "",
        user_role_id: str = "",
    ) -> Tuple[int, Any]:
        payload: dict = {"groupDpsk": bool(group), "amount": max(1, int(amount or 1))}
        if username:
            payload["userName"] = username
        if passphrase:
            payload["passphraseList"] = [passphrase]
        if user_role_id:
            payload["userRoleId"] = user_role_id
        if vlan_id and 1 <= int(vlan_id) <= 4094:
            payload["vlanId"] = int(vlan_id)
        return self._request(
            "POST",
            self._url(f"/rkszones/{zone_id}/wlans/{wlan_id}/dpsk/batchGenUnbound"),
            payload,
        )

    def delete_dpsk(self, zone_id: str, wlan_id: str, id_list: List[str]) -> Tuple[int, Any]:
        return self._request(
            "POST",
            self._url(f"/rkszones/{zone_id}/wlans/{wlan_id}/dpsk"),
            {"idList": list(id_list)},
        )

    def fetch_wlans_in_zone(self, zone_id: str) -> List[dict]:
        code, resp = self._request("GET", self._url(f"/rkszones/{zone_id}/wlans"))
        if isinstance(resp, dict) and isinstance(resp.get("list"), list):
            return resp["list"]
        if isinstance(resp, list):
            return resp
        return []

    def fetch_wlan(self, zone_id: str, wlan_id: str) -> Tuple[int, Any]:
        return self._request("GET", self._url(f"/rkszones/{zone_id}/wlans/{wlan_id}"))

    def list_psk_wlans(self, log=print) -> List[dict]:
        """존별 WLAN을 모아 PSK/SAE 대상 행으로 반환"""
        rows = []
        zones = self.fetch_zones()
        log(f"Zone {len(zones)} 개")
        for z in zones:
            zid = z.get("id") or z.get("zoneId") or ""
            zname = z.get("name") or z.get("zoneName") or ""
            if not zid:
                continue
            wlans = self.fetch_wlans_in_zone(zid)
            log(f"  zone={zname} WLAN {len(wlans)} 개")
            for w in wlans:
                enc = w.get("encryption") or {}
                if not isinstance(enc, dict):
                    enc = {}
                method = str(enc.get("method") or w.get("encryptionMethod") or "")
                wtype = str(w.get("type") or "")
                changeable = _enc_changeable(wtype, method)
                rows.append({
                    "zoneId": zid,
                    "zoneName": zname,
                    "wlanId": w.get("id") or "",
                    "wlanName": w.get("name") or "",
                    "ssid": w.get("ssid") or w.get("name") or "",
                    "type": wtype or "N/A",
                    "method": method or "N/A",
                    "hasPsk": method == "WPA2" or _enc_has_psk(method),
                    "hasSae": method == "WPA3" or _enc_has_sae(method),
                    "changeable": changeable,
                })
        return rows

    def collect_wlan_details(self, zone_id: Optional[str] = None, log=print) -> List[dict]:
        """PHP get_wlan_details: 각 WLAN GET 으로 type/encryption/passphrase 조회."""
        zones = self.fetch_zones()
        rows = []
        for z in zones:
            zid = z.get("id") or z.get("zoneId") or ""
            zname = z.get("name") or z.get("zoneName") or ""
            if not zid:
                continue
            if zone_id and zone_id != "ALL" and zid != zone_id:
                continue
            wlans = self.fetch_wlans_in_zone(zid)
            log(f"  zone={zname} WLAN {len(wlans)} 개 → 상세 조회")
            for w in wlans:
                wid = w.get("id") or ""
                ssid = w.get("ssid") or w.get("name") or ""
                code, data = self.fetch_wlan(zid, wid) if wid else (0, {})
                if not isinstance(data, dict):
                    data = {}
                enc = data.get("encryption") if isinstance(data.get("encryption"), dict) else {}
                method = str(enc.get("method") or "")
                wtype = str(data.get("type") or w.get("type") or "")
                algo = str(enc.get("algorithm") or "")
                mfp = enc.get("mfp") or enc.get("managementFrameProtection") or ""
                psk = enc.get("passphrase") or ""
                sae = enc.get("saePassphrase") or ""
                dpsk_obj = data.get("dpsk") if isinstance(data.get("dpsk"), dict) else {}
                dpsk_on = bool(
                    dpsk_obj.get("dpskEnabled")
                    or dpsk_obj.get("enabled")
                    or ("dpsk" in (wtype or "").lower())
                )
                changeable = _enc_changeable(wtype, method) and not dpsk_on
                rows.append({
                    "zoneId": zid,
                    "zoneName": zname,
                    "wlanId": wid,
                    "wlanName": data.get("name") or w.get("name") or "",
                    "ssid": data.get("ssid") or ssid,
                    "type": wtype or "N/A",
                    "dpsk": "Y" if dpsk_on else "N",
                    "method": method or "N/A",
                    "algorithm": algo or "N/A",
                    "mfp": mfp if mfp else "-",
                    "passphrase": psk if psk else "-",
                    "saePassphrase": sae if sae else "-",
                    "changeable": changeable,
                    "http": code,
                })
                log(f"    {ssid} type={wtype} dpsk={dpsk_on} method={method} changeable={changeable} HTTP={code}")
        return rows

    def update_wlan_password(
        self,
        zone_id: str,
        wlan_id: str,
        new_password: str,
        method: str = "WPA2",
        log=print,
    ) -> Tuple[bool, str]:
        """WPA2/WPA3는 PHP와 동일. WPA23_Mixed는 passphrase + saePassphrase 동시 변경."""
        raw = method or "WPA2"
        key = raw.upper().replace("-", "").replace("_", "")
        if _enc_is_mixed(raw):
            payload = {
                "encryption": {
                    "method": raw if raw else "WPA23_Mixed",
                    "algorithm": "AES",
                    "passphrase": new_password,
                    "saePassphrase": new_password,
                    "mfp": "capable",
                }
            }
        elif key == "WPA3":
            payload = {
                "encryption": {
                    "method": "WPA3",
                    "algorithm": "AES",
                    "saePassphrase": new_password,
                    "mfp": "required",
                }
            }
        elif key == "WPA2":
            payload = {
                "encryption": {
                    "method": "WPA2",
                    "algorithm": "AES",
                    "passphrase": new_password,
                    "mfp": "disabled",
                }
            }
        else:
            return False, f"지원하지 않는 method={method}"
        url = self._url(f"/rkszones/{zone_id}/wlans/{wlan_id}")
        log(f"      PATCH {url}")
        log(f"      payload={json.dumps(payload, ensure_ascii=False)}")
        code, resp = self._request("PATCH", url, payload)
        if code in (200, 204):
            return True, f"암호 변경 성공 HTTP={code}"
        err = ""
        if isinstance(resp, dict):
            err = resp.get("message") or resp.get("error") or resp.get("raw") or str(resp)
        else:
            err = str(resp)
        return False, f"암호 변경 실패 HTTP={code} {err}"

    def fetch_switches(self) -> List[dict]:
        body = self._domain_filter_body({
            "sortInfo": {"sortColumn": "serialNumber", "dir": "ASC"},
            "page": 1,
        })
        code, resp = self._request("POST", self._url("/switch", switch=True), body)
        if isinstance(resp, dict) and isinstance(resp.get("list"), list):
            return resp["list"]
        return []

    def collect_all(self, log=print) -> dict:
        ok, msg = self.login()
        log(msg)
        if not ok:
            return {"ok": False, "error": msg}

        log("클러스터 정보 조회...")
        cluster = self.fetch_cluster()
        log(f"  clusterName={cluster or 'N/A'}")

        log("AP 리스트 조회...")
        aps = self.fetch_aps()
        log(f"  AP {len(aps)} 대")

        log("BSSID/WLAN 조회...")
        bssids_raw = self.fetch_bssids()
        bssid_rows = flatten_bssids(bssids_raw, aps)
        log(f"  BSSID 행 {len(bssid_rows)} 개")

        log("Switch 리스트 조회...")
        switches = self.fetch_switches()
        log(f"  Switch {len(switches)} 대")

        return {
            "ok": True,
            "host": self.host,
            "api_version": self.api_version,
            "controller_version": self.controller_version,
            "cluster_name": cluster,
            "aps": aps,
            "bssid_rows": bssid_rows,
            "switches": switches,
        }


def _radio_id_to_desc(radio_id) -> str:
    """원본 PHP: 0→2.4_Ghz, 1→5.0_Ghz, 2→6.0_Ghz (0 은 falsy 이므로 is None 으로 구분)"""
    if radio_id is None or radio_id == "":
        return "N/A"
    try:
        rid = int(radio_id)
    except (TypeError, ValueError):
        return _na(radio_id)
    if rid == 0:
        return "2.4_Ghz"
    if rid == 1:
        return "5.0_Ghz"
    if rid == 2:
        return "6.0_Ghz"
    return str(rid)


def flatten_bssids(bssid_data: List[dict], ap_data: List[dict]) -> List[dict]:
    """원본 PHP처럼 wlanBssids 펼치고 AP IP / eirp24G / eirp50G / eirp6G 매칭"""
    ap_by_mac = {}
    for ap in ap_data:
        mac = (ap.get("apMac") or ap.get("mac") or "").lower()
        if mac:
            ap_by_mac[mac] = ap

    rows = []
    for item in bssid_data:
        ap_mac = item.get("apMac") or ""
        device_name = item.get("deviceName") or ""
        ap = ap_by_mac.get(str(ap_mac).lower(), {}) if ap_mac else {}
        # 원본 PHP 필드명과 동일
        ip = ap.get("ip") if ap.get("ip") is not None else "N/A"
        eirp2g = ap.get("eirp24G") if ap.get("eirp24G") is not None else "N/A"
        eirp5g = ap.get("eirp50G") if ap.get("eirp50G") is not None else "N/A"
        eirp6g = ap.get("eirp6G") if ap.get("eirp6G") is not None else "N/A"

        wlans = item.get("wlanBssids") or []
        if not isinstance(wlans, list):
            continue
        for w in wlans:
            # radioId 가 0 이어도 유지 (or 사용 금지)
            radio_id = w.get("radioId")
            if radio_id is None:
                radio_id = w.get("radio")
            wlan_name = w.get("wlanName")
            if wlan_name is None:
                wlan_name = w.get("ssid")
            rows.append({
                "deviceName": device_name,
                "apMac": ap_mac,
                "wlanName": _na(wlan_name),
                "bssid": _na(w.get("bssid")),
                "radioid": _radio_id_to_desc(radio_id),
                "ip": _na(ip),
                "eirp2G": _na(eirp2g),
                "eirp5G": _na(eirp5g),
                "eirp6G": _na(eirp6g),
            })
    return rows


# 원본 PHP AP 리스트 / CSV 열 순서
AP_ROW_FIELDS = (
    "AP_Name", "IP", "AP_MAC", "serial", "Model",
    "channel2G", "channel5G", "channel6G",
    "status", "config_status", "firmwareVer",
    "airtime2G", "airtime5G", "airtime6G",
    "noise2G", "noise5G", "noise6G",
    "eirp2G", "eirp5G", "eirp6G",
    "Clients", "poePort", "ZoneDomain",
)


def _ap_val(ap: dict, *keys):
    for k in keys:
        if k in ap and ap[k] is not None and ap[k] != "":
            return ap[k]
    return None


def ap_to_row(ap: dict) -> dict:
    # 원본 api-sz/index.php 와 동일 키
    zone = _ap_val(ap, "zoneName")
    domain = _ap_val(ap, "domainName")
    if zone or domain:
        zone_domain = f"{zone or 'N/A'} ({domain or 'N/A'})"
    else:
        zone_domain = "N/A"
    return {
        "AP_Name": _na(_ap_val(ap, "deviceName", "apName")),
        "IP": _na(_ap_val(ap, "ip", "externalIp")),
        "AP_MAC": _na(_ap_val(ap, "apMac", "mac")),
        "serial": _na(_ap_val(ap, "serial")),
        "Model": _na(_ap_val(ap, "model")),
        "channel2G": _na(_ap_val(ap, "channel24G", "channel2G")),
        "channel5G": _na(_ap_val(ap, "channel5G", "channel50G")),
        "channel6G": _na(_ap_val(ap, "channel6G")),
        "status": _na(_ap_val(ap, "connectionStatus", "status")),
        "config_status": _na(_ap_val(ap, "configurationStatus", "configState")),
        "firmwareVer": _na(_ap_val(ap, "firmwareVersion", "firmware")),
        "airtime2G": _na(_ap_val(ap, "airtime24G", "airtime2G")),
        "airtime5G": _na(_ap_val(ap, "airtime5G", "airtime50G")),
        "airtime6G": _na(_ap_val(ap, "airtime6G")),
        "noise2G": _na(_ap_val(ap, "noise24G", "noise2G")),
        "noise5G": _na(_ap_val(ap, "noise5G", "noise50G")),
        "noise6G": _na(_ap_val(ap, "noise6G")),
        "eirp2G": _na(_ap_val(ap, "eirp24G", "eirp2G")),
        "eirp5G": _na(_ap_val(ap, "eirp50G", "eirp5G")),
        "eirp6G": _na(_ap_val(ap, "eirp6G")),
        "Clients": _na(_ap_val(ap, "numClients", "clientCount")),
        "poePort": _na(_ap_val(ap, "poePortStatus", "poePortType", "poePort")),
        "ZoneDomain": zone_domain,
    }


def switch_to_row(sw: dict) -> dict:
    return {
        "switchName": _na(sw.get("name") or sw.get("switchName")),
        "ipAddress": _na(sw.get("ipAddress") or sw.get("ip")),
        "macAddress": _na(sw.get("macAddress") or sw.get("mac")),
        "serialNumber": _na(sw.get("serialNumber") or sw.get("serial")),
        "model": _na(sw.get("model")),
        "status": _na(sw.get("status")),
        "upTime": _na(sw.get("upTime") or sw.get("uptime")),
        "firmwareVersion": _na(sw.get("firmwareVersion") or sw.get("firmware")),
    }


def save_csv(path: Path, fieldnames: List[str], rows: List[dict]) -> Path:
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path
