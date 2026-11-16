#!/usr/bin/env python3
"""并行分段下载直链 mp4 并合并校验 (支持字节级实时进度、速度/ETA 计算与中途取消)。

用法:
  python3 download_mp4.py <mp4_url> <out_path> [--segments 4] [--rate 1024k]
"""
import concurrent.futures, os, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CURL_BIN = "curl.exe" if os.name == "nt" else "curl"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36" if os.name == "nt" else
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
REFERER = "https://ft.nuaa.edu.cn/"
MAX_RETRY = 3


def head_size(url):
    """curl -sI 拿 Content-Length(有些 CDN HEAD 返回 HTML, 需要过滤)。"""
    for _ in range(3):
        out = subprocess.run(
            [CURL_BIN, "-sI", "--max-time", "20", "-H", f"Referer: {REFERER}",
             "-H", f"User-Agent: {UA}", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
        for line in out.splitlines():
            if line.lower().startswith("content-length:"):
                return int(line.split(":", 1)[1].strip())
    raise RuntimeError("HEAD failed to get Content-Length")


def dl_range(url, start, end, tmp, seg_i, rate=None, cancel_event=None):
    out = os.path.join(tmp, f"part_{seg_i}.mp4")
    expected = end - start + 1
    cmd = [CURL_BIN, "-sL", "--max-time", "5400", "--retry", str(MAX_RETRY),
           "-H", f"Referer: {REFERER}", "-H", f"User-Agent: {UA}",
           "-r", f"{start}-{end}", url, "-o", out]
    if rate:
        cmd.extend(["--limit-rate", rate])
    for _ in range(MAX_RETRY):
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("下载任务已取消")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            while proc.poll() is None:
                if cancel_event and cancel_event.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        proc.kill()
                    raise RuntimeError("下载任务已取消")
                time.sleep(0.25)
        finally:
            if proc.poll() is None:
                proc.kill()
        if proc.returncode == 0 and os.path.exists(out) and os.path.getsize(out) == expected:
            return out
    raise RuntimeError(f"分段 {seg_i + 1} 下载失败")


def download_mp4_file(url, out_path, nseg=4, rate=None, progress_cb=None, detail_cb=None, cancel_event=None):
    """下载直链 MP4 并合并校验。

    - progress_cb(done_segs, total_segs, msg): 兼容简单回调
    - detail_cb(dict): 每 0.4s 实时推送字节级进度:
        {
          "stage": "head" | "downloading" | "merging" | "done",
          "downloaded_bytes": int,
          "total_bytes": int,
          "pct": float,
          "speed_bps": float,
          "eta_sec": float | None,
          "seg_pcts": list[float],
          "message": str
        }
    """
    if ".." in str(out_path):
        raise SystemExit(f"拒绝包含 .. 的输出路径: {out_path}")
    out_p = Path(os.path.expanduser(str(out_path))).resolve()
    out_allowed = [Path.home().resolve(), Path.cwd().resolve()]
    if not any(out_p == d or out_p.is_relative_to(d) for d in out_allowed):
        raise SystemExit(f"输出路径必须在用户主目录或当前目录之下: {out_path}")
    os.makedirs(out_p.parent, exist_ok=True)
    out_path = str(out_p)

    if detail_cb:
        detail_cb({
            "stage": "head",
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "pct": 0.0,
            "speed_bps": 0.0,
            "eta_sec": None,
            "seg_pcts": [0.0] * nseg,
            "message": "阶段 2/4: 正在连接视频服务器探测文件大小 (HEAD)..."
        })

    total = head_size(url)
    msg = f"视频总大小: {total/1048576:.1f} MB ({total} 字节)"
    print(msg)
    if progress_cb:
        progress_cb(0, nseg, msg)

    have = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    if have >= total:
        print("file already complete")
        if progress_cb:
            progress_cb(nseg, nseg, "文件已存在且完整, 跳过下载")
        if detail_cb:
            detail_cb({
                "stage": "done",
                "downloaded_bytes": total,
                "total_bytes": total,
                "pct": 100.0,
                "speed_bps": 0.0,
                "eta_sec": 0,
                "seg_pcts": [100.0] * nseg,
                "message": "文件已存在且完整, 跳过下载"
            })
    else:
        seg_size = (total + nseg - 1) // nseg
        tmp = tempfile.mkdtemp(prefix="nuaa-dl-")
        ranges = [(i * seg_size, min((i + 1) * seg_size - 1, total - 1)) for i in range(nseg)]
        part_paths = [os.path.join(tmp, f"part_{i}.mp4") for i in range(nseg)]
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=nseg) as ex:
                futures = [
                    ex.submit(dl_range, url, s, e, tmp, i, rate, cancel_event)
                    for i, (s, e) in enumerate(ranges)
                ]
                prev_bytes = 0
                prev_time = time.time()
                ema_speed = 0.0
                last_log_time = 0.0

                while not all(f.done() for f in futures):
                    if cancel_event and cancel_event.is_set():
                        break
                    time.sleep(0.4)
                    now = time.time()
                    dt = max(0.05, now - prev_time)

                    seg_bytes = []
                    seg_pcts = []
                    for i, (s, e) in enumerate(ranges):
                        p_file = part_paths[i]
                        exp_len = e - s + 1
                        cur_b = os.path.getsize(p_file) if os.path.exists(p_file) else 0
                        cur_b = min(cur_b, exp_len)
                        seg_bytes.append(cur_b)
                        seg_pcts.append(round(cur_b * 100.0 / max(1, exp_len), 1))

                    dl_b = sum(seg_bytes)
                    inst_speed = max(0.0, (dl_b - prev_bytes) / dt)
                    ema_speed = inst_speed if ema_speed == 0.0 else (0.35 * inst_speed + 0.65 * ema_speed)
                    prev_bytes = dl_b
                    prev_time = now

                    pct = min(99.5, (dl_b * 100.0) / max(1, total))
                    eta = int((total - dl_b) / ema_speed) if ema_speed > 1024 else None

                    if detail_cb:
                        detail_cb({
                            "stage": "downloading",
                            "downloaded_bytes": dl_b,
                            "total_bytes": total,
                            "pct": round(pct, 1),
                            "speed_bps": ema_speed,
                            "eta_sec": eta,
                            "seg_pcts": seg_pcts,
                            "message": f"阶段 3/4: 正在 {nseg} 线程并行下载 ({dl_b/1048576:.1f} / {total/1048576:.1f} MB)"
                        })

                    if now - last_log_time >= 3.5 and progress_cb:
                        last_log_time = now
                        spd_mb = ema_speed / 1048576.0
                        eta_str = f"{eta//60:02d}:{eta%60:02d}" if eta is not None else "--:--"
                        progress_cb(
                            sum(1 for f in futures if f.done()),
                            nseg,
                            f"已下载 {dl_b/1048576:.1f}/{total/1048576:.1f} MB ({pct:.1f}%) | 速度 {spd_mb:.2f} MB/s | 剩余 {eta_str}"
                        )

                # 检查所有 future 是否抛错
                for f in futures:
                    f.result()

            if detail_cb:
                detail_cb({
                    "stage": "merging",
                    "downloaded_bytes": total,
                    "total_bytes": total,
                    "pct": 99.8,
                    "speed_bps": 0.0,
                    "eta_sec": 1,
                    "seg_pcts": [100.0] * nseg,
                    "message": "阶段 4/4: 分段下载完成, 正在合并写入 MP4 文件..."
                })

            parts = sorted(os.listdir(tmp), key=lambda x: int(x.split("_")[1].split(".")[0]))
            with open(out_path, "wb") as out:
                for p in parts:
                    with open(os.path.join(tmp, p), "rb") as src:
                        shutil.copyfileobj(src, out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    final = os.path.getsize(out_path)
    print(f"merged {out_path}: {final} bytes ({final/1048576:.0f} MB) "
          f"| complete: {final == total}")
    if detail_cb:
        detail_cb({
            "stage": "done",
            "downloaded_bytes": final,
            "total_bytes": total,
            "pct": 100.0,
            "speed_bps": 0.0,
            "eta_sec": 0,
            "seg_pcts": [100.0] * nseg,
            "message": f"下载并合并完成 ({final/1048576:.1f} MB)"
        })
    duration_sec = None
    try:
        dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=noprint_wrappers=1:nokey=1", out_path],
                             capture_output=True, text=True)
        if dur.returncode == 0 and dur.stdout.strip():
            secs = float(dur.stdout.strip())
            duration_sec = secs
            print(f"duration: {int(secs//60)}:{int(secs%60):02d} "
                  f"({secs:.1f}s) | size {final/1048576:.0f} MB")
        else:
            print("ffprobe unavailable/not a media file; size check only")
    except (FileNotFoundError, OSError, ValueError):
        print("ffprobe unavailable/not a media file; size check only")
    return {"out_path": out_path, "size": final, "total": total,
            "complete": final == total, "duration": duration_sec}


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    url, out_path = sys.argv[1], sys.argv[2]
    nseg = 4
    if "--segments" in sys.argv:
        nseg = int(sys.argv[sys.argv.index("--segments") + 1])
    rate = None
    if "--rate" in sys.argv:
        rate = sys.argv[sys.argv.index("--rate") + 1]
    download_mp4_file(url, out_path, nseg=nseg, rate=rate)


if __name__ == "__main__":
    main()
