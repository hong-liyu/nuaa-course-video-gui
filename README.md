# nuaa-course-video (Web GUI 版)

NUAA（南京航空航天大学）「飞天云课堂」课程录像与 AI 字幕文稿提取工具，提供开箱即用的**本地 Web 控制台**与跨平台命令行脚本（支持 Windows / macOS / Linux）。

> A local Web GUI and CLI tool for downloading course lecture videos and exporting Markdown transcripts from NUAA's Flipped-Classroom VOD platform (`ft.nuaa.edu.cn`).

> **衍生声明 / Upstream Credit**：本项目基于 [jaluova/nuaa-course-video](https://github.com/jaluova/nuaa-course-video)（MIT License）进行二次开发。原项目完成了飞天云课堂统一身份认证扫码、AES 加密参数解析、课程/字幕/直链 API 调用及分段下载等全部核心能力；本仓库在此基础上进行了 **Windows 全面适配**、新增了**本地 Web 控制台（`gui.py` / `start.bat`）**、增加了**按时间锚点导出 `.md` 复习文稿与字节级实时下载进度监控**，并精简了界面交互。

## 免责声明 / 使用须知

- 本工具仅供本校学生**个人学习复习**使用，请勿用于商业用途。
- 课程录像与课件的著作权归学校和授课教师所有，**请勿将下载内容传播或上传至公开网络**。
- 请控制请求频率（默认配置已足够克制），不要给学校服务器制造压力。
- 如学校或教师明确规定禁止下载，请遵守规定并停止使用。
- 使用本工具产生的任何后果由使用者自行承担。
- 本工具不收集、不存储、不外传任何凭据：登录态只存在于本机临时文件，用后可一键清除。

## 功能特性

- 🌐 **本地 Web 控制台**：开箱即用的单页 Web 界面（`gui.py` + `start.bat`），支持 Windows / macOS
- 🔐 **扫码登录**：走学校统一身份认证的二维码流程，全程不需要提供账号密码，网页端内嵌实时二维码与状态轮询
- 📋 **课程与课次管理**：同步本学期全部课程、关键词过滤或粘贴课程链接直达，支持单课操作或勾选批量操作
- 📝 **课堂字幕导出 Markdown**：将平台 AI 转写字幕按自定义时间锚点（1 / 3 / 5 / 10 分钟）清洗排版为单节 `.md` 与 `00_全课复习文稿汇总.md`
- 🎬 **原片下载与实时监控**：1080p H.264 MP4 直链，4 路并行分段下载 + 字节级实时进度、速度（MB/s）、ETA 与分段进度条，支持中途取消与 ffprobe 时长校验
- 🧹 **凭据卫生**：jwt 登录态只写在系统临时目录文件（`0600` 权限），支持界面一键清除

## 环境要求

| 依赖 | 用途 | 必需 |
|---|---|---|
| Google Chrome / Edge | 无头模式跑登录流程 | 是 |
| Python (3.9+) | 主逻辑；首次运行自动创建 venv 并安装 `websockets`/`pycryptodome` | 是 |
| curl | 接口调用与分段下载 | 是 |
| ffmpeg/ffprobe | 下载后校验时长 | 可选 |

## 快速开始（Web 控制台，推荐 ⭐）

### 1. 获取代码

```bash
git clone https://github.com/hong-liyu/nuaa-course-video.git
cd nuaa-course-video
```

### 2. 启动界面

- **Windows 用户**：直接双击仓库根目录下的 [`start.bat`](start.bat)（或 [`启动GUI.bat`](启动GUI.bat)），或在终端运行：
  ```powershell
  python gui.py
  ```
- **macOS / Linux 用户**：
  ```bash
  python3 gui.py
  ```

启动后会自动在默认浏览器打开 `http://127.0.0.1:8765`（端口被占用时自动顺延）。

### 3. 四步操作流程

1. **扫码登录（左侧 `AUTH` 卡片）**
   点击 **「🔐 启动扫码登录」**，网页内会立即显示统一身份认证二维码。使用手机（南航 App 或微信）扫码并在手机端确认，页面会自动获取令牌并同步本学期全部课程列表。
2. **选择课程（右侧 `STEP 01`）**
   - 在课程列表中**单击**目标课程行，即可自动加载该课堂的全部课次（支持按课程名、教师、班级关键词实时过滤）；
   - 或者直接粘贴飞天云课堂网页链接（`.../video-detail?id=NNN`）或输入数字 `teclId`，点击 **「➡️ 直达加载课次」**。
3. **导出 `.md` 文稿或下载 MP4 视频（右侧 `STEP 02`）**
   - **导出 Markdown 复习文稿**：可选择时间锚点间隔（`1 / 3 / 5 / 10 分钟`），点击单节课右侧 **「📝 导出 .md」**，或点击上方 **「🚀 一键导出本课【全部课次】.md 文稿与汇总」**。输出保存在 `output/<课程名>_<teclId>/` 目录下，包含单讲 `.md` 及 `00_全课复习文稿汇总.md`。
   - **下载 1080p MP4 原片**：点击单节课右侧 **「🎬 下载 MP4」**，或勾选多节课后点击 **「🎬 批量下载勾选课次 MP4 视频」**。
4. **实时进度监控（左侧 `PROGRESS` 卡片）**
   - 执行导出或下载时，实时显示当前阶段（请求直链 / 探测大小 / 4 线程并行下载 / 合并写入）、已下载/总大小（MB）、实时网速（MB/s）、预计剩余时间（ETA）及 4 通道分段进度条；
   - 支持点击 **「🛑 取消任务」** 随时中止后台任务并清理临时分段文件。

---

## 命令行直接使用

```bash
# 0. 只知道课名时: 列出本学期全部课程, 找到 teclId(支持关键词过滤)
python scripts/nuaa_api.py courses 软件工程

# 1. 扫码登录（生成二维码并等待手机确认；加 --open 可用系统查看器弹出二维码图片）
python scripts/qr_login.py --open

# 2. 列出该课堂的全部课次录像（teclId = 页面 URL 里的 id 参数）
python scripts/nuaa_api.py list <teclId>
# → id=1234567 | 2025-09-01 14:00:00 ~ 14:50:00 | 某某老师 | ...

# 3. 一键批量导出整门课全部字幕 (单课清洗版 .md + 00_全课复习文稿汇总.md)
python scripts/nuaa_api.py subtitles-all <teclId> ./output/我的课程字幕

# 4. 拉取单节课 AI 字幕转 SRT
python scripts/nuaa_api.py subtitle <courseId> ./output/课程名.srt

# 5. 取某节课的 MP4 播放直链（带 auth_key 签名，有时效，拿到尽快下）
python scripts/nuaa_api.py url <courseId>

# 6. 并行分段下载 + 合并 + 校验
python scripts/download_mp4.py "<mp4_url>" ./output/课程名_教师_日期_时间.mp4
```

> 播放地址过期（下载中途 403）时，重新执行第 5 步换新地址，再跑第 6 步即可。

## 工作原理

```
无头 Chrome (全新临时配置, 随机调试端口)
   │  打开视频页 → 302 到统一身份认证 (authserver.nuaa.edu.cn)
   ▼
二维码登录:  GET /authserver/qrCode/getToken   → 令牌
             GET /authserver/qrCode/getCode?uuid=  → 二维码 PNG
             GET /authserver/qrCode/getStatus.htl  → 0 等待 / 2 已扫 / 1 已确认 / 3 过期
   │  手机确认后: 页面 location.reload() 拿新 CAS 会话 → 页面内提交 #qrLoginForm
   ▼
登录态: sessionStorage 里的 jwt-token + tenantid → 存入临时 state 文件
   │  (此后无头 Chrome 即可关闭, 全程不再需要浏览器)
   ▼
课程数据:  GET /v1/subject_vod_list?...&teclIds=<teclId>…   → 课次列表
           GET /v1/course_vod_urls?courseId=<双重URL编码的 AES(id,"ivs3.0")> → MP4 直链
           GET /v1/course_vod_subtitle?courseId=<明文id> → AI 字幕
   ▼
下载: vodserver.nuaa.edu.cn 的签名 MP4, 只需 Referer + 浏览器 UA, 无需 cookie
```

要点（均经实测验证）：

- 接口鉴权是 **`jwt-token` + `tenantid` 请求头**，不是 cookie；
- `course_vod_urls` 的 `courseId` 参数是 CryptoJS `AES.encrypt(id, "ivs3.0")`（OpenSSL salted base64）且**必须双重 URL 编码**（服务端解码两次，单重编码时 base64 的 `+` 会变空格导致解密失败）；
- 字幕接口用**明文** courseId，返回 `{bg, ed, res}` 毫秒时间轴；
- 视频是直链 MP4（平台本身 `vodDownloadEnable=1`），无 DRM。

### 平台接口速查

| 端点 | 用途 | 鉴权 |
|---|---|---|
| `GET /authserver/qrCode/getToken?ts=` | 二维码令牌 | 无 |
| `GET /authserver/qrCode/getCode?uuid=` | 二维码图片 | 无 |
| `GET /authserver/qrCode/getStatus.htl?uuid=` | 扫码状态轮询 | 无 |
| `POST /authserver/login?display=qrLogin&service=…` | 页面表单提交换票据 | 页面会话 |
| `GET /v1/group_subject_vod_list?page…` | 本学期课程列表（找 teclId） | `jwt-token` + `tenantid` |
| `GET /v1/subject_vod_list?teclIds=…` | 课次列表 | `jwt-token` + `tenantid` |
| `GET /v1/course_vod_urls?courseId=…` | 播放直链 | 同上 |
| `GET /v1/course_vod_subtitle?courseId=…` | AI 字幕 | 同上 |

## 项目结构

```
nuaa-course-video/
├── README.md
├── LICENSE
├── gui.py                  # 本地 Web 控制台（扫码/课程管理/.md导出/MP4实时进度下载）
├── start.bat               # Windows 一键启动脚本
├── 启动GUI.bat             # Windows 中文快捷启动入口
├── SKILL.md                # CLI 流程说明与接口要点备忘
└── scripts/
    ├── qr_login.py         # 无头 Chrome + 二维码登录 → state 文件
    ├── nuaa_aes.py         # courseId AES 加密（含站点样本自检）
    ├── nuaa_api.py         # courses / list / url / subtitle / subtitles-all
    └── download_mp4.py     # 并行分段下载 + 实时字节级进度 + 合并校验
```

## 已知限制

- 平台登录态是会话 cookie，**每次运行都需要扫一次码**（这是平台设计，无法安全绕过）。
- AI 字幕由平台侧按课生成，部分课次可能缺失或延迟若干天。
- 播放地址的 `auth_key` 有时效；过期重取即可，不支持离线缓存地址。
- 学校接口或页面改版可能导致流程失效（脚本内的自检会尽量给出明确报错）。

## 安全与信任说明

- 全流程**不接触账号密码**：登录走学校统一身份认证的官方二维码接口，确认动作在你手机上完成。
- 脚本持有的唯一凭据是飞天云课堂单个应用的 `jwt-token` 会话：仅能访问本平台的课表/播放/字幕接口，不是统一身份认证密码，动不了教务、邮箱等其他系统；写入本机临时文件时权限为 `0600`（仅文件主可读），流程结束即删除，服务端也会在数小时后过期。
- 凭据只发往 `ft.nuaa.edu.cn`（https），脚本不与任何第三方服务器通信。
- 请只从可信源获取脚本。**恶意改版的二维码工具可以把你的扫码确认会话转发给攻击者**（扫码钓鱼）——如果得到的二维码来源不明，不要扫。

## 致谢与原项目贡献

- **原项目**：衷心感谢 [jaluova/nuaa-course-video](https://github.com/jaluova/nuaa-course-video) 作者完成了飞天云课堂登录认证流程、AES 参数加密分析、API 封装及多线程下载等核心工作。
- **平台方**：南京航空航天大学「飞天云课堂」（苏州科达 Kedacom 智慧课堂产品线）。
- 接口结构与 AES 口令均来自平台前端公开 JS 的分析，未对平台做任何破解或攻击。

## License

[MIT](LICENSE)
