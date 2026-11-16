#!/usr/bin/env python3
"""飞天云课堂 (ft.nuaa.edu.cn) 统一身份认证扫码登录。

启动一个全新的无头 Chrome(独立临时配置,与用户的 Chrome 互不影响),
打开目标页面 -> 被重定向到统一身份认证登录页 -> 通过后端接口生成二维码
(保存到 /tmp/nuaa-qr.png, 默认不弹窗) -> 轮询扫码状态 -> 确认后页面内提交表单
完成登录 -> 读出 sessionStorage 中的 jwt/tenant 写入 state 文件。

用法:
  python3 qr_login.py <video_page_url> [--state <json_path>] [--keep] [--open]

要点(踩坑记录):
- 页面前端代码的 getQrCode() 可能因 QR_LOGIN_ENABLED 关闭而不工作,
  必须直接调后端接口: /authserver/qrCode/getToken -> getCode -> getStatus.htl
- 二维码必须校验 PNG 魔数再展示, 否则可能把 HTML 占位页当二维码给用户
- 二维码默认不弹窗(只打印路径, 供对话内展示); 独立终端需要弹出时加 --open
- 登录提交必须在浏览器页面会话内完成(form.submit()), 用 curl 单独 POST 会失败
- 登录成功后 sessionStorage 键: jy-application-vod-he-ui_STORAGE_KEY_JWT_TOKEN
                            与 jy-application-vod-he-ui_STORAGE_KEY_TENANT_ID
"""
import asyncio, json, os, shutil, signal, socket, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

TMP_DIR = "/tmp" if os.name != "nt" and os.path.isdir("/tmp") else tempfile.gettempdir()
STATE_DEFAULT = os.path.join(TMP_DIR, "nuaa-skill-state.json")
QR_PNG = os.path.join(TMP_DIR, "nuaa-qr.png")
LOG_PATH = os.path.join(TMP_DIR, "nuaa-qr-login.log")
VENV_DIR = os.path.expanduser("~/.cache/nuaa-course-video/venv")
CURL_BIN = "curl.exe" if os.name == "nt" else "curl"

# 仅对本地环回地址 (127.0.0.1 / localhost) 绕过代理, 保留用户访问校内/外网所需的系统代理:
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _find_chrome():
    env_bin = os.environ.get("CHROME_BIN")
    if env_bin and os.path.exists(env_bin):
        return env_bin
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    if os.name == "nt":
        for env_var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env_var)
            if base:
                candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
        for env_var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            base = os.environ.get(env_var)
            if base:
                candidates.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
    for name in ("google-chrome", "chrome", "chromium", "msedge"):
        which_path = shutil.which(name)
        if which_path:
            candidates.append(which_path)
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return candidates[0]


CHROME = _find_chrome()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36" if os.name == "nt" else
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _venv_python():
    if os.name == "nt":
        return os.path.join(VENV_DIR, "Scripts", "python.exe")
    return os.path.join(VENV_DIR, "bin", "python3")


def bootstrap_venv():
    """确保 websockets 可用; 没有 venv 就建一个并装依赖后重新 exec。"""
    try:
        import websockets  # noqa: F401
        return
    except ImportError:
        pass
    py = sys.executable
    vpy = _venv_python()
    os.makedirs(os.path.dirname(VENV_DIR), exist_ok=True)
    if not os.path.exists(vpy):
        subprocess.run([py, "-m", "venv", VENV_DIR], check=True)
    subprocess.run([vpy, "-m", "pip", "install", "-q", "websockets", "pycryptodome"], check=True)
    if os.name == "nt":
        sys.exit(subprocess.call([vpy] + sys.argv))
    os.execv(vpy, [vpy] + sys.argv)


bootstrap_venv()
import websockets  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def is_png(path):
    try:
        with open(path, "rb") as f:
            return f.read(8) == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False


def show_qr(path):
    """默认只打印二维码路径(供对话内/ZCode 内联展示), 不弹任何窗口;

    显式传 --open 时, macOS 经 Preview / Windows 经系统默认图片查看器弹出。
    历史参数 --no-open 仍接受, 行为与默认一致, 已有调用不受影响。"""
    if "--open" in sys.argv:
        if sys.platform == "darwin":
            subprocess.run(["open", path])
        elif os.name == "nt":
            os.startfile(path)
    else:
        print("QR_PNG_PATH=" + path)


async def cdp_eval(ws, n, expr):
    await ws.send(json.dumps({"id": n, "method": "Runtime.evaluate",
                              "params": {"expression": expr, "returnByValue": True}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == n:
            return msg.get("result", {}).get("result", {}).get("value")


async def wait_for_target(port, timeout=90):
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            raw = _DIRECT_OPENER.open(f"http://127.0.0.1:{port}/json/list", timeout=3).read()
            targets = json.loads(raw)
            pages = [t for t in targets if t.get("type") == "page"]
            good = [t for t in pages if t.get("url", "").startswith(("http://", "https://"))]
            if good:
                return good
        except Exception as e:
            last_err = e
        await asyncio.sleep(0.5)
    raise RuntimeError(f"CDP target not available in time (last_err={last_err})")


def _curl_get(path):
    url = "https://authserver.nuaa.edu.cn" + path
    return subprocess.run([CURL_BIN, "-s", "--max-time", "10", url],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def generate_qr_sync():
    """立即通过后端接口生成二维码 PNG 并打印路径 (无需等待无头浏览器加载完 SPA)。"""
    token = _curl_get(f"/authserver/qrCode/getToken?ts={int(time.time()*1000)}").strip()
    if not token or token.startswith("{") or "<" in token:
        raise RuntimeError("getToken failed: " + str(token)[:100])
    qr_png = Path(QR_PNG).resolve()
    allowed_qr_dirs = [Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()]
    if os.name != "nt":
        allowed_qr_dirs.append(Path("/private/tmp"))
    if not any(qr_png == d or qr_png.is_relative_to(d) for d in allowed_qr_dirs):
        raise RuntimeError(f"unexpected qr path: {qr_png}")
    with open(qr_png, "wb") as f:
        subprocess.run([CURL_BIN, "-s", "--max-time", "15",
                        f"https://authserver.nuaa.edu.cn/authserver/qrCode/getCode?uuid={token}"],
                       stdout=f)
    if not is_png(QR_PNG):
        raise RuntimeError("QR endpoint returned non-PNG (likely an HTML page); refresh and retry")
    show_qr(QR_PNG)
    print(f"QR generated: {token} -> shown. Ask the user to scan it now.")
    return token


async def qr_loop(ws, initial_token=None):
    """轮询二维码状态, 用户确认后在无头浏览器会话中提交表单。"""
    counter = [100]

    async def ev(expr):
        r = await cdp_eval(ws, counter[0], expr)
        counter[0] += 1
        return r

    token = initial_token or generate_qr_sync()
    last_status = None
    while True:
        await asyncio.sleep(2)
        st = _curl_get(f"/authserver/qrCode/getStatus.htl?ts={int(time.time()*1000)}&uuid={token}").strip()
        if st != last_status:
            print("scan status:", st)
            last_status = st
        if st == "3":  # 过期
            token = generate_qr_sync()
            last_status = None
        elif st == "1":  # 已确认, 刷新登录页拿新 execution 后立刻提交
            # 关键: 登录页若开了太久, CAS 的 execution 会话过期, 提交会被弹回。
            # 实测确认后的 uuid 在几分钟内仍有效, 所以先 location.reload()
            # 拿全新会话, 再写入 uuid 并提交, 全程控制在几秒内。
            for attempt in range(2):
                try:
                    await ev("location.reload()")
                except Exception:
                    pass  # reload 期间执行上下文会销毁, 忽略
                form_ok = False
                for _ in range(15):
                    await asyncio.sleep(1)
                    try:
                        if await ev("!!document.getElementById('qrLoginForm')"):
                            form_ok = True
                            break
                    except Exception:
                        continue  # 页面跳转中, 下次再查
                if not form_ok:
                    raise RuntimeError("qrLoginForm not found after reload")
                ok = await ev(f"""(() => {{
                  const inp = document.getElementById('uuid');
                  if (!inp) return false;
                  inp.value = '{token}';
                  document.getElementById('qrLoginForm').submit();
                  return true;
                }})()""")
                if not ok:
                    raise RuntimeError("uuid input missing at submit")
                print("confirmed, login form submitted (attempt %d)" % (attempt + 1))
                # 等跳转: 落到 ft.nuaa 即成功; 还停在 authserver 说明被弹回, 重试一次
                landed = False
                for _ in range(20):
                    await asyncio.sleep(2)
                    try:
                        href = await ev("location.href") or ""
                    except Exception:
                        continue
                    if "ft.nuaa.edu.cn" in href:
                        landed = True
                        break
                    if "authserver" not in href:
                        break
                if landed:
                    return True
                print("submit bounced back, retrying with fresh page...")
            raise RuntimeError("login submit bounced twice; QR token likely expired, restart the skill")


async def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # 后台运行时日志实时可见
    state_path = STATE_DEFAULT
    keep = "--keep" in sys.argv
    pos_args = []
    skip_next = False
    for i, a in enumerate(sys.argv[1:], 1):
        if skip_next:
            skip_next = False
            continue
        if a == "--state" and i < len(sys.argv):
            state_path = sys.argv[i + 1]
            skip_next = True
            continue
        if a.startswith("--"):
            continue
        pos_args.append(a)
    target = pos_args[0] if pos_args else "https://ft.nuaa.edu.cn/jy-application-vod-he-ui/"

    if not os.path.exists(CHROME):
        raise RuntimeError(f"未找到 Chrome/Edge 浏览器可执行文件: {CHROME} (可通过环境变量 CHROME_BIN 指定路径)")

    # 第一步: 立即拉取并展示二维码 (耗时 < 1 秒, 不受 Chrome 启动速度影响)
    initial_token = generate_qr_sync()

    prof = tempfile.mkdtemp(prefix="nuaa-skill-prof-")
    port = free_port()
    logf = open(LOG_PATH, "a", encoding="utf-8", errors="replace")
    popen_kwargs = {"stdout": logf, "stderr": logf}
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    chrome = subprocess.Popen(
        [CHROME, f"--user-data-dir={prof}", "--headless=new", "--disable-gpu",
         "--disable-extensions", "--no-first-run", "--no-default-browser-check",
         f"--remote-debugging-port={port}", f"--user-agent={UA}", target],
        **popen_kwargs)
    print(f"headless chrome pid={chrome.pid} port={port} bin={CHROME}")

    try:
        pages = await wait_for_target(port)
        page = pages[0]
        import inspect
        ws_kwargs = {"max_size": 80 * 1024 * 1024}
        if "proxy" in inspect.signature(websockets.connect).parameters:
            ws_kwargs["proxy"] = None
        ws = await websockets.connect(page["webSocketDebuggerUrl"], **ws_kwargs)
        ii = [1]

        async def ev(expr):
            r = await cdp_eval(ws, ii[0], expr)
            ii[0] += 1
            return r

        # 等待页面跳转到统一身份认证
        href = ""
        redirected_to_sso = False
        for _ in range(35):
            await asyncio.sleep(1)
            href = await ev("location.href") or ""
            if "authserver.nuaa.edu.cn" in href:
                redirected_to_sso = True
                break
        if not redirected_to_sso:
            print("no SSO redirect; assuming existing session")
        else:
            print("redirected to SSO:", href[:80])
            await qr_loop(ws, initial_token=initial_token)
            # 登录提交后等 SPA 跳回
            for _ in range(60):
                await asyncio.sleep(2)
                href = await ev("location.href") or ""
                if "ft.nuaa.edu.cn" in href:
                    break
            print("final:", href[:100])
            if "ft.nuaa.edu.cn" not in href:
                raise RuntimeError("login did not land on ft.nuaa.edu.cn")

        # 等 SPA 完成初始化并写入 sessionStorage(轮询至多 20s)
        tokens = None
        for _ in range(10):
            await asyncio.sleep(2)
            tokens = await ev("""JSON.stringify({
              jwt: sessionStorage.getItem('jy-application-vod-he-ui_STORAGE_KEY_JWT_TOKEN'),
              refresh: sessionStorage.getItem('jy-application-vod-he-ui_STORAGE_KEY_REFRESH_TOKEN'),
              tenant: sessionStorage.getItem('jy-application-vod-he-ui_STORAGE_KEY_TENANT_ID') || 'RBAC'
            })""")
            if tokens and json.loads(tokens).get("jwt"):
                break
        if not tokens or not json.loads(tokens).get("jwt"):
            raise RuntimeError("jwt not found in sessionStorage after login")
        data = json.loads(tokens)
        state = {"jwt": data["jwt"], "tenant": data["tenant"], "page_url": href,
                 "created": time.time()}
        sp = Path(os.path.expanduser(state_path)).resolve()  # --state 来自 argv, 限制写入范围
        sp_allowed = [Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve(),
                      Path.home().resolve(), Path.cwd().resolve()]
        if os.name != "nt":
            sp_allowed.append(Path("/private/tmp"))
        if ".." in state_path or not any(
                sp == d or sp.is_relative_to(d) for d in sp_allowed):
            raise RuntimeError(f"refusing state path: {state_path}")
        # state 里是登录 jwt, 限制为仅文件主可读写(0600), 防同机其他用户读取
        fd = os.open(sp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        print("state saved to", sp, "| jwt len", len(data["jwt"]),
              "| tenant", data["tenant"])
        await ws.close()
    finally:
        logf.close()
        if not keep:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(chrome.pid)],
                               capture_output=True)
                time.sleep(0.5)
            else:
                try:
                    os.killpg(os.getpgid(chrome.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
            shutil.rmtree(prof, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())