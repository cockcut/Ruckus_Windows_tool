# -*- coding: utf-8 -*-
"""GitHub Public 저장소에서 커밋 SHA로 업데이트 확인/적용. git 불필요."""

from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path

import requests

GITHUB_OWNER = "cockcut"
GITHUB_REPO = "Ruckus_Windows_tool"
GITHUB_BRANCH = "main"
API_REF = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/ref/heads/{GITHUB_BRANCH}"
ZIP_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/archive/refs/heads/{GITHUB_BRANCH}.zip"
SHA_FILE_NAME = ".update_sha"
SKIP_DIR_NAMES = {".git", "__pycache__", "results", "upload", ".grok"}
SKIP_FILE_NAMES = {SHA_FILE_NAME, "update_token.txt"}
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


def check_update(root: Path) -> dict:
    """
    반환: {ok, available, local, remote, message}
    네트워크 실패는 available=False, ok=False — 프로그램은 그대로 실행.
    """
    try:
        remote = fetch_remote_sha()
    except Exception as e:
        return {
            "ok": False,
            "available": False,
            "local": read_local_sha(root),
            "remote": "",
            "message": f"업데이트 확인 실패: {e}",
        }
    local = read_local_sha(root)
    available = bool(remote) and remote != local
    return {
        "ok": True,
        "available": available,
        "local": local,
        "remote": remote,
        "message": "새 버전이 있습니다." if available else "최신 버전입니다.",
    }


def _should_skip(rel: Path) -> bool:
    parts = rel.parts
    if any(p in SKIP_DIR_NAMES for p in parts):
        return True
    if rel.name in SKIP_FILE_NAMES:
        return True
    if len(parts) >= 2 and parts[0] == "firmware" and rel.suffix.lower() == ".bl7":
        return True
    if rel.suffix.lower() in {".pyc", ".pyo"}:
        return True
    return False


def apply_update(root: Path, expected_sha: str = "") -> dict:
    root = Path(root)
    try:
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
        return {"ok": True, "message": f"업데이트 완료 ({copied}개 파일). 프로그램을 다시 실행하세요.", "sha": sha}
    except Exception as e:
        return {"ok": False, "message": f"업데이트 실패: {e}"}
