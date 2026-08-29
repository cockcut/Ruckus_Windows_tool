# -*- coding: utf-8 -*-
"""
HSITX Ruckus Technical Tool (Windows Native)
PHP 없이 동작하는 콘솔 프로그램
Expect 대체: Paramiko 기반 SSH 자동화
"""

import os
import sys
import csv
import time
from datetime import datetime
from pathlib import Path

# 로컬 모듈
sys.path.insert(0, str(Path(__file__).parent))
from modules.ssh_helper import (
    process_ap,
    RuckusSSH,
    DEFAULT_USER,
    DEFAULT_PASSWORD,
    STANDARD_PASSWORD,
)

# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------

def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def pause():
    input("\n[Enter] 키를 누르면 메뉴로 돌아갑니다...")


def print_header(title: str = ""):
    print("=" * 60)
    print("  HSITX Ruckus Technical Tool (Windows)")
    if title:
        print(f"  ▸ {title}")
    print("=" * 60)


def load_csv(path: str) -> list:
    """8컬럼 CSV 로드 (인코딩·구분자 자동 감지)"""
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        text = None
        for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr", "utf-16", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise UnicodeDecodeError("unknown", raw, 0, 1, "CSV 인코딩을 알 수 없습니다")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    sample_lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")][:5]
    sample = "\n".join(sample_lines) if sample_lines else text[:2048]
    counts = {",": sample.count(","), "\t": sample.count("\t"), ";": sample.count(";")}
    delim = max(counts, key=counts.get) if max(counts.values()) > 0 else ","
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",\t;").delimiter
    except Exception:
        pass
    rows = []
    reader = csv.reader(text.splitlines(), delimiter=delim)
    for i, row in enumerate(reader, 1):
        if not row:
            continue
        if len(row) == 1 and row[0]:
            cell = str(row[0])
            for sep in ("\t", ";", ","):
                if sep in cell:
                    row = [p.strip() for p in cell.split(sep)]
                    break
        if not row or (str(row[0]).strip().startswith("#")):
            continue
        while len(row) < 8:
            row.append("")
        row = [str(c).strip().strip('"').strip("'") for c in row[:8]]
        if not row[0]:
            continue
        ip = row[0].split()[0]
        rows.append({
            "ip": ip,
            "user": row[1] or DEFAULT_USER,
            "pass": row[2] or DEFAULT_PASSWORD,
            "new_ip": row[3],
            "subnet": row[4],
            "gw": row[5],
            "sz": row[6],
            "hostname": row[7],
            "line": i,
        })
    return rows


def save_results(results: list, operation: str):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path("results") / "ap_batch"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"result_{operation}_{ts}.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ip", "status", "message", "serial", "mac", "user", "password"])
        for r in results:
            w.writerow([
                r.get("ip", ""),
                r.get("status", ""),
                r.get("message", ""),
                r.get("serial", ""),
                r.get("mac", ""),
                r.get("user", ""),
                r.get("password", ""),
            ])
    print(f"\n결과 저장됨 → {csv_path}")
    return csv_path


# ---------------------------------------------------------------------------
# 메뉴 6: AP 일괄 작업 (Expect 대체 핵심)
# ---------------------------------------------------------------------------

OPERATIONS = {
    "1": ("connect_sz", "SZ(SCG) IP 설정"),
    "2": ("changeip", "AP IP 변경 (new_ip/subnet/gw)"),
    "3": ("devicename", "Device Name(호스트명) 변경"),
    "4": ("sz_devicename_changeip", "SZ + 호스트명 + IP 동시 변경"),
    "5": ("reboot", "재부팅"),
    "6": ("factory_reset", "공장 초기화 + 재부팅"),
}


def menu_ap_batch():
    clear_screen()
    print_header("6. AP IP 일괄 변경 / SZ 연동 / 공장초기화 등")

    print("\n작업 종류를 선택하세요:")
    for k, (op, desc) in OPERATIONS.items():
        print(f"  {k}. {desc}  ({op})")
    print("  0. 뒤로")

    choice = input("\n선택 > ").strip()
    if choice == "0" or choice not in OPERATIONS:
        return

    operation, desc = OPERATIONS[choice]
    print(f"\n선택된 작업: {desc}")

    # CSV 경로 입력
    default = "samples/ap_sample.csv"
    path = input(f"CSV 파일 경로 [{default}] > ").strip() or default
    if not Path(path).is_file():
        print(f"파일을 찾을 수 없습니다: {path}")
        pause()
        return

    rows = load_csv(path)
    if not rows:
        print("유효한 행이 없습니다.")
        pause()
        return

    print(f"\n총 {len(rows)} 대 장비를 처리합니다.")
    debug = input("디버그 모드? (y/N) > ").strip().lower() == "y"
    confirm = input("계속할까요? (y/N) > ").strip().lower()
    if confirm != "y":
        return

    results = []
    for i, row in enumerate(rows, 1):
        print(f"\n[{i}/{len(rows)}] {row['ip']}")
        r = process_ap(
            ip=row["ip"],
            user=row["user"],
            password=row["pass"],
            operation=operation,
            new_ip=row["new_ip"],
            subnet=row["subnet"],
            gw=row["gw"],
            sz=row["sz"],
            hostname=row["hostname"],
            debug=debug,
        )
        results.append(r)
        time.sleep(0.3)

    # 요약
    ok_cnt = sum(1 for r in results if r["status"] == "OK")
    print("\n" + "-" * 40)
    print(f"완료: 성공 {ok_cnt} / 전체 {len(results)}")
    save_results(results, operation)
    pause()


# ---------------------------------------------------------------------------
# 단일 AP 테스트 (빠른 검증용)
# ---------------------------------------------------------------------------

def menu_single_test():
    clear_screen()
    print_header("단일 AP SSH 테스트 (Expect 대체 검증)")

    print("비밀번호 정책 (자동):")
    print(f"  1) 공장초기화 AP: {DEFAULT_USER}/{DEFAULT_PASSWORD}")
    print(f"     → 강제 변경 → {STANDARD_PASSWORD} → 재로그인")
    print(f"  2) 이미 변경된 AP: {DEFAULT_PASSWORD} 실패 시 → {STANDARD_PASSWORD} 재시도")
    print()
    print("보통 IP만 입력하면 됩니다. (계정/비번은 Enter로 기본값 사용)\n")

    ip = input("AP IP > ").strip()
    user = input(f"Username [{DEFAULT_USER}] > ").strip() or DEFAULT_USER
    pw = input(f"Password 첫시도 [{DEFAULT_PASSWORD}] (Enter=자동정책) > ").strip()
    if not ip:
        return

    print("\n연결 테스트 중... (인증 전 과정이 아래에 표시됩니다)\n")
    ssh = RuckusSSH(timeout=20, debug=True, verbose=True)
    # password=None/빈값이면 sp-admin → Ruckus!234 순서로 자동 시도
    ok, msg = ssh.connect(
        ip,
        username=user,
        password=pw or None,
        new_password=STANDARD_PASSWORD,
        standard_password=STANDARD_PASSWORD,
    )
    if not ok:
        print(f"실패: {msg}")
        pause()
        return

    print("로그인 성공!  boarddata 조회...")
    serial, mac = ssh.get_boarddata()
    print(f"Serial : {serial}")
    print(f"MAC    : {mac}")

    print("\n추가 명령어를 입력하세요 (빈 줄 = 종료)")
    while True:
        cmd = input("rkscli> ").strip()
        if not cmd:
            break
        ok, out = ssh.run(cmd, success_pattern=r"rkscli", timeout=10)
        lines = [l for l in out.splitlines() if l.strip()]
        for l in lines[-15:]:
            print(l)
        print("---")

    ssh.close()
    pause()


# ---------------------------------------------------------------------------
# 기타 메뉴 (스텁 – 이후 확장)
# ---------------------------------------------------------------------------

def menu_stub(name: str):
    clear_screen()
    print_header(name)
    print("\n이 기능은 뼈대만 준비되어 있습니다.")
    print("API 기반 기능은 requests 로,")
    print("SSH 기반 기능은 modules/ssh_helper.py 를 확장하면 됩니다.")
    print("\n원하시면 다음 단계에서 구체적인 기능을 채워 드리겠습니다.")
    pause()


def menu_oui():
    clear_screen()
    print_header("11. OUI 조회")
    print("IEEE OUI 리스트를 다운로드하여 로컬에 저장합니다.")
    confirm = input("다운로드 할까요? (y/N) > ").strip().lower()
    if confirm != "y":
        return
    try:
        import urllib.request
        url = "https://standards-oui.ieee.org/"
        out_dir = Path("oui")
        out_dir.mkdir(exist_ok=True)
        html_path = out_dir / "oui.html"
        txt_path = out_dir / "oui.txt"
        print("다운로드 중...")
        urllib.request.urlretrieve(url, html_path)
        # 간단 파싱
        with open(html_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        lines = []
        for line in content.splitlines():
            if "(hex)" in line:
                parts = line.split("(hex)")
                if len(parts) >= 2:
                    oui = parts[0].strip()
                    vendor = parts[1].strip()
                    lines.append(f"{oui}\t{vendor}")
        ts = datetime.now().strftime("%Y/%m/%d %H:%M")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"* {ts} 업데이트\n\n")
            f.write("OUI\t\tVendor\n")
            f.write("-" * 80 + "\n")
            f.write("\n".join(lines))
            f.write("\n" + "-" * 80 + "\nEND\n")
        print(f"완료 → {txt_path}  ({len(lines)} entries)")
    except Exception as e:
        print(f"실패: {e}")
    pause()


# ---------------------------------------------------------------------------
# 메인 메뉴
# ---------------------------------------------------------------------------

MENU = """
  1. SmartZone 정보 보기                    (API – 추후)
  2. 언리시드 정보 보기                     (API – 추후)
  3. PSK/SAE 패스워드 일괄변경              (API – 추후)
  4. SmartZone DPSK 관리                    (API – 추후)
  5. Unleashed DPSK 관리                    (API – 추후)
  6. AP IP 일괄 변경 / SZ 연동 / 공장초기화  ★ SSH 자동화
  7. AP 펌웨어 업그레이드 (CSV)             (SSH – 추후)
  8. AP 펌웨어 CLI 명령어 생성              (파일생성 – 추후)
  9. AP → SZ 펌웨어 업그레이드 + 연동       (SSH – 추후)
 10. ICX ARP 조회 (SNMP)                    (추후)
 11. OUI 조회 (매일 업데이트)
 12. 단일 AP SSH 테스트 (디버그용)          ★
  0. 종료
"""


def main():
    while True:
        clear_screen()
        print_header()
        print(MENU)
        choice = input("선택 > ").strip()

        if choice == "0":
            print("종료합니다.")
            break
        elif choice == "6":
            menu_ap_batch()
        elif choice == "11":
            menu_oui()
        elif choice == "12":
            menu_single_test()
        elif choice in ("1", "2", "3", "4", "5", "7", "8", "9", "10"):
            titles = {
                "1": "1. SmartZone 정보 보기",
                "2": "2. 언리시드 정보 보기",
                "3": "3. PSK/SAE 패스워드 일괄변경",
                "4": "4. SmartZone DPSK 관리",
                "5": "5. Unleashed DPSK 관리",
                "7": "7. AP 펌웨어 업그레이드",
                "8": "8. AP 펌웨어 CLI 명령어 생성",
                "9": "9. AP → SZ 펌웨어 업그레이드",
                "10": "10. ICX ARP 조회",
            }
            menu_stub(titles[choice])
        else:
            print("잘못된 선택입니다.")
            time.sleep(0.8)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n사용자에 의해 중단되었습니다.")
        sys.exit(0)
