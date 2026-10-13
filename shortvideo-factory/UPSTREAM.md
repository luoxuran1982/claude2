---
author: Claude
created: 2026-10-13
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

原则：尽量不改上游。窗口、使用指南、运行状态、缩放、打包都放在 `svf/` 和 `packaging/`；Streamlit 页面里的中文化和缩放由外壳在运行时插入脚本完成（`svf/web/inject.js`），不改上游文件。只有「让上游把数据写到用户数据目录」这类必须在上游内部改的地方才打补丁。每处改动在源码里都带「AI 短视频工厂补丁」注释，完整 diff 见 [upstream-patches.diff](upstream-patches.diff)（由 `packaging/upstream_diff.py` 生成）。

| 编号 | 文件 | 改了什么 | 为什么 |
| --- | --- | --- | --- |
| P1 | `app/config/config.py` | `config_file` 优先取环境变量 `MPT_CONFIG_FILE`；保存配置时的临时文件放在配置文件所在目录（原来是项目根目录） | 安装目录只读（macOS 的 .app 改了会破坏签名），配置和 API Key 必须放用户数据目录；原来临时文件写在项目根目录，只读时保存会失败 |
| P2 | `app/utils/utils.py` | `storage_dir()`、`font_dir()`、`song_dir()` 分别优先取 `MPT_STORAGE_DIR`、`MPT_FONT_DIR`、`MPT_SONG_DIR` | 生成的视频、素材缓存、用户自己放的字体和音乐都放用户数据目录，升级重装不丢 |
| P3 | `webui/Main.py` | 界面里的字体、音乐目录改用 `utils.font_dir()` / `utils.song_dir()`（原来写死 `resource/fonts`）；「打开任务目录」用 `utils.task_dir()`（原来写死 `storage/tasks`）；默认字幕字体可由 `MPT_DEFAULT_FONT` 指定 | 和 P2 保持一致；微软雅黑不随包分发后，「恢复默认」要落到存在的开源字体上 |
| P4 | `webui/i18n/zh.json` | 9 个仍是英文的标签改成中文：Base Url → 接口地址（Base URL），Account ID → 账号 ID，以及 VoxCPM / Chatterbox / Kokoro / OFox / MuAPI 的 Base URL、MiniMax TTS Endpoint / Model | 界面全中文的要求；只改译文，不改键名 |

不设环境变量时，P1–P3 的行为和上游完全一样（从源码直接 `streamlit run webui/Main.py` 照常可用）。

## 依赖的版本

随包 Python（3.11，与上游 `.python-version` 一致）里的依赖按上游 `uv.lock` 的精确版本安装，清单 `requirements-runtime.txt` 由 `packaging/make_runtime_requirements.py` 生成。原因：上游 `requirements.txt` 只钉了直接依赖，间接依赖会解析到最新版；实测 starlette 1.8.0 + streamlit 1.59.1 时 Streamlit 自带的图标字体请求返回 500，界面上的图标变成 `expand_more`、`settings` 这样的英文单词，而上游锁定的 starlette 1.2.1 没有这个问题。

默认不打包的可选依赖：`faster-whisper`（以及只被它用到的 `ctranslate2`、`onnxruntime`、`av` 等）。它只在把「字幕生成方式」改成 whisper 时才用，还要另外下载约 3 GB 的模型；默认的 edge 方式直接用 Edge TTS 返回的时间戳生成字幕，不需要它。需要的话见 [01 使用指南](<docs/01 使用指南.md>) 的「可选组件」。

## 怎样升级到上游新版本

1. 克隆上游新版本，记下 commit；用 `python packaging/upstream_diff.py <旧版上游目录>` 确认当前补丁（或直接看本文件和 `upstream-patches.diff`）。
2. 用新版源码整体替换 `upstream/`（同样删掉上表的字体和音乐），先单独提交一次「原样副本」。
3. 按上表 P1–P4 逐条重新打补丁（上游改了同一处时，按「为什么」一栏的目的重新实现），再提交。
4. 在 `upstream/` 里运行 `python ../packaging/make_runtime_requirements.py`（需要 uv）重新生成依赖清单；检查新版本有没有新增大体积的可选依赖。
5. 改 `svf/__init__.py` 里的 `UPSTREAM_COMMIT`、`UPSTREAM_VERSION`，递增应用版本号；更新本文件和 docs。
6. 跑 `python tests/run_upstream_tests.py` 和外壳测试，推送后看 CI 的冒烟测试；在容器里用 `tests/ui_check.py` 检查界面是否还有新冒出来的英文（Streamlit 升级常会改提示文字，需要在 `svf/web/inject.js` 的对照表里补上）。
