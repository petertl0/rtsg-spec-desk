"""MMS / text-message video: auto-sized to fit a hard file-size cap, with a user-chosen first frame (the thumbnail phones show)."""
import re
import subprocess
from fractions import Fraction
from pathlib import Path

from . import media


def _ladder(video_kbps, cfg):
    for step in cfg["size_ladder"]:
        if video_kbps >= step["min_kbps"]:
            return step["width"]
    return cfg["size_ladder"][-1]["width"]


def _dims(src_w, src_h, sar, max_w):
    dw = src_w * sar
    if dw >= src_h:  # landscape/square: cap width
        w = min(max_w, int(dw))
        h = round(w * src_h / dw)
    else:  # portrait: cap the long side the same way
        h = min(max_w, src_h)
        w = round(h * dw / src_h)
    return max(2, w // 2 * 2), max(2, h // 2 * 2)


def encode_mms(src, src_info, preset, out_path, thumb_time=0.0, workdir=None):
    cfg = preset["mms"]
    sv = src_info["video"]
    if not sv:
        raise media.MediaError("MMS needs a video master")
    notes = []
    dur = src_info["duration"]
    thumb_time = max(0.0, min(float(thumb_time or 0), max(0.0, dur - 0.05)))
    fps = cfg["fps"]
    lead = 1 / fps  # the chosen frame is inserted as frame 1
    total = dur + lead
    has_audio = src_info["audio"] is not None
    a_kbps = cfg["audio_kbps"] if has_audio else 0
    budget_kbits = cfg["max_kb"] * 8 * 1.024 * cfg["safety"]  # KB -> kbit
    v_kbps = int(budget_kbits / total - a_kbps - cfg["overhead_kbps"])
    if v_kbps < cfg["min_video_kbps"]:
        raise media.MediaError(f"A {dur:.0f}s spot can't fit in {cfg['max_kb']} KB at watchable quality; cut a shorter version")
    sar = Fraction(sv["sar"].replace(":", "/")) if sv["sar"] not in ("1:1", None) else Fraction(1)
    w, h = _dims(sv["width"], sv["height"], sar, _ladder(v_kbps, cfg))
    interlaced = sv["field_order"] not in (None, "progressive", "unknown")
    pre = ("yadif=mode=0," if interlaced else "") + ("scale=trunc(iw*sar/2)*2:ih,setsar=1," if sar != 1 else "")
    notes.append(f"Auto-sized to {w}x{h} @ {fps} fps, ~{v_kbps} kbps video" + (f" + {a_kbps} kbps mono audio" if has_audio else ""))
    notes.append(f"First frame taken from {thumb_time:.2f}s of the source")

    g = [f"[0:v]{pre}scale={w}:{h}:flags=lanczos,setsar=1,fps={fps},format=yuv420p[main]",
         f"[1:v]{pre}scale={w}:{h}:flags=lanczos,setsar=1,fps={fps},format=yuv420p,trim=end_frame=1,setpts=PTS-STARTPTS[first]",
         "[first][main]concat=n=2:v=1:a=0[v]"]
    if has_audio:
        L = preset.get("loudness")
        ln = f"loudnorm=I={L['target_lufs']}:TP={L['max_tp'] - 0.3}:LRA=11,aresample=44100," if L else ""
        g.append(f"[0:a:0]aresample=44100,aformat=channel_layouts=mono,{ln}adelay={int(lead * 1000)}[a]")
    wd = Path(workdir or Path(out_path).parent)
    passlog = str(wd / "mms2pass")
    common = ["-map", "[v]"] + (["-map", "[a]"] if has_audio else []) + [
        "-c:v", "libx264", "-profile:v", "baseline", "-level:v", "3.0", "-pix_fmt", "yuv420p",
        "-preset", "slow", "-b:v", f"{v_kbps}k", "-maxrate", f"{int(v_kbps * 1.3)}k", "-bufsize", f"{v_kbps * 2}k",
        "-g", str(fps * 2), "-r", str(fps), "-fps_mode", "cfr", "-passlogfile", passlog]
    base = ["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-ss", f"{thumb_time:.3f}", "-i", str(src),
            "-filter_complex", ";".join(g)]
    for attempt in range(3):
        media.run(base + common + ["-pass", "1"] + (["-c:a", "aac", "-b:a", f"{a_kbps}k"] if has_audio else []) + ["-f", "mp4", "/dev/null"])
        cmd = base + common + ["-pass", "2"]
        if has_audio:
            cmd += ["-c:a", "aac", "-b:a", f"{a_kbps}k", "-ac", "1", "-ar", "44100"]
        cmd += ["-movflags", "+faststart", str(out_path)]
        media.run(cmd)
        size_kb = Path(out_path).stat().st_size / 1024
        if size_kb <= cfg["max_kb"]:
            break
        v_kbps = int(v_kbps * cfg["max_kb"] / size_kb * 0.95)
        common[common.index("-b:v") + 1] = f"{v_kbps}k"
        common[common.index("-maxrate") + 1] = f"{int(v_kbps * 1.3)}k"
        common[common.index("-bufsize") + 1] = f"{v_kbps * 2}k"
        notes.append(f"Re-encoded at {v_kbps} kbps to get under {cfg['max_kb']} KB")
    for f in wd.glob("mms2pass*"):
        f.unlink(missing_ok=True)
    return notes, (w, h), thumb_time


def _first_frame_ssim(out_path, src, thumb_time, w, h):
    """Compare frame 1 of the output with the chosen source frame (SSIM, 1.0 = identical)."""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(out_path),
                        "-ss", f"{thumb_time:.3f}", "-i", str(src), "-filter_complex",
                        f"[0:v]trim=end_frame=1,setpts=PTS-STARTPTS,format=yuv420p[a];"
                        f"[1:v]scale={w}:{h}:flags=lanczos,setsar=1,trim=end_frame=1,setpts=PTS-STARTPTS,format=yuv420p[b];"
                        f"[a][b]ssim", "-frames:v", "1", "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"All:([\d.]+)", p.stderr)
    return float(m.group(1)) if m else None


def qc_mms(out_path, preset, src, thumb_time, dims):
    cfg = preset["mms"]
    info = media.probe(out_path)
    v, a = info["video"], info["audio"]
    c = [media.check("Container", "MP4", "MP4" if "mp4" in (info["format"] or "") else info["format"], "mp4" in (info["format"] or "")),
         media.check("Video codec", "H.264 Baseline", f"{v['codec']} {v['profile']}",
                     v["codec"] == "h264" and "baseline" in (v["profile"] or "").lower()),
         media.check("Resolution", f"≤ {cfg['size_ladder'][0]['width']}px, even", f"{v['width']}x{v['height']}",
                     max(v["width"], v["height"]) <= cfg["size_ladder"][0]["width"] and v["width"] % 2 == 0 and v["height"] % 2 == 0),
         media.check("Frame rate", f"{cfg['fps']} fps CFR", f"{v['fps']:.2f}", abs(v["fps"] - cfg["fps"]) < 0.05)]
    if a:
        c.append(media.check("Audio", "AAC mono", f"{a['codec']} {a['channels']}ch", a["codec"] == "aac" and a["channels"] == 1))
    kb = info["size_bytes"] / 1024
    c.append(media.check("File size", f"≤ {cfg['max_kb']} KB", f"{kb:.0f} KB", kb <= cfg["max_kb"]))
    s = _first_frame_ssim(out_path, src, thumb_time, *dims)
    c.append(media.check("First frame", f"matches source @ {thumb_time:.2f}s", f"SSIM {s:.3f}" if s is not None else "n/a",
                         s is not None and s >= 0.85))
    if a and preset.get("loudness"):
        loud = media.measure_loudness(out_path)
        media.qc_loudness(preset, loud, c)
    else:
        loud = None
    return c, info, loud
