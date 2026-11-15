#!/usr/bin/env python3
"""
扫描 audio/，补全元数据、封面和歌词。

优先级：
  元数据  tag > AcoustID 指纹（需 key）> 目录/文件名解析
  封面    已有 .jpg/.png > 内嵌封面 > iTunes Search API
  歌词    已有 .lrc > LRCLIB

产物写在音频同目录：
  <stem>.jpg   封面
  <stem>.lrc   歌词
补全的 title/artist/album 写回 tag（只补空缺，不覆盖）。
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from mutagen import File as MutagenFile
from mutagen.flac import Picture
from mutagen.id3 import APIC

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma"}
AUDIO_ROOT = Path("audio")
STATE_FILE = Path("scrape_state.json")
COVER_EXTS = [".jpg", ".jpeg", ".png"]

REQ_TIMEOUT = 15
USER_AGENT = "YAONET-Audio-Scraper/1.0"
ITUNES_SLEEP = 0.6
LRCLIB_SLEEP = 0.3
ACOUSTID_SLEEP = 0.4


def log(msg):
    print(msg, flush=True)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state):
    content = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if STATE_FILE.exists():
        try:
            if STATE_FILE.read_text(encoding="utf-8") == content:
                return
        except Exception:
            pass
    STATE_FILE.write_text(content, encoding="utf-8")


# ---------- 元数据 ----------

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


def parse_from_path(path):
    rel = path.relative_to(AUDIO_ROOT)
    parts = list(rel.parts)
    stem = Path(parts[-1]).stem
    dirs = parts[:-1]

    artist = ""
    album = ""
    if len(dirs) >= 2:
        artist, album = dirs[-2].strip(), dirs[-1].strip()
    elif len(dirs) == 1:
        album = dirs[0].strip()

    title = stem.strip()
    m = re.match(r"^(\d{1,3})\s*[-._ ]\s*(.+)$", stem)
    if m:
        rest = m.group(2).strip()
        if " - " in rest:
            a, t = rest.split(" - ", 1)
            artist = artist or a.strip()
            title = t.strip()
        else:
            title = rest
    elif " - " in stem:
        a, t = stem.split(" - ", 1)
        artist = artist or a.strip()
        title = t.strip()

    return {"title": title, "artist": artist, "album": album}


def identify_acoustid(path, api_key):
    if not api_key:
        return {}
    try:
        import acoustid
    except ImportError:
        log("  [warn] pyacoustid 未安装，跳过指纹识别")
        return {}
    try:
        for score, _rid, title, artist in acoustid.match(api_key, str(path)):
            if score >= 0.6:
                return {"title": title or "", "artist": artist or ""}
    except Exception as e:
        log(f"  [warn] AcoustID 失败: {e}")
    return {}


def merge_metadata(tag, path_meta, fp):
    return {
        "title": tag["title"] or fp.get("title") or path_meta["title"],
        "artist": tag["artist"] or fp.get("artist") or path_meta["artist"],
        "album": tag["album"] or path_meta["album"],
        "duration": tag["duration"],
    }


def write_tags(path, meta):
    try:
        audio = MutagenFile(str(path), easy=True)
        if audio is None:
            return
        if audio.tags is None:
            audio.add_tags()
        changed = False
        for key in ("title", "artist", "album"):
            val = meta.get(key, "").strip()
            if not val:
                continue
            cur = audio.tags.get(key)
            if isinstance(cur, list):
                cur_val = cur[0] if cur else ""
            elif isinstance(cur, str):
                cur_val = cur
            else:
                cur_val = ""
            if not cur_val:
                audio.tags[key] = [val]
                changed = True
        if changed:
            audio.save()
    except Exception as e:
        log(f"  [warn] 写回 tag 失败: {e}")


# ---------- 封面 ----------

def find_existing_cover(path):
    for ext in COVER_EXTS:
        p = path.with_suffix(ext)
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def extract_embedded_cover(path):
    try:
        audio = MutagenFile(str(path))
    except Exception:
        return None
    if audio is None or audio.tags is None:
        return None

    try:
        for tag in audio.tags.values():
            if isinstance(tag, APIC) and tag.data:
                return tag.data, tag.mime or "image/jpeg"
    except Exception:
        pass

    try:
        pics = getattr(audio, "pictures", None)
        if pics:
            pic = pics[0]
            if isinstance(pic, Picture) and pic.data:
                return pic.data, pic.mime or "image/jpeg"
    except Exception:
        pass

    try:
        covers = audio.tags.get("covr")
        if covers:
            return bytes(covers[0]), "image/jpeg"
    except Exception:
        pass

    return None


def fetch_cover_itunes(artist, title):
    if not title:
        return None
    term = f"{artist} {title}".strip()
    try:
        r = requests.get(
            "https://itunes.apple.com/search",
            params={"term": term, "media": "music", "entity": "song", "limit": 1},
            timeout=REQ_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        results = r.json().get("results") or []
        if not results:
            return None
        art = results[0].get("artworkUrl100", "")
        if not art:
            return None
        hd = art.replace("100x100bb", "600x600bb")
        r = requests.get(hd, timeout=REQ_TIMEOUT, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        return r.content, "image/jpeg"
    except Exception as e:
        log(f"  [warn] iTunes 封面失败: {e}")
        return None


def save_cover(path, data, mime):
    ext = ".png" if "png" in (mime or "").lower() else ".jpg"
    out = path.with_suffix(ext)
    out.write_bytes(data)
    return out


# ---------- 歌词 ----------

def find_existing_lyrics(path):
    p = path.with_suffix(".lrc")
    if p.exists() and p.stat().st_size > 0:
        return p
    return None


def fetch_lyrics_lrclib(artist, title, album, duration):
    if not title:
        return None
    params = {"track_name": title}
    if artist:
        params["artist_name"] = artist
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = int(duration)

    try:
        r = requests.get(
            "https://lrclib.net/api/get",
            params=params,
            timeout=REQ_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        if r.status_code == 200:
            d = r.json()
            return d.get("syncedLyrics") or d.get("plainLyrics")

        q = f"{artist} {title}".strip()
        r = requests.get(
            "https://lrclib.net/api/search",
            params={"q": q},
            timeout=REQ_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        if r.status_code == 200:
            arr = r.json() or []
            if arr:
                return arr[0].get("syncedLyrics") or arr[0].get("plainLyrics")
    except Exception as e:
        log(f"  [warn] LRCLIB 失败: {e}")
    return None


# ---------- 主流程 ----------

def iter_audio_files():
    if not AUDIO_ROOT.exists():
        return
    for dirpath, dirnames, filenames in os.walk(AUDIO_ROOT):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            if p.suffix.lower() in AUDIO_EXTS:
                yield p


def main():
    api_key = os.environ.get("ACOUSTID_API_KEY", "").strip()
    if not api_key:
        log("未配置 ACOUSTID_API_KEY：跳过指纹识别，改用 tag / 路径 / iTunes")

    state = load_state()
    files = list(iter_audio_files())
    log(f"发现 {len(files)} 个音频文件")
    if not files:
        save_state(state)
        return

    for idx, path in enumerate(files, 1):
        log(f"[{idx}/{len(files)}] {path}")
        key = path.as_posix()
        entry = state.setdefault(key, {})

        tag = read_tags(path)
        path_meta = parse_from_path(path)

        fp = {}
        if api_key and (not tag["title"] or not tag["artist"]):
            fp = identify_acoustid(path, api_key)
            if fp:
                time.sleep(ACOUSTID_SLEEP)

        meta = merge_metadata(tag, path_meta, fp)
        if not meta["title"]:
            log("  [skip] 无法识别标题")
            entry["status"] = "no_title"
            continue

        log(f"  title={meta['title']!r} artist={meta['artist']!r} album={meta['album']!r}")

        # 封面
        existing = find_existing_cover(path)
        if existing:
            log(f"  封面已存在: {existing.name}")
            entry["cover"] = existing.name
        else:
            data = extract_embedded_cover(path)
            source = "embedded"
            if not data:
                data = fetch_cover_itunes(meta["artist"], meta["title"])
                source = "itunes"
                time.sleep(ITUNES_SLEEP)
            if data:
                raw, mime = data
                saved = save_cover(path, raw, mime)
                log(f"  封面已保存 ({source}): {saved.name}")
                entry["cover"] = saved.name
            else:
                log("  [warn] 未找到封面")
                entry["cover"] = None

        # 歌词
        existing = find_existing_lyrics(path)
        if existing:
            log(f"  歌词已存在: {existing.name}")
            entry["lyrics"] = existing.name
        else:
            lrc = fetch_lyrics_lrclib(meta["artist"], meta["title"], meta["album"], meta["duration"])
            time.sleep(LRCLIB_SLEEP)
            if lrc:
                out = path.with_suffix(".lrc")
                out.write_text(lrc, encoding="utf-8")
                log(f"  歌词已保存: {out.name}")
                entry["lyrics"] = out.name
            else:
                log("  [warn] 未找到歌词")
                entry["lyrics"] = None

        write_tags(path, meta)
        entry["status"] = "ok"
        entry["title"] = meta["title"]
        entry["artist"] = meta["artist"]
        entry["album"] = meta["album"]

    save_state(state)
    log("刮削完成")


if __name__ == "__main__":
    main()
