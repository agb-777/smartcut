#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

EPS = 0.0005
CRF = 18
PRESET = "medium"
AUDIO_BITRATE = "192k"

H264_PROFILES = {
    "baseline": "baseline",
    "constrained baseline": "baseline",
    "main": "main",
    "high": "high",
    "high 10": "high10",
    "high 4:2:2": "high422",
    "high 4:4:4 predictive": "high444",
}
H265_PROFILES = {
    "main": "main",
    "main 10": "main10",
    "main still picture": "main",
}


def on_signal(signum, frame):
    raise KeyboardInterrupt


def die(msg):
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(1)


def info(msg):
    print(f"[INFO] {msg}")


def parse_time(value):
    m = re.fullmatch(r"(\d+):([0-5]?\d):([0-5]?\d(?:\.\d+)?)", value.strip())
    if not m:
        raise argparse.ArgumentTypeError(
            f"invalid time format '{value}', use HH:MM:SS (e.g. 00:01:30)"
        )
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def fmt_time(t):
    t = round(max(0.0, t), 3)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def fmt_size(path):
    try:
        n = os.path.getsize(path)
    except OSError:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_eta(seconds):
    seconds = int(max(0, seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def sec(x):
    return f"{x:.6f}"


def run(cmd, capture=False, spinner=None):
    t0 = time.time()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, errors="replace")
    except FileNotFoundError:
        die(f"command not found: {cmd[0]}")
    frames = "|/-\\"
    i = 0
    while True:
        try:
            out, err = proc.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if spinner and sys.stdout.isatty():
                sys.stdout.write(f"\r      {frames[i % 4]} {spinner} {time.time() - t0:.0f}s")
                sys.stdout.flush()
                i += 1
    if spinner and sys.stdout.isatty():
        sys.stdout.write("\r" + " " * 70 + "\r")
        sys.stdout.flush()
    if proc.returncode != 0:
        tail = "\n".join(err.strip().splitlines()[-15:])
        die(f"command failed: {' '.join(cmd)}\n{tail}")
    return out if capture else None


def run_ffmpeg(cmd, total=None):
    full = [cmd[0], "-nostdin", "-progress", "pipe:1", "-nostats", *cmd[1:]]
    errf = tempfile.TemporaryFile(mode="w+", errors="replace")
    try:
        p = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=errf,
                             text=True, errors="replace")
    except FileNotFoundError:
        die(f"command not found: {cmd[0]}")

    tty = sys.stdout.isatty()
    t0 = time.time()
    cur, speed, next_mark = 0.0, "", 10
    width = 24

    def render(final=False):
        nonlocal next_mark
        if not total or total <= 0:
            return
        pct = 100.0 if final else min(99.9, max(0.0, cur / total * 100))
        elapsed = time.time() - t0
        eta = elapsed * (total - cur) / cur if cur > 0 and not final else 0
        filled = int(width * pct / 100)
        bar = "#" * filled + "-" * (width - filled)
        extra = f"  speed {speed}" if speed and speed != "N/A" else ""
        text = f"      [{bar}] {pct:5.1f}%{extra}  ETA {fmt_eta(eta)}"
        if tty:
            sys.stdout.write("\r" + text.ljust(79))
            sys.stdout.flush()
        elif pct >= next_mark or final:
            print(text)
            next_mark = (int(pct) // 10 + 1) * 10

    try:
        for line in p.stdout:
            key, _, val = line.strip().partition("=")
            if key in ("out_time_us", "out_time_ms"):
                try:
                    cur = max(0.0, int(val) / 1e6)
                except ValueError:
                    continue
            elif key == "speed":
                speed = val.strip()
            elif key == "progress":
                render(final=(val == "end"))
        p.wait()
    except BaseException:
        p.kill()
        p.wait()
        raise
    if tty and total:
        sys.stdout.write("\n")
        sys.stdout.flush()
    errf.seek(0)
    err = errf.read()
    errf.close()
    if p.returncode != 0:
        tail = "\n".join(err.strip().splitlines()[-15:])
        die(f"command failed: {' '.join(cmd)}\n{tail}")


def ffmpeg(*args, label=None, total=None):
    if label:
        info(label)
    run_ffmpeg(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], total)


def probe(path):
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", path], capture=True)
    data = json.loads(out)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"
                  and s.get("disposition", {}).get("attached_pic", 0) == 0), None)
    if video is None:
        die("no video stream found in this file")
    has_audio = any(s["codec_type"] == "audio" for s in data["streams"])
    fmt = data["format"]
    duration = float(fmt.get("duration", 0) or 0)
    start_time = float(fmt.get("start_time", 0) or 0)
    return video, has_audio, duration, start_time


def get_rotation(video):
    for sd in video.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                return int(round(float(sd["rotation"])))
            except (TypeError, ValueError):
                return 0
    try:
        return -int(video.get("tags", {}).get("rotate", 0))
    except (TypeError, ValueError):
        return 0


def get_packets(path, stream_index, start_offset):
    out = run(["ffprobe", "-v", "error", "-select_streams", str(stream_index),
               "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", path],
              capture=True, spinner="Reading keyframes...")
    pkts = []
    for line in out.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        try:
            pts = float(parts[0]) - start_offset
        except ValueError:
            return None
        pkts.append((pts, "K" in parts[1]))
    return pkts or None


def closed_keyframe_indices(pkts):
    n = len(pkts)
    prefix_max = [float("-inf")] * n
    m = float("-inf")
    for i in range(n):
        prefix_max[i] = m
        m = max(m, pkts[i][0])
    suffix_min = [float("inf")] * n
    m = float("inf")
    for i in range(n - 1, -1, -1):
        suffix_min[i] = m
        m = min(m, pkts[i][0])
    return [i for i in range(n)
            if pkts[i][1] and prefix_max[i] < pkts[i][0] < suffix_min[i]]


def build_encoder_args(v):
    codec = v.get("codec_name")
    profile = (v.get("profile") or "").lower()
    pix_fmt = v.get("pix_fmt")
    level = v.get("level")
    args = []

    if codec == "h264":
        args += ["-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF)]
        if profile in H264_PROFILES:
            args += ["-profile:v", H264_PROFILES[profile]]
        if isinstance(level, int) and level > 0:
            args += ["-level:v", f"{level // 10}.{level % 10}"]
        args += ["-x264-params", "open-gop=0"]
    elif codec == "hevc":
        args += ["-c:v", "libx265", "-preset", PRESET, "-crf", str(CRF)]
        x265 = ["open-gop=0", "repeat-headers=1", "log-level=error"]
        if profile in H265_PROFILES:
            x265.append(f"profile={H265_PROFILES[profile]}")
        args += ["-x265-params", ":".join(x265)]
    else:
        return None

    if pix_fmt:
        args += ["-pix_fmt", pix_fmt]
    for key, opt in (("color_space", "-colorspace"),
                     ("color_primaries", "-color_primaries"),
                     ("color_transfer", "-color_trc"),
                     ("color_range", "-color_range")):
        val = v.get(key)
        if val and val != "unknown":
            args += [opt, val]
    return args


def fallback_encoder_args(v):
    return ["-c:v", "libx264", "-preset", PRESET, "-crf", str(CRF),
            "-pix_fmt", v.get("pix_fmt") or "yuv420p"]


def make_plan(pkts, start, end):
    pts_sorted = sorted(p for p, _ in pkts)
    if len(pts_sorted) < 2:
        return None
    diffs = sorted(b - a for a, b in zip(pts_sorted, pts_sorted[1:]) if b > a)
    frame_dur = diffs[len(diffs) // 2] if diffs else 0.04

    s_eff = next((p for p in pts_sorted if p >= start - EPS), None)
    if s_eff is None:
        return None
    e_eff = next((p for p in pts_sorted if p >= end - EPS), None)
    to_eof = e_eff is None
    if to_eof:
        e_eff = pts_sorted[-1] + frame_dur
    if e_eff - s_eff < frame_dur * 0.5:
        return None

    closed = closed_keyframe_indices(pkts)
    if not closed:
        return None

    i1 = next((i for i in closed if pkts[i][0] >= s_eff - EPS), None)
    if i1 is None:
        return None
    k1 = pkts[i1][0]

    if to_eof:
        k2 = e_eff
        b_packets = None
        c_len = 0.0
    else:
        i2 = next((i for i in reversed(closed) if pkts[i][0] <= e_eff + EPS), None)
        if i2 is None or pkts[i2][0] - k1 < frame_dur * 1.5 or i2 <= i1:
            return None
        k2 = pkts[i2][0]
        b_packets = i2 - i1
        c_len = e_eff - k2

    if k1 >= e_eff - EPS:
        return None

    return dict(s_eff=s_eff, e_eff=e_eff, k1=k1, k2=k2,
                a_len=max(0.0, k1 - s_eff), b_len=k2 - k1,
                c_len=max(0.0, c_len), b_packets=b_packets)


def smartcut(inp, outp, start, end):
    video, has_audio, duration, fstart = probe(inp)
    codec = video.get("codec_name")
    vmap = f"0:{video['index']}"
    rotation = get_rotation(video)

    ext = os.path.splitext(outp)[1].lower()
    if ext not in (".mp4", ".mkv", ".mov"):
        die("output extension must be .mp4, .mkv, or .mov")
    if duration and start >= duration:
        die(f"start ({fmt_time(start)}) is beyond the video duration ({fmt_time(duration)})")
    if start >= end:
        die("start time must be earlier than end time")
    if duration and end > duration:
        info(f"End exceeds video duration, clamped to {fmt_time(duration)}")
    enc = build_encoder_args(video)
    smart_ok = enc is not None
    if enc is None:
        info(f"Codec '{codec}' is not supported by smartcut, falling back to full re-encode (libx264)")
        enc = fallback_encoder_args(video)

    pkts = None
    if smart_ok:
        info("Reading frame/keyframe list...")
        pkts = get_packets(inp, video["index"], fstart)
        if pkts is None:
            smart_ok = False
            info("Incomplete packet timestamps, falling back to full re-encode")

    plan = None
    if smart_ok:
        plan = make_plan(pkts, start, end)
        if plan is None:
            info("Range too short or no suitable closed keyframe, re-encoding this range only")

    tmp = tempfile.mkdtemp(prefix=".smartcut_", dir=os.path.dirname(outp))
    t_start = time.time()
    try:
        segments = []

        if plan is None:
            s_eff, e_eff = start, (min(end, duration) if duration else end)
            ts = os.path.join(tmp, "full.ts")
            ffmpeg("-noautorotate", "-ss", sec(max(0, s_eff - EPS)), "-i", inp,
                   "-t", sec(e_eff - s_eff), "-map", vmap, "-an", *enc,
                   "-fps_mode", "passthrough", "-f", "mpegts", ts,
                   label="Re-encoding the whole range...", total=e_eff - s_eff)
            segments.append((ts, None))
        else:
            s_eff, e_eff = plan["s_eff"], plan["e_eff"]
            k1, k2 = plan["k1"], plan["k2"]
            a_len, c_len = plan["a_len"], plan["c_len"]
            info(f"Start : {fmt_time(start)} (nearest frame: {fmt_time(s_eff)})")
            info(f"End   : {fmt_time(end)} (nearest frame: {fmt_time(e_eff)})")
            info(f"Segment A (re-encode): {a_len:.3f}s | B (copy): {plan['b_len']:.3f}s | "
                 f"C (re-encode): {c_len:.3f}s")

            if a_len > EPS:
                ts_a = os.path.join(tmp, "a.ts")
                ffmpeg("-noautorotate", "-ss", sec(max(0, s_eff - EPS)), "-i", inp,
                       "-t", sec(a_len), "-map", vmap, "-an", *enc,
                       "-fps_mode", "passthrough", "-f", "mpegts", ts_a,
                       label=f"[A] Re-encoding {a_len:.3f}s (start to first keyframe)...",
                       total=a_len)
                segments.append((ts_a, a_len))

            ts_b = os.path.join(tmp, "b.ts")
            b_cmd = ["-ss", sec(k1 + EPS), "-i", inp, "-map", vmap, "-an"]
            if plan["b_packets"] is not None:
                b_cmd += ["-frames:v", str(plan["b_packets"])]
            bsf = {"h264": "h264_mp4toannexb", "hevc": "hevc_mp4toannexb"}.get(codec)
            b_cmd += ["-c:v", "copy"]
            if bsf:
                b_cmd += ["-bsf:v", bsf]
            b_cmd += ["-avoid_negative_ts", "make_zero", "-f", "mpegts", ts_b]
            ffmpeg(*b_cmd, label=f"[B] Copying {plan['b_len']:.3f}s without re-encode...",
                   total=plan["b_len"])
            segments.append((ts_b, plan["b_len"]))

            if c_len > EPS:
                ts_c = os.path.join(tmp, "c.ts")
                ffmpeg("-noautorotate", "-ss", sec(max(0, k2 - EPS)), "-i", inp,
                       "-t", sec(c_len), "-map", vmap, "-an", *enc,
                       "-fps_mode", "passthrough", "-f", "mpegts", ts_c,
                       label=f"[C] Re-encoding {c_len:.3f}s (last keyframe to end)...",
                       total=c_len)
                segments.append((ts_c, c_len))

        list_path = os.path.join(tmp, "list.txt")
        with open(list_path, "w") as f:
            for path, dur in segments:
                f.write(f"file '{path}'\n")
                if dur is not None:
                    f.write(f"duration {dur:.6f}\n")

        audio_path = None
        if has_audio:
            audio_path = os.path.join(tmp, "audio.mkv")
            ffmpeg("-ss", sec(max(0, s_eff)), "-i", inp, "-t", sec(e_eff - s_eff),
                   "-map", "0:a", "-vn", "-sn", "-c:a", "aac", "-b:a", AUDIO_BITRATE,
                   audio_path, label="Re-encoding audio (AAC)...", total=e_eff - s_eff)

        mux = []
        if rotation:
            mux += ["-display_rotation", str(rotation)]
        mux += ["-f", "concat", "-safe", "0", "-i", list_path]
        if audio_path:
            mux += ["-i", audio_path]
        mux += ["-map", "0:v:0"]
        if audio_path:
            mux += ["-map", "1:a"]
        mux += ["-c", "copy"]
        if ext in (".mp4", ".mov"):
            if codec == "hevc":
                mux += ["-tag:v", "hvc1"]
            mux += ["-movflags", "+faststart"]
        out_tmp = os.path.join(tmp, "output" + ext)
        mux += ["-avoid_negative_ts", "make_zero", out_tmp]
        ffmpeg(*mux, label="Merging all segments...", total=e_eff - s_eff)
        os.replace(out_tmp, outp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    out_dur = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                   "-of", "csv=p=0", outp], capture=True).strip()
    try:
        info(f"Output duration: {fmt_time(float(out_dur))} "
             f"(target {fmt_time(e_eff - s_eff)}), size {fmt_size(outp)}")
    except ValueError:
        pass
    info(f"Done -> {outp} (total {time.time() - t_start:.1f}s)")


def main():
    ap = argparse.ArgumentParser(
        description="Frame-accurate video cutting with ffmpeg using the smartcut method."
    )
    ap.add_argument("input", help="input video file")
    ap.add_argument("-s", "--start", type=parse_time, required=True,
                    help="start time, HH:MM:SS (e.g. 00:01:30)")
    ap.add_argument("-e", "--end", type=parse_time, required=True,
                    help="end time, HH:MM:SS (e.g. 00:05:45)")
    ap.add_argument("-o", "--output",
                    help="output file (.mp4/.mkv/.mov), default: <name>_cut<ext>")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            die(f"{tool} not found in PATH, please install ffmpeg")
    if not os.path.isfile(args.input):
        die(f"file not found: {args.input}")

    base, ext = os.path.splitext(args.input)
    out_ext = ext if ext.lower() in (".mp4", ".mkv", ".mov") else ".mp4"
    outp = os.path.abspath(args.output or f"{base}_cut{out_ext}")
    inp = os.path.abspath(args.input)
    if not os.path.isdir(os.path.dirname(outp)):
        die(f"output directory does not exist: {os.path.dirname(outp)}")
    if os.path.exists(outp):
        if os.path.samefile(outp, inp):
            die("output must not be the same file as input")
        if not sys.stdin.isatty():
            die(f"output file already exists: {outp}")
        if input(f"[WARN] {outp} already exists. Overwrite? [y/N]: ").strip().lower() != "y":
            die("aborted")

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)
    try:
        smartcut(inp, outp, args.start, args.end)
    except KeyboardInterrupt:
        print()
        die("interrupted")


if __name__ == "__main__":
    main()
