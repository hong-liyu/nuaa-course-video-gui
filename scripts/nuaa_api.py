#!/usr/bin/env python3
"""飞天云课堂 API 封装 (使用 qr_login.py 产出的 state 文件里的 jwt)。

用法:
  python3 nuaa_api.py courses [关键词]         # 列出本学期全部课程(找 teclId 用), 支持子串过滤
  python3 nuaa_api.py list <teclId>            # 列出该课堂下的全部录像(id/时间/教师)
  python3 nuaa_api.py url <courseId>           # 取某节课的 mp4 播放地址(带 auth_key)
  python3 nuaa_api.py refresh <courseId>       # 同上(播放地址过期时重新签发)
  python3 nuaa_api.py subtitle <courseId> [out.srt]  # 拉 AI 字幕并转 SRT(无字幕则报错)
  python3 nuaa_api.py subtitles-all <teclId> [out_dir] # 批量拉取该课程全部课次字幕并生成复习文稿

state 文件(默认系统临时目录/nuaa-skill-state.json): {"jwt": ..., "tenant": ...}
"""
import ipaddress, json, os, re, socket, subprocess, sys, tempfile, urllib.parse, urllib.request
from pathlib import Path

BASE = "https://ft.nuaa.edu.cn/jy-application-vod-he"
TMP_DIR = "/tmp" if os.name != "nt" and os.path.isdir("/tmp") else tempfile.gettempdir()
STATE_DEFAULT = os.path.join(TMP_DIR, "nuaa-skill-state.json")
VENV_DIR = os.path.expanduser("~/.cache/nuaa-course-video/venv")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36" if os.name == "nt" else
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HERE = os.path.dirname(os.path.abspath(__file__))


def _venv_python():
    if os.name == "nt":
        return os.path.join(VENV_DIR, "Scripts", "python.exe")
    return os.path.join(VENV_DIR, "bin", "python3")


def bootstrap_venv():
    try:
        from Crypto.Cipher import AES  # noqa: F401
        return
    except ImportError:
        pass
    vpy = _venv_python()
    if not os.path.exists(vpy):
        os.makedirs(os.path.dirname(VENV_DIR), exist_ok=True)
        subprocess.run([sys.executable, "-m", "venv", VENV_DIR], check=True)
    subprocess.run([vpy, "-m", "pip", "install", "-q", "pycryptodome"], check=True)
    if os.name == "nt":
        sys.exit(subprocess.call([vpy] + sys.argv))
    os.execv(vpy, [vpy] + sys.argv)


bootstrap_venv()
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, HERE)
from nuaa_aes import encrypt_quoted  # noqa: E402


def load_state(state_path):
    """--state 来自 argv: 拒绝上跳目录, 限制在系统临时目录、用户主目录或当前目录内。"""
    if ".." in state_path:
        raise SystemExit(f"拒绝包含 .. 的 state 路径: {state_path}")
    p = Path(os.path.expanduser(state_path)).resolve()
    allowed = [Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve(),
               Path.home().resolve(), Path.cwd().resolve()]
    if os.name != "nt":
        allowed.append(Path("/private/tmp"))
    if not any(p == d or p.is_relative_to(d) for d in allowed):
        raise SystemExit(f"state 路径必须在系统临时目录、用户主目录或当前目录之下: {state_path}")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _check_id(value, kind="id"):
    """课程/课次 id 都是纯数字; argv 注入在这里统一拦下。"""
    if not re.fullmatch(r"[0-9]{4,15}", str(value)):
        raise SystemExit(f"{kind} 必须是数字 id: {value}")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向: 重定向目标可能绕过协议/主机校验。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
_ALLOWED_HOST = "ft.nuaa.edu.cn"


def _guard_url(url):
    """SSRF 防护: 仅 https + 域名白名单; 环回/链路本地/组播/保留地址一律阻断。

    私网(RFC1918)地址放行: 校内 DNS 会把 ft.nuaa.edu.cn 解析到校园网内网地址,
    校外解析为公网地址, 两种情况都属正常访问。"""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != _ALLOWED_HOST:
        raise ValueError(f"unexpected api url: {url}")
    for info in socket.getaddrinfo(parts.hostname, 443, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            raise ValueError(f"blocked unsafe resolved address: {ip}")


def call(path_qs, state):
    if not path_qs.startswith("/v1/"):  # 只允许本平台 API 路径前缀
        raise ValueError(f"unexpected api path: {path_qs[:60]}")
    url = BASE + path_qs  # BASE 是常量 https 主机
    _guard_url(url)
    req = urllib.request.Request(url, headers={
        "jwt-token": state["jwt"],
        "tenantid": state.get("tenant", "RBAC"),
        "User-Agent": UA,
    })
    with _OPENER.open(req, timeout=30) as r:
        return json.loads(r.read().decode())


def fetch_courses(state, keyword=""):
    """获取本学期全部课程列表(供 GUI 和 CLI 共用)。"""
    data = call("/v1/group_subject_vod_list?page.pageIndex=1&page.pageSize=1000", state)
    recs = (data.get("data") or {}).get("records") or []
    if keyword:
        kw = keyword.lower()
        recs = [r for r in recs if kw in (r.get("subjName") or "").lower()
                or kw in (r.get("teclName") or "").lower()
                or kw in ", ".join(r.get("teacNames") or []).lower()
                or kw == str(r.get("teclId") or "")]
    return recs


def cmd_courses(keyword, state):
    """列出本学期的全部课程(点播分组列表), 只知道课名时用它找 teclId。

    课程发现走 /v1/group_subject_vod_list(学生视角"我的课程"分组)。
    注意 /v1/myself/curriculum 是教师侧接口, 学生账号调用返回 500, 别用。"""
    all_recs = fetch_courses(state, "")
    if not all_recs:
        print("没有课程记录(登录态可能已失效, 重新扫码)")
        sys.exit(1)
    recs = fetch_courses(state, keyword) if keyword else all_recs
    if not recs:
        print(f"没有匹配「{keyword}」的课程; 不带关键词运行 courses 可看本学期全部课程")
        sys.exit(1)
    for r in recs:
        print(f"teclId={r['teclId']} | {r.get('subjName')} | "
              f"{', '.join(r.get('teacNames') or [])} | 班级 {r.get('teclName')} | "
              f"录像 {r.get('courTimes')} 次")


# courses 子命令前置分发(无路径数据流; list/url/subtitle 仍走文件尾的主分发)
if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "courses":
    _sp = STATE_DEFAULT
    if "--state" in sys.argv:
        _sp = sys.argv[sys.argv.index("--state") + 1]
    _kw = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else ""
    cmd_courses(_kw, load_state(_sp))
    sys.exit(0)


def fetch_lectures(tecl_id, state):
    """获取指定课堂(teclId)下的所有课次录像列表。"""
    tecl_id = _check_id(tecl_id, "teclId")
    qs = (f"/v1/subject_vod_list?page.pageIndex=1&page.pageSize=1000"
          f"&teclIds={tecl_id}%20%20%20%20%20%20"
          f"&page.orders%5B0%5D.asc=true&page.orders%5B0%5D.field=courBeginTime")
    data = call(qs, state)
    return (data.get("data") or {}).get("records") or []


def cmd_list(tecl_id, state):
    for r in fetch_lectures(tecl_id, state):
        print(f"id={r['id']} | {r.get('courBeginTime')} ~ {r.get('courEndTime')} | "
              f"{', '.join(r.get('teacNames') or [])} | "
              f"teclId={r.get('teclId')} | vodStatus={r.get('vodStatus')}")


def fetch_vod_info(course_id, state):
    """获取指定课次(courseId)的播放直链与课程信息字典。"""
    course_id = _check_id(course_id, "courseId")
    qs = f"/v1/course_vod_urls?courseId={encrypt_quoted(str(course_id))}"
    data = call(qs, state)
    return data.get("data") or {}


def cmd_url(course_id, state):
    info = fetch_vod_info(course_id, state)
    views = info.get("courseVodViewList") or []
    if not views:
        print("no playable url; response:", json.dumps(info, ensure_ascii=False)[:300])
        return
    print(f"course: {info.get('courName')} | {info.get('tecName')} | "
          f"{info.get('classRoomName')} | vodDownloadEnable={info.get('vodDownloadEnable')}")
    for v in views:
        print(v["url"])


def _ms_to_srt_ts(ms):
    h, rem = divmod(int(ms), 3600000)
    m, rem = divmod(rem, 60000)
    s, ms2 = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms2:03d}"


def _safe_out_path(course_id, out_path):
    """规范化并校验输出路径(本机 CLI, 但限制在用户目录内)。

    - 默认文件名剥离路径分隔符(course_id 来自 argv);
    - 原始输入显式拒绝上跳目录片段;
    - 最终路径必须位于用户主目录或当前工作目录之下。"""
    cid = re.sub(r"[^0-9A-Za-z_-]", "_", str(course_id))
    if not out_path:
        return os.path.abspath(f"{cid}.srt")
    if ".." in out_path:
        raise SystemExit(f"拒绝包含 .. 的输出路径: {out_path}")
    allowed = [Path.home().resolve(), Path.cwd().resolve()]
    p = Path(os.path.expanduser(out_path)).resolve()
    if not any(p == d or p.is_relative_to(d) for d in allowed):
        raise SystemExit(f"输出路径必须在用户主目录或当前目录之下: {out_path}")
    os.makedirs(p.parent, exist_ok=True)
    return str(p)


def _entries_to_clean_md(entries, title="", interval_ms=180000):
    """将毫秒字幕条目合并为按固定时间间隔(默认3分钟)锚点的复习 Markdown 段落。"""
    lines = []
    if title:
        lines.append(f"# {title}\n")
    bucket = []
    bucket_start = None
    for e in entries:
        txt = (e.get("res") or "").strip()
        if not txt:
            continue
        bg = int(e.get("bg", 0))
        if bucket_start is None:
            bucket_start = bg
        elif bg - bucket_start >= interval_ms and bucket:
            h, rem = divmod(bucket_start // 1000, 3600)
            m, s = divmod(rem, 60)
            ts_str = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
            lines.append(f"**[{ts_str}]** " + " ".join(bucket) + "\n")
            bucket = []
            bucket_start = bg
        bucket.append(txt)
    if bucket and bucket_start is not None:
        h, rem = divmod(bucket_start // 1000, 3600)
        m, s = divmod(rem, 60)
        ts_str = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        lines.append(f"**[{ts_str}]** " + " ".join(bucket) + "\n")
    return "\n".join(lines)


def fetch_subtitle_entries(course_id, state):
    """拉取指定课次(courseId)的字幕列表并按毫秒起始时间排序。"""
    course_id = _check_id(course_id, "courseId")
    data = call(f"/v1/course_vod_subtitle?courseId={course_id}", state)
    entries = data.get("data") or []
    entries.sort(key=lambda e: e.get("bg", 0))
    return entries


def export_lecture_md(course_id, state, md_path, title="", interval_ms=180000):
    """导出单课字幕为清洗后的 Markdown 复习文稿 (不生成 .srt)。

    返回 (条目数量, 覆盖分钟数, md_text)。若无字幕则返回 (0, 0.0, "")。"""
    entries = fetch_subtitle_entries(course_id, state)
    if not entries:
        return 0, 0.0, ""
    md_text = _entries_to_clean_md(entries, title=title, interval_ms=interval_ms)
    if md_path:
        md_p = _safe_out_path(course_id, str(md_path))
        with open(md_p, "w", encoding="utf-8") as f:
            f.write(md_text)
    span = (entries[-1]["ed"] - entries[0]["bg"]) / 60000.0
    return len(entries), span, md_text


def export_lecture_subtitle(course_id, state, srt_path, md_path=None, title="", interval_ms=180000):
    """导出单课字幕为 SRT 及可选的清洗后 Markdown 复习文稿。

    返回 (条目数量, 覆盖分钟数, md_text)。若无字幕则返回 (0, 0.0, "")。"""
    entries = fetch_subtitle_entries(course_id, state)
    if not entries:
        return 0, 0.0, ""
    srt_p = _safe_out_path(course_id, str(srt_path))
    with open(srt_p, "w", encoding="utf-8-sig") as f:
        for i, e in enumerate(entries, 1):
            f.write(f"{i}\n{_ms_to_srt_ts(e['bg'])} --> {_ms_to_srt_ts(e['ed'])}\n"
                    f"{e.get('res', '').strip()}\n\n")
    md_text = _entries_to_clean_md(entries, title=title, interval_ms=interval_ms)
    if md_path:
        md_p = _safe_out_path(course_id, str(md_path))
        with open(md_p, "w", encoding="utf-8") as f:
            f.write(md_text)
    span = (entries[-1]["ed"] - entries[0]["bg"]) / 60000.0
    return len(entries), span, md_text


def cmd_subtitle(course_id, state, out_path=None):
    """拉取 AI 字幕(条目: bg/ed 毫秒时间轴 + res 文本)并转成 SRT。

    平台未生成字幕时接口返回 data:[](空), 此时打印提示并以退出码 1 结束。"""
    out_p = _safe_out_path(course_id, out_path)
    count, span, _ = export_lecture_subtitle(course_id, state, out_p)
    if not count:
        print(f"courseId {course_id}: 平台上还没有字幕数据 (AI 转写可能未生成), "
              f"可过几天重试")
        sys.exit(1)
    print(f"courseId {course_id}: {count} 条字幕 -> {out_p} "
          f"(覆盖约 {span:.0f} 分钟)")


def cmd_subtitles_all(tecl_id, state, out_dir=None):
    """批量拉取某门课(teclId)下全部课次的字幕, 保存 SRT + 单节复习文稿 + 全课汇总文稿。"""
    tecl_id = _check_id(tecl_id, "teclId")
    target_dir = out_dir or f"subtitles_{tecl_id}"
    if ".." in target_dir:
        raise SystemExit(f"拒绝包含 .. 的输出目录: {target_dir}")
    allowed = [Path.home().resolve(), Path.cwd().resolve()]
    dir_p = Path(os.path.expanduser(target_dir)).resolve()
    if not any(dir_p == d or dir_p.is_relative_to(d) for d in allowed):
        raise SystemExit(f"输出目录必须在用户主目录或当前目录之下: {target_dir}")
    os.makedirs(dir_p, exist_ok=True)

    qs = (f"/v1/subject_vod_list?page.pageIndex=1&page.pageSize=1000"
          f"&teclIds={tecl_id}%20%20%20%20%20%20"
          f"&page.orders%5B0%5D.asc=true&page.orders%5B0%5D.field=courBeginTime")
    records = (call(qs, state).get("data") or {}).get("records") or []
    if not records:
        print(f"teclId {tecl_id}: 未找到任何课次记录")
        sys.exit(1)

    combined_md = [f"# 课程全套字幕复习汇总 (teclId={tecl_id})\n"]
    ok_count = 0
    empty_count = 0
    for idx, r in enumerate(records, 1):
        cid = str(r["id"])
        begin = (r.get("courBeginTime") or "").replace(":", "-").replace(" ", "_")
        end_t = (r.get("courEndTime") or "").split(" ")[-1]
        subj = r.get("subjName") or r.get("courName") or ""
        teachers = ", ".join(r.get("teacNames") or [])
        safe_begin = re.sub(r"[^0-9A-Za-z_-]", "_", begin)
        stem = f"{idx:02d}_{safe_begin}_{cid}"

        data = call(f"/v1/course_vod_subtitle?courseId={cid}", state)
        entries = data.get("data") or []
        if not entries:
            print(f"[{idx:02d}/{len(records)}] id={cid} ({r.get('courBeginTime')}): 暂无字幕, 跳过")
            empty_count += 1
            continue

        entries.sort(key=lambda e: e.get("bg", 0))
        title = f"第 {idx:02d} 讲 | {r.get('courBeginTime')} ~ {end_t} | {subj} {teachers}".strip()
        md_text = _entries_to_clean_md(entries, title=title)
        md_path = dir_p / f"{stem}.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_text)

        combined_md.append(md_text + "\n---\n")
        span = (entries[-1]["ed"] - entries[0]["bg"]) / 60000
        print(f"[{idx:02d}/{len(records)}] id={cid} ({r.get('courBeginTime')}): "
              f"{len(entries)} 条 (~{span:.0f} 分钟) -> {md_path.name}")
        ok_count += 1

    if ok_count > 0:
        summary_path = dir_p / "00_全课复习文稿汇总.md"
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write("\n".join(combined_md))
        print(f"\n完成! 成功导出 {ok_count} 节课字幕 (跳过无字幕 {empty_count} 节)")
        print(f"目录: {dir_p}")
        print(f"汇总复习文稿: {summary_path}")
    else:
        print(f"\n该课程共 {len(records)} 节课, 但平台均未生成 AI 字幕。")


if __name__ == "__main__":
    valid_cmds = ("list", "url", "refresh", "subtitle", "subtitles-all")
    if len(sys.argv) < 3 or sys.argv[1] not in valid_cmds:
        print(__doc__)
        sys.exit(2)
    state_path = STATE_DEFAULT
    if "--state" in sys.argv:
        state_path = sys.argv[sys.argv.index("--state") + 1]
    pos_args = []
    skip_next = False
    for a in sys.argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if a == "--state":
            skip_next = True
            continue
        pos_args.append(a)
    state = load_state(state_path)
    cmd = pos_args[0]
    arg = pos_args[1]
    opt_out = pos_args[2] if len(pos_args) > 2 else None
    if cmd == "list":
        cmd_list(arg, state)
    elif cmd == "subtitle":
        cmd_subtitle(arg, state, opt_out)
    elif cmd == "subtitles-all":
        cmd_subtitles_all(arg, state, opt_out)
    else:
        cmd_url(arg, state)