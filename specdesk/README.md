# RTSG Spec Desk

This is RTSG's internal tool for transcoding and QC'ing ad creative. You upload a master file, pick your platforms, and download files that meet each platform's spec, plus a QC report you can send to vendors.

## What it does

**Video presets**
- CTV/OTT: Roku, Hulu/Disney+ (ProRes 422 HQ or H.264), VAST/DSP mezzanine at 1080p, and a 720p streaming rendition.
- Social: Meta Feed (4:5 and 1:1), Meta Reels/Stories (9:16), and YouTube (16:9, 9:16 Shorts and 1:1).

Every video output goes through the same processing:
- Deinterlaces interlaced sources.
- Corrects anamorphic (non-square) pixels.
- Conforms the frame rate to one the platform accepts, with constant frame rate.
- Fits the picture to the target aspect by letterboxing, center-cropping or blur-filling.
- Encodes the CTV presets at CBR so they meet minimum-bitrate rules.
- Uses a closed GOP and puts faststart metadata at the front of the MP4.

**Audio presets**
- Spotify (MP3), Pandora/SiriusXM (MP3 and WAV), and iHeart (WAV and MP3).
- A video master can feed these directly; the audio is pulled from it.

**Loudness**
- Every output gets two-pass EBU R128 loudness normalization to the platform's target in LUFS and its true-peak ceiling in dBTP.

**Spot length**
- A source within 1 second of a standard length (:15, :30 and so on) is trimmed or padded to that exact length.
- You can turn this off in the UI.

**Display banners**
- Batches of up to 200 JPG, PNG or GIF files, or a ZIP.
- "Optimize" mode shrinks each file under the 150 KB weight limit and QCs it against the IAB sizes.
- "Generate" mode takes one master image and makes every selected IAB size by center-cropping it.
- Animated GIFs are checked for animation length and loop count.

**Verification**
- After encoding, every file is measured again: ffprobe reads the codec, profile, resolution, pixel aspect, scan type, constant frame rate, bitrate, sample rate, channels, duration and file size.
- A separate `ebur128` pass measures true-peak loudness.
- Each check is marked pass, warning or fail. The report records a SHA-256 hash for every file.

**Privacy and team access**
- Your uploaded original is deleted as soon as the job finishes.
- The outputs are deleted after `RETENTION_MINUTES` (default 60), or right away if you click "Delete now".
- Team seats are set with the `TEAM_USERS` environment variable.

## Editing specs

All platform values are in `app/specs.json`: resolution, bitrate, loudness, allowed durations, file-size caps and banner limits.
- Edit that file and redeploy. You don't need to touch any code.
- Each preset's `verify` list names the values that come from common industry practice rather than a spec sheet checked this week. **Confirm those against each platform's current published specs before relying on them.**
- `loudness.enforce: false` (used for social) turns a loudness miss into a warning instead of a failure. Meta and YouTube normalize playback themselves.
- You can add a preset by copying an existing block and giving it a new `id`. The UI picks it up automatically.

## Run locally

```bash
brew install ffmpeg            # or apt install ffmpeg
pip install -r requirements.txt
TEAM_USERS="ted@rtsg.co:yourpassword" uvicorn app.main:app --port 8080
# open http://localhost:8080
```

## Deploy (Render, recommended)

Netlify can't run ffmpeg, so this needs a container host.

1. Push this folder to a private GitHub repo.
2. In Render, go to **New → Blueprint** and pick the repo. `render.yaml` sets up a Docker web service with a 50 GB work disk.
3. Set `TEAM_USERS` to one `email:password` pair per seat, separated by commas, for example `ted@rtsg.co:xxxx,media@rtsg.co:yyyy`. `SECRET_KEY` is generated for you.
4. Add a custom domain, such as `specs.rtsg.co`, under Settings → Custom Domains.

Fly.io, Railway or any VPS with Docker also work: run `docker build -t specdesk . && docker run -p 8080:8080 --env-file .env specdesk`.

### Sizing notes

- Encoding is CPU-bound. At `X264_PRESET=medium` on one vCPU, a 1080p :30 takes about 30–60 seconds per output, and ProRes takes about 2 minutes. For big batches, use a larger instance or set `X264_PRESET=fast`.
- Jobs run one at a time by default (`WORKERS=1`). Raise it if the instance has more cores.
- Job state is kept in memory, so a redeploy clears the job list. Files are deleted within an hour anyway, so this is by design.
- `MAX_UPLOAD_MB` defaults to 4096. Some hosts set their own upload or request-timeout limits, so test with your largest ProRes master after deploying.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `TEAM_USERS` | – | `email:password,...` for each seat (required) |
| `SECRET_KEY` | random | Signs session cookies. Set it, or everyone is signed out on each restart. |
| `RETENTION_MINUTES` | 60 | How long output files are kept |
| `MAX_UPLOAD_MB` | 4096 | Largest file you can upload |
| `X264_PRESET` | medium | Trades speed against quality: `fast`, `medium` or `slow` |
| `WORKERS` | 1 | Number of jobs that run at the same time |
| `HTTPS_ONLY` | 1 in Docker | Sends cookies only over HTTPS. Set to 0 for local HTTP. |
| `SPECS_PATH` | app/specs.json | Points to another spec file |

## Known limits

- HTML5 (zipped) banners aren't processed; only static and animated image banners are.
- Captions and slates aren't handled. Masters should be clean with no slate, since CTV platforms reject slated files.
- 5.1 audio is downmixed to stereo.
- A source with no audio gets a silent track, and that output fails the loudness check on purpose, because platforms reject silent spots.
