"""Display banner QC, resizing and file-weight optimisation (Pillow)."""
import io
from pathlib import Path

from PIL import Image, ImageSequence

EXT = {"JPEG": "jpg", "PNG": "png", "GIF": "gif"}


def parse_size(s):
    w, h = s.lower().split("x")
    return int(w), int(h)


def has_alpha(img):
    if img.mode in ("RGBA", "LA"):
        return img.getchannel("A").getextrema()[0] < 255
    return img.mode == "P" and "transparency" in img.info


def gif_stats(img):
    frames, total = 0, 0
    for f in ImageSequence.Iterator(img):
        frames += 1
        total += f.info.get("duration", img.info.get("duration", 100)) or 100
    loop = img.info.get("loop", None)  # 0 = infinite
    return frames, total / 1000.0, loop


def cover_resize(img, w, h):
    src_w, src_h = img.size
    scale = max(w / src_w, h / src_h)
    nw, nh = max(w, round(src_w * scale)), max(h, round(src_h * scale))
    im = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return im.crop((left, top, left + w, top + h))


def fit_under(img, fmt, max_bytes):
    """Encode img as fmt under max_bytes. Returns (bytes, fmt, note)."""
    if fmt == "png":
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        if buf.tell() <= max_bytes:
            return buf.getvalue(), "png", None
        for colors in (256, 128, 64):
            q = img.convert("RGBA").quantize(colors=colors, method=Image.FASTOCTREE, dither=Image.FLOYDSTEINBERG)
            buf = io.BytesIO()
            q.save(buf, "PNG", optimize=True)
            if buf.tell() <= max_bytes:
                return buf.getvalue(), "png", f"Quantized to {colors} colors"
        if not has_alpha(img):
            data, _, note = fit_under(img, "jpg", max_bytes)
            return data, "jpg", "Converted PNG → JPG to meet weight" + (f"; {note}" if note else "")
        return buf.getvalue(), "png", "Could not reach weight limit without losing transparency"
    # jpg: binary search quality
    rgb = img.convert("RGB")
    best, lo, hi = None, 30, 95
    while lo <= hi:
        q = (lo + hi) // 2
        buf = io.BytesIO()
        rgb.save(buf, "JPEG", quality=q, optimize=True, progressive=True, subsampling=2 if q < 90 else 0)
        if buf.tell() <= max_bytes:
            best, lo = (buf.getvalue(), q), q + 1
        else:
            hi = q - 1
    if best:
        return best[0], "jpg", (f"JPEG quality {best[1]}" if best[1] < 95 else None)
    buf = io.BytesIO()
    rgb.save(buf, "JPEG", quality=30, optimize=True, progressive=True)
    return buf.getvalue(), "jpg", "Could not reach weight limit at minimum quality"


def optimize_gif(img, max_bytes):
    buf = io.BytesIO()
    frames = [f.copy() for f in ImageSequence.Iterator(img)]
    durations = [f.info.get("duration", img.info.get("duration", 100)) for f in ImageSequence.Iterator(img)]
    kw = dict(save_all=True, append_images=frames[1:], optimize=True, duration=durations, disposal=2)
    if "loop" in img.info:
        kw["loop"] = img.info["loop"]
    frames[0].save(buf, "GIF", **kw)
    return buf.getvalue()


def qc_banner(data, name, fmt, cfg, target=None):
    im = Image.open(io.BytesIO(data))
    w, h = im.size
    size = f"{w}x{h}"
    c = []
    if target:
        c.append(_c("Dimensions", target, size, size == target))
    else:
        c.append(_c("IAB standard size", "one of " + ", ".join(cfg["sizes"]), size, size in cfg["sizes"]))
    kb = len(data) / 1024
    c.append(_c("File weight", f"≤ {cfg['max_kb']} KB", f"{kb:.1f} KB", kb <= cfg["max_kb"]))
    c.append(_c("Format", "/".join(f.upper() for f in cfg["formats"]), fmt.upper(), fmt in cfg["formats"]))
    if fmt == "gif":
        frames, secs, loop = gif_stats(im)
        if frames > 1:
            c.append(_c("Animation length", f"≤ {cfg['max_animation_seconds']}s total",
                        f"{secs:.1f}s per loop", secs <= cfg["max_animation_seconds"], "warn"))
            loops = "infinite" if loop == 0 else (str((loop or 0) + 1) if loop is not None else "1")
            total = None if loop == 0 else secs * (int(loops))
            ok = loop != 0 and total <= cfg["max_animation_seconds"] and int(loops) <= cfg["max_loops"]
            c.append(_c("Loops", f"≤ {cfg['max_loops']} and stops", loops, ok, "warn"))
    return c, size


def _c(name, exp, meas, ok, sev="fail"):
    return {"check": name, "expected": exp, "measured": meas, "result": "pass" if ok else sev}


def process_banner(src_path, cfg, mode, sizes, out_dir):
    """mode: 'optimize' (keep dimensions) or 'resize' (generate each size in `sizes`).
    Returns a list of output dicts."""
    src_path = Path(src_path)
    im = Image.open(src_path)
    src_fmt = EXT.get(im.format, "png")
    max_bytes = cfg["max_kb"] * 1024
    stem = src_path.stem
    outputs = []
    animated = src_fmt == "gif" and getattr(im, "n_frames", 1) > 1

    targets = [None] if (mode == "optimize" or animated) else sizes
    for t in targets:
        notes = []
        if animated:
            if mode == "resize":
                notes.append("Animated GIFs are QC'd and optimized only, not resized")
            data = optimize_gif(im, max_bytes)
            orig = src_path.read_bytes()
            if len(orig) < len(data):
                data = orig
            fmt = "gif"
        else:
            base = im.convert("RGBA") if has_alpha(im) else im.convert("RGB")
            if t:
                tw, th = parse_size(t)
                if max(tw / im.size[0], th / im.size[1]) > 1.01:
                    notes.append(f"Upscaled from {im.size[0]}x{im.size[1]}; check sharpness")
                base = cover_resize(base, tw, th)
                if abs(im.size[0] / im.size[1] - parse_size(t)[0] / parse_size(t)[1]) > 0.05:
                    notes.append(f"Center-cropped from {im.size[0]}x{im.size[1]}")
            want = "png" if (src_fmt == "png" and has_alpha(im)) else ("png" if src_fmt == "png" else "jpg")
            if src_fmt == "gif":
                want = "png"
            orig = src_path.read_bytes()
            if not t and len(orig) <= max_bytes and src_fmt in cfg["formats"]:
                data, fmt = orig, src_fmt
                notes.append("Already within weight; left untouched")
            else:
                data, fmt, note = fit_under(base, want, max_bytes)
                if note:
                    notes.append(note)
        checks, size = qc_banner(data, stem, fmt, cfg, t)
        fname = f"{stem}.{fmt}" if stem.endswith(size) else f"{stem}_{size}.{fmt}"
        (out_dir / fname).write_bytes(data)
        outputs.append({"file": fname, "size": size, "bytes": len(data), "notes": notes, "checks": checks})
    return outputs
