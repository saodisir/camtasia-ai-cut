#!/usr/bin/env python3
"""Acceptance gate for a video-editor edit plan.

Run this AFTER `cut --dry-run --semantic-plan` and BEFORE feeding
`kept_intervals` to any downstream project generator.

It answers two questions:
  1. How many Agent-written semantic edits survived the skill's validator?
  2. Did pause compression swallow any sentence the Agent decided to keep?

Primary verdict uses AUDIO ENERGY coverage, not ASR timestamps. Bailian's
word-level timestamps are linearly interpolated across each sentence, so a
time-only check reports ~45% for a sentence whose speech is actually 99%
intact. When audio_16k.wav is present, we measure how much of each sentence's
real signal energy falls inside kept_intervals.

Very short utterances ("嗯", "呃", "这那", "再见", ...) are reported separately
and excluded from the gate: removing them is exactly what pause compression
is for. Use --min-chars to change that boundary.

The plan file is never modified. Exit code 1 means the plan failed the gate.

Usage:
  /usr/bin/python3 verify_edit_plan.py \
      --context  <dir>/semantic_context.json \
      --plan     <dir>/<name>_edit_plan.json \
      --semantic-plan <dir>/semantic_plan.json
"""

import argparse
import array
import json
import sys
import wave
from pathlib import Path

PUNCTUATION = "，。、！？!?…,.;:：；\"'“”‘’()（） \t\n"


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def bare_text(text):
    return "".join(ch for ch in text if ch not in PUNCTUATION)


def energy_profile(audio_path, window_ms=10):
    """Return (samples_per_window, per-window signal energy)."""
    with wave.open(str(audio_path), "rb") as handle:
        rate = handle.getframerate()
        pcm = array.array("h")
        pcm.frombytes(handle.readframes(handle.getnframes()))
    size = max(1, int(rate * window_ms / 1000))
    energy = []
    for offset in range(0, len(pcm), size):
        chunk = pcm[offset:offset + size]
        energy.append(sum(value * value for value in chunk))
    return window_ms, energy


def energy_coverage(energy, window_ms, begin_ms, end_ms, kept):
    lo = max(0, int(begin_ms / window_ms))
    hi = min(len(energy), int(end_ms / window_ms) + 1)
    total = sum(energy[lo:hi])
    if total <= 0:
        return 100.0
    held = 0.0
    for index in range(lo, hi):
        center = (index + 0.5) * window_ms
        if any(start <= center <= finish for start, finish in kept):
            held += energy[index]
    return 100.0 * held / total


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--context", required=True, help="semantic_context.json")
    ap.add_argument("--plan", required=True, help="*_edit_plan.json")
    ap.add_argument("--semantic-plan", help="Agent-written semantic_plan.json (optional)")
    ap.add_argument("--audio", help="audio_16k.wav (default: next to the plan)")
    ap.add_argument("--min-chars", type=int, default=4,
                    help="utterances shorter than this are brief sounds, excluded from the gate")
    ap.add_argument("--min-energy", type=float, default=85.0,
                    help="minimum audio-energy coverage %% for a substantive sentence")
    ap.add_argument("--fail-under", type=float, default=50.0,
                    help="time-coverage threshold used only when no audio is available")
    args = ap.parse_args()

    context = load(args.context)
    plan = load(args.plan)
    kept = plan["kept_intervals"]
    atoms = context["atoms"]
    order = [a["id"] for a in atoms]

    print("=== plan overview ===")
    original = plan["original_duration_s"]
    new = plan["new_duration_s"]
    print("  %.1fs -> %.1fs  (removed %.1fs, %.1f%%)" %
          (original, new, original - new, 100 * (1 - new / original)))
    print("  pause cuts %d | merged cuts %d | kept clips %d" %
          (len(plan["pause_cuts"]), len(plan["merged_cut_intervals"]), len(kept)))
    durations = [(b - a) / 1000 for a, b in kept]
    print("  clip length: min %.3fs / max %.3fs / mean %.2fs" %
          (min(durations), max(durations), sum(durations) / len(durations)))

    removed_ids = set()
    if args.semantic_plan:
        agent_plan = load(args.semantic_plan)
        positions = {key: index for index, key in enumerate(order)}
        for edit in agent_plan.get("edits", []):
            first = edit.get("remove_start_id")
            last = edit.get("remove_end_id")
            if first in positions and last in positions:
                removed_ids.update(order[positions[first]:positions[last] + 1])
        print("  semantic edits accepted: %d / %d" %
              (len(plan["semantic_cuts"]), len(agent_plan.get("edits", []))))
        print("  Agent-marked removed atoms: %d" % len(removed_ids))

    audio_path = Path(args.audio) if args.audio else Path(args.plan).parent / "audio_16k.wav"
    use_energy = audio_path.exists()
    if use_energy:
        window_ms, energy = energy_profile(audio_path)
        print("\n=== acceptance: AUDIO ENERGY coverage (ASR timestamps are interpolated) ===")
    else:
        window_ms, energy = None, None
        print("\n=== acceptance: TIME coverage (no audio_16k.wav found) ===")

    substantive, brief, failures = 0, 0, []
    time_low = []
    for atom in atoms:
        if atom["id"] in removed_ids:
            continue
        begin, finish = atom["start"] * 1000, atom["end"] * 1000
        overlap = sum(max(0, min(finish, b) - max(begin, a)) for a, b in kept)
        time_pct = 100 * overlap / (finish - begin) if finish > begin else 100.0
        label = atom["text"][:24]
        if len(bare_text(atom["text"])) < args.min_chars:
            brief += 1
            continue
        substantive += 1
        if use_energy:
            verdict = energy_coverage(energy, window_ms, begin, finish, kept)
            if verdict < args.min_energy:
                failures.append((atom["id"], time_pct, round(verdict, 1), label))
        else:
            verdict = time_pct
            if verdict < args.fail_under:
                failures.append((atom["id"], time_pct, round(verdict, 1), label))
        if time_pct < 50:
            time_low.append((atom["id"], round(time_pct, 1),
                             round(verdict, 1), label))

    print("  substantive sentences checked: %d" % substantive)
    print("  brief sounds excluded (<=%d chars): %d" % (args.min_chars - 1, brief))
    if use_energy:
        print("  gate: energy coverage >= %.0f%%" % args.min_energy)
        print("  sentences whose TIME coverage looks low but speech is intact: %d" %
              len(time_low))
        for ident, time_pct, energy_pct, label in time_low[:10]:
            print("    %s  time %5.1f%% / energy %5.1f%%  %s" %
                  (ident, time_pct, energy_pct, label))
    if failures:
        print("  FAILED:")
        for ident, time_pct, verdict, label in failures[:20]:
            print("    %s  time %5.1f%% / verdict %5.1f%%  %s" %
                  (ident, time_pct, verdict, label))
        if len(failures) > 20:
            print("    ... and %d more" % (len(failures) - 20))
        print("\n[FAIL] plan did not pass the acceptance gate")
        return 1
    print("\n[PASS] every substantive sentence kept its speech")
    return 0


if __name__ == "__main__":
    sys.exit(main())
