#!/usr/bin/env python3
"""
扫描 audio/，按 Kodi 音乐刮削思路补全元数据、封面和歌词。

识别三层兜底：
  1. 标签里的 MusicBrainz Recording ID 精确查询
  2. MusicBrainz 用 artist + title 文本搜索
  3. AcoustID 音频指纹识别（仅在 1、2 都失败且配置了 API Key 时触发），
     识别成功后再用 artist + title 搜一次 MusicBrainz

封面：Cover Art Archive（基于 release MBID）→ 内嵌封面 → iTunes
歌词：LRCLIB

产物写在音频同目录：
  <stem>.jpg   封面
  <stem>.lrc   歌词
补全的 title / artist / album 写回标签（只补空缺，不覆盖）。
"""

import json
import os
import re
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

MB_BASE = "https://musicbrainz.org/ws/2"
CAA_BASE = "https://coverartarchive.org"
LRCLIB_BASE = "https://lrclib.net/api"
ITUNES_BASE = "https://itunes.apple.com"

REQ_TIMEOUT = 20
USER_AGENT = "YAONET-AudioScraper/1.0 (https://github.com/yaonet/audio)"
MB_SLEEP = 1.1
CAA_SLEEP = 0.5
ITUNES_SLEEP = 0.6
LRCLIB_SLEEP = 0.3
ACOUSTID_SLEEP = 0.4
MB_MIN_SCORE = 85
ACOUSTID_MIN_SCORE = 0.5


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


# ---------- 标签读取 ----------

def read_tags(path):
    info = {
        "title": "", "artist": "", "album": "", "duration": 0,
        "mb_recording": "", "mb_release": "", "mb_artist": ""
    }

    try:
        easy = MutagenFile(str(path), easy=True)
    except Exception:
        easy = None
    if easy is not None:
        def pick(key):
            val = easy.tags.get(key) if easy.tags else None
            if isinstance(val, list):
                return str(val[0]).strip() if val else ""
            if isinstance(val, str):
                return val.strip()
            return ""
        info["title"] = pick("title")
        info["artist"] = pick("artist")
        info["album"] = pick("album")
        try:
            info["duration"] = round(float(easy.info.length), 2)
        except Exception:
            pass

    try:
        raw = MutagenFile(str(path))
    except Exception:
        raw = None
    if raw is None or raw.tags is None:
        return info

    # ID3
    try:
        for key, value in raw.tags.items():
            if not isinstance(key, str):
                continue
            kl = key.lower()
            if kl.startswith("txxx:musicbrainz track id"):
                info["mb_recording"] = str(value).strip()
            elif kl.startswith("txxx:musicbrainz album id"):
                info["mb_release"] = str(value).strip()
            elif kl.startswith("txxx:musicbrainz artist id"):
                info["mb_artist"] = str(value).strip()
            elif kl.startswith("ufid:http://musicbrainz.org"):
                info["mb_recording"] = info["mb_recording"] or str(value).strip()
    except Exception:
        pass

    # Vorbis Comment
    try:
        getter = getattr(raw.tags, "get", None)
        if callable(getter):
            for key, attr in (
                ("musicbrainz_trackid", "mb_recording"),
                ("musicbrainz_albumid", "mb_release"),
                ("musicbrainz_artistid", "mb_artist"),
            ):
                val = raw.tags.get(key)
                if isinstance(val, list):
                    val = val[0] if val else ""
                if val:
                    info[attr] = str(val).strip()
    except Exception:
        pass

    # MP4
    try:
        for key, attr in (
            ("----:com.apple.iTunes:MusicBrainz Track Id", "mb_recording"),
            ("----:com.apple.iTunes:MusicBrainz Album Id", "mb_release"),
            ("----:com.apple.iTunes:MusicBrainz Artist Id", "mb_artist"),
        ):
            val = raw.tags.get(key)
            if isinstance(val, list):
                val = val[0] if val else b""
            if isinstance(val, bytes):
                val = val.decode("utf-8", "ignore")
            if val:
                info[attr] = str(val).strip()
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


# ---------- MusicBrainz ----------

def mb_get(path, params):
    params = dict(params)
    params["fmt"] = "json"
    try:
        r = requests.get(
            MB_BASE + path,
            params=params,
            timeout=REQ_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"  [warn] MusicBrainz 请求失败: {e}")
        return None
    finally:
        time.sleep(MB_SLEEP)


def mb_lookup_recording(mbid):
    return mb_get(f"/recording/{mbid}", {"inc": "artist-credits+releases"})


def mb_search_recording(artist, title):
    if not title:
        return None
    if artist:
        query = f'artist:"{artist}" AND recording:"{title}"'
    else:
        query = f'recording:"{title}"'
    data = mb_get("/recording", {"query": query, "limit": 5})
    if not data:
        return None
    items = data.get("recordings") or []
    best = None
    best_score = 0
    for item in items:
        try:
            score = int(item.get("score", 0))
        except Exception:
            score = 0
        if score > best_score:
            best_score = score
            best = item
    if best and best_score >= MB_MIN_SCORE:
        return best
    return None


def pick_release(recording):
    if not recording:
        return ""
    for rel in recording.get("releases") or []:
        rid = rel.get("id") or ""
        if rid:
            return rid
    return ""


def extract_recording_info(recording):
    if not recording:
        return {}
    title = recording.get("title") or ""
    artist = ""
    credits = recording.get("artist-credit") or []
    if credits:
        parts = []
        for c in credits:
            if isinstance(c, dict):
                nm = c.get("name") or (c.get("artist") or {}).get("name") or ""
                if nm:
                    parts.append(nm)
        artist = " / ".join(parts)
    album = ""
    releases = recording.get("releases") or []
    if releases:
        album = releases[0].get("title") or ""
    return {
        "title": title,
        "artist": artist,
        "album": album,
        "mb_recording": recording.get("id") or "",
        "mb_release": pick_release(recording),
    }


# ---------- AcoustID 指纹兜底 ----------

def identify_acoustid(path, api_key):
    """
    使用 AcoustID 音频指纹识别，仅作为最后一层兜底。
    返回 {title, artist, recording_id, score}，失败返回空 dict。
    """
    if not api_key:
        return {}
    try:
        import acoustid
    except ImportError:
        log("  [warn] pyacoustid 未安装，跳过指纹识别")
        return {}

    best = None
    best_score = 0.0
    try:
        for item in acoustid.match(api_key, str(path), meta="recordings"):
            try:
                score = float(item[0])
                recording_id = item[1] if len(item) > 1 else ""
                title = item[2] if len(item) > 2 else ""
                artist = item[3] if len(item) > 3 else ""
            except (IndexError, TypeError, ValueError):
                continue
            if score > best_score:
                best_score = score
                best = {
                    "title": (title or "").strip(),
                    "artist": (artist or "").strip(),
                    "recording_id": recording_id or "",
                    "score": score,
                }
    except Exception as e:
        log(f"  [warn] AcoustID 失败: {e}")
        return {}
    finally:
        time.sleep(ACOUSTID_SLEEP)

    if best and best_score >= ACOUSTID_MIN_SCORE:
        log(
            f"  AcoustID 匹配: score={best_score:.2f} "
            f"title={best['title']!r} artist={best['artist']!r}"
        )
        return best
    return {}


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


def fetch_cover_caa(release_mbid):
    if not release_mbid:
        return None
    url = f"{CAA_BASE}/release/{release_mbid}/front-500"
    try:
        r = requests.get(url, timeout=REQ_TIMEOUT, headers={"User-Agent": USER_AGENT})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        if r.content:
            return r.content, "image/jpeg"
    except Exception as e:
        log(f"  [warn] Cover Art Archive 失败: {e}")
    finally:
        time.sleep(CAA_SLEEP)
    return None


def fetch_cover_itunes(artist, title):
    if not title:
        return None
    term = f"{artist} {title}".strip()
    try:
        r = requests.get(
            f"{ITUNES_BASE}/search",
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
    finally:
        time.sleep(ITUNES_SLEEP)


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
            f"{LRCLIB_BASE}/get",
            params=params,
            timeout=REQ_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        if r.status_code == 200:
            d = r.json()
            text = d.get("syncedLyrics") or d.get("plainLyrics")
            if text:
                return text

        q = f"{artist} {title}".strip()
        r = requests.get(
            f"{LRCLIB_BASE}/search",
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
    finally:
        time.sleep(LRCLIB_SLEEP)
    return None


# ---------- 写回标签 ----------

def write_tags(path, meta):
    try:
        audio = MutagenFile(str(path), easy=True)
        if audio is None:
            return
        if audio.tags is None:
            audio.add_tags()
        changed = False
        for key in ("title", "artist", "album"):
            val = (meta.get(key) or "").strip()
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
        log(f"  [warn] 写回标签失败: {e}")


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
    if api_key:
        log("已启用 AcoustID 指纹兜底")
    else:
        log("未配置 ACOUSTID_API_KEY：跳过指纹兜底，仅使用标签 + MusicBrainz")

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

        tags = read_tags(path)
        path_meta = parse_from_path(path)

        title = tags["title"] or path_meta["title"]
        artist = tags["artist"] or path_meta["artist"]
        album = tags["album"] or path_meta["album"]

        # 第一层：MBID 精确查询
        recording = None
        if tags["mb_recording"]:
            log(f"  使用 MusicBrainz Recording ID: {tags['mb_recording']}")
            recording = mb_lookup_recording(tags["mb_recording"])

        # 第二层：MusicBrainz 文本搜索
        if not recording and title:
            log(f"  MusicBrainz 搜索: artist={artist!r} title={title!r}")
            recording = mb_search_recording(artist, title)

        # 第三层：AcoustID 指纹兜底
        acoustid_used = False
        if not recording and api_key:
            log("  MusicBrainz 未匹配，尝试 AcoustID 指纹识别")
            fp = identify_acoustid(path, api_key)
            if fp:
                acoustid_used = True
                title = fp.get("title") or title
                artist = fp.get("artist") or artist
                log("  使用 AcoustID 结果重新搜索 MusicBrainz")
                recording = mb_search_recording(artist, title)

        mb_info = extract_recording_info(recording) if recording else {}
        if mb_info:
            title = mb_info.get("title") or title
            artist = mb_info.get("artist") or artist
            album = mb_info.get("album") or album
            log(f"  MusicBrainz 匹配: title={title!r} artist={artist!r} album={album!r}")
        elif acoustid_used:
            log(f"  AcoustID 结果（未经 MusicBrainz 确认）: title={title!r} artist={artist!r}")
        else:
            log("  未匹配到 MusicBrainz 记录，保留标签/路径信息")

        if not title:
            log("  [skip] 无法识别标题")
            entry["status"] = "no_title"
            continue

        meta = {"title": title, "artist": artist, "album": album, "duration": tags["duration"]}

        # 封面
        existing = find_existing_cover(path)
        if existing:
            log(f"  封面已存在: {existing.name}")
            entry["cover"] = existing.name
        else:
            cover = extract_embedded_cover(path)
            source = "embedded"
            if not cover and mb_info.get("mb_release"):
                cover = fetch_cover_caa(mb_info["mb_release"])
                source = "caa"
            if not cover:
                cover = fetch_cover_itunes(artist, title)
                source = "itunes"
            if cover:
                raw, mime = cover
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
            lrc = fetch_lyrics_lrclib(artist, title, album, tags["duration"])
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
        entry["mb_recording"] = mb_info.get("mb_recording", "")
        entry["mb_release"] = mb_info.get("mb_release", "")
        entry["acoustid_used"] = acoustid_used

    save_state(state)
    log("刮削完成")


if __name__ == "__main__":
    main()
