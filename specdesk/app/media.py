"""ffmpeg-based transcoding and post-encode QC for video and audio presets."""
import json
import os
import math
import re
import subprocess
from fractions import Fraction
from pathlib import Path

FPS_MAP = {23.976: "24000/1001", 29.97: "30000/1001", 59.94: "60000/1001"}


class MediaError(Exception):
    pass


def run(cmd, timeout=None):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-15:])
        raise MediaError(f"ffmpeg failed:\n{tail}")
    return p


# ---------------------------------------------------------------- probing
def probe(path):
    p = run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    data = json.loads(p.stdout)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or (v or a or {}).get("duration") or 0)
    info = {
        "format": fmt.get("format_name"),
        "duration": duration,
        "size_bytes": int(fmt.get("size") or Path(path).stat().st_size),
        "bitrate": int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        "video": None,
        "audio": None,
    }
    if v:
        r = _frac(v.get("r_frame_rate"))
        avg = _frac(v.get("avg_frame_rate"))
        sar = v.get("sample_aspect_ratio") or "1:1"
        if sar in ("0:1", "N/A"):
            sar = "1:1"
        vbr = int(v["bit_rate"]) if v.get("bit_rate") else None
        info["video"] = {
            "codec": v.get("codec_name"),
            "profile": v.get("profile"),
            "width": v.get("width"),
            "height": v.get("height"),
            "sar": sar,
            "pix_fmt": v.get("pix_fmt"),
            "field_order": v.get("field_order", "progressive"),
            "fps": float(r) if r else None,
            "avg_fps": float(avg) if avg else None,
            "fps_str": v.get("r_frame_rate"),
            "bitrate": vbr,
            "level": v.get("level"),
        }
    if a:
        info["audio"] = {
            "codec": a.get("codec_name"),
            "sample_rate": int(a.get("sample_rate") or 0),
            "channels": a.get("channels"),
            "bitrate": int(a["bit_rate"]) if a.get("bit_rate") else None,
            "bits": a.get("bits_per_sample") or a.get("bits_per_raw_sample"),
        }
    return info


def _frac(s):
    try:
        f = Fraction(s)
        return f if f > 0 else None
    except Exception:
        return None


def measure_loudness(path):
    """Independent EBU R128 measurement (integrated LUFS, true peak dBTP, LRA)."""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
                        "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"],
                       capture_output=True, text=True)
    err = p.stderr
    summ = err[err.rfind("Summary:"):] if "Summary:" in err else err
    def grab(pat):
        m = re.search(pat, summ, re.S)
        if not m:
            return None
        v = float(m.group(1))
        return v if math.isfinite(v) else None
    i = grab(r"Integrated loudness:\s+I:\s+(-?[\d.]+|-inf)")
    lra = grab(r"LRA:\s+(-?[\d.]+)")
    tp = grab(r"True peak:\s+Peak:\s+(-?[\d.]+|-inf)")
    return {"integrated": i, "true_peak": tp, "lra": lra}


def loudnorm_pass1(path, target, tp, lra=11):
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
                        "-af", f"loudnorm=I={target}:TP={tp}:LRA={lra}:print_format=json",
                        "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", p.stderr, re.S)
    if not m:
        return None
    d = json.loads(m.group(0))
    try:
        if not math.isfinite(float(d["input_i"])):
            return None
    except Exception:
        return None
    return d


def loudnorm_filter(path, loud, sample_rate):
    """Two-pass loudness normalisation. Returns an audio filter string or None if source is silent.
    Aims slightly inside the true-peak ceiling so the independent re-measure passes."""
    target, tp = loud["target_lufs"], loud["max_tp"] - 0.3
    m = loudnorm_pass1(path, target, tp)
    if not m:
        return None
    return (f"loudnorm=I={target}:TP={tp}:LRA=11:"
            f"measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
            f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:"
            f"offset={m['target_offset']}:linear=true,aresample={sample_rate}")


# ---------------------------------------------------------------- planning helpers
def pick_fps(src_fps, preset):
    if "fps_allowed" in preset:
        allowed = preset["fps_allowed"]
        if src_fps:
            best = min(allowed, key=lambda f: abs(f - src_fps))
            return best
        return allowed[-1]
    lo, hi = preset.get("fps_range", [23, 60])
    if src_fps and lo - 0.5 <= src_fps <= hi + 0.5:
        # snap to a standard rate close to source
        std = [23.976, 24, 25, 29.97, 30, 50, 59.94, 60]
        return min(std, key=lambda f: abs(f - src_fps))
    return preset.get("fps_default", 30)


def fps_str(f):
    return FPS_MAP.get(f, str(int(f)) if float(f).is_integer() else str(f))


def conform_duration(src_dur, preset, allow_conform=True):
    """Return (target_duration or None, note). Snaps to nearest allowed length if within 1s."""
    durs = preset.get("durations")
    if not durs:
        return None, None
    nearest = min(durs, key=lambda d: abs(d - src_dur))
    diff = src_dur - nearest
    tol = preset.get("duration_tolerance", 0.5)
    if abs(diff) <= 0.02:
        return nearest, None
    if allow_conform and abs(diff) <= 1.0:
        how = "trimmed" if diff > 0 else ("padded with black/silence" if "width" in preset else "padded with silence")
        return nearest, f"Source {src_dur:.2f}s {how} to exact :{nearest}"
    if abs(diff) <= tol:
        return None, None
    return None, f"Source is {src_dur:.2f}s; nearest allowed length is :{nearest}"


def video_filter(w, h, fit, interlaced, fps, anamorphic=False):
    pre = ("yadif=mode=0," if interlaced else "") + ("scale=trunc(iw*sar/2)*2:ih,setsar=1," if anamorphic else "")
    if fit == "crop":
        core = f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}"
    elif fit == "blur":
        return (f"[0:v]{pre}split=2[bg][fg];"
                f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=40:5,eq=brightness=-0.08[bgb];"
                f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos[fgs];"
                f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps}[vout]")
    else:
        core = (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black")
    return f"[0:v]{pre}{core},setsar=1,fps={fps}[vout]"


# ---------------------------------------------------------------- encoding
def encode_video(src, src_info, preset, out_path, fit="pad", conform=True, log=None):
    notes = []
    w, h = preset["width"], preset["height"]
    sv = src_info["video"]
    if not sv:
        raise MediaError("Source has no video stream")
    fps = pick_fps(sv["fps"], preset)
    if sv["fps"] and abs(fps - sv["fps"]) > 0.01:
        notes.append(f"Frame rate converted {sv['fps']:.3f} → {fps}")
    if sv["avg_fps"] and sv["fps"] and abs(sv["avg_fps"] - sv["fps"]) > 0.05:
        notes.append("Source was variable frame rate; conformed to constant")
    interlaced = sv["field_order"] not in (None, "progressive", "unknown")
    if interlaced:
        notes.append("Source was interlaced; deinterlaced")
    sar = Fraction(sv["sar"].replace(":", "/")) if sv["sar"] not in ("1:1", None) else Fraction(1)
    anamorphic = sar != 1
    src_ar, dst_ar = sv["width"] * sar / sv["height"], w / h
    if abs(src_ar - dst_ar) > 0.01:
        notes.append({"pad": "Letterboxed/pillarboxed", "crop": "Center-cropped",
                      "blur": "Blur-filled"}[fit] + f" {sv['width']}x{sv['height']} → {w}x{h}")
    fstr = fps_str(fps)
    target_dur, dnote = conform_duration(src_info["duration"], preset, conform)
    if dnote:
        notes.append(dnote)

    vf = video_filter(w, h, fit, interlaced, fstr, anamorphic)
    if target_dur and target_dur > src_info["duration"]:
        vf = vf.replace("[vout]", f",tpad=stop_mode=add:stop_duration={target_dur - src_info['duration'] + 0.1:.3f}[vout]")

    has_audio = src_info["audio"] is not None
    cmd = ["ffmpeg", "-hide_banner", "-y", "-i", str(src)]
    if not has_audio:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={preset['sample_rate']}:cl=stereo"]
        notes.append("Source had no audio; silent stereo track added")
    cmd += ["-filter_complex", vf]

    af_parts = []
    loud = preset.get("loudness")
    if has_audio and loud:
        lf = loudnorm_filter(src, loud, preset["sample_rate"])
        if lf:
            af_parts.append(lf)
        else:
            notes.append("Source audio is silent; loudness not normalised")
    if target_dur and target_dur > src_info["duration"]:
        af_parts.append("apad")
    cmd += ["-map", "[vout]", "-map", "0:a:0" if has_audio else "1:a:0"]
    if af_parts:
        cmd += ["-af", ",".join(af_parts)]
    if not has_audio or target_dur:
        cmd += ["-t", f"{target_dur or src_info['duration']:.3f}"]

    fnum = float(Fraction(fstr))
    if preset["vcodec"] == "prores":
        cmd += ["-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0", "-pix_fmt", "yuv422p10le"]
    else:
        gop = max(1, round(fnum * preset.get("gop_seconds", 2)))
        mbps = preset["bitrate_mbps"]
        if fnum > 31 and preset.get("bitrate_mbps_high_fps"):
            mbps = preset["bitrate_mbps_high_fps"]
        level = "4.2" if fnum > 31 else "4.1"
        if w * h > 1920 * 1080:
            level = "5.1"
        cmd += ["-c:v", "libx264", "-preset", os.environ.get("X264_PRESET", "medium"), "-profile:v", "high", "-level:v", level,
                "-pix_fmt", "yuv420p", "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
                "-flags", "+cgop", "-bf", str(preset.get("bframes", 3))]
        if preset.get("rate_control") == "cbr":
            b = f"{mbps}M"
            cmd += ["-b:v", b, "-minrate", b, "-maxrate", b, "-bufsize", f"{mbps * 2}M",
                    "-x264-params", "nal-hrd=cbr:force-cfr=1"]
        else:
            cmd += ["-b:v", f"{mbps}M", "-maxrate", f"{mbps * 1.5:g}M", "-bufsize", f"{mbps * 2}M"]
        cmd += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    cmd += ["-fps_mode", "cfr", "-r", fstr]

    if preset["acodec"] == "aac":
        cmd += ["-c:a", "aac", "-b:a", f"{preset['audio_kbps']}k"]
    else:
        cmd += ["-c:a", preset["acodec"]]
    cmd += ["-ar", str(preset["sample_rate"]), "-ac", str(preset["channels"])]
    if preset["container"] == "mp4":
        cmd += ["-movflags", "+faststart"]
    cmd += [str(out_path)]
    if log:
        log(" ".join(cmd))
    run(cmd)
    return notes


def encode_audio(src, src_info, preset, fmt, out_path, conform=True):
    notes = []
    if not src_info["audio"]:
        raise MediaError("Source has no audio stream")
    target_dur, dnote = conform_duration(src_info["duration"], preset, conform)
    if dnote:
        notes.append(dnote)
    af = []
    lf = loudnorm_filter(src, preset["loudness"], preset["sample_rate"])
    if lf:
        af.append(lf)
    else:
        notes.append("Source audio is silent; loudness not normalised")
    if target_dur and target_dur > src_info["duration"]:
        af.append("apad")
    cmd = ["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-vn", "-map", "0:a:0"]
    if af:
        cmd += ["-af", ",".join(af)]
    if target_dur:
        cmd += ["-t", f"{target_dur:.3f}"]
    cmd += ["-ar", str(preset["sample_rate"]), "-ac", str(preset["channels"])]
    if fmt == "mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", f"{preset['mp3_kbps']}k", "-write_xing", "1", "-id3v2_version", "3"]
    else:
        cmd += ["-c:a", "pcm_s24le" if preset.get("wav_bits") == 24 else "pcm_s16le"]
    cmd += [str(out_path)]
    run(cmd)
    return notes


# ---------------------------------------------------------------- QC
def check(name, expected, measured, ok, severity="fail"):
    return {"check": name, "expected": expected, "measured": measured,
            "result": "pass" if ok else severity}


def qc_loudness(preset, loud_meas, checks):
    loud = preset.get("loudness")
    if not loud:
        return
    sev = "fail" if loud.get("enforce") else "warn"
    i, tp = loud_meas["integrated"], loud_meas["true_peak"]
    t, tol = loud["target_lufs"], loud["tolerance"]
    checks.append(check("Integrated loudness", f"{t} ±{tol} LUFS",
                        f"{i:.1f} LUFS" if i is not None else "silent",
                        i is not None and abs(i - t) <= tol, sev))
    checks.append(check("True peak", f"≤ {loud['max_tp']} dBTP",
                        f"{tp:.1f} dBTP" if tp is not None else "n/a",
                        tp is not None and tp <= loud["max_tp"] + 0.05, sev))
    if loud_meas.get("lra") is not None:
        checks.append({"check": "Loudness range", "expected": "info", "measured": f"{loud_meas['lra']:.1f} LU", "result": "info"})


def qc_duration(preset, dur, checks):
    if preset.get("durations"):
        tol = preset.get("duration_tolerance", 0.5)
        nearest = min(preset["durations"], key=lambda d: abs(d - dur))
        checks.append(check("Duration", "/".join(f":{d}" for d in preset["durations"]) + f" (±{tol}s)",
                            f"{dur:.3f}s", abs(dur - nearest) <= tol))
    elif preset.get("max_duration"):
        checks.append(check("Duration", f"≤ {preset['max_duration']}s", f"{dur:.2f}s",
                            dur <= preset["max_duration"]))


def qc_video(path, preset):
    info = probe(path)
    v, a = info["video"], info["audio"]
    c = []
    cont_ok = preset["container"] in (info["format"] or "") and Path(path).suffix.lower() == "." + preset["container"]
    c.append(check("Container", preset["container"].upper(),
                   Path(path).suffix[1:].upper() if cont_ok else info["format"], cont_ok))
    exp_codec = "prores" if preset["vcodec"] == "prores" else "h264"
    c.append(check("Video codec", exp_codec, v["codec"], v["codec"] == exp_codec))
    if preset["vcodec"] == "prores":
        c.append(check("ProRes profile", "422 HQ", v["profile"], "HQ" in (v["profile"] or "")))
    else:
        c.append(check("H.264 profile", "High", v["profile"], (v["profile"] or "").lower() == "high"))
        c.append(check("Chroma", "4:2:0 8-bit", v["pix_fmt"], v["pix_fmt"] == "yuv420p"))
    c.append(check("Resolution", f"{preset['width']}x{preset['height']}", f"{v['width']}x{v['height']}",
                   v["width"] == preset["width"] and v["height"] == preset["height"]))
    c.append(check("Pixel aspect", "1:1 (square)", v["sar"], v["sar"] == "1:1"))
    c.append(check("Scan", "progressive", v["field_order"] or "progressive",
                   (v["field_order"] or "progressive") in ("progressive", "unknown")))
    fps_ok_list = preset.get("fps_allowed")
    cfr = v["avg_fps"] is not None and abs(v["avg_fps"] - v["fps"]) < 0.02
    if fps_ok_list:
        c.append(check("Frame rate", "/".join(str(f) for f in fps_ok_list), f"{v['fps']:.3f}",
                       any(abs(v["fps"] - f) < 0.01 for f in fps_ok_list)))
    else:
        lo, hi = preset.get("fps_range", [23, 60])
        c.append(check("Frame rate", f"{lo}–{hi}", f"{v['fps']:.3f}", lo - 0.1 <= v["fps"] <= hi + 0.1))
    c.append(check("Constant frame rate", "CFR", "CFR" if cfr else f"VFR (avg {v['avg_fps']:.3f})", cfr))

    vbr = v["bitrate"] or ((info["bitrate"] or 0) - ((a or {}).get("bitrate") or 0))
    if preset["vcodec"] != "prores":
        lo, hi = preset.get("min_mbps"), preset.get("max_mbps")
        exp = (f"{lo}–{hi} Mbps" if lo and hi else f"~{preset['bitrate_mbps']} Mbps")
        ok = (lo is None or vbr / 1e6 >= lo - 0.05) and (hi is None or vbr / 1e6 <= hi + 0.05)
        c.append(check("Video bitrate", exp, f"{vbr / 1e6:.2f} Mbps", ok))
    else:
        c.append({"check": "Video bitrate", "expected": "ProRes native", "measured": f"{vbr / 1e6:.1f} Mbps", "result": "info"})

    if a:
        exp_ac = {"aac": "aac", "pcm_s24le": "pcm_s24le"}[preset["acodec"]]
        c.append(check("Audio codec", exp_ac, a["codec"], a["codec"] == exp_ac))
        c.append(check("Sample rate", f"{preset['sample_rate']} Hz", f"{a['sample_rate']} Hz",
                       a["sample_rate"] == preset["sample_rate"]))
        c.append(check("Channels", str(preset["channels"]), str(a["channels"]), a["channels"] == preset["channels"]))
        if preset.get("min_audio_kbps") and a.get("bitrate"):
            c.append(check("Audio bitrate", f"≥ {preset['min_audio_kbps']} kbps", f"{a['bitrate'] // 1000} kbps",
                           a["bitrate"] / 1000 >= preset["min_audio_kbps"] - 2))
    else:
        c.append(check("Audio track", "present", "missing", False))

    qc_duration(preset, info["duration"], c)
    mb = info["size_bytes"] / 1048576
    c.append(check("File size", f"≤ {preset['max_file_mb']:,} MB", f"{mb:,.1f} MB", mb <= preset["max_file_mb"]))
    loud = measure_loudness(path) if a else {"integrated": None, "true_peak": None, "lra": None}
    qc_loudness(preset, loud, c)
    return c, info, loud


def qc_audio(path, preset, fmt):
    info = probe(path)
    a = info["audio"]
    c = []
    exp_codec = "mp3" if fmt == "mp3" else ("pcm_s24le" if preset.get("wav_bits") == 24 else "pcm_s16le")
    c.append(check("Format", fmt.upper(), f"{info['format']} / {a['codec']}", a["codec"] == exp_codec))
    c.append(check("Sample rate", f"{preset['sample_rate']} Hz", f"{a['sample_rate']} Hz", a["sample_rate"] == preset["sample_rate"]))
    c.append(check("Channels", str(preset["channels"]), str(a["channels"]), a["channels"] == preset["channels"]))
    if fmt == "mp3":
        br = (a.get("bitrate") or info.get("bitrate") or 0) / 1000
        c.append(check("Bitrate", f"≥ {preset['min_mp3_kbps']} kbps", f"{br:.0f} kbps", br >= preset["min_mp3_kbps"] - 2))
    else:
        c.append(check("Bit depth", f"{preset.get('wav_bits', 16)}-bit", f"{a.get('bits')}-bit",
                       int(a.get("bits") or 0) == preset.get("wav_bits", 16)))
    qc_duration(preset, info["duration"], c)
    mb = info["size_bytes"] / 1048576
    c.append(check("File size", f"≤ {preset['max_file_mb']:,} MB", f"{mb:,.2f} MB", mb <= preset["max_file_mb"]))
    loud = measure_loudness(path)
    qc_loudness(preset, loud, c)
    return c, info, loud


def status_of(checks):
    rs = {c["result"] for c in checks}
    return "fail" if "fail" in rs else ("warn" if "warn" in rs else "pass")
