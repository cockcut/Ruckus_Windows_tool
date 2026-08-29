# -*- coding: utf-8 -*-
"""
AP 펌웨어 CLI 명령어 / .rcks 생성기
원본 fw/fw.sh 로직 이식
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Tuple


HEADER_AND_CSS = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Firmware Update Commands</title>
<style>
body { font-family: Arial, sans-serif; background-color: #f4f4f4; color: #333; margin: 20px; }
.command-block { background-color: #fff; border: 1px solid #ddd; border-radius: 8px;
  padding: 15px; margin-bottom: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
h1 { color: #d9534f; text-align: center; border-bottom: 2px solid #eee;
  padding-bottom: 10px; margin-top: 0; }
h2 { color: #333; margin-top: 0; margin-bottom: 10px; }
pre { background-color: #e9e9e9; padding: 10px; border-radius: 5px;
  white-space: pre-wrap; word-wrap: break-word; }
</style>
</head>
<body>
"""

FOOTER = """
</body>
</html>
"""


def parse_bl7_name(path: Path) -> Tuple[str, str]:
    """
    파일명: MODEL_VERSION....bl7
    예: R350_110.0.0.0.1217.bl7 → model=R350, version=110.0.0.0.1217 (2번째 필드부터 합침)
    원본: cut -d'_' -f1 = model, cut -d'_' -f2 = version (첫 _ 이후 첫 세그먼트만)
    """
    stem = path.stem  # without .bl7
    if "_" not in stem:
        return stem, "unknown"
    model, version = stem.split("_", 1)
    # 원본은 cut -f2 만 쓰므로 첫 번째 _ 뒤 전체를 version_full 로 쓸 수도 있음
    # fw.sh: version_full=$(echo "$filename" | cut -d'_' -f2)  → 두 번째 필드만
    parts = stem.split("_")
    model_name = parts[0]
    version_full = parts[1] if len(parts) > 1 else "unknown"
    return model_name, version_full


def version_category(version_full: str) -> Tuple[str, str]:
    """단독형 / 언리시드 / 기타"""
    if re.match(r"^1[0-9]{2}", version_full):
        return "standalone", f"단독형 펌웨어 ({version_full} 버전)"
    if version_full.startswith("200."):
        return "unleashed", f"언리시드 펌웨어 ({version_full} 버전)"
    return "other", f"기타 펌웨어 ({version_full} 버전)"


def web_relative_path(output_path: Path, web_root_hint: str = "") -> str:
    """
    AP 가 HTTP 로 받을 경로 (LOCATION_PATH).
    원본: OUTPUT_PATH 에서 /var/www/html/ 접두 제거.
    Windows 에서는 사용자가 지정한 '웹 상대 경로' 또는 폴더명 사용.
    """
    s = str(output_path).replace("\\", "/")
    for prefix in ("/var/www/html/", "C:/var/www/html/", "c:/var/www/html/"):
        if s.lower().startswith(prefix.lower()):
            return s[len(prefix):].lstrip("/")
    if web_root_hint:
        return web_root_hint.strip("/").replace("\\", "/")
    # 기본: 출력 폴더 이름만 (예: firmware)
    return output_path.name


def build_firmware_package(
    bl7_files: List[Path],
    server_ip: str,
    server_port: int = 69,
    output_path: Path = None,
    location_path: str = "",
    protocol: str = "tftp",
) -> dict:
    """
    .bl7 목록으로 .rcks + 버전별 HTML + index.html 생성.
    반환: {ok, message, index_html, rcks_files, versions, models}
    """
    output_path = Path(output_path)
    output_path.mkdir(parents=True, exist_ok=True)

    files = [Path(f) for f in bl7_files if Path(f).is_file() and Path(f).suffix.lower() == ".bl7"]
    if not files:
        return {"ok": False, "message": "유효한 .bl7 파일이 없습니다.", "index_html": None}

    protocol = "tftp"
    loc = location_path.strip().replace("\\", "/").strip("/") if location_path else web_relative_path(output_path)
    path_note = "(TFTP: 파일명만 사용, 내장 TFTP 공유 폴더 = firmware)"

    # 버전 그룹
    standalone: Dict[str, str] = {}
    unleashed: Dict[str, str] = {}
    other: Dict[str, str] = {}
    file_info = []  # (path, model, version)

    for f in files:
        model, ver = parse_bl7_name(f)
        cat, desc = version_category(ver)
        if cat == "standalone":
            standalone[ver] = desc
        elif cat == "unleashed":
            unleashed[ver] = desc
        else:
            other[ver] = desc
        file_info.append((f, model, ver, desc))

    def sort_versions(keys):
        # 문자열 버전 내림차순 유사
        return sorted(keys, reverse=True)

    # --- index.html ---
    sections = []
    for group in (standalone, unleashed, other):
        for prefix in sort_versions(group.keys()):
            desc = group[prefix]
            sections.append(
                f'<div class="section"><h2>{desc}</h2>'
                f'<p>{desc} 업그레이드 스크립트 링크입니다.</p><ul>'
                f'<li><a href="./{prefix}.html">{desc} (RCKS 파일용)</a></li>'
                f'<li><a href="./{prefix}_bl7.html">{desc} (BL7 파일용)</a></li>'
                f"</ul></div>"
            )

    index_html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AP 펌웨어 업그레이드 가이드</title>
<style>
body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #f0f2f5; color: #333; margin: 0; padding: 20px; }}
.container {{ max-width: 900px; margin: 20px auto; padding: 20px 40px; background: #fff;
  border-radius: 12px; box-shadow: 0 6px 15px rgba(0,0,0,0.1); }}
h1 {{ color: #d9534f; text-align: center; border-bottom: 2px solid #eee; padding-bottom: 10px; }}
.section {{ margin-bottom: 24px; padding: 16px; background: #f9f9f9; border-radius: 8px; border: 1px solid #e0e0e0; }}
.section h2 {{ font-size: 1.3em; color: #34495e; margin-top: 0; border-bottom: 2px solid #bdc3c7; padding-bottom: 5px; }}
.section a {{ color: #007bff; font-weight: bold; text-decoration: none; }}
.section a:hover {{ text-decoration: underline; }}
.meta {{ color: #666; font-size: 0.95em; }}
code {{ background: #e8f5e9; padding: 2px 6px; border-radius: 4px; color: #27ae60; }}
</style>
</head>
<body>
<div class="container">
  <h1>AP 펌웨어 업그레이드 하기 (CLI 명령어)</h1>
  <p class="meta">AP SSH 접속 후 모델별 명령을 붙여넣으세요. (초기화 명령 포함)</p>
  <p class="meta">프로토콜: <code>{protocol}</code> / 서버: <code>{server_ip}:{server_port}</code>
     {path_note}</p>
  <p>☞ 펌웨어 다운로드 후 <code>reboot</code> 또는 전원 재인가</p>
  <hr>
  {''.join(sections)}
</div>
</body>
</html>
"""
    index_path = output_path / "index.html"
    index_path.write_text(index_html, encoding="utf-8")

    # 버전별 HTML 초기화
    all_versions = {}
    all_versions.update(standalone)
    all_versions.update(unleashed)
    all_versions.update(other)
    version_blocks_rcks: Dict[str, List[str]] = {v: [] for v in all_versions}
    version_blocks_bl7: Dict[str, List[str]] = {v: [] for v in all_versions}

    rcks_created = []
    for f, model, ver, desc in file_info:
        size = f.stat().st_size
        # bl7 을 output 으로 복사 (HTTP 서빙용) — 이미 output 안이면 스킵
        dest_bl7 = output_path / f.name
        if f.resolve() != dest_bl7.resolve():
            dest_bl7.write_bytes(f.read_bytes())

        rcks_name = f"{model}_{ver}_cntrl.rcks"
        rcks_path = output_path / rcks_name

        if protocol == "tftp":
            # TFTP: control / rcks 내부 경로는 파일명만
            bl7_ref = f.name
            rcks_ref = rcks_name
        else:
            bl7_ref = f"{loc}/{f.name}" if loc else f.name
            bl7_ref = bl7_ref.replace("//", "/")
            rcks_ref = f"{loc}/{rcks_name}" if loc else rcks_name
            rcks_ref = rcks_ref.replace("//", "/")

        rcks_body = f"[rcks_fw.main]\n0.0.0.0\n{bl7_ref}\n{size}\n"
        rcks_path.write_text(rcks_body, encoding="utf-8")
        rcks_created.append(str(rcks_path))

        cmd_rcks = f"""-------------------------------------------------------
{model}
-------------------------------------------------------
fw set proto {protocol}
fw set port {server_port}
fw set host {server_ip}
fw set control {rcks_ref}
fw update
set factory
-------------------------------------------------------"""

        cmd_bl7 = f"""-------------------------------------------------------
{model}
-------------------------------------------------------
fw set proto {protocol}
fw set port {server_port}
fw set host {server_ip}
fw set control {bl7_ref}
fw update
set factory
-------------------------------------------------------"""

        block_r = (
            f'<div class="command-block"><h2>모델: {model}</h2>'
            f"<pre>{cmd_rcks}</pre></div>\n"
        )
        block_b = (
            f'<div class="command-block"><h2>모델: {model}</h2>'
            f"<pre>{cmd_bl7}</pre></div>\n"
        )
        version_blocks_rcks.setdefault(ver, []).append(block_r)
        version_blocks_bl7.setdefault(ver, []).append(block_b)

    for ver, desc in all_versions.items():
        p1 = output_path / f"{ver}.html"
        p2 = output_path / f"{ver}_bl7.html"
        body1 = HEADER_AND_CSS + f"<h1>{desc}</h1>\n" + "".join(version_blocks_rcks.get(ver, [])) + FOOTER
        body2 = HEADER_AND_CSS + f"<h1>{desc} - BL7</h1>\n" + "".join(version_blocks_bl7.get(ver, [])) + FOOTER
        p1.write_text(body1, encoding="utf-8")
        p2.write_text(body2, encoding="utf-8")

    return {
        "ok": True,
        "message": f"{len(files)}개 펌웨어 처리 완료",
        "index_html": str(index_path),
        "rcks_files": rcks_created,
        "versions": list(all_versions.keys()),
        "file_count": len(files),
        "location_path": loc,
        "server": f"{server_ip}:{server_port}",
        "protocol": protocol,
    }
