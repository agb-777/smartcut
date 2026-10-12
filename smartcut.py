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
from contextlib import nullcontext

try:
    from rich import box
    from rich.console import Console
    from rich.markup import escape
    from rich.panel import Panel
    from rich.progress import (BarColumn, Progress, TaskProgressColumn,
                               TextColumn, TimeElapsedColumn,
                               TimeRemainingColumn)
    from rich.prompt import Confirm
    from rich.table import Table
except ImportError:
    print("[ERROR] module 'rich' not found, install with: pip install rich",
          file=sys.stderr)
    sys.exit(1)

EPS = 0.0005
CRF = 18
PRESET = "medium"
AUDIO_BITRATE = "192k"

console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)

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
    err_console.print(f"[bold red]ERROR[/] {escape(msg)}", soft_wrap=True)
    sys.exit(1)


def info(msg):
    console.print(f"[bold cyan]INFO[/]  {escape(msg)}", soft_wrap=True)


def step(msg):
    console.print(f"[bold magenta]>>[/]    {escape(msg)}", soft_wrap=True)


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


def sec(x):
    return f"{x:.6f}"


def run(cmd, capture=False, spinner=None):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors="replace")

    ctx = console.status(spinner, spinner="dots") if spinner else nullcontext()
    try:
        with ctx:
            out, err = proc.communicate()
    except BaseException:
        proc.kill()
        proc.wait()
        raise

    if proc.returncode != 0:
        tail = "\n".join(err.strip().splitlines()[-15:])
        die(f"command failed: {' '.join(cmd)}\n{tail}")
    return out if capture else None


def run_ffmpeg(cmd, total):
    full = [cmd[0], "-nostdin", "-progress", "pipe:1", "-nostats", *cmd[1:]]
    errf = tempfile.TemporaryFile(mode="w+", errors="replace")
    p = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=errf,
                         text=True, errors="replace")

    progress = Progress(
        TextColumn("     "),
        BarColumn(bar_width=28),
        TaskProgressColumn(),
        TextColumn("[dim]{task.fields[speed]:>7}[/]"),
        TextColumn("[dim]elapsed[/]"),
        TimeElapsedColumn(),
        TextColumn("[dim]ETA[/]"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )

    try:
        with progress:
            task = progress.add_task("", total=total, speed="")
            for line in p.stdout:
                key, _, val = line.strip().partition("=")
                if key in ("out_time_us", "out_time_ms"):
                    try:
                        cur = max(0.0, int(val) / 1e6)
                    except ValueError:
                        continue
                    progress.update(task, completed=min(total, cur))
                elif key == "speed":
                    try:
                        x = float(val.strip().rstrip("x"))
                        progress.update(task, speed=f"{x:.2f}x" if x < 10 else f"{x:.0f}x")
                    except ValueError:
                        progress.update(task, speed="")
                elif key == "progress" and val == "end":
                    progress.update(task, completed=total)
            p.wait()
    except BaseException:
        p.kill()
        p.wait()
        raise

    errf.seek(0)
    err = errf.read()
    errf.close()
    if p.returncode != 0:
        tail = "\n".join(err.strip().splitlines()[-15:])
        die(f"command failed: {' '.join(cmd)}\n{tail}")


def ffmpeg(*args, label, total, out=None):
    step(label)
    run_ffmpeg(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], total)
    if out and (not os.path.exists(out) or os.path.getsize(out) == 0):
        die(f"ffmpeg produced an empty segment ({os.path.basename(out)}). "
            "Seeking in this input is probably unreliable (common with .ts/mpegts). "
            "Remux it first, e.g.: ffmpeg -i input.ts -c copy input.mkv")


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


def print_plan(start, end, plan):
    table = Table(title="Smartcut Plan", box=box.ROUNDED, title_style="bold",
                  caption=f"requested {fmt_time(start)} - {fmt_time(end)}",
                  caption_style="dim")
    table.add_column("Segment", justify="center", style="bold")
    table.add_column("Mode")
    table.add_column("Range")
    table.add_column("Duration", justify="right")

    s_eff, e_eff = plan["s_eff"], plan["e_eff"]
    k1, k2 = plan["k1"], plan["k2"]
    if plan["a_len"] > EPS:
        table.add_row("A", "[yellow]re-encode[/]",
                      f"{fmt_time(s_eff)} - {fmt_time(k1)}", f"{plan['a_len']:.3f}s")
    table.add_row("B", "[green]copy[/]",
                  f"{fmt_time(k1)} - {fmt_time(k2)}", f"{plan['b_len']:.3f}s")
    if plan["c_len"] > EPS:
        table.add_row("C", "[yellow]re-encode[/]",
                      f"{fmt_time(k2)} - {fmt_time(e_eff)}", f"{plan['c_len']:.3f}s")
    console.print(table)


def print_summary(outp, out_dur, target, elapsed):
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("Output", escape(outp))
    grid.add_row("Duration", f"{fmt_time(out_dur)} [dim](target {fmt_time(target)})[/]")
    grid.add_row("Size", fmt_size(outp))
    grid.add_row("Time", f"{elapsed:.1f}s")
    console.print(Panel(grid, title="[bold green]Done[/]", border_style="green",
                        expand=False))


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

    try:
        tmp = tempfile.mkdtemp(prefix=".smartcut_", dir=os.path.dirname(outp))
    except OSError as e:
        die(f"cannot write to output directory: {e}")
    t_start = time.time()
    try:
        segments = []

        if plan is None:
            s_eff, e_eff = start, (min(end, duration) if duration else end)
            ts = os.path.join(tmp, "full.ts")
            ffmpeg("-noautorotate", "-ss", sec(max(0, s_eff - EPS)), "-i", inp,
                   "-t", sec(e_eff - s_eff), "-map", vmap, "-an", *enc,
                   "-fps_mode", "passthrough", "-f", "mpegts", ts,
                   label="Re-encoding the whole range...", total=e_eff - s_eff, out=ts)
            segments.append((ts, None))
        else:
            s_eff, e_eff = plan["s_eff"], plan["e_eff"]
            k1, k2 = plan["k1"], plan["k2"]
            a_len, c_len = plan["a_len"], plan["c_len"]
            print_plan(start, end, plan)

            if a_len > EPS:
                ts_a = os.path.join(tmp, "a.ts")
                ffmpeg("-noautorotate", "-ss", sec(max(0, s_eff - EPS)), "-i", inp,
                       "-t", sec(a_len), "-map", vmap, "-an", *enc,
                       "-fps_mode", "passthrough", "-f", "mpegts", ts_a,
                       label=f"[A] Re-encoding {a_len:.3f}s (start to first keyframe)...",
                       total=a_len, out=ts_a)
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
                   total=plan["b_len"], out=ts_b)
            segments.append((ts_b, plan["b_len"]))

            if c_len > EPS:
                ts_c = os.path.join(tmp, "c.ts")
                ffmpeg("-noautorotate", "-ss", sec(max(0, k2 - EPS)), "-i", inp,
                       "-t", sec(c_len), "-map", vmap, "-an", *enc,
                       "-fps_mode", "passthrough", "-f", "mpegts", ts_c,
                       label=f"[C] Re-encoding {c_len:.3f}s (last keyframe to end)...",
                       total=c_len, out=ts_c)
                segments.append((ts_c, c_len))

        list_path = os.path.join(tmp, "list.txt")
        with open(list_path, "w") as f:
            for path, dur in segments:
                f.write("file '{}'\n".format(path.replace("'", "'\\''")))
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
        print_summary(outp, float(out_dur), e_eff - s_eff, time.time() - t_start)
    except ValueError:
        info(f"Done -> {outp} (total {time.time() - t_start:.1f}s)")


PROG = os.path.basename(sys.argv[0]) or "smartcut.py"

HELP_TEXT = f"""\
Usage: {PROG} [OPTION]... -s TIME -e TIME FILE

  -s, --start=TIME     start time, HH:MM:SS[.mmm]
  -e, --end=TIME       end time, HH:MM:SS[.mmm]
  -o, --output=FILE    output file (.mp4, .mkv or .mov)
  -h, --help           display this help and exit

Examples:
  {PROG} video.mp4 -s 00:01:30 -e 00:05:45
  {PROG} video.mkv -s 00:00:10.5 -e 00:00:42 -o clip.mkv
"""


class HelpAction(argparse.Action):
    def __init__(self, option_strings, dest=argparse.SUPPRESS,
                 default=argparse.SUPPRESS, help=None):
        super().__init__(option_strings, dest=dest, default=default,
                         nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        sys.stdout.write(HELP_TEXT)
        parser.exit()


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, f"{self.prog}: {message}\n"
                     f"Try '{self.prog} --help' for more information.\n")


def main():
    ap = Parser(prog=PROG, add_help=False)
    ap.add_argument("input")
    ap.add_argument("-s", "--start", type=parse_time, required=True)
    ap.add_argument("-e", "--end", type=parse_time, required=True)
    ap.add_argument("-o", "--output")
    ap.add_argument("-h", "--help", action=HelpAction)
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

    for sig_name in ("SIGTERM", "SIGHUP"):  # SIGHUP is missing on Windows
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            signal.signal(sig, on_signal)
    try:
        if os.path.exists(outp):
            if os.path.samefile(outp, inp):
                die("output must not be the same file as input")
            if not sys.stdin.isatty():
                die(f"output file already exists: {outp}")
            if not Confirm.ask(f"[bold yellow]WARN[/]  {escape(outp)} already exists. Overwrite?",
                               default=False, console=console):
                die("aborted")
        smartcut(inp, outp, args.start, args.end)
    except KeyboardInterrupt:
        console.print()
        die("interrupted")


if __name__ == "__main__":
    main()
