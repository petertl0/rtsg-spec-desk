"""RTSG Spec Desk — internal ad creative transcoding + QC service."""
import hashlib
import hmac
import html
import json
import os
import queue
import secrets
import shutil
import threading
import time
import traceback
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import display, media

BASE = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR", "/tmp/specdesk"))
DATA.mkdir(parents=True, exist_ok=True)
RETENTION_MIN = int(os.environ.get("RETENTION_MINUTES", "60"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "4096"))
WORKERS = int(os.environ.get("WORKERS", "1"))
SPECS_PATH = Path(os.environ.get("SPECS_PATH", BASE / "specs.json"))
SECRET = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

VIDEO_EXT = {".mp4", ".mov", ".mxf", ".m4v", ".avi", ".mkv", ".webm", ".mpg", ".mpeg", ".ts"}
AUDIO_EXT = {".wav", ".mp3", ".aif", ".aiff", ".m4a", ".aac", ".flac", ".ogg"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif"}


def load_users():
    """TEAM_USERS="ted@rtsg.co:pass1,jane@rtsg.co:pass2" (up to however many seats you want)."""
    raw = os.environ.get("TEAM_USERS", "")
    users = {}
    for pair in raw.split(","):
        if ":" in pair:
            e, p = pair.split(":", 1)
            users[e.strip().lower()] = p.strip()
    return users


def load_specs():
    return json.loads(SPECS_PATH.read_text())


app = FastAPI(title="RTSG Spec Desk", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET, max_age=60 * 60 * 12,
                   https_only=os.environ.get("HTTPS_ONLY", "0") == "1", same_site="lax")

JOBS = {}
LOCK = threading.Lock()
Q = queue.Queue()


# ------------------------------------------------------------------ auth
def user_of(request: Request):
    return request.session.get("user")


def require(request: Request):
    u = user_of(request)
    if not u:
        raise HTTPException(401, "Not signed in")
    return u


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    pw = body.get("password") or ""
    users = load_users()
    if not users:
        raise HTTPException(500, "No TEAM_USERS configured on the server")
    if email in users and hmac.compare_digest(users[email], pw):
        request.session["user"] = email
        return {"user": email}
    time.sleep(0.8)
    raise HTTPException(401, "Invalid email or password")


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    return {"user": user_of(request)}


# ------------------------------------------------------------------ presets
@app.get("/api/presets")
def presets(request: Request):
    require(request)
    s = load_specs()
    out = {"video": [], "audio": [], "display": s["display"]}
    for p in s["video"]:
        out["video"].append({"id": p["id"], "group": p["group"], "name": p["name"], "summary": summarize(p), "verify": p.get("verify", [])})
    for p in s["audio"]:
        out["audio"].append({"id": p["id"], "group": p["group"], "name": p["name"], "summary": summarize(p), "verify": p.get("verify", [])})
    out["retention_minutes"] = RETENTION_MIN
    return out


def summarize(p):
    L = p.get("loudness", {})
    loud = f"{L.get('target_lufs')} LUFS / {L.get('max_tp')} dBTP" if L else ""
    if "width" in p:
        v = "ProRes 422 HQ" if p["vcodec"] == "prores" else f"H.264 {p['bitrate_mbps']} Mbps"
        return f"{p['width']}x{p['height']} · {v} · {p['container'].upper()} · {loud}"
    return f"{'/'.join(f.upper() for f in p['formats'])} · {p['sample_rate'] / 1000:g} kHz · {loud}"


# ------------------------------------------------------------------ jobs
def new_job(user, kind, source_names, options):
    jid = uuid.uuid4().hex[:12]
    d = DATA / jid
    (d / "in").mkdir(parents=True)
    (d / "out").mkdir()
    job = {"id": jid, "user": user, "kind": kind, "created": time.time(), "finished": None,
           "status": "queued", "step": "Waiting in queue", "progress": 0, "sources": source_names,
           "options": options, "source_info": None, "outputs": [], "error": None}
    with LOCK:
        JOBS[jid] = job
    return job


def job_dir(jid):
    return DATA / jid


def get_job(request, jid):
    require(request)
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "Job not found or already expired")
    return job


async def save_upload(up: UploadFile, dest: Path):
    size = 0
    with dest.open("wb") as f:
        while chunk := await up.read(1024 * 1024 * 4):
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1048576:
                raise HTTPException(413, f"File exceeds {MAX_UPLOAD_MB} MB limit")
            f.write(chunk)
    return size


def safe_name(n):
    n = Path(n or "file").name
    keep = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in n).strip().replace(" ", "_")
    return keep or "file"


@app.post("/api/jobs/media")
async def create_media_job(request: Request, file: UploadFile = File(...), presets: str = Form(...),
                           fit: str = Form("pad"), conform: str = Form("1")):
    user = require(request)
    ids = [p for p in presets.split(",") if p]
    if not ids:
        raise HTTPException(400, "Choose at least one output preset")
    name = safe_name(file.filename)
    ext = Path(name).suffix.lower()
    if ext not in VIDEO_EXT | AUDIO_EXT:
        raise HTTPException(400, f"Unsupported file type {ext}")
    if fit not in ("pad", "crop", "blur"):
        fit = "pad"
    job = new_job(user, "media", [name], {"presets": ids, "fit": fit, "conform": conform == "1"})
    try:
        await save_upload(file, job_dir(job["id"]) / "in" / name)
    except Exception:
        purge(job["id"])
        raise
    Q.put(job["id"])
    return {"id": job["id"]}


@app.post("/api/jobs/display")
async def create_display_job(request: Request, files: list[UploadFile] = File(...), mode: str = Form("optimize"),
                             sizes: str = Form("")):
    user = require(request)
    cfg = load_specs()["display"]
    job = new_job(user, "display", [], {"mode": mode, "sizes": [s for s in sizes.split(",") if s]})
    try:
        names = await _collect_banners(job, files, cfg)
    except Exception:
        purge(job["id"])
        raise
    job["sources"] = names
    Q.put(job["id"])
    return {"id": job["id"]}


async def _collect_banners(job, files, cfg):
    names = []
    d_in = job_dir(job["id"]) / "in"
    for up in files:
        n = safe_name(up.filename)
        ext = Path(n).suffix.lower()
        dest = d_in / n
        await save_upload(up, dest)
        if ext == ".zip":
            with zipfile.ZipFile(dest) as z:
                for zi in z.infolist():
                    zn = safe_name(zi.filename)
                    if zi.is_dir() or Path(zn).suffix.lower() not in IMAGE_EXT or zi.filename.startswith("__MACOSX"):
                        continue
                    (d_in / zn).write_bytes(z.read(zi))
                    names.append(zn)
            dest.unlink()
        elif ext in IMAGE_EXT:
            names.append(n)
        else:
            dest.unlink()
    if not names:
        raise HTTPException(400, "No JPG/PNG/GIF images found")
    if len(names) > cfg["max_batch"]:
        raise HTTPException(400, f"Batch limit is {cfg['max_batch']} banners")
    if job["options"]["mode"] == "resize" and not job["options"]["sizes"]:
        raise HTTPException(400, "Pick at least one size to generate")
    return names


@app.get("/api/jobs")
def list_jobs(request: Request):
    require(request)
    with LOCK:
        jobs = sorted(JOBS.values(), key=lambda j: -j["created"])
    return [public(j) for j in jobs]


@app.get("/api/jobs/{jid}")
def job_status(request: Request, jid: str):
    return public(get_job(request, jid))


def public(j):
    exp = (j["finished"] + RETENTION_MIN * 60) if j["finished"] else None
    return {**{k: v for k, v in j.items() if k != "log"}, "expires": exp}


@app.get("/api/jobs/{jid}/files/{name}")
def download(request: Request, jid: str, name: str):
    get_job(request, jid)
    p = (job_dir(jid) / "out" / name).resolve()
    if not str(p).startswith(str((job_dir(jid) / "out").resolve())) or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, filename=name)


@app.get("/api/jobs/{jid}/zip")
def download_zip(request: Request, jid: str):
    job = get_job(request, jid)
    out = job_dir(jid) / "out"
    zp = job_dir(jid) / f"specdesk_{jid}.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.iterdir()):
            z.write(f, f.name)
        z.writestr("QC_REPORT.html", render_report(job))
    return FileResponse(zp, filename=f"specdesk_{jid}.zip")


@app.get("/api/jobs/{jid}/report", response_class=HTMLResponse)
def report(request: Request, jid: str):
    return HTMLResponse(render_report(get_job(request, jid)))


@app.delete("/api/jobs/{jid}")
def delete_job(request: Request, jid: str):
    get_job(request, jid)
    purge(jid)
    return {"ok": True}


def purge(jid):
    with LOCK:
        JOBS.pop(jid, None)
    shutil.rmtree(job_dir(jid), ignore_errors=True)


# ------------------------------------------------------------------ worker
def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def process_media(job):
    specs = load_specs()
    by_id = {p["id"]: ("video", p) for p in specs["video"]} | {p["id"]: ("audio", p) for p in specs["audio"]}
    src = job_dir(job["id"]) / "in" / job["sources"][0]
    out = job_dir(job["id"]) / "out"
    job["step"] = "Analyzing source"
    info = media.probe(src)
    info["loudness"] = media.measure_loudness(src) if info["audio"] else None
    job["source_info"] = info
    stem = Path(job["sources"][0]).stem
    targets = []
    for pid in job["options"]["presets"]:
        if pid not in by_id:
            continue
        kind, p = by_id[pid]
        if kind == "video":
            targets.append((kind, p, None))
        else:
            for fmt in p["formats"]:
                targets.append((kind, p, fmt))
    for n, (kind, p, fmt) in enumerate(targets):
        label = p["name"] + (f" ({fmt.upper()})" if fmt else "")
        job["step"] = f"Encoding {label} ({n + 1}/{len(targets)})"
        entry = {"preset": p["id"], "name": label, "group": p["group"], "status": "running",
                 "file": None, "notes": [], "checks": [], "verify": p.get("verify", [])}
        job["outputs"].append(entry)
        try:
            if kind == "video":
                if not info["video"]:
                    raise media.MediaError("Source has no video; video presets need a video master")
                fname = f"{stem}_{p['id']}.{p['container']}"
                entry["notes"] = media.encode_video(src, info, p, out / fname, job["options"]["fit"], job["options"]["conform"])
                job["step"] = f"Verifying {label}"
                checks, oinfo, loud = media.qc_video(out / fname, p)
            else:
                fname = f"{stem}_{p['id']}.{fmt}"
                entry["notes"] = media.encode_audio(src, info, p, fmt, out / fname, job["options"]["conform"])
                job["step"] = f"Verifying {label}"
                checks, oinfo, loud = media.qc_audio(out / fname, p, fmt)
            entry.update(file=fname, checks=checks, status=media.status_of(checks),
                         bytes=(out / fname).stat().st_size, sha256=sha256(out / fname),
                         measured={"duration": oinfo["duration"], "loudness": loud})
        except Exception as e:
            entry.update(status="error", error=str(e)[-1500:])
        job["progress"] = round(100 * (n + 1) / len(targets))


def process_display(job):
    cfg = load_specs()["display"]
    d = job_dir(job["id"])
    total = len(job["sources"])
    for n, name in enumerate(job["sources"]):
        job["step"] = f"Processing {name} ({n + 1}/{total})"
        try:
            outs = display.process_banner(d / "in" / name, cfg, job["options"]["mode"], job["options"]["sizes"], d / "out")
            for o in outs:
                job["outputs"].append({"preset": "display", "name": f"{name} → {o['size']}", "group": "Display",
                                       "file": o["file"], "bytes": o["bytes"], "notes": o["notes"],
                                       "checks": o["checks"], "status": media.status_of(o["checks"]),
                                       "sha256": hashlib.sha256((d / "out" / o["file"]).read_bytes()).hexdigest()})
        except Exception as e:
            job["outputs"].append({"preset": "display", "name": name, "group": "Display", "status": "error",
                                   "error": str(e), "checks": [], "notes": []})
        job["progress"] = round(100 * (n + 1) / total)


def worker():
    while True:
        jid = Q.get()
        job = JOBS.get(jid)
        if not job:
            continue
        job["status"] = "processing"
        try:
            (process_media if job["kind"] == "media" else process_display)(job)
            job["status"] = "done"
            job["step"] = "Complete"
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)[-1500:]
            traceback.print_exc()
        finally:
            job["finished"] = time.time()
            # originals are removed as soon as processing ends
            shutil.rmtree(job_dir(jid) / "in", ignore_errors=True)


def janitor():
    while True:
        now = time.time()
        for jid, j in list(JOBS.items()):
            if j["finished"] and now - j["finished"] > RETENTION_MIN * 60:
                purge(jid)
        # orphaned dirs (e.g. after a restart)
        for d in DATA.iterdir():
            if d.is_dir() and d.name not in JOBS and now - d.stat().st_mtime > RETENTION_MIN * 60:
                shutil.rmtree(d, ignore_errors=True)
        time.sleep(60)


@app.on_event("startup")
def start_threads():
    for _ in range(WORKERS):
        threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=janitor, daemon=True).start()


# ------------------------------------------------------------------ report
def render_report(job):
    e = html.escape
    ts = datetime.fromtimestamp(job["finished"] or time.time(), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = []
    counts = {"pass": 0, "warn": 0, "fail": 0, "error": 0}
    for o in job["outputs"]:
        counts[o["status"]] = counts.get(o["status"], 0) + 1
        checks = "".join(
            f"<tr class='{c['result']}'><td>{e(c['check'])}</td><td>{e(str(c['expected']))}</td>"
            f"<td>{e(str(c['measured']))}</td><td class='r'>{c['result'].upper()}</td></tr>" for c in o["checks"])
        notes = "".join(f"<li>{e(n)}</li>" for n in o.get("notes") or [])
        verify = ", ".join(o.get("verify") or [])
        meta = ""
        if o.get("file"):
            meta = (f"<div class='meta'>{e(o['file'])} · {o.get('bytes', 0) / 1048576:,.2f} MB"
                    f"<br><span class='hash'>SHA-256 {o.get('sha256', '')}</span></div>")
        rows.append(f"""
        <section class='out'>
          <div class='hd'><h2>{e(o['name'])}</h2><span class='badge {o['status']}'>{o['status'].upper()}</span></div>
          {meta}
          {'<p class=err>' + e(o.get('error', '')) + '</p>' if o['status'] == 'error' else ''}
          {'<ul class=notes>' + notes + '</ul>' if notes else ''}
          {'<table><tr><th>Check</th><th>Spec</th><th>Measured</th><th></th></tr>' + checks + '</table>' if checks else ''}
          {"<p class='verify'>Spec values to confirm with platform: " + e(verify) + "</p>" if verify else ''}
        </section>""")
    si = job.get("source_info") or {}
    src = ""
    if si:
        v, a, l = si.get("video"), si.get("audio"), si.get("loudness") or {}
        bits = [f"{si['duration']:.2f}s", f"{si['size_bytes'] / 1048576:,.1f} MB"]
        if v:
            bits.append(f"{v['codec']} {v['width']}x{v['height']} @ {v['fps']:.3f} fps, {v['field_order']}")
        if a:
            bits.append(f"{a['codec']} {a['sample_rate']} Hz {a['channels']}ch")
        if l.get("integrated") is not None:
            bits.append(f"{l['integrated']:.1f} LUFS / {l['true_peak']:.1f} dBTP")
        src = " · ".join(e(b) for b in bits)
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>QC Report {job['id']}</title>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=Barlow:wght@400;500&display=swap" rel="stylesheet">
<style>
body{{font-family:Barlow,system-ui,sans-serif;color:#111;margin:0;background:#fff}}
.wrap{{max-width:900px;margin:0 auto;padding:32px 20px}}
header{{border-bottom:4px solid #c8102e;padding-bottom:14px;margin-bottom:20px}}
h1,h2{{font-family:'Barlow Condensed',sans-serif;text-transform:uppercase;letter-spacing:.02em;margin:0}}
h1{{font-size:30px}} h2{{font-size:19px}}
.sub{{color:#555;font-size:14px;margin-top:6px}}
.sum{{display:flex;gap:18px;font-size:14px;margin:10px 0 0}}
.out{{border:1px solid #ddd;border-radius:6px;padding:14px 16px;margin:14px 0;break-inside:avoid}}
.hd{{display:flex;justify-content:space-between;align-items:center;gap:10px}}
.badge{{font:700 12px 'Barlow Condensed','Arial Narrow',sans-serif;padding:3px 9px;border-radius:3px;color:#fff;letter-spacing:.06em}}
.badge.pass{{background:#1a7f37}} .badge.warn{{background:#b7791f}} .badge.fail,.badge.error{{background:#c8102e}}
.meta{{font-size:13px;color:#444;margin:6px 0}} .hash{{font-family:ui-monospace,monospace;font-size:11px;color:#777;word-break:break-all}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}}
th,td{{text-align:left;padding:5px 6px;border-bottom:1px solid #eee}} th{{color:#666;font-weight:500}}
td.r{{font-weight:700;text-align:right}} tr.pass td.r{{color:#1a7f37}} tr.warn td.r{{color:#b7791f}} tr.fail td.r{{color:#c8102e}} tr.info td.r{{color:#888}}
.notes{{font-size:13px;color:#444;margin:6px 0;padding-left:18px}} .err{{color:#c8102e;font-size:13px;white-space:pre-wrap}}
.verify{{font-size:12px;color:#8a6d00;margin:8px 0 0}}
footer{{font-size:12px;color:#777;margin-top:24px;border-top:1px solid #eee;padding-top:10px}}
@media print{{.wrap{{padding:0}}}}
</style></head><body><div class='wrap'>
<header><h1>Creative QC Report</h1>
<div class='sub'>RTSG Spec Desk · Job {job['id']} · {e(ts)} · Prepared by {e(job['user'])}</div>
<div class='sub'>Source: {e(', '.join(job['sources'][:5]))}{' +' + str(len(job['sources']) - 5) + ' more' if len(job['sources']) > 5 else ''}</div>
{f"<div class='sub'>{src}</div>" if src else ''}
<div class='sum'><b>{len(job['outputs'])} deliverables</b><span style='color:#1a7f37'>{counts['pass']} pass</span><span style='color:#b7791f'>{counts['warn']} warning</span><span style='color:#c8102e'>{counts['fail'] + counts['error']} fail</span></div>
</header>
{''.join(rows)}
<footer>Every deliverable was re-measured after encoding with ffprobe and an independent EBU R128 / ITU-R BS.1770 loudness pass (ffmpeg ebur128, true-peak).
SHA-256 hashes identify the exact files measured. Spec targets reflect RTSG's configured presets; confirm against each platform's current published specifications.</footer>
</div></body></html>"""


# ------------------------------------------------------------------ static
@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/healthz")
def health():
    return {"ok": True, "queue": Q.qsize(), "jobs": len(JOBS)}


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
