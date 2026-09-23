"""Broadcast TV delivery (Extreme Reach style): slate + black pre-roll, SMPTE start timecode, exact program length."""
import re
import subprocess
from datetime import date
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import media

FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/Library/Fonts", "/System/Library/Fonts/Supplemental"]
ADID_RE = re.compile(r"^[A-Z0-9]{4}[A-Z0-9]{7}[HD]?$")


def _font(bold, size):
    names = ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", "Arial Bold.ttf" if bold else "Arial.ttf"]
    for d in FONT_DIRS:
        for n in names:
            p = Path(d) / n
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def clean_adid(s):
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()


def render_slate(path, w, h, fields):
    """Standard broadcast slate card: black background, white text, one field per row."""
    img = Image.new("RGB", (w, h), (0, 0, 0))
    d = ImageDraw.Draw(img)
    s = h / 1080
    title_f, label_f, value_f, small_f = _font(True, int(64 * s)), _font(True, int(34 * s)), _font(False, int(40 * s)), _font(False, int(26 * s))
    x0, x1 = int(200 * s), int(640 * s)
    y = int(150 * s)
    d.text((x0, y), (fields.get("title") or "UNTITLED").upper()[:48], font=title_f, fill=(255, 255, 255))
    y += int(110 * s)
    d.line((x0, y, w - x0, y), fill=(200, 16, 46), width=max(2, int(4 * s)))
    y += int(40 * s)
    rows = [("AD-ID / ISCI", fields.get("adid")), ("ADVERTISER", fields.get("advertiser")), ("PRODUCT", fields.get("product")),
            ("AGENCY", fields.get("agency")), ("LENGTH", fields.get("length")), ("FORMAT", fields.get("format")),
            ("AUDIO", fields.get("audio")), ("DATE", fields.get("date")), ("NOTES", fields.get("notes"))]
    for label, val in rows:
        if not val:
            continue
        d.text((x0, y + int(4 * s)), label, font=label_f, fill=(150, 156, 165))
        d.text((x1, y), str(val)[:60], font=value_f, fill=(255, 255, 255))
        y += int(62 * s)
    d.text((x0, h - int(90 * s)), "Program begins 01:00:00:00", font=small_f, fill=(150, 156, 165))
    img.save(path)


def plan(preset, src_info, conform):
    target, note = media.conform_duration(src_info["duration"], preset, conform)
    prog = target or src_info["duration"]
    return prog, note


def encode_broadcast(src, src_info, preset, out_path, slate_fields, fit="pad", conform=True):
    notes = []
    sv = src_info["video"]
    if not sv:
        raise media.MediaError("Broadcast delivery needs a video master")
    w, h = preset["width"], preset["height"]
    sl = preset["slate"]
    S, B, T = sl["slate_seconds"], sl["black_seconds"], sl.get("tail_black_seconds", 0)
    sr = preset["sample_rate"]
    fps = media.pick_fps(sv["fps"], preset)
    if sv["fps"] and abs(fps - sv["fps"]) > 0.01:
        notes.append(f"Frame rate converted {sv['fps']:.3f} → {fps}")
    interlaced = sv["field_order"] not in (None, "progressive", "unknown")
    if interlaced:
        notes.append("Source was interlaced; deinterlaced")
    sar = Fraction(sv["sar"].replace(":", "/")) if sv["sar"] not in ("1:1", None) else Fraction(1)
    if abs(sv["width"] * sar / sv["height"] - w / h) > 0.01:
        notes.append({"pad": "Letterboxed/pillarboxed", "crop": "Center-cropped", "blur": "Blur-filled"}[fit]
                     + f" {sv['width']}x{sv['height']} → {w}x{h}")
    fstr = media.fps_str(fps)
    prog, dnote = plan(preset, src_info, conform)
    if dnote:
        notes.append(dnote)

    adid = clean_adid(slate_fields.get("adid"))
    fields = dict(slate_fields)
    fields["adid"] = adid
    fields["length"] = f":{round(prog)}"
    fields["format"] = f"{w}x{h} {'23.98' if fps == 23.976 else fps}p · H.264 {preset['bitrate_mbps']} Mbps"
    fields["audio"] = f"Stereo · {preset['loudness']['target_lufs']} LKFS · ≤ {preset['loudness']['max_tp']} dBTP"
    fields["date"] = fields.get("date") or date.today().strftime("%m/%d/%Y")
    slate_png = Path(out_path).with_suffix(".slate.png")
    render_slate(slate_png, w, h, fields)
    notes.append(f"Slate {S}s + black {B}s added; program starts 01:00:00:00" + (f"; {T}s tail black" if T else ""))

    has_audio = src_info["audio"] is not None
    cmd = ["ffmpeg", "-hide_banner", "-y", "-i", str(src),
           "-loop", "1", "-framerate", fstr, "-t", f"{S}", "-i", str(slate_png)]
    vf = media.video_filter(w, h, fit, interlaced, fstr, sar != 1)
    pad = max(0.0, prog - src_info["duration"]) + 0.2
    g = [vf.replace("[vout]", f",format=yuv420p,tpad=stop_mode=add:stop_duration={pad:.3f},trim=duration={prog:.4f},setpts=PTS-STARTPTS[pv]")]
    if has_audio:
        lf = media.loudnorm_filter(src, preset["loudness"], sr)
        chain = (lf + "," if lf else "") + f"aresample={sr},aformat=sample_fmts=s32:channel_layouts=stereo,apad,atrim=duration={prog:.4f},asetpts=PTS-STARTPTS"
        if not lf:
            notes.append("Source audio is silent; loudness not normalised")
        g.append(f"[0:a:0]{chain}[pa]")
    else:
        notes.append("Source had no audio; silent stereo track added")
        g.append(f"anullsrc=r={sr}:cl=stereo,aformat=sample_fmts=s32,atrim=duration={prog:.4f}[pa]")
    g.append(f"[1:v]scale={w}:{h},setsar=1,fps={fstr},format=yuv420p,trim=duration={S},setpts=PTS-STARTPTS[sv]")
    g.append(f"anullsrc=r={sr}:cl=stereo,aformat=sample_fmts=s32,atrim=duration={S}[sa]")
    g.append(f"color=c=black:s={w}x{h}:r={fstr}:d={B},format=yuv420p,setsar=1[bv]")
    g.append(f"anullsrc=r={sr}:cl=stereo,aformat=sample_fmts=s32,atrim=duration={B}[ba]")
    segs = "[sv][sa][bv][ba][pv][pa]"
    n = 3
    if T:
        g.append(f"color=c=black:s={w}x{h}:r={fstr}:d={T},format=yuv420p,setsar=1[tv]")
        g.append(f"anullsrc=r={sr}:cl=stereo,aformat=sample_fmts=s32,atrim=duration={T}[ta]")
        segs += "[tv][ta]"
        n = 4
    g.append(f"{segs}concat=n={n}:v=1:a=1[v][a]")

    fnum = float(Fraction(fstr))
    gop = max(1, round(fnum * preset.get("gop_seconds", 1)))
    mb = preset["bitrate_mbps"]
    cmd += ["-filter_complex", ";".join(g), "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", media.os.environ.get("X264_PRESET", "medium"), "-profile:v", "high", "-level:v", "4.1",
            "-pix_fmt", "yuv420p", "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0", "-flags", "+cgop", "-bf", "2",
            "-b:v", f"{mb}M", "-minrate", f"{mb}M", "-maxrate", f"{mb}M", "-bufsize", f"{mb * 2}M",
            "-x264-params", "nal-hrd=cbr:force-cfr=1",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
            "-fps_mode", "cfr", "-r", fstr,
            "-c:a", preset["acodec"], "-ar", str(sr), "-ac", "2",
            "-timecode", sl["start_timecode"], "-t", f"{S + B + prog + T:.4f}"]
    if adid:
        cmd += ["-metadata", f"title={adid}"]
    cmd += [str(out_path)]
    try:
        media.run(cmd)
    finally:
        slate_png.unlink(missing_ok=True)
    if adid and not ADID_RE.match(adid):
        notes.append(f"Ad-ID '{adid}' doesn't match the 11–12 character Ad-ID format; double-check it")
    return notes, prog


def _probe_timecode(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags=timecode:stream_tags=timecode",
                        "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    vals = [l.strip() for l in p.stdout.splitlines() if l.strip()]
    return vals[0] if vals else None


def _max_volume(path, start, dur):
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start}", "-t", f"{dur}", "-i", str(path),
                        "-vn", "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", p.stderr)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return -120.0


def _black_ratio(path, start, dur):
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start}", "-t", f"{dur}", "-i", str(path),
                        "-an", "-vf", "blackdetect=d=0.1:pix_th=0.10", "-f", "null", "-"], capture_output=True, text=True)
    total = sum(float(x) for x in re.findall(r"black_duration:([\d.]+)", p.stderr))
    return total / dur if dur else 0


def qc_broadcast(path, preset, fields):
    sl = preset["slate"]
    S, B, T = sl["slate_seconds"], sl["black_seconds"], sl.get("tail_black_seconds", 0)
    checks, info, _ = media.qc_video(path, {**preset, "durations": None, "max_duration": None})
    # replace whole-file loudness with program-only measurement
    checks = [c for c in checks if c["check"] not in ("Integrated loudness", "True peak", "Loudness range")]
    prog = info["duration"] - S - B - T
    nearest = min(preset["durations"], key=lambda d: abs(d - prog))
    checks.append(media.check("Program length", "/".join(f":{d}" for d in preset["durations"]) + " (frame-exact)",
                              f"{prog:.3f}s", abs(prog - nearest) <= 0.05))
    checks.append(media.check("Total with pre-roll", f"{S}s slate + {B}s black + program" + (f" + {T}s black" if T else ""),
                              f"{info['duration']:.3f}s", abs(info["duration"] - (S + B + nearest + T)) <= 0.1))
    tc = _probe_timecode(path)
    checks.append(media.check("Start timecode", sl["start_timecode"], tc or "none", (tc or "").replace(";", ":") == sl["start_timecode"]))
    br = _black_ratio(path, S + 0.05, B - 0.1)
    checks.append(media.check("Black before program", f"{B}s", f"{br * 100:.0f}% black", br >= 0.9))
    sb = _black_ratio(path, 0.2, S - 0.4)
    checks.append(media.check("Slate present", "slate card (not black)", "slate card" if sb < 0.5 else "black", sb < 0.5))
    mv = _max_volume(path, 0, S + B - 0.05)
    checks.append(media.check("Silence under slate/black", "≤ -60 dBFS", f"{mv:.1f} dBFS" if mv is not None else "n/a",
                              mv is not None and mv <= -60))
    adid = clean_adid(fields.get("adid"))
    if adid:
        checks.append(media.check("Ad-ID format", "4-char prefix + 7 chars (+H/D)", adid, bool(ADID_RE.match(adid)), "warn"))
    else:
        checks.append(media.check("Ad-ID on slate", "provided", "missing", False, "warn"))
    loud = measure_program_loudness(path, S + B, nearest)
    media.qc_loudness(preset, loud, checks)
    return checks, info, loud


def measure_program_loudness(path, start, dur):
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start}", "-t", f"{dur}", "-i", str(path), "-vn",
                        "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"], capture_output=True, text=True)
    tmp = p.stderr
    summ = tmp[tmp.rfind("Summary:"):] if "Summary:" in tmp else tmp
    def grab(pat):
        m = re.search(pat, summ, re.S)
        try:
            return float(m.group(1)) if m else None
        except ValueError:
            return None
    i = grab(r"Integrated loudness:\s+I:\s+(-?[\d.]+)")
    return {"integrated": i if i is not None and i > -69 else None,
            "true_peak": grab(r"True peak:\s+Peak:\s+(-?[\d.]+)"), "lra": grab(r"LRA:\s+(-?[\d.]+)")}
