# -*- coding: utf-8 -*-
"""GitHub Public 저장소 업데이트. 소스는 zip, exe는 Releases/저장소 exe 파일."""

from __future__ import annotations

import io
import os
import shutil
import zipfile
from pathlib import Path

import requests

GITHUB_OWNER = "cockcut"
GITHUB_REPO = "Ruckus_Windows_tool"
GITHUB_BRANCH = "main"
API_REF = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/ref/heads/{GITHUB_BRANCH}"
API_RELEASES = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
API_CONTENTS = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents"
ZIP_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/archive/refs/heads/{GITHUB_BRANCH}.zip"
EXE_NAME = "HSITX_Ruckus_Technical_Tool.exe"
EXE_REPO_PATHS = (
    EXE_NAME,
    f"dist/{EXE_NAME}",
    f"release/{EXE_NAME}",
)
SHA_FILE_NAME = ".update_sha"
SKIP_DIR_NAMES = {".git", "__pycache__", "results", "upload", ".grok"}
SKIP_FILE_NAMES = {SHA_FILE_NAME, "update_token.txt", "build_exe.bat"}
HEADERS = {"User-Agent": "HSITX-Ruckus-Tool-Updater"}


def sha_path(root: Path) -> Path:
    return Path(root) / SHA_FILE_NAME


def read_local_sha(root: Path) -> str:
    p = sha_path(root)
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def write_local_sha(root: Path, sha: str) -> None:
    sha_path(root).write_text((sha or "").strip() + "\n", encoding="utf-8")


def _get(url: str, timeout: int = 20, stream: bool = False):
    try:
        return requests.get(url, headers=HEADERS, timeout=timeout, stream=stream)
    except requests.exceptions.SSLError:
        return requests.get(url, headers=HEADERS, timeout=timeout, stream=stream, verify=False)


def fetch_remote_sha() -> str:
    r = _get(API_REF, timeout=15)
    if r.status_code == 404:
        raise RuntimeError("저장소를 찾을 수 없습니다. Public 여부/주소/브랜치(main)를 확인하세요.")
    if r.status_code == 403:
        raise RuntimeError("GitHub API 제한 또는 저장소 접근 거부(Private).")
    r.raise_for_status()
    data = r.json()
    sha = (data.get("object") or {}).get("sha") or ""
    if not sha:
        raise RuntimeError("GitHub 응답에 SHA가 없습니다.")
    return sha


def find_remote_exe() -> dict:
    """
    GitHub Releases 최신 자산 우선, 없으면 저장소 안의 exe 경로.
    반환: {id, name, url, size}
    """
    r = _get(API_RELEASES, timeout=15)
    if r.status_code == 200:
        data = r.json()
        assets = data.get("assets") or []
        picked = None
        for a in assets:
            name = a.get("name") or ""
            if name.lower().endswith(".exe") and "dpsk" not in name.lower():
                picked = a
                if name == EXE_NAME:
                    break
        if picked and picked.get("browser_download_url"):
            return {
                "id": f"rel:{data.get('tag_name') or data.get('id')}:{picked.get('id')}",
                "name": picked.get("name") or EXE_NAME,
                "url": picked.get("browser_download_url"),
                "size": picked.get("size") or 0,
            }
    elif r.status_code not in (404,):
        if r.status_code == 403:
            raise RuntimeError("GitHub API 제한 또는 저장소 접근 거부.")
        r.raise_for_status()

    last_err = "GitHub Releases와 저장소에서 exe를 찾지 못했습니다."
    for rel in EXE_REPO_PATHS:
        cr = _get(f"{API_CONTENTS}/{rel}?ref={GITHUB_BRANCH}", timeout=15)
        if cr.status_code == 404:
            continue
        if cr.status_code == 403:
            raise RuntimeError("GitHub API 제한 또는 저장소 접근 거부.")
        cr.raise_for_status()
        info = cr.json()
        dl = info.get("download_url")
        if not dl:
            last_err = f"{rel} 다운로드 URL이 없습니다."
            continue
        return {
            "id": f"file:{info.get('sha')}",
            "name": info.get("name") or EXE_NAME,
            "url": dl,
            "size": info.get("size") or 0,
        }
    raise RuntimeError(last_err + " Releases에 exe를 올리거나 저장소 루트/dist/release 에 두세요.")


def check_update(root: Path, frozen: bool = False) -> dict:
    try:
        if frozen:
            remote_info = find_remote_exe()
            remote = remote_info["id"]
            extra = {"exe_url": remote_info["url"], "exe_name": remote_info["name"], "exe_size": remote_info["size"]}
        else:
            rsrc = _get(f"{API_CONTENTS}/gui_app.py?ref={GITHUB_BRANCH}", timeout=15)
            if rsrc.status_code != 200:
                return {
                    "ok": True,
                    "available": False,
                    "local": read_local_sha(root),
                    "remote": "",
                    "frozen": frozen,
                    "message": "저장소에 프로그램 소스(gui_app.py)가 없습니다.",
                }
            remote = fetch_remote_sha()
            extra = {}
    except Exception as e:
        return {
            "ok": False,
            "available": False,
            "local": read_local_sha(root),
            "remote": "",
            "frozen": frozen,
            "message": f"업데이트 확인 실패: {e}",
        }
    local = read_local_sha(root)
    available = bool(remote) and remote != local
    out = {
        "ok": True,
        "available": available,
        "local": local,
        "remote": remote,
        "frozen": frozen,
        "message": "새 버전이 있습니다." if available else "최신 버전입니다.",
    }
    out.update(extra)
    return out


def _should_skip(rel: Path) -> bool:
    parts = rel.parts
    if any(p in SKIP_DIR_NAMES for p in parts):
        return True
    if rel.name in SKIP_FILE_NAMES:
        return True
    if len(parts) >= 2 and parts[0] == "firmware" and rel.suffix.lower() == ".bl7":
        return True
    if rel.suffix.lower() in {".pyc", ".pyo", ".exe"}:
        return True
    return False


def apply_source_update(root: Path, expected_sha: str = "") -> dict:
    root = Path(root)
    r = _get(ZIP_URL, timeout=60, stream=True)
    r.raise_for_status()
    raw = r.content
    if not raw:
        return {"ok": False, "message": "다운로드한 zip이 비어 있습니다."}
    tmp = root / ".update_tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        zf.extractall(tmp)
    tops = [p for p in tmp.iterdir() if p.is_dir()]
    src_root = tops[0] if len(tops) == 1 else tmp
    copied = 0
    for src in src_root.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(src_root)
        if _should_skip(rel):
            continue
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied += 1
    shutil.rmtree(tmp, ignore_errors=True)
    sha = expected_sha or fetch_remote_sha()
    write_local_sha(root, sha)
    return {"ok": True, "message": f"소스 업데이트 완료 ({copied}개 파일). 프로그램을 다시 실행하세요.", "sha": sha}


def apply_exe_update(root: Path, exe_path: str, info: dict | None = None) -> dict:
    root = Path(root)
    exe_path = Path(exe_path)
    info = info or {}
    url = info.get("exe_url")
    remote_id = info.get("remote") or ""
    if not url:
        found = find_remote_exe()
        url = found["url"]
        remote_id = found["id"]
    r = _get(url, timeout=180, stream=True)
    r.raise_for_status()
    data = r.content
    if not data or len(data) < 1024:
        return {"ok": False, "message": "다운로드한 exe가 비정상입니다."}
    if data[:2] != b"MZ":
        return {"ok": False, "message": "받은 파일이 Windows exe가 아닙니다. GitHub에 exe를 올렸는지 확인하세요."}
    new_path = exe_path.with_suffix(exe_path.suffix + ".new")
    new_path.write_bytes(data)
    bat = root / "_replace_exe.bat"
    exe_name = exe_path.name
    new_name = new_path.name
    bat.write_text(
        "\r\n".join([
            "@echo off",
            "cd /d \"%~dp0\"",
            "timeout /t 3 /nobreak >nul",
            ":RETRY",
            f'del /f /q "{exe_name}"',
            f'if exist "{exe_name}" (',
            "  timeout /t 1 /nobreak >nul",
            "  goto RETRY",
            ")",
            f'move /y "{new_name}" "{exe_name}"',
            "for /f \"tokens=1 delims==\" %%A in ('set _PYI_ 2^>nul') do set \"%%A=\"",
            "set \"_PYI_APPLICATION_HOME_DIR=\"",
            "set \"_PYI_PARENT_PROCESS_LEVEL=\"",
            "set \"PYTHONHOME=\"",
            "set \"PYTHONPATH=\"",
            f'explorer.exe \"%cd%\\{exe_name}\"',
            'del "%~f0"',
            "",
        ]),
        encoding="ascii",
    )
    if remote_id:
        write_local_sha(root, remote_id)
    return {
        "ok": True,
        "message": "새 exe를 받았습니다. 프로그램이 종료된 뒤 자동으로 교체·재실행됩니다.",
        "replace_bat": str(bat),
        "sha": remote_id,
    }


def apply_update(root: Path, expected_sha: str = "", frozen: bool = False, exe_path: str = "", info: dict | None = None) -> dict:
    try:
        if frozen:
            if not exe_path:
                return {"ok": False, "message": "실행 중인 exe 경로를 알 수 없습니다."}
            return apply_exe_update(root, exe_path, info)
        return apply_source_update(root, expected_sha)
    except Exception as e:
        return {"ok": False, "message": f"업데이트 실패: {e}"}
