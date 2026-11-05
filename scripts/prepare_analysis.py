#!/usr/bin/env python3
"""Stage 1 of camtasia-ai-cut: build an analysis-only MP4 from a Camtasia recording.

This script does NOT cut anything. It only prepares the input that the official
`video-editor` skill needs, because that skill accepts ordinary MP4/MOV/MKV
files and cannot open a native Camtasia .cmproj package.

A Camtasia .trec is a QuickTime container carrying three streams:

    index 0  tscc2   full-resolution screen capture (proprietary codec)
    index 1  h264    low-resolution proxy (stream-copyable)
    index 2  aac     microphone

We remux the proxy + audio into an MP4. The proxy is only used for ASR timing
and semantic reading; it never enters the final project, which keeps using the
original in-package .trec.

Why the gain: Camtasia screen recordings on this machine measured -40.7 dB
mean on a short sample and -43.4 dB on a 12-minute sample. The official skill's
auto silence threshold has a -30.5 dB floor, so quiet-but-real speech is
classified as silence and pause compression swallows whole sentences. Raising
the ANALYSIS COPY's level to about -28 dB fixes that without touching the
skill, the plan format, the .cmproj or the .trec.

Usage:
  /usr/bin/python3 prepare_analysis.py \
      --project "/path/to/Recording.cmproj" \
      --out "/path/to/analysis/分析源.mp4"

  /usr/bin/python3 prepare_analysis.py --project "..." --probe-only
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


def tool(name):
    found = shutil.which(name)
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/" + name, "/usr/local/bin/" + name):
        if Path(candidate).is_file():
            return candidate
    sys.exit("Missing %s; install it or add it to PATH" % name)


def run(command):
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode != 0:
        sys.exit("Command failed:\n  %s\n%s" % (" ".join(str(c) for c in command), done.stderr[-2000:]))
    return done.stdout


def probe_streams(ffprobe, media):
    raw = run([ffprobe, "-v", "error", "-show_streams", "-show_format",
               "-of", "json", str(media)])
    info = json.loads(raw)
    return info.get("streams", []), info.get("format", {})


def locate_trec(project):
    """Prefer the .trec actually referenced by sourceBin, else the largest one."""
    main = project / "project.tscproj"
    if not main.is_file():
        sys.exit("Not a native .cmproj package: %s" % project)
    data = json.loads(main.read_text(encoding="utf-8"))
    candidates = []
    for entry in data.get("sourceBin", []):
        src = str(entry.get("src", ""))
        if src.lower().endswith(".trec"):
            path = project / src.lstrip("./")
            if path.is_file():
                candidates.append(path)
    if not candidates:
        candidates = [p for p in (project / "recordings").rglob("*.trec") if p.is_file()]
    if not candidates:
        sys.exit("No .trec found inside %s" % project)
    winner = max(candidates, key=lambda p: p.stat().st_size)
    return winner, data


def pick_streams(streams):
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if not videos or not audios:
        sys.exit("Expected at least one video and one audio stream")
    # tscc2 is the full-res Camtasia codec; the h264 proxy is what we can copy.
    usable = [s for s in videos if s.get("codec_name") not in ("tscc2", "tscc")]
    video = usable[0] if usable else videos[0]
    return int(video["index"]), int(audios[0]["index"]), video.get("codec_name")


def measure_mean_db(ffmpeg, media, audio_index):
    done = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(media), "-map", "0:%d" % audio_index,
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True)
    hit = re.search(r"mean_volume:\s*(-?[\d.]+) dB", done.stderr)
    if not hit:
        sys.exit("volumedetect produced no mean_volume:\n%s" % done.stderr[-1500:])
    return float(hit.group(1))


def project_timing(data):
    """Return (editRate, source_ms, clip_count) from tim0; source_ms is the common range."""
    try:
        rate = float(data["editRate"])
        tracks = data["timeline"]["sceneTrack"]["scenes"][0]["csml"]["tracks"]
        medias = tracks[0]["medias"]
        if len(medias) != 1:
            return rate, None, len(medias)
        per_ms = rate / 1000.0
        source = float(medias[0]["duration"]) / per_ms
        # A flat .trec import has a second (audio) track that is slightly
        # shorter. Report the common range, matching cmproj_apply_cuts.py.
        if len(tracks) > 1 and tracks[1].get("medias"):
            second = float(tracks[1]["medias"][0]["duration"]) / per_ms
            source = min(source, second)
        return rate, source, 1
    except (KeyError, IndexError, TypeError, ValueError):
        return None, None, None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", help="native .cmproj package (read-only)")
    ap.add_argument("--trec", help="or a standalone .trec file")
    ap.add_argument("--out", help="analysis MP4 path")
    ap.add_argument("--target-db", type=float, default=-28.0,
                    help="target mean level for the analysis copy (default -28 dB)")
    ap.add_argument("--max-gain-db", type=float, default=24.0)
    ap.add_argument("--probe-only", action="store_true",
                    help="report level and streams without writing anything")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    if bool(args.project) == bool(args.trec):
        sys.exit("Choose exactly one of --project or --trec")
    if not args.probe_only and not args.out:
        sys.exit("--out is required unless --probe-only is set")

    ffmpeg, ffprobe = tool("ffmpeg"), tool("ffprobe")
    data = None
    if args.project:
        project = Path(args.project).expanduser().resolve()
        trec, data = locate_trec(project)
    else:
        trec = Path(args.trec).expanduser().resolve()
    if not trec.is_file():
        sys.exit("Recording not found: %s" % trec)

    streams, fmt = probe_streams(ffprobe, trec)
    vi, ai, codec = pick_streams(streams)
    src_db = measure_mean_db(ffmpeg, trec, ai)
    src_s = float(fmt.get("duration") or 0)

    print("[probe] %s" % trec)
    print("[probe] streams: video=%d (%s), audio=%d | duration=%.3fs | mean=%.1f dB"
          % (vi, codec, ai, src_s, src_db))

    gain = max(0.0, min(args.max_gain_db, args.target_db - src_db))
    gain = round(gain * 2) / 2.0  # half-dB steps, reproducible
    print("[probe] analysis gain = +%.1f dB -> expected mean %.1f dB" % (gain, src_db + gain))

    if data:
        rate, source_ms, count = project_timing(data)
        if rate:
            print("[probe] project editRate=%s | track0 clips=%s | source=%.3fs"
                  % (int(rate), count, (source_ms or 0) / 1000.0))
        if count is not None and count != 1:
            print("[warn] track 0 already has %d clips. This skill expects a freshly "
                  "recorded single-clip project; downstream cmproj_apply_cuts.py will "
                  "refuse it." % count)
        if source_ms and abs(source_ms / 1000.0 - src_s) > 1.0:
            print("[warn] project duration and .trec duration differ by more than 1s")

    if args.probe_only:
        print("[ok] probe only, nothing written")
        return

    out = Path(args.out).expanduser().resolve()
    if out.exists() and not args.overwrite:
        sys.exit("Refusing to overwrite: %s" % out)
    out.parent.mkdir(parents=True, exist_ok=True)
    afilter = "volume=%.1fdB,alimiter=limit=0.95" % gain
    run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(trec), "-map", "0:%d" % vi, "-map", "0:%d" % ai,
         "-c:v", "copy", "-af", afilter, "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart", str(out)])

    out_streams, out_fmt = probe_streams(ffprobe, out)
    out_s = float(out_fmt.get("duration") or 0)
    delta_ms = (out_s - src_s) * 1000.0
    print("[ok] %s (%.1f MB)" % (out.name, out.stat().st_size / 1024 / 1024))
    print("[ok] analysis duration=%.3fs | delta vs .trec=%+.0f ms "
          "(AAC frame padding is expected)" % (out_s, delta_ms))

    manifest = {
        "schema_version": 1,
        "source_project": str(args.project) if args.project else None,
        "source_trec": str(trec),
        "analysis_mp4": str(out),
        "video_stream": vi,
        "video_codec": codec,
        "audio_stream": ai,
        "source_mean_db": round(src_db, 1),
        "gain_db": gain,
        "expected_mean_db": round(src_db + gain, 1),
        "source_duration_s": round(src_s, 6),
        "analysis_duration_s": round(out_s, 6),
        "duration_delta_ms": round(delta_ms, 3),
        "note": "Analysis copy only. The .cmproj and .trec are untouched; "
                "the final Camtasia project keeps using the original in-package media.",
    }
    manifest_path = out.with_name(out.stem + "_manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[ok] manifest: %s" % manifest_path.name)
    print("[next] run video-editor cut --dry-run on %s" % out.name)


if __name__ == "__main__":
    main()
