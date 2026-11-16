#!/usr/bin/env python3
"""NUAA 飞天云课堂 — 本地 Web 控制台 (专注：导出 .md 课堂字幕文稿 & 下载 MP4 原片)。

启动方式:
  python gui.py [--port 8765] [--no-browser]
或直接双击根目录下的 `start.bat` / `启动GUI.bat`。
"""
import datetime
import http.server
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

import nuaa_api  # noqa: E402
import download_mp4  # noqa: E402

STATE_DEFAULT = Path(nuaa_api.STATE_DEFAULT)
QR_PNG_DEFAULT = Path(nuaa_api.TMP_DIR) / "nuaa-qr.png"
QR_LOGIN_SCRIPT = str(SCRIPTS_DIR / "qr_login.py")
DEFAULT_OUT_ROOT = HERE / "output"
DATA_DIR = Path.home() / ".nuaa-course-video"
SETTINGS_FILE = DATA_DIR / "settings.json"
HISTORY_FILE = DATA_DIR / "history.json"


def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _open_path_in_os(path: Path):
    target = str(path.resolve())
    if not os.path.exists(target):
        raise FileNotFoundError(f"路径不存在: {target}")
    if os.name == "nt":
        os.startfile(target)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target])


class AppState:
    def __init__(self):
        self.lock = threading.Lock()
        settings = _read_json(SETTINGS_FILE, {})
        self.out_root = Path(settings.get("out_root") or DEFAULT_OUT_ROOT)
        self.interval_ms = settings.get("interval_ms", 180000)
        self.naming = settings.get("naming", "standard")
        self.notify = bool(settings.get("notify", False))
        self.history = _read_json(HISTORY_FILE, [])
        self.logs = []

        # Task & real-time telemetry state
        self.task_running = False
        self.task_type = ""          # "md" | "mp4" | ""
        self.task_title = "当前无运行任务"
        self.task_stage = "空闲"
        self.progress = 0.0
        self.queue_current = 0
        self.queue_total = 0
        self.downloaded_bytes = 0
        self.total_bytes = 0
        self.speed_bps = 0.0
        self.eta_sec = None
        self.seg_pcts = []
        self.cancel_event = threading.Event()
        self.task_result = {}
        self.retry_request = None

        # QR login state
        self.qr_proc = None
        self.qr_running = False
        self.qr_status = "idle"
        self.qr_hint = "点击「启动扫码登录」获取统一身份认证二维码"
        self.qr_png_path = QR_PNG_DEFAULT
        self.qr_updated_at = 0.0

        # Cached courses & lectures
        self.courses = []
        self.current_tecl_id = ""
        self.lectures = []

        self.log("系统初始化完成。欢迎使用 NUAA 飞天云课堂控制台！")

    def log(self, msg: str):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        with self.lock:
            self.logs.append(entry)
            if len(self.logs) > 400:
                self.logs = self.logs[-400:]
        print(entry, flush=True)

    def reset_telemetry(self, task_type="", title="当前无运行任务", stage="空闲"):
        with self.lock:
            self.task_type = task_type
            self.task_title = title
            self.task_stage = stage
            self.progress = 0.0
            self.queue_current = 0
            self.queue_total = 0
            self.downloaded_bytes = 0
            self.total_bytes = 0
            self.speed_bps = 0.0
            self.eta_sec = None
            self.seg_pcts = []

    def update_telemetry(self, **kwargs):
        with self.lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def cancel_active_task(self):
        if self.task_running:
            self.cancel_event.set()
            self.log("🛑 已发送取消信号，正在停止当前任务...")
            return {"ok": True}
        return {"ok": False, "error": "当前没有正在运行的任务"}

    def auth_info(self):
        if STATE_DEFAULT.exists():
            try:
                data = json.loads(STATE_DEFAULT.read_text(encoding="utf-8"))
                jwt_val = data.get("jwt") or ""
                created = data.get("created")
                age_mins = int((time.time() - float(created)) // 60) if created else None
                if jwt_val:
                    return {
                        "logged_in": True,
                        "tenant": data.get("tenant", "RBAC"),
                        "age_mins": age_mins,
                        "created": created,
                    }
            except Exception:
                pass
        return {"logged_in": False, "tenant": "", "age_mins": None, "created": None}

    def start_qr_login(self, target_url: str = ""):
        if self.qr_proc is not None and self.qr_proc.poll() is None:
            return {"ok": True, "message": "扫码进程已在运行中"}

        try:
            if self.qr_png_path.exists():
                self.qr_png_path.unlink()
        except Exception:
            pass

        self.qr_running = True
        self.qr_status = "starting"
        self.qr_hint = "正在拉取统一身份认证二维码..."
        self.log("启动统一身份认证扫码登录...")

        def worker():
            vpy = nuaa_api._venv_python()
            py_bin = vpy if os.path.exists(vpy) else sys.executable
            cmd = [py_bin, QR_LOGIN_SCRIPT]
            if target_url:
                cmd.append(target_url)
            cmd.extend(["--state", str(STATE_DEFAULT)])
            try:
                popen_kwargs = {
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT,
                    "text": True,
                    "encoding": "utf-8",
                    "errors": "replace",
                }
                self.qr_proc = subprocess.Popen(cmd, **popen_kwargs)

                for raw_line in self.qr_proc.stdout:
                    line = raw_line.strip()
                    if not line:
                        continue
                    self.log(f"[扫码] {line}")
                    if line.startswith("QR_PNG_PATH="):
                        p_str = line.split("=", 1)[1].strip()
                        self.qr_png_path = Path(p_str)
                        self.qr_updated_at = time.time()
                        self.qr_status = "waiting_scan"
                        self.qr_hint = "请使用手机（南航 App 或微信）扫描二维码"
                    elif line.startswith("scan status:"):
                        st = line.split(":", 1)[1].strip()
                        if st == "0":
                            self.qr_status = "waiting_scan"
                            self.qr_hint = "等待手机扫码中..."
                        elif st == "2":
                            self.qr_status = "scanned"
                            self.qr_hint = "📱 已扫码！请在手机端点击「确认登录」"
                        elif st == "1":
                            self.qr_status = "confirmed"
                            self.qr_hint = "✅ 手机已确认，正在跳转飞天云课堂获取 JWT..."
                        elif st == "3":
                            self.qr_status = "starting"
                            self.qr_hint = "🔄 二维码已过期，正在自动刷新..."
                    elif line.startswith("state saved to"):
                        self.qr_status = "success"
                        self.qr_hint = "🎉 登录成功！已保存飞天云课堂会话令牌"

                rc = self.qr_proc.wait()
                if self.auth_info()["logged_in"]:
                    self.qr_status = "success"
                    self.qr_hint = "🎉 登录成功！可以开始加载课程"
                elif rc != 0:
                    self.qr_status = "error"
                    self.qr_hint = f"扫码登录中断 (退出码 {rc})，请点击重新启动"
            except Exception as e:
                self.qr_status = "error"
                self.qr_hint = f"扫码异常: {e}"
                self.log(f"扫码异常: {e}")
            finally:
                self.qr_proc = None
                self.qr_running = False

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    def stop_qr_login(self):
        proc = self.qr_proc
        if proc and proc.poll() is None:
            self.log("正在停止扫码登录进程...")
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            else:
                proc.terminate()
        self.qr_proc = None
        self.qr_running = False
        self.qr_status = "idle"
        self.qr_hint = "已停止扫码"

    def clear_auth(self):
        self.stop_qr_login()
        for p in (STATE_DEFAULT, self.qr_png_path):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass
        self.qr_status = "idle"
        self.qr_hint = "已清除本机临时登录凭据"
        self.log("已安全清除本机登录凭据文件。")


APP = AppState()


# =============================================================================
# Embedded Single-Page Web Application (HTML/CSS/JS)
# =============================================================================
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NUAA 飞天云课堂 · 字幕文稿 (.md) 导出与视频下载</title>
<style>
:root{--bg-canvas:#f3f5f8;--bg-surface:#fff;--ink-primary:#192436;--ink-secondary:#556477;--ink-muted:#778597;--accent-cobalt:#2458bd;--accent-emerald:#18805c;--accent-amber:#ad6b10;--font-mono:Consolas,monospace;--font-sans:"Microsoft YaHei UI","PingFang SC",system-ui,sans-serif}
*{box-sizing:border-box} body{margin:0;background:var(--bg-canvas);color:var(--ink-primary);font:14px/1.5 var(--font-sans)} button,input,select{font:inherit} button{cursor:pointer} button:disabled{cursor:not-allowed;opacity:.48} button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #97b7f2;outline-offset:2px}
.top-header{height:64px;background:#fff;border-bottom:1px solid #e1e6ed;display:flex;align-items:center;justify-content:space-between;padding:0 28px;gap:16px}.brand-group{display:flex;align-items:center;gap:12px}.brand-emblem{background:#2458bd;color:white;border-radius:9px;padding:8px;font-size:11px;font-weight:700}.brand-title{font-size:16px;font-weight:700}.brand-sub{font-size:12px;color:var(--ink-muted)}.header-actions,.btn-group,.toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.header-status-pill{display:flex;align-items:center;gap:7px;color:var(--ink-secondary);font-size:13px}.status-dot{width:8px;height:8px;border-radius:50%;background:#99a6b5}.status-dot.online{background:#18805c}.status-dot.pending{background:#d18a24}
.app-shell{display:grid;grid-template-columns:280px minmax(0,1fr);gap:16px;max-width:1560px;margin:auto;padding:20px 24px 16px}.sidebar,.main-area{min-width:0}.card{background:#fff;border:1px solid #e1e6ed;border-radius:12px}.course-card,.lecture-card{min-height:560px}.card-header{padding:20px 20px 14px;display:flex;align-items:center;justify-content:space-between;gap:12px}.card-title{font-size:16px;font-weight:700}.card-body{padding:0 20px 20px}.subtle{color:var(--ink-muted);font-size:12px}.sidebar .card-body{display:grid;gap:12px}.input-text,.select-box{height:38px;border:1px solid #d5dce6;border-radius:7px;background:white;color:var(--ink-primary);padding:0 10px;min-width:0}.input-text{width:100%}.btn{border:1px solid #d5dce6;border-radius:7px;background:#fff;color:#344256;min-height:36px;padding:7px 12px;white-space:nowrap}.btn:hover:not(:disabled){background:#f3f6fb}.btn-primary{background:#2458bd;border-color:#2458bd;color:white}.btn-primary:hover:not(:disabled){background:#1d489e}.btn-danger{color:#b33e3e}.btn-sm{min-height:32px;padding:5px 10px;font-size:12px}.toolbar{justify-content:space-between;padding-bottom:16px;border-bottom:1px solid #edf0f4}.toolbar-left,.toolbar-right{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.table-container{overflow:auto;max-height:calc(100vh - 280px)}.data-table{width:100%;border-collapse:collapse;text-align:left}.data-table th{color:#718093;font-size:12px;font-weight:500;background:#fff;position:sticky;top:0}.data-table td,.data-table th{padding:13px 10px;border-bottom:1px solid #edf0f4;white-space:nowrap}.data-table tbody tr{cursor:pointer}.data-table tbody tr:hover{background:#f7f9fc}.data-table tr.selected{background:#edf4ff}.mono-id{font-family:var(--font-mono);font-size:12px;color:var(--ink-muted)}.badge{font-size:12px;color:#536579}.badge-green{color:#18805c}.badge-amber{color:#ad6b10}.course-list{max-height:calc(100vh - 290px);overflow:auto}.course-item{display:block;width:100%;text-align:left;border:0;border-radius:8px;background:transparent;padding:11px 12px;color:var(--ink-primary)}.course-item:hover{background:#f5f7fa}.course-item.selected{background:#edf4ff;color:#2458bd}.course-name{display:block;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.course-meta{display:block;color:var(--ink-muted);font-size:12px;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.empty{padding:36px 12px;text-align:center;color:var(--ink-muted);line-height:1.8}.link-entry{border-top:1px solid #edf0f4;padding-top:12px}.link-entry summary{cursor:pointer;color:var(--ink-secondary);font-size:13px}.link-entry .btn-group{margin-top:10px}.link-entry input{flex:1;min-width:120px}
.task-panel{max-width:1512px;margin:0 auto 24px;background:#fff;border:1px solid #e1e6ed;border-radius:12px;padding:14px 20px}.task-top{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.task-top strong{min-width:150px}.task-top .progress-track{flex:1;min-width:100px}.progress-track,.seg-mini-track{height:6px;background:#e8edf4;border-radius:10px;overflow:hidden}.progress-fill,.seg-mini-fill{height:100%;width:0;background:#2458bd}.task-details summary{cursor:pointer;color:var(--ink-secondary);font-size:12px}.task-details[open] summary{margin-top:12px}.metrics-grid{display:flex;gap:24px;flex-wrap:wrap;margin:14px 0}.metric-label{color:var(--ink-muted);font-size:12px}.metric-val{font-variant-numeric:tabular-nums;font-weight:600}.log-console{background:#f6f8fb;color:#536579;border-radius:8px;padding:12px;white-space:pre-wrap;max-height:180px;overflow:auto;font:12px/1.6 var(--font-mono)}.seg-bars{display:flex;gap:12px}.seg-item{flex:1;font-size:12px}.dialog{border:0;border-radius:14px;padding:0;width:min(460px,calc(100% - 32px));box-shadow:0 20px 60px #1a2d4b33}.dialog::backdrop{background:#17263a80}.dialog-head{padding:20px 24px;border-bottom:1px solid #edf0f4;display:flex;align-items:center;justify-content:space-between}.dialog-head h2{font-size:17px;margin:0}.dialog-body{padding:20px 24px 24px}.dialog-body label{display:block;font-weight:600;margin-bottom:8px}.dialog-body .btn-group{margin-top:12px}.qr-viewport{height:210px;background:#f6f8fb;border-radius:8px;display:grid;place-items:center;text-align:center;color:var(--ink-muted)}.qr-viewport img{max-width:190px;max-height:190px}.qr-hint{text-align:center;color:var(--ink-secondary);margin:12px 0}#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#25364c;color:white;border-radius:8px;padding:10px 16px;opacity:0;pointer-events:none;z-index:10}#toast.show{opacity:1}.export-menu{position:relative}.export-menu summary{list-style:none}.export-menu summary::-webkit-details-marker{display:none}.export-options{position:absolute;top:40px;left:0;z-index:2;background:white;border:1px solid #d5dce6;border-radius:8px;padding:5px;box-shadow:0 8px 20px #1924361a;min-width:160px}.export-options button{display:block;width:100%;text-align:left;border:0}.selected-count{color:var(--ink-muted);font-size:12px}
@media(max-width:800px){.top-header{padding:0 16px}.brand-sub{display:none}.app-shell{grid-template-columns:1fr;padding:12px}.course-card,.lecture-card{min-height:0}.course-list{max-height:240px}.table-container{max-height:none}.task-panel{margin:0 12px 12px}.card-header,.card-body{padding-left:16px;padding-right:16px}}
</style>
</head>
<body>
<header class="top-header"><div class="brand-group"><div class="brand-emblem">NUAA</div><div><div class="brand-title">飞天云课堂下载助手</div><div class="brand-sub">课程文稿与录像</div></div></div><div class="header-actions"><div class="header-status-pill"><span class="status-dot" id="headerStatusDot"></span><span id="headerStatusText">检测登录状态...</span></div><button class="btn btn-sm" onclick="document.getElementById('authDialog').showModal()">登录</button><button class="btn btn-sm" onclick="showHistory()">任务历史</button><button class="btn btn-sm" onclick="document.getElementById('settingsDialog').showModal()">设置</button></div></header>
<div class="app-shell"><aside class="sidebar"><div class="card course-card"><div class="card-header"><div class="card-title">我的课程</div><button class="btn btn-sm" onclick="fetchCoursesFromPlatform()">同步</button></div><div class="card-body"><input type="search" class="input-text" id="courseFilterInput" placeholder="搜索课程或教师" aria-label="搜索课程或教师" oninput="renderCoursesTable()"><div class="course-list" id="coursesTbody"><div class="empty">登录后同步课程，<br>或通过下方链接打开课程。</div></div><details class="link-entry"><summary>通过课程链接打开</summary><div class="btn-group"><input class="input-text" id="directTeclInput" placeholder="粘贴链接或课程 ID" aria-label="课程链接或 ID"><button class="btn btn-sm" onclick="loadByDirectTecl()">打开</button></div></details></div></div></aside>
<main class="main-area"><div class="card lecture-card"><div class="card-header"><div><div class="card-title" id="currentCourseHeader">选择一门课程</div><div class="subtle">选择课次后导出文稿或下载录像</div></div><span class="selected-count" id="selectedCount">已选 0 节</span></div><div class="card-body"><div class="toolbar"><div class="toolbar-left"><details class="export-menu"><summary class="btn btn-primary">导出文稿 ▾</summary><div class="export-options"><button class="btn" onclick="exportMarkdown(false); this.closest('details').open=false">导出选中课次</button><button class="btn" onclick="exportMarkdown(true); this.closest('details').open=false">导出全部课次</button></div></details><button class="btn" onclick="downloadSelectedVideos()">下载视频</button><button class="btn btn-sm" onclick="toggleAllLectures(true)">全选</button><button class="btn btn-sm" onclick="toggleAllLectures(false)">取消全选</button></div><div class="toolbar-right"><label for="intervalSelect" class="subtle">时间锚点</label><select class="select-box" id="intervalSelect"><option value="60000">1 分钟</option><option value="180000" selected>3 分钟</option><option value="300000">5 分钟</option><option value="600000">10 分钟</option></select></div></div><div class="toolbar" style="padding:12px 0"><div class="toolbar-left"><label class="subtle" for="fileFilter">本地文件</label><select id="fileFilter" class="select-box" onchange="renderLecturesTable()"><option value="all">全部</option><option value="missingVideo">未下载视频</option><option value="missingMd">未导出文稿</option><option value="hasFile">已有文件</option></select><button class="btn btn-sm" onclick="refreshFileStatus()">刷新文件状态</button><button class="btn btn-sm" onclick="selectFiltered()">选择筛选结果</button></div><div class="toolbar-right"><label class="subtle" for="dateFrom">日期</label><input type="date" id="dateFrom" class="select-box" onchange="renderLecturesTable()" aria-label="开始日期"><span>至</span><input type="date" id="dateTo" class="select-box" onchange="renderLecturesTable()" aria-label="结束日期"></div></div><div class="table-container"><table class="data-table"><thead><tr><th>选择</th><th>讲次</th><th>课次 ID</th><th>上课时间</th><th>下课时间</th><th>教师</th><th>平台状态</th><th>本地文件</th><th>操作</th></tr></thead><tbody id="lecturesTbody"><tr><td colspan="9" class="empty">请先从左侧选择课程</td></tr></tbody></table></div></div></div></main></div>
<section class="task-panel" aria-label="任务进度"><div class="task-top"><strong id="taskTitleText">当前无运行任务</strong><span id="taskStageText" class="subtle">空闲</span><div class="progress-track"><div class="progress-fill" id="progressBarFill"></div></div><span id="taskPctText">0.0%</span><button class="btn btn-sm btn-danger" id="btnCancelTask" onclick="cancelActiveTask()" disabled>取消任务</button></div><details class="task-details"><summary>任务详情与日志</summary><div class="metrics-grid"><div><div class="metric-label">已下载 / 总大小</div><div class="metric-val" id="metricSize">-- / --</div></div><div><div class="metric-label">下载速度</div><div class="metric-val" id="metricSpeed">-- MB/s</div></div><div><div class="metric-label">预计剩余</div><div class="metric-val" id="metricEta">--:--</div></div><div><div class="metric-label">课次进度</div><div class="metric-val" id="metricQueue">0 / 0</div></div></div><div id="segBarsWrap" style="display:none"><div class="subtle">分段进度</div><div class="seg-bars" id="segBarsContainer"></div></div><div id="taskResult" class="subtle" aria-live="polite"></div><button class="btn btn-sm" id="retryFailed" onclick="retryFailed()" hidden>重试失败课次</button><div class="log-console" id="logConsole"></div><button class="btn btn-sm" onclick="openOutputFolder()">打开保存文件夹</button></details></section>
<dialog class="dialog" id="authDialog"><div class="dialog-head"><h2>扫码登录</h2><button class="btn btn-sm" onclick="document.getElementById('authDialog').close()">关闭</button></div><div class="dialog-body"><div class="qr-viewport" id="qrViewport">点击下方按钮获取登录二维码</div><div class="qr-hint" id="qrHintText">未启动扫码</div><div class="btn-group"><button class="btn btn-primary" id="btnStartQr" onclick="startQrLogin()">启动扫码登录</button><button class="btn" id="btnStopQr" onclick="stopQrLogin()" disabled>停止</button></div></div></dialog>
<dialog class="dialog" id="settingsDialog"><div class="dialog-head"><h2>设置</h2><button class="btn btn-sm" onclick="document.getElementById('settingsDialog').close()">关闭</button></div><div class="dialog-body"><label for="outRootInput">保存位置</label><input class="input-text" id="outRootInput"><div class="btn-group"><button class="btn btn-primary" onclick="saveOutRoot()">保存</button><button class="btn" onclick="openOutputFolder()">打开文件夹</button></div><label for="namingSelect" style="margin-top:18px">文件命名</label><select class="select-box" id="namingSelect"><option value="standard">标准（视频含课程名）</option><option value="compact">简洁（讲次、日期和 ID）</option></select><label style="margin-top:18px;font-weight:400"><input type="checkbox" id="notifyCheck"> 后台任务完成时发送系统通知</label><div class="subtle">时间锚点以主页面当前选择为准，保存设置时一并记住。</div><button class="btn" onclick="savePreferences()">保存偏好</button><hr style="border:0;border-top:1px solid #edf0f4;margin:24px 0"><button class="btn btn-danger" onclick="clearAuth()">清除登录凭据</button></div></dialog>
<dialog class="dialog" id="historyDialog"><div class="dialog-head"><h2>任务历史</h2><button class="btn btn-sm" onclick="document.getElementById('historyDialog').close()">关闭</button></div><div class="dialog-body" id="historyList"></div></dialog><div id="toast" role="status">操作成功</div>
<script>
  let allCourses = [];
  let currentTeclId = "";
  let currentCourseName = "";
  let currentLectures = [];
  let checkedCourseIds = new Set();
  let lastQrTimestamp = 0;
  let fileStatus = {};
  let settingsLoaded = false;
  let lastResultKey = '';
  let resultInitialized = false;

  function showToast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 2800);
  }

  async function apiGet(path) {
    const res = await fetch(path);
    return await res.json();
  }

  async function apiPost(path, body = {}) {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    return await res.json();
  }

  function formatBytes(b) {
    if (!b || b <= 0) return '0.0 MB';
    return (b / 1048576).toFixed(1) + ' MB';
  }

  function formatEta(sec) {
    if (sec === null || sec === undefined || sec < 0) return '--:--';
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  }

  // Poll status every 600ms for smooth real-time download progress
  async function pollStatus() {
    try {
      const st = await apiGet('/api/status');
      const dot = document.getElementById('headerStatusDot');
      const txt = document.getElementById('headerStatusText');
      if (st.auth.logged_in) {
        dot.className = 'status-dot online';
        const age = st.auth.age_mins !== null ? ` (${st.auth.age_mins}m前)` : '';
        txt.textContent = `已登录飞天云课堂${age}`;
      } else if (st.qr.running) {
        dot.className = 'status-dot pending';
        txt.textContent = '正在扫码认证...';
      } else {
        dot.className = 'status-dot';
        txt.textContent = '未登录';
      }

      if (!settingsLoaded) {
        document.getElementById('intervalSelect').value = String(st.settings.interval_ms);
        document.getElementById('namingSelect').value = st.settings.naming;
        document.getElementById('notifyCheck').checked = st.settings.notify;
        settingsLoaded = true;
      }
      const outInput = document.getElementById('outRootInput');
      if (document.activeElement !== outInput && !outInput.value) {
        outInput.value = st.out_root;
      }

      document.getElementById('qrHintText').textContent = st.qr.hint;
      document.getElementById('btnStartQr').disabled = st.qr.running;
      document.getElementById('btnStopQr').disabled = !st.qr.running;

      if (st.qr.has_png && st.qr.updated_at !== lastQrTimestamp) {
        lastQrTimestamp = st.qr.updated_at;
        const vp = document.getElementById('qrViewport');
        vp.innerHTML = `<img src="/api/qr_image?ts=${Date.now()}" alt="扫码登录二维码">`;
      } else if (st.qr.status === 'success' && lastQrTimestamp !== -1) {
        lastQrTimestamp = -1;
        if (document.getElementById('authDialog').open) document.getElementById('authDialog').close();
        document.getElementById('qrViewport').innerHTML = `
          <div style="color:var(--accent-emerald);font-weight:700;">
            <div style="font-size:36px;margin-bottom:6px;">✅</div>
            登录成功！<br><span style="font-size:12px;color:var(--ink-secondary);font-weight:400;">会话令牌已就绪</span>
          </div>`;
        if (allCourses.length === 0) {
          fetchCoursesFromPlatform();
        }
      }

      // Update Telemetry Panel
      const t = st.telemetry;
      const result = t.result || {};
      if (result.id && result.id !== lastResultKey && !t.running) {
        if (resultInitialized) {
          showToast(result.status || '任务已结束');
          if (document.getElementById('notifyCheck').checked && document.hidden && 'Notification' in window && Notification.permission === 'granted') new Notification('飞天云课堂下载助手', {body: result.summary});
          refreshFileStatus();
        }
        lastResultKey = result.id;
      }
      resultInitialized = true;
      document.getElementById('taskResult').textContent = result.summary || '';
      if (result.summary && !t.running && document.getElementById('taskResult').dataset.last !== result.summary) { document.querySelector('.task-details').open = true; document.getElementById('taskResult').dataset.last = result.summary; }
      document.getElementById('retryFailed').hidden = t.running || !(result.failed || []).length;
      document.getElementById('btnCancelTask').disabled = !t.running;
      document.getElementById('taskTitleText').textContent = t.title || '当前无运行任务';
      document.getElementById('taskStageText').textContent = t.stage || '空闲';
      document.getElementById('taskPctText').textContent = `${(t.progress || 0).toFixed(1)}%`;
      document.getElementById('progressBarFill').style.width = `${t.progress || 0}%`;

      document.getElementById('metricQueue').textContent =
        t.queue_total > 0 ? `${t.queue_current} / ${t.queue_total}` : '0 / 0';

      if (t.total_bytes > 0) {
        document.getElementById('metricSize').textContent =
          `${formatBytes(t.downloaded_bytes)} / ${formatBytes(t.total_bytes)}`;
      } else {
        document.getElementById('metricSize').textContent = '-- / --';
      }

      if (t.speed_bps > 0) {
        document.getElementById('metricSpeed').textContent = `${(t.speed_bps / 1048576).toFixed(2)} MB/s`;
      } else {
        document.getElementById('metricSpeed').textContent = '-- MB/s';
      }

      document.getElementById('metricEta').textContent = formatEta(t.eta_sec);

      const segWrap = document.getElementById('segBarsWrap');
      const segContainer = document.getElementById('segBarsContainer');
      if (t.seg_pcts && t.seg_pcts.length > 0) {
        segWrap.style.display = 'block';
        segContainer.innerHTML = t.seg_pcts.map((p, idx) => `
          <div class="seg-item">
            <div style="display:flex;justify-content:space-between;">
              <span>段${idx + 1}</span>
              <span>${p.toFixed(0)}%</span>
            </div>
            <div class="seg-mini-track">
              <div class="seg-mini-fill" style="width:${p}%;"></div>
            </div>
          </div>
        `).join('');
      } else {
        segWrap.style.display = 'none';
      }

      const logBox = document.getElementById('logConsole');
      const joined = (st.logs || []).join('\n');
      if (logBox.dataset.lastLen !== String(joined.length)) {
        logBox.textContent = joined;
        logBox.dataset.lastLen = String(joined.length);
        logBox.scrollTop = logBox.scrollHeight;
      }
    } catch (e) {
      // ignore transient error
    }
  }

  async function startQrLogin() {
    lastQrTimestamp = 0;
    document.getElementById('qrViewport').innerHTML = `
      <div style="color:var(--accent-cobalt);font-size:13px;">
        <div style="font-size:26px;margin-bottom:8px;">⏳</div>
        正在拉取统一身份认证二维码...
      </div>`;
    await apiPost('/api/qr/start', {});
    pollStatus();
  }

  async function stopQrLogin() {
    await apiPost('/api/qr/stop', {});
    pollStatus();
  }

  async function clearAuth() {
    await apiPost('/api/auth/clear', {});
    lastQrTimestamp = 0;
    document.getElementById('qrViewport').innerHTML = `
      <div style="color:var(--ink-muted);font-size:12.5px;">
        登录凭据已清除<br>点击下方按钮可重新扫码
      </div>`;
    showToast('已清除本机登录凭据');
    pollStatus();
  }

  async function cancelActiveTask() {
    const res = await apiPost('/api/task/cancel', {});
    if (res.ok) showToast('🛑 已发送取消指令');
  }

  async function saveOutRoot() {
    const val = document.getElementById('outRootInput').value.trim();
    const res = await apiPost('/api/settings/out_root', { out_root: val });
    if (res.ok) { showToast('保存目录已更新'); refreshFileStatus(); } else alert(res.error || '保存失败');
  }

  async function openOutputFolder() {
    await apiPost('/api/open_path', { kind: 'out_root' });
  }

  async function fetchCoursesFromPlatform() {
    showToast('正在从平台拉取本学期课程...');
    const res = await apiGet('/api/courses');
    if (!res.ok) {
      alert(res.error || '拉取课程失败，请先完成扫码登录');
      return;
    }
    allCourses = res.courses || [];
    renderCoursesTable();
    showToast(`已加载 ${allCourses.length} 门本学期课程`);
  }

  function renderCoursesTable() {
    const kw = document.getElementById('courseFilterInput').value.trim().toLowerCase();
    const tbody = document.getElementById('coursesTbody');
    const filtered = allCourses.filter(c => {
      if (!kw) return true;
      const s = `${c.teclId} ${c.subjName || ''} ${(c.teacNames || []).join(',')} ${c.teclName || ''}`.toLowerCase();
      return s.includes(kw);
    });

    if (filtered.length === 0) {
      tbody.innerHTML = `<div class="empty">${kw ? '没有匹配的课程' : '暂无课程，请先登录并同步课程'}</div>`;
      return;
    }
    tbody.innerHTML = '';
    filtered.forEach(c => {
      const button = document.createElement('button');
      button.className = 'course-item' + (String(c.teclId) === String(currentTeclId) ? ' selected' : '');
      const name = document.createElement('span');
      name.className = 'course-name';
      name.textContent = c.subjName || `课程 ${c.teclId}`;
      const meta = document.createElement('span');
      meta.className = 'course-meta';
      meta.textContent = `${(c.teacNames || []).join('、') || '教师未标注'} · ${c.courTimes || 0} 节`;
      button.append(name, meta);
      button.onclick = () => selectCourse(c.teclId, c.subjName);
      tbody.appendChild(button);
    });
  }

  async function selectCourse(teclId, subjName) {
    currentTeclId = String(teclId);
    currentCourseName = subjName || `课程_${teclId}`;
    document.getElementById('directTeclInput').value = currentTeclId;
    renderCoursesTable();
    document.getElementById('currentCourseHeader').textContent = `正在加载《${currentCourseName}》(teclId=${currentTeclId}) 的课次列表...`;

    const res = await apiGet(`/api/lectures?teclId=${encodeURIComponent(currentTeclId)}&subjName=${encodeURIComponent(currentCourseName)}`);
    if (!res.ok) {
      alert(res.error || '加载课次失败');
      return;
    }
    currentLectures = res.lectures || [];
    if (res.subjName) currentCourseName = res.subjName;
    checkedCourseIds = new Set();
    await refreshFileStatus();
  }

  function loadByDirectTecl() {
    const raw = document.getElementById('directTeclInput').value.trim();
    if (!raw) {
      alert('请输入课程网页链接或数字 teclId');
      return;
    }
    const m = raw.match(/[?&]id=(\d+)/);
    const tid = m ? m[1] : raw;
    if (!/^\d{4,15}$/.test(tid)) {
      alert('无法识别有效的数字 teclId');
      return;
    }
    selectCourse(tid, `课程_${tid}`);
  }

  function renderLecturesTable() {
    document.getElementById('currentCourseHeader').textContent =
      `${currentCourseName} · ${currentLectures.length} 节课次`;
    document.getElementById('selectedCount').textContent = `已选 ${checkedCourseIds.size} 节`;
    const tbody = document.getElementById('lecturesTbody');
    if (currentLectures.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;color:var(--ink-muted);padding:28px;">该课程下暂无课次记录</td></tr>`;
      return;
    }
    const visible = filteredLectures();
    if (!visible.length) { tbody.innerHTML = '<tr><td colspan="9" class="empty">没有符合条件的课次</td></tr>'; return; }
    tbody.innerHTML = visible.map(({r, i}) => {
      const cid = String(r.id);
      const checked = checkedCourseIds.has(cid);
      const idxStr = String(i + 1).padStart(2, '0');
      const statusOk = String(r.vodStatus) === '2';
      return `
        <tr class="${checked ? 'selected' : ''}" onclick="toggleLectureRow('${cid}')">
          <td style="text-align:center;">
            <input type="checkbox" ${checked ? 'checked' : ''} onclick="event.stopPropagation(); toggleLectureRow('${cid}')">
          </td>
          <td class="mono-id">第 ${idxStr} 讲</td>
          <td class="mono-id">${cid}</td>
          <td>${r.courBeginTime || ''}</td>
          <td>${r.courEndTime || ''}</td>
          <td>${(r.teacNames || []).join(', ')}</td>
          <td><span class="badge ${statusOk ? 'badge-green' : 'badge-amber'}">${statusOk ? '已生成录像' : '状态 ' + r.vodStatus}</span></td>
          <td>${fileStatus[cid]?.md ? '文稿 ✓' : '文稿 —'} · ${fileStatus[cid]?.srt ? '字幕 ✓' : '字幕 —'} · ${fileStatus[cid]?.mp4 ? '视频 ✓' : '视频 —'}</td>
          <td onclick="event.stopPropagation();">
            <div class="btn-group">
              <button class="btn btn-sm" onclick="exportSingleMd('${cid}')">导出文稿</button>
              <button class="btn btn-sm btn-primary" onclick="downloadSingleVideo('${cid}', '第${idxStr}讲')">下载视频</button>
            </div>
          </td>
        </tr>
      `;
    }).join('');
  }

  function filteredLectures() {
    const filter = document.getElementById('fileFilter').value;
    const from = document.getElementById('dateFrom').value, to = document.getElementById('dateTo').value;
    return currentLectures.map((r, i) => ({r, i})).filter(({r}) => {
      const date = (r.courBeginTime || '').slice(0, 10), f = fileStatus[String(r.id)] || {};
      return (!from || date >= from) && (!to || date <= to) &&
        (filter === 'all' || (filter === 'missingVideo' && !f.mp4) || (filter === 'missingMd' && !f.md) || (filter === 'hasFile' && (f.md || f.srt || f.mp4)));
    });
  }
  function selectFiltered() { filteredLectures().forEach(({r}) => checkedCourseIds.add(String(r.id))); renderLecturesTable(); }
  async function refreshFileStatus() {
    if (!currentTeclId || !currentLectures.length) return;
    const selectedCourse = currentTeclId;
    const res = await apiPost('/api/files/status', {teclId: currentTeclId, subjName: currentCourseName});
    if (res.ok && selectedCourse === currentTeclId) { fileStatus = res.files; renderLecturesTable(); }
  }
  async function savePreferences() {
    if (document.getElementById('notifyCheck').checked && 'Notification' in window && Notification.permission === 'default') await Notification.requestPermission();
    const res = await apiPost('/api/settings/preferences', {interval_ms: Number(document.getElementById('intervalSelect').value), naming: document.getElementById('namingSelect').value, notify: document.getElementById('notifyCheck').checked});
    if (res.ok) showToast('偏好已保存'); else alert(res.error);
  }
  async function showHistory() {
    const res = await apiGet('/api/history');
    const box = document.getElementById('historyList'); box.replaceChildren();
    if (!res.items.length) box.textContent = '暂无任务记录';
    res.items.forEach(item => {
      const row = document.createElement('div'); row.style.cssText = 'padding:12px 0;border-bottom:1px solid #edf0f4';
      const title = document.createElement('strong'); title.textContent = `${item.course} · ${item.kind === 'md' ? '导出文稿' : '下载视频'}`;
      const detail = document.createElement('div'); detail.className = 'subtle'; detail.textContent = `${item.time} · ${item.status} · ${item.summary}`;
      const actions = document.createElement('div'); actions.className = 'btn-group';
      const open = document.createElement('button'); open.className = 'btn btn-sm'; open.textContent = '打开文件夹'; open.onclick = async () => {const r = await apiPost('/api/history/open', {id:item.id}); if (!r.ok) alert(r.error);};
      const again = document.createElement('button'); again.className = 'btn btn-sm'; again.textContent = '重新运行'; again.onclick = async () => {
        const choice = prompt('重新运行时如何处理已有文件？\n1 跳过（默认）  2 覆盖  3 另存', '1');
        if (choice === null) return;
        const policy = {'1':'skip','2':'overwrite','3':'rename'}[choice.trim()]; if (!policy) { alert('请输入 1、2 或 3'); return; }
        const r = await apiPost('/api/history/rerun', {id:item.id, policy}); if (!r.ok) alert(r.error); else { document.getElementById('historyDialog').close(); showToast('任务已开始'); }
      };
      actions.append(open, again); row.append(title, detail, actions); box.append(row);
    });
    document.getElementById('historyDialog').showModal();
  }

  function toggleLectureRow(cid) {
    if (checkedCourseIds.has(cid)) checkedCourseIds.delete(cid);
    else checkedCourseIds.add(cid);
    renderLecturesTable();
  }

  function toggleAllLectures(selectAll) {
    if (selectAll) {
      checkedCourseIds = new Set(currentLectures.map(r => String(r.id)));
    } else {
      checkedCourseIds.clear();
    }
    renderLecturesTable();
  }

  async function startTask(path, body) {
    const preview = await apiPost('/api/task/preview', {...body, kind: path.endsWith('export_md') ? 'md' : 'mp4'});
    if (!preview.ok) return preview;
    let policy = 'skip';
    if (preview.conflicts.length) {
      const choice = prompt(`发现 ${preview.conflicts.length} 个已有文件（含汇总文稿；完整性未验证）。输入处理方式：\n1 跳过已有文件（默认）\n2 覆盖已有文件\n3 另存为新文件`, '1');
      if (choice === null) return {ok: false, error: '已取消启动任务'};
      if (!['1', '2', '3'].includes(choice.trim())) return {ok: false, error: '无效选择，请输入 1、2 或 3'};
      policy = {'1':'skip','2':'overwrite','3':'rename'}[choice.trim()];
    }
    return apiPost(path, {...body, policy});
  }

  async function retryFailed() {
    const res = await apiPost('/api/task/retry', {});
    if (!res.ok) alert(res.error || '重试失败');
    else showToast('已开始重试失败课次');
  }

  async function exportSingleMd(cid) {
    const intervalMs = parseInt(document.getElementById('intervalSelect').value, 10) || 180000;
    const res = await startTask('/api/export_md', {
      teclId: currentTeclId,
      subjName: currentCourseName,
      courseIds: [String(cid)],
      isFullCourse: false,
      intervalMs: intervalMs
    });
    if (!res.ok) {
      alert(res.error || '导出 .md 失败');
      return;
    }
    showToast('已启动本节课 .md 文稿导出，请查看下方任务进度');
  }

  async function exportMarkdown(isAll) {
    if (!currentTeclId || currentLectures.length === 0) {
      alert('请先在上方选择一门课程加载课次！');
      return;
    }
    const ids = isAll ? currentLectures.map(r => String(r.id)) : Array.from(checkedCourseIds);
    if (ids.length === 0) {
      alert('请先勾选至少一节课次（或直接点击「🚀 一键导出本课全部课次 .md 文稿」）！');
      return;
    }
    const intervalMs = parseInt(document.getElementById('intervalSelect').value, 10) || 180000;
    const res = await startTask('/api/export_md', {
      teclId: currentTeclId,
      subjName: currentCourseName,
      courseIds: ids,
      isFullCourse: isAll,
      intervalMs: intervalMs
    });
    if (!res.ok) {
      alert(res.error || '启动 .md 导出失败');
      return;
    }
    showToast(`已启动导出 ${ids.length} 节课的 .md 文稿，请查看下方任务进度`);
  }

  async function downloadSingleVideo(cid, label) {
    const res = await startTask('/api/download_videos', {
      teclId: currentTeclId,
      subjName: currentCourseName,
      courseIds: [String(cid)]
    });
    if (!res.ok) {
      alert(res.error || '启动视频下载失败');
      return;
    }
    showToast(`已开始下载 ${label} MP4 视频，可在下方查看任务详情`);
  }

  async function downloadSelectedVideos() {
    if (!currentTeclId || currentLectures.length === 0) {
      alert('请先选择一门课程！');
      return;
    }
    const ids = Array.from(checkedCourseIds);
    if (ids.length === 0) {
      alert('请先在表格左侧勾选要下载的课次，或直接点击对应课次右侧的「下载视频」按钮！');
      return;
    }
    if (ids.length > 3) {
      const ok = confirm(`您当前勾选了 ${ids.length} 节课（每节课约 500~900 MB），确定要批量下载全部 ${ids.length} 节课的视频吗？`);
      if (!ok) return;
    }
    const res = await startTask('/api/download_videos', {
      teclId: currentTeclId,
      subjName: currentCourseName,
      courseIds: ids
    });
    if (!res.ok) {
      alert(res.error || '启动视频下载失败');
      return;
    }
    showToast(`已启动 ${ids.length} 节课的 MP4 下载队列`);
  }

  setInterval(pollStatus, 600);
  pollStatus();
</script>
</body>
</html>
"""


# =============================================================================
# Background Workers for .md Export & MP4 Download
# =============================================================================
def _friendly_error(exc):
    text = str(exc)
    low = text.lower()
    if isinstance(exc, OSError) and (getattr(exc, "errno", None) == 28 or getattr(exc, "winerror", None) == 112):
        return "磁盘空间不足，请清理空间后重试"
    if any(x in low for x in ("401", "403", "unauthorized", "token", "jwt", "登录")):
        return "登录可能已过期，请重新扫码后重试"
    if any(x in low for x in ("timeout", "timed out", "connection", "网络", "curl")):
        return "网络连接失败，请检查网络后重试"
    return f"处理失败：{text or type(exc).__name__}"


def _task_paths(tecl_id, subj_name, course_ids, kind, is_full_course=False, lectures=None, out_root=None, naming=None):
    if str(tecl_id) != str(APP.current_tecl_id) and lectures is None:
        raise ValueError("当前课程已变化，请重新选择课程")
    safe_subj = re.sub(r'[\\/:*?"<>|\s]+', "_", subj_name or "课程").strip("_") or "课程"
    out_dir = (Path(out_root or APP.out_root) / f"{safe_subj}_{tecl_id}").resolve()
    wanted = {str(x) for x in course_ids}
    indexed = [(i, r) for i, r in enumerate(lectures if lectures is not None else APP.lectures, 1) if str(r.get("id")) in wanted]
    if not indexed or len(indexed) != len(wanted):
        raise ValueError("未找到部分课次，请重新加载课程")
    paths = []
    for idx, r in indexed:
        cid = str(r.get("id"))
        begin = re.sub(r"[^0-9A-Za-z_-]", "_", (r.get("courBeginTime") or "").replace(":", "-").replace(" ", "_"))
        name = f"{idx:02d}_{begin}_{cid}.md" if kind == "md" else (f"{idx:02d}_{begin}_{cid}.mp4" if (naming or APP.naming) == "compact" else f"{idx:02d}_{safe_subj}_{begin}_{cid}.mp4")
        paths.append((idx, r, out_dir / name))
    summary = out_dir / ("00_全课复习文稿汇总.md" if is_full_course else "00_选中课次复习文稿汇总.md") if kind == "md" and (len(indexed) > 1 or is_full_course) else None
    return out_dir, paths, summary


def _destination(path, policy):
    if not path.exists():
        return path
    if policy == "skip":
        return None
    if policy == "overwrite":
        return path
    for n in range(1, 10000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError("无法生成新文件名")


def _start_task(kind, tecl_id, subj_name, course_ids, is_full_course=False, interval_ms=180000, policy="skip", lectures=None, make_summary=True, out_root=None, naming=None):
    if policy not in ("skip", "overwrite", "rename"):
        return {"ok": False, "error": "无效的文件冲突处理方式"}
    if not STATE_DEFAULT.exists():
        return {"ok": False, "error": "尚未登录，请先扫码登录"}
    try:
        out_dir, paths, summary = _task_paths(tecl_id, subj_name, course_ids, kind, is_full_course, lectures, out_root, naming)
        if not make_summary:
            summary = None
        state = nuaa_api.load_state(str(STATE_DEFAULT))
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return {"ok": False, "error": _friendly_error(exc)}
    with APP.lock:
        if APP.task_running:
            return {"ok": False, "error": "当前已有任务运行，请等待完成或取消任务"}
        APP.task_running = True
        APP.task_result = {}
    APP.cancel_event.clear()
    APP.reset_telemetry(task_type=kind, title=f"{'导出文稿' if kind == 'md' else '下载视频'}：{subj_name}", stage="准备中")
    APP.update_telemetry(queue_total=len(paths), queue_current=0)
    snapshot = [{k: r.get(k) for k in ("id", "courBeginTime", "courEndTime", "teacNames", "vodStatus")} for r in (lectures if lectures is not None else APP.lectures)]
    task_id = uuid.uuid4().hex
    task_root = str(out_dir.parent)
    task_naming = naming or APP.naming

    def worker():
        successes, failures, skipped, pending = [], [], [], []
        combined = [f"# 《{subj_name}》课堂字幕复习文稿汇总\n"]
        try:
            for offset, (idx, record, original) in enumerate(paths):
                cid = str(record.get("id"))
                if APP.cancel_event.is_set():
                    pending.extend(str(item[1].get("id")) for item in paths[offset:])
                    break
                APP.update_telemetry(queue_current=offset + 1, stage=f"正在处理第 {idx:02d} 讲 ({offset + 1}/{len(paths)})", progress=offset * 100 / len(paths))
                try:
                    dest = _destination(original, policy)
                    if dest is None:
                        skipped.append(cid)
                        if kind == "md" and summary:
                            combined.append(original.read_text(encoding="utf-8") + "\n---\n")
                        APP.log(f"第 {idx:02d} 讲：已有文件，已跳过")
                        continue
                    if kind == "md":
                        title = f"第 {idx:02d} 讲 | {record.get('courBeginTime')} | {subj_name}"
                        count, span, md_text = nuaa_api.export_lecture_md(cid, state, md_path=None, title=title, interval_ms=interval_ms)
                        if not count:
                            raise ValueError("平台暂无字幕，可稍后重试")
                        temp = dest.with_name(dest.name + ".tmp")
                        try:
                            temp.write_text(md_text, encoding="utf-8")
                            os.replace(temp, dest)
                        finally:
                            temp.unlink(missing_ok=True)
                        combined.append(md_text + "\n---\n")
                    else:
                        info = nuaa_api.fetch_vod_info(cid, state)
                        views = info.get("courseVodViewList") or []
                        if not views:
                            raise ValueError("平台暂无可用录像，可稍后重试")
                        temp = dest.with_name(dest.stem + ".downloading.mp4")
                        try:
                            temp.unlink(missing_ok=True)
                            def detail(d):
                                APP.update_telemetry(stage=d.get("message") or "下载中", downloaded_bytes=d.get("downloaded_bytes", 0), total_bytes=d.get("total_bytes", 0), speed_bps=d.get("speed_bps", 0), eta_sec=d.get("eta_sec"), seg_pcts=d.get("seg_pcts") or [], progress=(offset + d.get("pct", 0) / 100) * 100 / len(paths))
                            result = download_mp4.download_mp4_file(views[0]["url"], str(temp), nseg=4, detail_cb=detail, cancel_event=APP.cancel_event)
                            if not result.get("complete"):
                                raise IOError("下载文件不完整，请重试")
                            if APP.cancel_event.is_set():
                                raise InterruptedError("任务已取消")
                            os.replace(temp, dest)
                        finally:
                            temp.unlink(missing_ok=True)
                    successes.append(cid)
                    APP.log(f"第 {idx:02d} 讲处理完成：{dest.name}")
                except (Exception, SystemExit) as exc:
                    if APP.cancel_event.is_set():
                        pending.extend(str(item[1].get("id")) for item in paths[offset:])
                        break
                    reason = _friendly_error(exc)
                    failures.append({"id": cid, "lecture": idx, "reason": reason})
                    APP.log(f"第 {idx:02d} 讲：{reason}")
            if summary and len(combined) > 1:
                try:
                    target = _destination(summary, policy)
                    if target:
                        temp = target.with_name(target.name + ".tmp")
                        try:
                            temp.write_text("\n".join(combined), encoding="utf-8")
                            os.replace(temp, target)
                        finally:
                            temp.unlink(missing_ok=True)
                except Exception as exc:
                    failures.append({"id": "", "lecture": "汇总", "reason": _friendly_error(exc)})
            status = "已取消" if APP.cancel_event.is_set() else ("部分失败" if failures and (successes or skipped) else "失败" if failures else "全部完成")
            message = f"{status}：成功 {len(successes)} 节，跳过 {len(skipped)} 节，失败 {len(failures)} 项，未处理 {len(pending)} 节。保存位置：{out_dir}"
            if failures:
                message += "；" + "；".join(f"第 {f['lecture']} 讲：{f['reason']}" if f['id'] else f"汇总文稿：{f['reason']}" for f in failures)
            APP.log(message)
            retry_ids = [f["id"] for f in failures if f["id"]]
            APP.retry_request = {"kind": kind, "tecl_id": tecl_id, "subj_name": subj_name, "course_ids": retry_ids, "is_full_course": False, "interval_ms": interval_ms, "policy": policy, "lectures": snapshot, "make_summary": False, "out_root": task_root, "naming": task_naming} if retry_ids else None
            item = {"id": task_id, "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "course": subj_name, "kind": kind, "status": status, "summary": message, "out_dir": str(out_dir), "request": {"kind": kind, "tecl_id": tecl_id, "subj_name": subj_name, "course_ids": list(course_ids), "is_full_course": is_full_course, "interval_ms": interval_ms, "lectures": snapshot, "out_root": task_root, "naming": task_naming}}
            try:
                with APP.lock:
                    APP.history = ([item] + APP.history)[:50]
                    _write_json(HISTORY_FILE, APP.history)
            except OSError as exc:
                APP.log(f"无法保存任务历史：{exc}")
            APP.update_telemetry(task_result={"id": task_id, "status": status, "summary": message, "failed": retry_ids}, task_stage=status, progress=(100.0 if not pending else (len(successes)+len(skipped)+len(failures))*100/len(paths)), speed_bps=0.0, eta_sec=None)
        finally:
            APP.update_telemetry(task_running=False)
    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True}


def _start_export_md_worker(tecl_id, subj_name, course_ids, is_full_course, interval_ms, policy="skip"):
    return _start_task("md", tecl_id, subj_name, course_ids, is_full_course, interval_ms, policy)


def _start_video_download_worker(tecl_id, subj_name, course_ids, policy="skip"):
    return _start_task("mp4", tecl_id, subj_name, course_ids, policy=policy)


class WebGuiHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, obj, status=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8", errors="replace"))

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            raw = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if path == "/api/status":
            has_png = APP.qr_png_path.exists() and APP.qr_png_path.stat().st_size > 100
            with APP.lock:
                logs_copy = list(APP.logs[-120:])
                telemetry = {
                    "running": APP.task_running,
                    "result": APP.task_result,
                    "type": APP.task_type,
                    "title": APP.task_title,
                    "stage": APP.task_stage,
                    "progress": APP.progress,
                    "queue_current": APP.queue_current,
                    "queue_total": APP.queue_total,
                    "downloaded_bytes": APP.downloaded_bytes,
                    "total_bytes": APP.total_bytes,
                    "speed_bps": APP.speed_bps,
                    "eta_sec": APP.eta_sec,
                    "seg_pcts": list(APP.seg_pcts),
                }
            self._send_json({
                "auth": APP.auth_info(),
                "qr": {
                    "running": APP.qr_running,
                    "status": APP.qr_status,
                    "hint": APP.qr_hint,
                    "has_png": has_png,
                    "updated_at": APP.qr_updated_at,
                },
                "out_root": str(APP.out_root),
                "settings": {"interval_ms": APP.interval_ms, "naming": APP.naming, "notify": APP.notify},
                "telemetry": telemetry,
                "logs": logs_copy,
            })
            return

        if path == "/api/qr_image":
            if APP.qr_png_path.exists():
                data = APP.qr_png_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_error(404, "QR image not ready")
            return

        if path == "/api/history":
            with APP.lock:
                items = [{k: v for k, v in item.items() if k != "request"} for item in APP.history]
            self._send_json({"ok": True, "items": items})
            return

        if path == "/api/courses":
            if not STATE_DEFAULT.exists():
                self._send_json({"ok": False, "error": "尚未登录，请先点击左侧「启动扫码登录」"})
                return
            try:
                state = nuaa_api.load_state(str(STATE_DEFAULT))
                recs = nuaa_api.fetch_courses(state, "")
                APP.courses = recs
                APP.log(f"成功从平台同步 {len(recs)} 门本学期课程。")
                self._send_json({"ok": True, "courses": recs})
            except Exception as e:
                APP.log(f"拉取课程列表失败: {e}")
                self._send_json({"ok": False, "error": _friendly_error(e)})
            return

        if path == "/api/lectures":
            tecl_id = (qs.get("teclId") or [""])[0].strip()
            subj_name = (qs.get("subjName") or [""])[0].strip()
            if not STATE_DEFAULT.exists():
                self._send_json({"ok": False, "error": "尚未登录，请先扫码登录"})
                return
            try:
                state = nuaa_api.load_state(str(STATE_DEFAULT))
                recs = nuaa_api.fetch_lectures(tecl_id, state)
                APP.current_tecl_id = tecl_id
                APP.lectures = recs
                if recs and (not subj_name or subj_name.startswith("课程_")):
                    subj_name = recs[0].get("subjName") or recs[0].get("courName") or subj_name
                APP.log(f"已加载课堂 teclId={tecl_id} ({subj_name}): 共 {len(recs)} 节课次。")
                self._send_json({"ok": True, "lectures": recs, "subjName": subj_name})
            except Exception as e:
                APP.log(f"加载课次失败: {e}")
                self._send_json({"ok": False, "error": _friendly_error(e)})
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        body = self._read_json_body()

        if path == "/api/qr/start":
            self._send_json(APP.start_qr_login(body.get("target_url", "")))
            return

        if path == "/api/qr/stop":
            APP.stop_qr_login()
            self._send_json({"ok": True})
            return

        if path == "/api/auth/clear":
            APP.clear_auth()
            self._send_json({"ok": True})
            return

        if path == "/api/task/cancel":
            self._send_json(APP.cancel_active_task())
            return

        if path == "/api/files/status":
            try:
                _, paths, _ = _task_paths(body.get("teclId"), body.get("subjName"), [r.get("id") for r in APP.lectures], "md")
                files = {}
                for _, record, md in paths:
                    cid = str(record.get("id"))
                    candidates = list(md.parent.iterdir()) if md.parent.exists() else []
                    pattern = re.compile(r"_" + re.escape(cid) + r"(?: \(\d+\))?\.(md|srt|mp4)$", re.I)
                    files[cid] = {ext: any((match := pattern.search(p.name)) and match.group(1).lower() == ext and p.is_file() for p in candidates) for ext in ("md", "srt", "mp4")}
                self._send_json({"ok": True, "files": files})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)})
            return

        if path == "/api/settings/preferences":
            try:
                interval = int(body.get("interval_ms"))
                naming = body.get("naming")
                if interval not in (60000, 180000, 300000, 600000) or naming not in ("standard", "compact"):
                    raise ValueError("无效的偏好设置")
                settings = {"out_root": str(APP.out_root), "interval_ms": interval, "naming": naming, "notify": bool(body.get("notify"))}
                _write_json(SETTINGS_FILE, settings)
                APP.interval_ms, APP.naming, APP.notify = interval, naming, settings["notify"]
                self._send_json({"ok": True})
            except (OSError, ValueError, TypeError) as exc:
                self._send_json({"ok": False, "error": str(exc)})
            return

        if path in ("/api/history/open", "/api/history/rerun"):
            with APP.lock:
                item = next((x for x in APP.history if x.get("id") == body.get("id")), None)
            if not item:
                self._send_json({"ok": False, "error": "找不到任务记录"})
            elif path.endswith("/open"):
                try:
                    _open_path_in_os(Path(item["out_dir"]))
                    self._send_json({"ok": True})
                except OSError as exc:
                    self._send_json({"ok": False, "error": str(exc)})
            else:
                self._send_json(_start_task(**item["request"], policy=body.get("policy", "skip")))
            return

        if path == "/api/settings/out_root":
            val = (body.get("out_root") or "").strip()
            if val and ".." not in val:
                p = Path(os.path.expanduser(val)).resolve()
                p.mkdir(parents=True, exist_ok=True)
                try:
                    _write_json(SETTINGS_FILE, {"out_root": str(p), "interval_ms": APP.interval_ms, "naming": APP.naming, "notify": APP.notify})
                except OSError as exc:
                    self._send_json({"ok": False, "error": str(exc)})
                    return
                APP.out_root = p
                APP.log(f"保存根目录已更新为: {p}")
                self._send_json({"ok": True, "out_root": str(p)})
            else:
                self._send_json({"ok": False, "error": "无效的目录路径"})
            return

        if path == "/api/open_path":
            try:
                APP.out_root.mkdir(parents=True, exist_ok=True)
                _open_path_in_os(APP.out_root)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)})
            return

        if path == "/api/task/preview":
            try:
                kind = body.get("kind")
                if kind not in ("md", "mp4"):
                    raise ValueError("无效的任务类型")
                _, paths, summary = _task_paths(body.get("teclId"), body.get("subjName"), body.get("courseIds") or [], kind, bool(body.get("isFullCourse")))
                conflicts = [p.name for _, _, p in paths if p.exists()]
                if summary and summary.exists():
                    conflicts.append(summary.name)
                self._send_json({"ok": True, "conflicts": conflicts})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)})
            return

        if path == "/api/task/retry":
            request = APP.retry_request
            self._send_json(_start_task(**request) if request else {"ok": False, "error": "没有可重试的失败课次"})
            return

        if path == "/api/export_md":
            res = _start_export_md_worker(
                body.get("teclId"),
                body.get("subjName"),
                body.get("courseIds") or [],
                bool(body.get("isFullCourse", True)),
                int(body.get("intervalMs") or 180000),
                body.get("policy", "skip"),
            )
            self._send_json(res)
            return

        if path == "/api/download_videos":
            res = _start_video_download_worker(
                body.get("teclId"),
                body.get("subjName"),
                body.get("courseIds") or [],
                body.get("policy", "skip"),
            )
            self._send_json(res)
            return

        self.send_error(404, "Not Found")


def _find_free_port(preferred=8765):
    for p in (preferred, 8766, 8767, 8788, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", p))
                return s.getsockname()[1]
        except OSError:
            continue
    return 8765


def main():
    port = _find_free_port(8765)
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), WebGuiHandler)
    url = f"http://127.0.0.1:{port}"
    print("=" * 64)
    print(f"  NUAA 飞天云课堂 Web 控制台已启动!")
    print(f"  请在浏览器访问: {url}")
    print("  (保持本窗口开启，按 Ctrl+C 可退出服务)")
    print("=" * 64, flush=True)

    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭服务...")
    finally:
        APP.stop_qr_login()
        server.server_close()


if __name__ == "__main__":
    main()
