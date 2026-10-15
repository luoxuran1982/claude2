---
author: Claude
created: 2026-10-13
updated: 2026-10-15
version: 1.1.0
tags: [Claude, AI 短视频工厂, 上游, MoneyPrinterTurbo]
---

本项目的视频生成功能全部来自开源项目 MoneyPrinterTurbo。这份文件记录来源、许可证、我们删掉了什么、改了什么以及原因，方便以后跟进上游新版本。

## 来源

| 项 | 值 |
| --- | --- |
| 项目 | MoneyPrinterTurbo（作者 harry0703） |
| 地址 | <https://github.com/harry0703/MoneyPrinterTurbo> |
| 版本 | 1.3.8（`pyproject.toml`） |
| commit | `bbe7eb9`（2026-10-10，fix(loomloom): avoid synthesizing narration from nontext results） |
| 许可证 | MIT（原文见 [upstream/LICENSE](upstream/LICENSE)，随安装包分发） |
| 放在哪 | `shortvideo-factory/upstream/`，不带 `.git`；仓库里的第一个提交是原样副本，之后的提交才是补丁，可以用 `git log -p -- shortvideo-factory/upstream` 逐个查看 |

## 按版权要求删掉的文件

这些文件不能（或说不清能不能）随我们的安装包分发，所以既不提交进仓库，也不打包：

| 文件 | 原因 | 替代 |
| --- | --- | --- |
| `resource/fonts/MicrosoftYaHeiBold.ttc`、`MicrosoftYaHeiNormal.ttc` | 微软雅黑，字体内嵌声明「Microsoft supplied font… as permitted by the license terms of the Microsoft product」，属于随 Windows 授权的商业字体 | 随包提供思源黑体 Noto Sans SC Bold（SIL OFL 1.1），是默认字幕字体；用户可自己把系统字体复制到数据目录的 `fonts/` |
| `resource/fonts/STHeitiLight.ttc`、`STHeitiMedium.ttc` | 华文黑体，版权声明 Changzhou SinoType，随 macOS 授权的商业字体 | 同上 |
| `resource/fonts/UTM Kabel KT.ttf` | 越南 UTM 字体，内嵌只写「Free for everyone」，是 ITC Kabel 的仿制，授权说不清 | 不需要（不含中文字形） |
| `resource/songs/output000.mp3` 等 29 首 | 上游 README 自述「来自 YouTube 视频，如有侵权，请删除」，来源和授权说不清 | 不随包提供；用户把自己有权使用的 mp3 放进数据目录的 `songs/`（应用首次启动时在那里放了说明） |

保留的上游字体 `BeVietnamPro-*.ttf`、`Charm-*.ttf` 是 SIL OFL 1.1（字体内嵌声明可查），许可证全文另存为 `fonts/OFL-BeVietnamPro.txt`、`fonts/OFL-Charm.txt`。上游 `docs/sponsors/` 里的赞助商标志只在仓库里保留（上游 README 引用），不打进安装包。

## 补丁（对上游源码的改动）

原则：尽量不改上游。窗口、使用指南、运行状态、缩放、打包都放在 `svf/` 和 `packaging/`；Streamlit 页面里的中文化和缩放由外壳在运行时插入脚本完成（`svf/web/inject.js`），不改上游文件。只有「让上游把数据写到用户数据目录」这类必须在上游内部改的地方才打补丁。每处改动在源码里都带「AI 短视频工厂补丁」或「[补丁 Pn]」注释，完整 diff 见 [upstream-patches.diff](upstream-patches.diff)（由 `packaging/upstream_diff.py` 生成）。

| 编号 | 文件 | 改了什么 | 为什么 |
| --- | --- | --- | --- |
| P1 | `app/config/config.py` | `config_file` 优先取环境变量 `MPT_CONFIG_FILE`；保存配置时的临时文件放在配置文件所在目录（原来是项目根目录） | 安装目录只读（macOS 的 .app 改了会破坏签名），配置和 API Key 必须放用户数据目录；原来临时文件写在项目根目录，只读时保存会失败 |
| P2 | `app/utils/utils.py` | `storage_dir()`、`font_dir()`、`song_dir()` 分别优先取 `MPT_STORAGE_DIR`、`MPT_FONT_DIR`、`MPT_SONG_DIR` | 生成的视频、素材缓存、用户自己放的字体和音乐都放用户数据目录，升级重装不丢 |
| P3 | `webui/Main.py` | 界面里的字体、音乐目录改用 `utils.font_dir()` / `utils.song_dir()`（原来写死 `resource/fonts`）；「打开任务目录」用 `utils.task_dir()`（原来写死 `storage/tasks`）；默认字幕字体可由 `MPT_DEFAULT_FONT` 指定 | 和 P2 保持一致；微软雅黑不随包分发后，「恢复默认」要落到存在的开源字体上 |
| P4 | `webui/i18n/zh.json` | 9 个仍是英文的标签改成中文：Base Url → 接口地址（Base URL），Account ID → 账号 ID，以及 VoxCPM / Chatterbox / Kokoro / OFox / MuAPI 的 Base URL、MiniMax TTS Endpoint / Model；1.0.2 起，「设置 → 素材来源」里 Pexels 钥匙标签的「推荐使用」改到 Pixabay，Pexels 注明「目前暂停发放新钥匙」 | 界面全中文的要求；只改译文，不改键名；Pexels 暂停发放新钥匙后，原来「Pexels 推荐使用、Pixabay 备用」的说法会误导新用户 |
| P5 | `webui/Main.py` | 文件开头尝试导入外壳扩展 `svf_ext.webui`（导入不到就是 `None`，以下各处都退回原行为）。① `VIDEO_SOURCE_GROUPS` 加「程序生成」组，视频来源下拉多一组（知识讲解卡片、混合），标签和说明、卡片配色由 `svf_ext` 提供；提交前的「有效来源」名单加上这三个值；② 「打开任务目录」「打开缓存目录」两处原来的 `webbrowser.open("file://…")`、`webbrowser.open(Path(…).as_uri())` 改成新函数 `_open_local_path`（1.0.2 调用 `svf_ext.desktop.open_path`：Windows `os.startfile`、macOS `open`、Linux `xdg-open`；1.0.3 起由 P7 改为请外壳主进程打开）；③ 「设置 → 素材来源」里 Pexels / Pixabay 钥匙输入框下面加格式提示，Coverr 下面加「素材下载代理」输入框；④ 选配音声音后、点「生成视频」时，声音和文案语言不一致就提醒；⑤ 试听失败「配音服务未返回试听音频」后面接上按日志判断的中文原因 | ① 用户讲知识类主题时素材库只有风景动物，要有不依赖素材的来源；② 用户反馈：Windows 上点「打开目录」没有任何反应（`webbrowser` 打开 `file://` 在 Windows 上静默失败）；③ 用户把 Pixabay 钥匙填进 Pexels 栏、Pexels 暂停发新钥匙；需要代理才能下载素材又不想开系统代理；④⑤ 用户选了 af-ZA、zu-ZA 声音导致「No audio was received」，原提示一律先说 403 |
| P6 | `app/services/task.py` | 文件开头尝试导入 `svf_ext.pipeline`（导入不到就是 `None`）。① `_SUPPORTED_VIDEO_SOURCES` 加上三个程序生成来源；② 纯「知识讲解卡片」不调用大模型生成搜索关键词（`generate_terms`）；③ `get_video_materials` 在 local / loomloom 分支之后加一个分支：来源是程序生成组时调用 `svf_pipeline.prepare_materials`（这时配音和字幕已经生成，按它们的时间分页画图，返回一个 mp4 交回上游后续流程）；④ 配音失败 `failed to synthesize audio; …`、素材下载失败 `failed to download video materials from …` 两句英文原文不变，后面接上中文原因 | ③ 讲解页要跟着配音和字幕的时间走，只有素材这一步之后才知道时间；其余流程（转场、字幕、配音、背景音乐、拼接）原样复用；② 少一次大模型调用、少一个失败点；④ 原因只在日志里，界面上看不到 |
| P7 | `webui/Main.py` | 1.0.3，都在「能导入 `svf_ext.webui`」时才生效：① 「生成视频」按钮：有任务在生成（`svf_webui.generation_busy()`）时显示「生成中…」并 `disabled`，下面一行说明；提交前调用 `svf_webui.claim_generation`，不放行就提示原因、不提交；提交后记下目标时长（`note_submission`）、整页重新渲染一次；提交失败撤销登记；② 任务管理：`_collect_task_summaries` 开头调用 `reconcile_tasks`（残留任务标「已中断」）；`_task_state_filter_key` 让「已取消」「已中断」归入「失败」；状态列显示「已取消」「已中断」；生成中的任务在删除按钮的位置显示 ⏹「取消」（`cancel_task`），列表上方说明为什么暂时不能删；③ `_open_local_path` 改为 `svf_webui.open_folder`（经外壳主进程打开），`_open_task_path` 文件夹不存在时不再静默返回；任务管理和「缓存管理」打开失败时显示原因、完整路径和「复制路径」（`render_open_failure`）；④ `_render_generation_logs`：原始日志上方加「日志解读」和「大模型调用记录」（`render_task_insights`），原始日志加行号；⑤ 「文案设置」里「生成视频脚本的语言」下面加「目标时长（秒）」（`render_target_duration`） | ① 用户连按三次「生成视频」起了 3 个任务，多出来的「生成中 0%」删不掉、停不下来；② 上游没有取消功能，删除按钮对生成中的任务是灰的、不说明原因；③ 1.0.2 在后台进程里 `os.startfile` 打开文件夹在 Windows 上仍然没反应（原因见下面「1.0.3」）；④⑤ 用户要对照日志学习、要能设定视频长度 |
| P8 | `app/services/task.py` | 1.0.3：`generate_final_videos` 开头调用 `svf_pipeline.fast_final_videos`；它返回结果（素材这一步留下了知识卡片快速通道的分页计划）就直接返回，否则（库存视频等其他来源）上游原流程不变 | 知识卡片出片太慢（51 秒竖屏最终渲染 5 分钟以上）：讲解视频不该再走为库存素材设计的「处理片段 → 拼接 → MoviePy 逐帧合成」，改为 FFmpeg 一次合成 |
| P9 | `webui/Main.py` | 1.1.0：任务管理每一行的「主题」下面调用 `svf_webui.render_task_links`：任务目录里有分镜清单（`svf_storyboard.json`）就显示「审片 / 分镜」链接，有大模型调用记录（`llm_calls.jsonl`）就显示「模型调用」链接，点了在外壳的同名顶层标签页里打开这个任务 | 用户要求已完成的任务在任务管理里也能打开分镜视图和模型调用；上游任务表格每行的按钮在 `_render_task_table` 里逐个写死，没有可以接的地方，只能加两行 |

不设环境变量时，P1–P3 的行为和上游完全一样（从源码直接 `streamlit run webui/Main.py` 照常可用）。P5–P9 只在能导入外壳扩展 `svf_ext` 时生效：应用里由生成界面后台的入口 `svf_child.py` 把它加到 `sys.path`；单独运行上游、或跑上游自带测试时导入不到，行为和上游完全一样。源码里每处改动都有「[补丁 P5]」…「[补丁 P9]」注释。

### 1.1.0：AI 导演、工坊主题、AI 插图、审片 / 分镜、模型调用、字数控制——只加了 P9 两行

| 功能 | 外壳模块 | 怎样接到上游 | 为什么这样接 |
| --- | --- | --- | --- |
| 新视频来源「知识讲解卡片（AI 导演）」「知识讲解卡片 + AI 插图」 | `svf_ext/__init__.py` 的 `CARD_SOURCES` | P5、P6 本来就是按 `svf_ext.CARD_SOURCES` 这个元组加「程序生成」组、加支持的来源、分流到 `svf_pipeline.prepare_materials`，元组里多两个值就接上了 | 不用改上游 |
| AI 导演 | `svf_ext/director.py`、`pipeline.py` | 在 P6 的素材分支里（此时配音、字幕已生成）调用上游 `llm._generate_response(prompt)`，所以界面上能选的所有大模型服务都能用，也自动进调用记录 | 复用用户配好的那个大模型和上游的各家适配 |
| 工坊主题 | `svf_ext/studio_cards.py`（移植自工坊 `studio/visuals.py`） | `cards.render_page` 遇到工坊主题、新版式或有插图时交给它；快速通道（P8）和 1.0.2 流程照常调用 `cards.render_page` | — |
| AI 插图 | `svf_ext/illustrate.py` | 调用上游 `material.generate_images_openai`（上游的「OpenAI 兼容文生图」素材来源用的同一个函数，读同一组设置 `openai_image_*`） | 用户已经在「设置 → 素材来源设置」里配过；上游自带重试和「结果不明不重提交」的计费保护 |
| 分镜画面和清单 | `svf_ext/shots.py` | 卡片类：P6 的素材分支里画；库存视频：`shots.install()` 包住 `task.generate_final_videos`，出片后按 `script.json` 的 `material_sources` 取素材缩略帧 | 上游按名字调用模块函数，换模块属性即可 |
| 字数控制三档 | `svf_ext/duration.py`、`webui.py` | 包住 `llm.build_script_prompt`（按档位加不同的字数要求）和 `llm.generate_script`（「程序检查」档数字数、偏差大时调用 `llm._generate_response` 请模型改写）；控件由 P7 的「目标时长」位置一起显示 | — |
| 调用记录的着色、思考过程、用途 | `svf_ext/llmlog.py` | 1.0.3 的包装上加：调用前记下提示词里哪些是变量（`parts`），读应答里的 `reasoning_content`，步骤函数返回后写一行「程序怎么用」 | — |
| 「审片 / 分镜」「模型调用」标签页 | `svf/review.py`、`svf/web/review.js`、`calls.js`、`svf/server.py` | 外壳主进程只读任务目录里的文件；生成页的链接由 P7 的 `render_task_insights` 显示，任务管理里的链接由 P9 显示 | 生成页已经很重，查看类功能放到外壳 |

### 1.0.3：新功能尽量「包一层」，少改上游

| 功能 | 外壳模块 | 怎样接到上游 | 为什么这样接 |
| --- | --- | --- | --- |
| 防重复提交、取消、已中断 | `svf_ext/tasks.py` | 导入 `svf_ext.webui` 时 `tasks.install()` 把模块里的 `webui_task.submit_generation`、`webui_task._run_generation`、`task.generate_script / generate_terms / generate_audio / generate_subtitle / get_video_materials / generate_final_videos`、`task._stage_progress_reporter` 换成「先检查 / 登记，再调原函数」的包装，并包住状态后端 `sm.state.update_task / patch_task`（已取消的任务不再被写回）；界面上的按钮、取消、状态文字由 P7 调用 | 上游按名字调用这些模块级函数，换掉模块属性就能接上，不用改流水线源码；取消只能在线程里「到点检查」+ 结束 FFmpeg 子进程，Python 线程不能强行终止 |
| 大模型调用记录 | `svf_ext/llmlog.py` | `llmlog.install()` 包住 `llm._generate_response`（所有大模型调用的必经之路）、`llm.generate_script / generate_terms / generate_social_metadata / test_connection` 和 `task.generate_script / generate_terms`（标出哪一步、哪个任务）；调用期间临时接住 OpenAI SDK `chat.completions.create` 的返回值取原始 JSON 和 token 用量（只读） | 不改上游源码；写记录前去掉所有钥匙 |
| 目标时长 | `svf_ext/duration.py` | 包住 `llm.build_script_prompt`（在末尾加字数要求）和 `task.generate_audio`（记下实际时长）；控件由 P7 放进界面 | 同上 |
| 日志解读 | `svf_ext/explain.py` | 只读上游 `webui_task.get_task_logs` 的快照；位置由 P7 决定 | — |
| 打开目录 | `svf/server.py` `/api/open-path`、`svf_ext/desktop.py` | P7 调用 `svf_webui.open_folder` → 本机 HTTP 请启动器打开 | 见下 |
| 知识卡片快速通道 | `svf_ext/fastcards.py`、`pipeline.py` | P6 的素材分支只留下分页计划，P8 在 `generate_final_videos` 开头接管 | 必须跳过上游的处理片段、拼接和 MoviePy 合成，只能在入口处分流 |

**「打开目录」1.0.2 为什么仍然无效**：1.0.2 的代码确实走到了（同一个包里的知识卡片来源可用，说明 `svf_ext` 加载成功），`os.startfile` 也没报错（报错会在 `webui.log` 留下「打开目录失败」）。发出请求的是生成界面后台：启动器用 `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` 启动、放在作业对象里的无窗口后台进程，按钮回调在 Streamlit 工作线程上（没有初始化 COM）。打开文件夹要由资源管理器经 COM 委托开新窗口、并抢到前台，从这样的进程发出时静默失败或窗口被压在应用窗口后面；「播放」是直接启动播放器程序，所以正常。1.0.3 改为子进程经本机 HTTP（`POST /api/open-path`，带令牌）请启动器打开：启动器拥有应用窗口、是前台进程，在 Windows 上另起线程先 `CoInitializeEx`、`AllowSetForegroundWindow` 再 `os.startfile`，失败退回 `explorer.exe`；只允许数据目录之内的路径。（这是按代码和 Windows 机制的推断，容器里不能复现，待用户在 Windows 上验收。）

### 1.0.2：知识讲解卡片等功能放在外壳扩展 `svf_ext/`

新功能的代码都在 `shortvideo-factory/svf_ext/`（安装包里是 `payload/child/svf_ext/`），和上游跑在同一个 Python 进程里，用上游已有的依赖（Pillow、MoviePy、loguru、edge-tts），不新增依赖。上游只靠 P5、P6 这两处入口调用它：

| 模块 | 做什么 | 上游哪里调用 |
| --- | --- | --- |
| `svf_ext/storyboard.py` | 文案拆句、按字幕（或朗读长度）定时、分页、按句子特征选版式、关键词 | P6 经 `pipeline` |
| `svf_ext/cards.py` | Pillow 画讲解页（移植自「AI 知识视频工坊」`studio/visuals.py`），字体用随包思源黑体 | P6 经 `pipeline` |
| `svf_ext/render.py` | 每页缓慢缩放/平移、页间淡入，原始帧经管道交给 FFmpeg 编码成一个 mp4 | P6 经 `pipeline` |
| `svf_ext/pipeline.py` | 素材流程接入点；混合来源先调上游 `material.download_videos`，不够再补卡片 | P6 |
| `svf_ext/hints.py` | 记下上游配音/素材模块最近的警告和错误日志（loguru 接收器），据此给中文原因；钥匙格式；语言不一致检测 | P5、P6 |
| `svf_ext/desktop.py` | 按平台打开文件夹 | P5 |
| `svf_ext/webui.py` | 界面上的小控件（卡片配色、钥匙提示、素材下载代理、语言提醒） | P5 |

**为了让画面和配音对得上**，程序生成来源把整条时间线做成**一个**片段交给上游，并在本次任务的参数里把「单个片段最大时长」临时改成时间线长度、「片段播放速度」改成 1（上游 `combine_videos` 会按最大时长切片段、按速度变速）。只改这一次任务的参数对象，不改设置。

**素材下载代理**写进上游已有的 `config.proxy`（`config.toml` 的 `[proxy]` 一节，http 和 https 都设成同一个地址）。上游 `material.py` 的素材搜索和下载（Pexels、Pixabay、Coverr）每次请求都传 `proxies=config.proxy`，AI 视频服务（秘塔、OFox、火山 Seedance、MuAPI）的接口也用它；大模型和配音不读这个设置。上游原来只能手改 `config.toml`，现在界面上能填，填了立即生效。

界面上英文出错提示的中文说明仍由外壳页面脚本 `svf/web/inject.js` 补：后台已经接上「【原因】…」时只把英文那句换成中文，判断不出原因时才补一段常见原因。

### 1.0.1 的改动都不碰上游源码

| 改动 | 怎么做的 | 为什么不改上游 |
| --- | --- | --- |
| edge-tts 7.2.7 → 7.3.1 | 只改依赖清单（见下节「依赖的版本」）。逐项核对过上游 `app/services/voice.py` 用到的接口：`edge_tts.Communicate(text, voice, rate=..., boundary="WordBoundary")`（7.3.1 的签名没变，`create_edge_tts_communicate` 按签名探测 `boundary` 也照常）、`Communicate.stream()` / `stream_sync()`、`edge_tts.SubMaker()` 的 `feed` / `get_srt` / `cues`、`edge_tts.srt_composer.Subtitle`——7.2.7 与 7.3.1 的 `submaker.py`、`srt_composer.py` 完全相同，`communicate.py` 只改了内部的偏移量计算和 SSL 上下文缓存，`drm.py` 换成 Sec-MS-GEC v2 令牌（`SEC_MS_GEC_VERSION` 由 `1-…` 变 `2-…`）。不需要补丁 | 上游代码本身没问题，只是锁定的版本旧 |
| 默认配音声音改成中文 | 外壳 `svf/userdata.py`：新建 `config.toml` 时在 `[ui]` 里写 `voice_name = "zh-CN-XiaoxiaoNeural-Female"`；老配置里没有 `voice_name` 时插在 `[ui]` 表头下一行（已有的值，包括空值，一律不动）。上游 `webui/Main.py` 本来就是先用 `config.ui["voice_name"]`，没有时才「按界面语言挑第一个匹配的声音」，界面声音列表顺序不变 | 写配置就够了，改 Main.py 的兜底逻辑反而会影响其他语言界面 |
| 配音失败提示补中文原因 | 外壳 `svf/web/inject.js` 的 `ALERTS`：只在红色提示框（`stAlert`）里，把上游 `task.py` 的 `failed to synthesize audio; verify the selected voice and TTS connectivity` 和 zh.json 的「配音服务未返回试听音频…」后面补一段中文说明 | 上游这句英文也是 API 的错误信息，上游测试和调用方可能依赖原文；在页面上补说明不影响它们 |

说明：容器里实测，全新数据目录、中文界面下上游本来就会默认选中 `zh-CN-XiaoxiaoNeural-Female`（不论浏览器语言），用户遇到的 `zu-ZA-ThandoNeural` 推测是在下拉里误选的（它排在列表最末）。1.0.1 把默认值写进配置，是为了不依赖界面语言这个前提。

## 依赖的版本

随包 Python（3.11，与上游 `.python-version` 一致）里的依赖按上游 `uv.lock` 的精确版本安装，清单 `requirements-runtime.txt` 由 `packaging/make_runtime_requirements.py` 生成。原因：上游 `requirements.txt` 只钉了直接依赖，间接依赖会解析到最新版；实测 starlette 1.8.0 + streamlit 1.59.1 时 Streamlit 自带的图标字体请求返回 500，界面上的图标变成 `expand_more`、`settings` 这样的英文单词，而上游锁定的 starlette 1.2.1 没有这个问题。

**覆盖上游锁定的版本**：`make_runtime_requirements.py` 里的 `OVERRIDES` 把个别包换成指定版本，每条写明原因，生成的清单头部也会列出。目前只有一条：

| 包 | 上游锁定 | 随包 | 原因 |
| --- | --- | --- | --- |
| edge-tts | 7.2.7 | 7.3.1 | 2026-10 微软 Edge 在线朗读接口改了验证，7.2.7 连接时被拒（`403, message='Invalid response status'`），配音全部失败，换声音也没用。edge-tts 7.3.0（2026-10-13）起改用 Sec-MS-GEC v2 令牌，7.3.1 是当前最新版。7.3.1 的依赖（aiohttp ≥3.8,<4；certifi ≥2023.11.17；tabulate ≥0.4.4,<1；typing-extensions ≥4.1,<5）锁文件里的 aiohttp 3.14.0、certifi 2026.2.25、tabulate 0.10.0、typing-extensions 4.15.0 都满足，没有连带升级 |

上游锁文件升级到 edge-tts ≥7.3.1 以后，删掉这条覆盖。

默认不打包的可选依赖：`faster-whisper`（以及只被它用到的 `ctranslate2`、`onnxruntime`、`av` 等）。它只在把「字幕生成方式」改成 whisper 时才用，还要另外下载约 3 GB 的模型；默认的 edge 方式直接用 Edge TTS 返回的时间戳生成字幕，不需要它。需要的话见 [01 使用指南](<docs/01 使用指南.md>) 的「可选组件」。

## 怎样升级到上游新版本

1. 克隆上游新版本，记下 commit；用 `python packaging/upstream_diff.py <旧版上游目录>` 确认当前补丁（或直接看本文件和 `upstream-patches.diff`）。
2. 用新版源码整体替换 `upstream/`（同样删掉上表的字体和音乐），先单独提交一次「原样副本」。
3. 按上表 P1–P9 逐条重新打补丁（P5–P8 依赖上游的 `VIDEO_SOURCE_GROUPS`、`_SUPPORTED_VIDEO_SOURCES`、`get_video_materials`、`generate_final_videos`、`open_task_folder`、`_render_task_table`、`_render_generation_logs`、`_render_generation_controls` 等；1.0.3 的外壳包装还依赖 `webui_task.submit_generation / _run_generation`、`llm._generate_response / build_script_prompt`、`task.generate_*`、`task._stage_progress_reporter` 这些名字，上游改名时 `svf_ext` 会在 `webui.log` 里记「外壳扩展没有接上上游」，跑 `tests/tasks_check.py`、`tests/llm_check.py` 确认，上游改名或改结构时按「为什么」重新找接入点；跑 `tests/test_cards.py` 的离线任务检查确认接通）（上游改了同一处时，按「为什么」一栏的目的重新实现），再提交。
4. 运行 `python packaging/make_runtime_requirements.py`（需要 uv）重新生成依赖清单；检查新版本有没有新增大体积的可选依赖；逐条检查 `OVERRIDES`：上游已跟上的删掉，没跟上的确认依赖要求仍满足。
5. 改 `svf/__init__.py` 里的 `UPSTREAM_COMMIT`、`UPSTREAM_VERSION`，递增应用版本号；更新本文件和 docs。
6. 跑 `python tests/run_upstream_tests.py` 和外壳测试，推送后看 CI 的冒烟测试；在容器里用 `tests/ui_check.py` 检查界面是否还有新冒出来的英文（Streamlit 升级常会改提示文字，需要在 `svf/web/inject.js` 的对照表里补上）。
