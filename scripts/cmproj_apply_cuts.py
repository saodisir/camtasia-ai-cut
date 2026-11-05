#!/usr/bin/env python3
"""Final stage of camtasia-ai-cut: turn a video-editor plan into a Camtasia project.

The official `video-editor` skill produces <name>_edit_plan.json with
`kept_intervals` in source milliseconds. This script copies a NATIVE Camtasia
.cmproj package and rewrites only the copy's project.tscproj so the timeline
holds one clip per retained interval. The source package is never modified,
and the final project keeps using the original in-package .trec media.

Camtasia 2026.0.7 native format (verified against two real saved projects):
  - .cmproj is a DIRECTORY; project.tscproj at its root is plain JSON
  - version "10.0", editRate 705600000 ticks per second
  - media paths are relative and in-package: ./media/... ./recordings/...

Two source layouts exist; the script detects them automatically.

  group : track 0 = one Group containing VMFile / ScreenVMFile / UnifiedMedia,
          all other tracks empty. Seen on a freshly recorded 34s sample.
          The Group is a WINDOW onto its own full internal timeline. Real-machine
          verification on 2026-11-05 settled it: Group.mediaStart carries the
          source offset, and the inner medias stay byte-identical.
          `--offset-mode inner` produced wrong frames; diagnosis only.
          Each Group is ~491.8 KB, so hundreds of clips make a huge project.

  flat  : track 0 = one ScreenVMFile (picture), track 1 = one UnifiedMedia
          (VMFile + AMFile), other tracks empty. Seen when a .trec is imported
          into a project and saved. Each clip pair is only ~2.8 KB, so long
          recordings stay small. Real-machine verification on 2026-11-05
          settled it: UnifiedMedia is a FLAT CONTAINER, not a window. Its
          inner VMFile/AMFile must each carry the source offset, while
          UnifiedMedia.mediaStart stays 0. This is the OPPOSITE of Group mode.
            inner : CORRECT, verified in Camtasia (default)
            outer : UnifiedMedia.mediaStart = source offset, inner untouched.
                    Camtasia refuses to open the project (OSStatus -19,
                    "Corrupt Project Opened"). Diagnosis only.
          The ScreenVMFile on track 0 always carries mediaStart itself; it has
          no inner medias.

Safety rules:
  - the source .cmproj must be an unedited single-clip project of either layout
  - the plan's original_duration_s must match the source duration
  - the output path must not already exist

Usage:
  /usr/bin/python3 cmproj_apply_cuts.py \
      --source "/path/to/Recording.cmproj" \
      --out "/path/to/AI初剪.cmproj" \
      --edit-plan "/path/to/work/分析源_edit_plan.json"
"""

import argparse
import json
import shutil
import sys
from copy import deepcopy
from pathlib import Path

VIDEO_TYPES = ("ScreenVMFile", "VMFile")


def normalize(pairs):
    cleaned = []
    for pair in pairs:
        a, b = float(pair[0]), float(pair[1])
        if b > a:
            cleaned.append((a, b))
    cleaned.sort()
    if not cleaned:
        raise RuntimeError("No retained ranges")
    return cleaned


def source_shape(data):
    """Detect the source layout. Return a dict describing templates and timing."""
    try:
        tracks = data["timeline"]["sceneTrack"]["scenes"][0]["csml"]["tracks"]
    except (KeyError, IndexError, TypeError):
        sys.exit("Unexpected project.tscproj structure; expected timeline/sceneTrack/scenes[0]/csml/tracks")
    if not tracks:
        sys.exit("No timeline tracks found")
    rate = float(data["editRate"])
    per_ms = rate / 1000.0
    first = tracks[0].get("medias", [])
    other_counts = [len(t.get("medias", [])) for t in tracks[1:]]

    if len(first) == 1 and first[0].get("_type") == "Group" and not any(other_counts):
        group = first[0]
        total = float(group["duration"])
        return {
            "shape": "group", "rate": rate, "per_ms": per_ms,
            "templates": {"group": group},
            "source_ms": total / per_ms,
            "track_totals": {"group": total},
        }

    if (
        len(first) == 1
        and first[0].get("_type") in VIDEO_TYPES
        and len(tracks) >= 2
        and other_counts[0] == 1
        and tracks[1]["medias"][0].get("_type") == "UnifiedMedia"
        and not any(other_counts[1:])
    ):
        video = first[0]
        unified = tracks[1]["medias"][0]
        vtotal = float(video["duration"])
        utotal = float(unified["duration"])
        return {
            "shape": "flat", "rate": rate, "per_ms": per_ms,
            "templates": {"video": video, "unified": unified},
            # The audio track is 66.7 ms shorter than the picture track on
            # real .trec imports. Use the COMMON range as the source duration:
            # any plan tail beyond it is AAC padding from the analysis copy and
            # has no media in this project, so ranges_from_plan drops it.
            "source_ms": min(vtotal, utotal) / per_ms,
            "track_totals": {"video": vtotal, "unified": utotal},
        }

    sys.exit(
        "Unsupported source layout: track 0 has %d clips (%s), other tracks hold %s. "
        "Expected either one Group with empty other tracks, or one ScreenVMFile "
        "plus one UnifiedMedia on track 1. This bridge only accepts unedited "
        "single-clip Camtasia projects."
        % (len(first), first[0].get("_type") if first else "none", other_counts)
    )


def ranges_from_plan(plan_path, source_end_ms):
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    pairs = plan.get("kept_intervals")
    if not isinstance(pairs, list) or not pairs:
        sys.exit("edit plan has no kept_intervals")

    plan_ms = float(plan.get("original_duration_s", 0)) * 1000.0
    if plan_ms <= 0:
        sys.exit("edit plan has no original_duration_s")
    if abs(plan_ms - source_end_ms) > 1000.0:
        sys.exit(
            "This plan belongs to a different recording: plan %.3fs vs project %.3fs. "
            "Re-run prepare_analysis.py and video-editor against this .cmproj."
            % (plan_ms / 1000.0, source_end_ms / 1000.0)
        )

    ranges = normalize(pairs)
    over = ranges[-1][1] - source_end_ms
    if over > 1000:
        sys.exit("plan ends at %.1f ms but source media is only %.1f ms"
                 % (ranges[-1][1], source_end_ms))
    if over > 0:
        # AAC frame padding makes the analysis MP4 a few ms longer than the
        # in-package .trec. Clamp the tail in memory; never edit the plan file.
        print("[clamp] plan tail exceeds source by %.0f ms; trimming in memory only" % over)
        ranges = [(a, min(b, source_end_ms)) for a, b in ranges if a < source_end_ms]
    return ranges


def ranges_from_cut(cut, source_end_ms):
    a, b = cut
    if b > source_end_ms:
        sys.exit("--cut end exceeds source duration (%.1f ms)" % source_end_ms)
    out = []
    if a > 0:
        out.append((0.0, a))
    if b < source_end_ms:
        out.append((b, source_end_ms))
    return normalize(out)


def build_group_clips(template, ranges, ticks_at, to_ticks, offset_mode, next_id):
    clips = []
    for i, (a, _b) in enumerate(ranges):
        g = deepcopy(template)
        g["id"] = next_id
        next_id += 1
        g["start"] = ticks_at[i]
        dur = ticks_at[i + 1] - ticks_at[i]
        g["duration"] = dur
        g["scalar"] = 1
        src_start = to_ticks(a)

        if offset_mode == "group":
            # Group = window onto its own FULL internal timeline.
            g["mediaStart"] = src_start
            g["mediaDuration"] = dur
            for inner in g.get("tracks", []):
                for m in inner.get("medias", []):
                    m["id"] = next_id
                    next_id += 1
                    # inner timing intentionally untouched
        else:
            # Diagnostic only: known to produce wrong frames in Camtasia.
            g["mediaStart"] = 0
            g["mediaDuration"] = dur
            for inner in g.get("tracks", []):
                for m in inner.get("medias", []):
                    m["id"] = next_id
                    next_id += 1
                    orig_total = float(m.get("mediaDuration") or dur)
                    src = min(float(src_start), orig_total)
                    usable = min(float(dur), max(0.0, orig_total - src))
                    m["start"] = 0
                    m["duration"] = int(usable)
                    m["mediaStart"] = int(src)
                    m["mediaDuration"] = int(usable)
        clips.append(g)
    return clips, next_id


def build_flat_clips(templates, totals, ranges, ticks_at, to_ticks, flat_mode, next_id):
    videos, unifieds = [], []
    video_total = totals["video"]
    unified_total = totals["unified"]

    for i, (a, _b) in enumerate(ranges):
        start = ticks_at[i]
        dur = ticks_at[i + 1] - ticks_at[i]
        src_start = to_ticks(a)

        v = deepcopy(templates["video"])
        v["id"] = next_id
        next_id += 1
        v["start"] = start
        v["duration"] = dur
        v["scalar"] = 1
        v["mediaStart"] = src_start
        v["mediaDuration"] = int(min(dur, max(0.0, video_total - src_start)))
        videos.append(v)

        u = deepcopy(templates["unified"])
        u["id"] = next_id
        next_id += 1
        u["start"] = start
        u["duration"] = dur
        u["scalar"] = 1
        u_usable = int(min(dur, max(0.0, unified_total - src_start)))
        if flat_mode == "outer":
            # DIAGNOSTIC ONLY: treating UnifiedMedia as a window leaves its
            # inner medias far longer than the container. Camtasia reports
            # OSStatus -19 / "Corrupt Project Opened" on such projects.
            u["mediaStart"] = src_start
            u["mediaDuration"] = u_usable
            for key in ("video", "audio"):
                m = u.get(key)
                if isinstance(m, dict):
                    m["id"] = next_id
                    next_id += 1
                    # inner timing intentionally untouched
        else:
            # VERIFIED: UnifiedMedia is a flat container. Its inner VMFile and
            # AMFile are independent media definitions and must each read
            # their own slice of the source.
            u["mediaStart"] = 0
            u["mediaDuration"] = dur
            for key in ("video", "audio"):
                m = u.get(key)
                if isinstance(m, dict):
                    m["id"] = next_id
                    next_id += 1
                    inner_total = float(m.get("mediaDuration") or unified_total)
                    inner_src = min(float(src_start), inner_total)
                    inner_usable = int(min(dur, max(0.0, inner_total - inner_src)))
                    m["start"] = 0
                    m["duration"] = inner_usable
                    m["mediaStart"] = int(inner_src)
                    m["mediaDuration"] = inner_usable
        unifieds.append(u)
    return videos, unifieds, next_id


def apply(source, out, ranges, title, shape, offset_mode, flat_mode):
    if out.exists():
        sys.exit("Refusing to overwrite: %s" % out)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, out)
    print("[ok] package copied: %s" % out)

    project = out / "project.tscproj"
    d = json.loads(project.read_text(encoding="utf-8"))
    rate = float(d["editRate"])
    scale = rate / 1000.0

    def to_ticks(ms):
        return int(round(ms * scale))

    tracks = d["timeline"]["sceneTrack"]["scenes"][0]["csml"]["tracks"]
    templates = {
        "group": tracks[0]["medias"][0],
    } if shape["shape"] == "group" else {
        "video": tracks[0]["medias"][0],
        "unified": tracks[1]["medias"][0],
    }
    print("[probe] version=%s | editRate=%d | layout=%s | flat_offset=%s"
          % (d.get("version"), int(rate), shape["shape"], flat_mode))

    # Cumulative ticks keep start[i] + duration[i] == start[i+1] exactly.
    cum = [0.0]
    for a, b in ranges:
        cum.append(cum[-1] + (b - a))
    ticks_at = [to_ticks(c) for c in cum]

    next_id = 1000
    if shape["shape"] == "group":
        clips, next_id = build_group_clips(
            templates["group"], ranges, ticks_at, to_ticks, offset_mode, next_id)
        tracks[0]["medias"] = clips
        by_track = {"track0": clips}
    else:
        videos, unifieds, next_id = build_flat_clips(
            templates, shape["track_totals"], ranges, ticks_at, to_ticks, flat_mode, next_id)
        tracks[0]["medias"] = videos
        tracks[1]["medias"] = unifieds
        by_track = {"track0": videos, "track1": unifieds}

    if len(ranges) <= 20:
        for i, (a, b) in enumerate(ranges):
            print("  clip %d: source %.1f-%.1f ms -> timeline %.3f-%.3fs"
                  % (i + 1, a, b, ticks_at[i] / rate, ticks_at[i + 1] / rate))

    total_s = cum[-1] / 1000.0
    d["title"] = title
    if isinstance(d.get("metadata"), dict):
        d["metadata"]["projectDuration"] = "%02d:%02d" % (int(total_s) // 60, int(total_s) % 60)

    project.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    print("[ok] project.tscproj rewritten in the COPY only (%.2f MB)"
          % (project.stat().st_size / 1024 / 1024))
    print("[ok] clips=%d | duration=%.3fs | removed=%.3fs"
          % (len(ranges), total_s, (shape["source_ms"] - cum[-1]) / 1000.0))
    return out, d, by_track, templates


def verify(out, d, by_track, ranges):
    print()
    print("=== static self-check ===")
    for name in ("project.tscproj", "docPrefs", "bookmarks.plist", "~project.tscproj"):
        print("  %-20s %s" % (name, "present" if (out / name).exists() else "MISSING"))
    print("  version=%s | editRate=%s | metadata.ProjectThumbnail=%s"
          % (d.get("version"), d.get("editRate"),
             "kept" if len(d.get("metadata", {}).get("ProjectThumbnail", "")) > 1000 else "MISSING"))

    missing = []
    for entry in d.get("sourceBin", []):
        src = str(entry.get("src", ""))
        rel = src[2:] if src.startswith("./") else src
        if not (out / rel).exists():
            missing.append(rel)
    print("  sourceBin entries=%d | missing media=%d" % (len(d.get("sourceBin", [])), len(missing)))
    for rel in missing:
        print("    MISSING", rel)

    ids, mismatch, seams, zero, track_bad = [], 0, 0, 0, 0
    for label, clips in by_track.items():
        if len(clips) != len(ranges):
            track_bad += 1
        seams += sum(1 for a, b in zip(clips, clips[1:])
                     if a["start"] + a["duration"] != b["start"])
        zero += sum(1 for c in clips if c["duration"] <= 0)
        for clip in clips:
            ids.append(clip["id"])
            if clip.get("duration") != clip.get("mediaDuration"):
                mismatch += 1
            for inner in clip.get("tracks", []):
                for m in inner.get("medias", []):
                    ids.append(m["id"])
                    if m.get("duration") != m.get("mediaDuration"):
                        mismatch += 1
            for key in ("video", "audio"):
                m = clip.get(key)
                if isinstance(m, dict) and "id" in m:
                    ids.append(m["id"])
        print("  %s clips=%d | broken seams=%d | zero-duration=%d"
              % (label, len(clips), seams, zero))
    print("  ids unique=%s | duration/mediaDuration mismatch=%d"
          % (len(ids) == len(set(ids)), mismatch))

    first_type = (d["timeline"]["sceneTrack"]["scenes"][0]["csml"]
                   ["tracks"][0]["medias"][0].get("_type"))
    layout = "group" if first_type == "Group" else "flat"
    # Real-machine verification on 2026-11-05: flat 260 clips opened fine;
    # group layout was only ever opened with 2 and 11 clips (and is huge).
    verified = 260 if layout == "flat" else 11
    if len(ranges) > verified:
        print("  [warn] %d clips per track exceeds the %d-clip %s scale verified "
              "in Camtasia (2026-11-05); open the copy and check before relying on it."
              % (len(ranges), verified, layout))

    bad = missing or seams or zero or mismatch or track_bad or len(ids) != len(set(ids))
    print("  [%s] static check" % ("FAIL" if bad else "PASS"))
    if bad:
        return 1
    print("  [next] open %s in Camtasia, then continue editing and export" % out.name)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="native .cmproj package (read-only)")
    ap.add_argument("--out", required=True, help="NEW package path, must not exist")
    ap.add_argument("--edit-plan", help="video-editor <name>_edit_plan.json")
    ap.add_argument("--cut", help="diagnostic cut START_MS,END_MS instead of a plan")
    ap.add_argument("--offset-mode", choices=["group", "inner"], default="group",
                    help="Group layout: offset placement (default: group, verified in Camtasia)")
    ap.add_argument("--flat-offset-mode", choices=["inner", "outer"], default="inner",
                    help="flat layout: where UnifiedMedia reads the source offset "
                         "(default: inner, verified in Camtasia). outer is known "
                         "to make Camtasia reject the project; diagnosis only.")
    ap.add_argument("--title", default="AI 初剪")
    args = ap.parse_args()

    if bool(args.cut) == bool(args.edit_plan):
        sys.exit("Choose exactly one of --edit-plan or --cut")

    source = Path(args.source).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    if not (source / "project.tscproj").is_file():
        sys.exit("Not a native .cmproj package: %s" % source)

    base = json.loads((source / "project.tscproj").read_text(encoding="utf-8"))
    shape = source_shape(base)
    print("[probe] layout=%s | source duration %.3fs"
          % (shape["shape"], shape["source_ms"] / 1000.0))

    if args.cut:
        try:
            a, b = (float(x) for x in args.cut.split(","))
        except ValueError:
            sys.exit("--cut expects START_MS,END_MS")
        if not 0 <= a < b:
            sys.exit("--cut expects 0 <= START < END")
        ranges = ranges_from_cut((a, b), shape["source_ms"])
        print("[probe] diagnostic cut %.0f-%.0f ms (NOT an AI decision)" % (a, b))
    else:
        plan_path = Path(args.edit_plan).expanduser().resolve()
        ranges = ranges_from_plan(plan_path, shape["source_ms"])
        print("[probe] plan: %d retained ranges" % len(ranges))

    out_path, d, by_track, _templates = apply(
        source, out, ranges, args.title, shape, args.offset_mode, args.flat_offset_mode)
    return verify(out_path, d, by_track, ranges)


if __name__ == "__main__":
    sys.exit(main())
