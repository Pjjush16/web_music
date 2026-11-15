#!/usr/bin/env python3
"""
扫描 audio/，生成符合播放器静态清单协议的 manifest.json。
若音频同目录存在封面或歌词，写入 cover / lyrics 字段。
"""

import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

try:
    from mutagen import File as MutagenFile
except ImportError:
    print("缺少依赖：pip install mutagen", file=sys.stderr)
    sys.exit(1)

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma"}
AUDIO_ROOT = Path("audio")
OUTPUT = Path("manifest.json")
DEFAULT_CATEGORY = "未分类"
COVER_EXTS = [".jpg", ".jpeg", ".png"]


def read_tags(path):
    info = {"title": "", "artist": "", "album": "", "duration": 0}
    try:
        audio = MutagenFile(str(path), easy=True)
    except Exception:
        return info
    if audio is None:
        return info

    def pick(key):
        val = audio.tags.get(key) if audio.tags else None
        if isinstance(val, list):
            return str(val[0]).strip() if val else ""
        if isinstance(val, str):
            return val.strip()
        return ""

    info["title"] = pick("title")
    info["artist"] = pick("artist")
    info["album"] = pick("album")
    try:
        info["duration"] = round(float(audio.info.length), 2)
    except Exception:
        pass
    return info


def parse_filename(stem):
    if " - " in stem:
        a, t = stem.split(" - ", 1)
        return a.strip(), t.strip()
    return "", stem.strip()


def make_id(rel_path):
    return hashlib.md5(rel_path.encode("utf-8")).hexdigest()[:12]


def iter_audio_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            if p.suffix.lower() in AUDIO_EXTS:
                yield p


def find_sidecar(path, exts):
    for ext in exts:
        p = path.with_suffix(ext)
        if p.exists():
            return p
    return None


def build():
    if not AUDIO_ROOT.exists():
        write_manifest([])
        return

    tracks = []
    for path in iter_audio_files(AUDIO_ROOT):
        rel_path = path.relative_to(AUDIO_ROOT)
        rel_repo = path.as_posix()
        parts = rel_path.parts
        category = parts[0] if len(parts) > 1 else DEFAULT_CATEGORY

        tags = read_tags(path)
        fa, ft = parse_filename(path.stem)

        track = {
            "id": make_id(rel_repo),
            "title": tags["title"] or ft,
            "artist": tags["artist"] or fa,
            "album": tags["album"] or "",
            "category": category,
            "desc": "",
            "duration": int(tags["duration"] or 0),
            "src": quote(rel_repo, safe="/"),
        }

        cover = find_sidecar(path, COVER_EXTS)
        if cover:
            track["cover"] = quote(cover.as_posix(), safe="/")

        lrc = find_sidecar(path, [".lrc"])
        if lrc:
            track["lyrics"] = quote(lrc.as_posix(), safe="/")

        tracks.append(track)

    tracks.sort(key=lambda t: (t["category"], t["title"]))
    write_manifest(tracks)


def write_manifest(tracks):
    content = json.dumps({"tracks": tracks}, ensure_ascii=False, indent=2) + "\n"
    if OUTPUT.exists():
        try:
            if OUTPUT.read_text(encoding="utf-8") == content:
                print(f"{OUTPUT} 无变化，共 {len(tracks)} 首")
                return
        except Exception:
            pass
    OUTPUT.write_text(content, encoding="utf-8")
    print(f"已写入 {OUTPUT}，共 {len(tracks)} 首")


if __name__ == "__main__":
    build()
