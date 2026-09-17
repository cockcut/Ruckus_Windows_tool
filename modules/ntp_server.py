# -*- coding: utf-8 -*-
"""Windows Time NTP server on/off (temporary lab use)."""

from __future__ import annotations

import locale
import subprocess
import sys

RULE_NAME = "HSITX-NTP-UDP-123"

_STATE_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$en = 0
$k = 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpServer'
if (Test-Path $k) { $en = [int](Get-ItemProperty $k).Enabled }
$svc = Get-Service w32time
$fw = [bool](Get-NetFirewallRule -DisplayName 'HSITX-NTP-UDP-123')
$ips = @(Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } |
    Select-Object -ExpandProperty IPAddress | Sort-Object -Unique) -join ', '
Write-Output ("ENABLED={0}" -f $en)
Write-Output ("SVC={0}" -f $svc.Status)
Write-Output ("START={0}" -f $svc.StartType)
Write-Output ("FW={0}" -f $(if ($fw) {'1'} else {'0'}))
Write-Output ("IPS={0}" -f $ips)
"""

_ON_PS = r"""
$ErrorActionPreference = 'Stop'
$RuleName = 'HSITX-NTP-UDP-123'
Write-Output 'NTP ON'
Set-Service w32time -StartupType Automatic
$svc = Get-Service w32time
if ($svc.Status -ne 'Running') { Start-Service w32time }
$ntpKey = 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpServer'
if (-not (Test-Path $ntpKey)) { throw 'NtpServer registry key not found' }
Set-ItemProperty -Path $ntpKey -Name Enabled -Type DWord -Value 1
Write-Output 'Registry NtpServer\Enabled = 1'
& w32tm.exe /config /reliable:yes /update | Out-String | Write-Output
Restart-Service w32time -Force
Start-Sleep -Seconds 2
$fw = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if (-not $fw) {
    New-NetFirewallRule -DisplayName $RuleName -Name $RuleName `
        -Direction Inbound -Protocol UDP -LocalPort 123 `
        -Action Allow -Profile Any -Enabled True | Out-Null
    Write-Output "Firewall added: $RuleName UDP 123"
} else {
    Enable-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
    Write-Output "Firewall exists: $RuleName"
}
Write-Output 'DONE'
"""

_OFF_PS = r"""
$ErrorActionPreference = 'Stop'
$RuleName = 'HSITX-NTP-UDP-123'
Write-Output 'NTP OFF restore default'
$ntpKey = 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpServer'
$cfgKey = 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\Config'
if (Test-Path $ntpKey) {
    Set-ItemProperty -Path $ntpKey -Name Enabled -Type DWord -Value 0
    Write-Output 'Registry NtpServer\Enabled = 0'
}
if (Test-Path $cfgKey) {
    Set-ItemProperty -Path $cfgKey -Name AnnounceFlags -Type DWord -Value 10
    Write-Output 'Registry Config\AnnounceFlags = 10'
}
$svc = Get-Service w32time -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq 'Running') {
    & w32tm.exe /config /reliable:no /update | Out-String | Write-Output
}
if ($svc) {
    if ($svc.Status -ne 'Stopped') {
        Stop-Service w32time -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
    Set-Service w32time -StartupType Manual
    Write-Output 'w32time Stopped / Manual'
}
$fw = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if ($fw) {
    Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
    Write-Output "Firewall removed: $RuleName"
} else {
    Write-Output 'Firewall not present'
}
Write-Output 'DONE'
"""


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_admin() -> bool:
    if not is_windows():
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _decode(data: bytes) -> str:
    if not data:
        return ""
    preferred = locale.getpreferredencoding(False) or ""
    for enc in (preferred, "cp949", "utf-8-sig", "utf-8"):
        if not enc:
            continue
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("cp949", errors="replace")


def _ps(script: str) -> tuple[int, str]:
    p = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
    )
    return p.returncode, (_decode(p.stdout) + _decode(p.stderr)).strip()


def _ps_admin(script: str) -> tuple[int, str]:
    """Run script elevated. Shows UAC if the GUI is not already admin."""
    if is_admin():
        return _ps(script)

    import tempfile
    from pathlib import Path as P

    td = P(tempfile.gettempdir())
    ps1 = td / "hsitx_ntp_job.ps1"
    log = td / "hsitx_ntp_job.log"
    try:
        if log.exists():
            log.unlink()
    except Exception:
        pass

    log_ps = str(log).replace("'", "''")
    wrapper = (
        "$ErrorActionPreference = 'Stop'\n"
        f"$log = '{log_ps}'\n"
        "function Write-Log([string]$m) {\n"
        "  $m | Out-File -FilePath $log -Append -Encoding utf8\n"
        "}\n"
        "try {\n"
        "  $out = & {\n"
        f"{script}\n"
        "  } 2>&1 | ForEach-Object { $_.ToString() }\n"
        "  if ($out) { $out | Out-File -FilePath $log -Append -Encoding utf8 }\n"
        "  'EXIT=0' | Out-File -FilePath $log -Append -Encoding utf8\n"
        "} catch {\n"
        "  $_ | Out-File -FilePath $log -Append -Encoding utf8\n"
        "  'EXIT=1' | Out-File -FilePath $log -Append -Encoding utf8\n"
        "}\n"
    )
    ps1.write_text(wrapper, encoding="utf-8-sig")

    # Start-Process -Verb RunAs (UAC). Hidden window; output is in the log file.
    launcher = (
        f"Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait "
        f"-WindowStyle Hidden "
        f"-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','{ps1}'"
    )
    p = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", launcher],
        capture_output=True,
    )
    extra = (_decode(p.stdout) + _decode(p.stderr)).strip()
    text = ""
    if log.exists():
        raw = log.read_bytes()
        text = _decode(raw).strip()
    if p.returncode != 0 and not text:
        msg = extra or "UAC가 취소되었거나 권한 상승에 실패했습니다."
        return p.returncode, msg
    code = 1 if "EXIT=1" in text else 0
    if "EXIT=0" not in text and "EXIT=1" not in text and p.returncode != 0:
        code = p.returncode
    if extra:
        text = (text + "\n" + extra).strip()
    return code, text


def query_state() -> dict:
    d = {
        "ENABLED": "0",
        "SVC": "-",
        "START": "-",
        "FW": "0",
        "IPS": "",
        "ON": False,
        "ok": True,
        "error": "",
    }
    if not is_windows():
        d["ok"] = False
        d["error"] = "Windows 에서만 동작합니다."
        return d
    code, text = _ps(_STATE_PS)
    if code != 0 and not text:
        d["ok"] = False
        d["error"] = f"상태 조회 실패 (exit {code})"
        return d
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    d["ON"] = d.get("ENABLED") == "1" and str(d.get("SVC", "")).lower() == "running"
    return d


def turn_on() -> tuple[bool, str]:
    if not is_windows():
        return False, "Windows 에서만 동작합니다."
    code, text = _ps_admin(_ON_PS)
    return code == 0, text or f"exit {code}"


def turn_off() -> tuple[bool, str]:
    if not is_windows():
        return False, "Windows 에서만 동작합니다."
    code, text = _ps_admin(_OFF_PS)
    return code == 0, text or f"exit {code}"
