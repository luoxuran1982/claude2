# AI 短视频工厂

把开源项目 [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo)（MIT 许可，GitHub 约 13 万星）包成可以双击安装的桌面应用：**给一个主题 → 大模型写文案和搜索关键词 → 从 Pexels / Pixabay 等免费素材站下载视频片段拼接 → 配音 + 字幕 + 背景音乐 → 合成竖屏或横屏短视频**。

当前版本 **1.0.0**，内置上游 MoneyPrinterTurbo 1.3.8（commit `bbe7eb9`，2026-10-10）。macOS（Apple 芯片）和 Windows（x64）各有安装包，打开后是独立窗口。做这个项目的目的，是和本仓库根目录的「AI 知识视频工坊」（`studio/`）对比两种做视频的思路，见 [04 与 AI 知识视频工坊的对比](<docs/04 与AI知识视频工坊的对比.md>)。

## 安装与打开

安装包在仓库的 [Releases 页面](https://github.com/luoxuran1982/claude2/releases)（标签 `shortvideo-factory-v1.0.0`）。

| | macOS（Apple 芯片） | Windows（x64） |
| --- | --- | --- |
| 下载 | `ShortVideoFactory-1.0.0-macOS-arm64.dmg` | `ShortVideoFactory-Setup-1.0.0-x64.exe`（另有免安装 `ShortVideoFactory-1.0.0-windows-x64.zip`） |
| 安装 | 双击 `.dmg`，把「AI 短视频工厂.app」拖进「应用程序」 | 双击 Setup.exe，装到 `%LOCALAPPDATA%\Programs\ShortVideoFactory\`，不需要管理员权限，开始菜单和桌面有快捷方式 |
| 首次打开 | 没有 Apple 签名：在「应用程序」里右键 →「打开」→「打开」；仍被拦就到「系统设置 → 隐私与安全性」点「仍要打开」 | 没有代码签名：SmartScreen 提示时点「更多信息 → 仍要运行」 |
| 退出 | ⌘Q、菜单「退出」、程序坞右键「退出」、点红色按钮关窗口、顶栏「退出」 | 点 ×、Alt+F4、任务栏右键「关闭窗口」、顶栏「退出」 |
| 卸载 | 把 `.app` 拖进废纸篓 | 「设置 → 应用」或开始菜单里的卸载快捷方式 |
| 数据目录（卸载后保留） | `~/Library/Application Support/ShortVideoFactory/` | `%LOCALAPPDATA%\ShortVideoFactory\` |

关闭窗口或退出时，生成界面后台（Streamlit）、正在运行的 FFmpeg 等本应用启动的进程都会结束，端口释放；最小化只是隐藏，应用继续运行。安装包约 300 MB（自带 Python 运行时和视频处理组件），不需要另装 Python 或 FFmpeg。

## 第一次使用要准备的

1. **大模型 API Key**（写文案、生成搜索关键词）：顶部「设置」里选服务商（DeepSeek、通义千问、Kimi、智谱、OpenAI 兼容接口、本地 Ollama 等），填 Key 和模型名，点「测试连接」。
2. **Pexels API Key**（免费，下载视频素材）：到 <https://www.pexels.com/api/> 注册后申请，填到「设置 → 素材来源」。也可以用 Pixabay（<https://pixabay.com/api/docs/>）。
3. 可选：把你有权使用的背景音乐（mp3）放进数据目录的 `songs/`。上游自带的音乐来源不明，没有随包分发。

详细步骤见 [01 使用指南](<docs/01 使用指南.md>)。应用里所有 Key 只保存在你电脑上的 `config.toml`，不上传、不进仓库。

## 应用内有什么

- **视频生成**：上游 MoneyPrinterTurbo 的完整界面（中文），文案、素材、配音、字幕、背景音乐、合成都在这里。
- **使用指南**：离线显示本 README、[UPSTREAM.md](UPSTREAM.md) 和 `docs/` 下全部文档；「源码」里逐个只读查看外壳和上游源码（带行号），可下载源码 zip。
- **运行状态**：本应用每个进程的职责、PID、内存（macOS RSS / Windows 工作集，MiB）和采样时间。
- **界面缩放**：顶栏右侧「−」、百分比下拉、「＋」、「100%」，70%–120% 每 5% 一档，作用于整个界面（包括生成界面），下次打开保留。

## 文档

| 文档 | 内容 |
| --- | --- |
| [00 项目阅读入口](<docs/00 项目阅读入口.md>) | 项目是什么、先读哪篇、当前状态 |
| [01 使用指南](<docs/01 使用指南.md>) | 安装、首次准备、出片流程、数据与结果、故障排查、进程与内存、缩放 |
| [02 软件框架与系统架构](<docs/02 软件框架与系统架构.md>) | 上游架构、外壳架构、进程关系、调用链、数据位置 |
| [03 源码阅读与重建指南](<docs/03 源码阅读与重建指南.md>) | 手写文件职责、阅读路线、修改与重建、升级上游 |
| [04 与 AI 知识视频工坊的对比](<docs/04 与AI知识视频工坊的对比.md>) | 两边的生成思路、成本、准确性、版权、适合题材、可借鉴之处、对比实验步骤 |
| [UPSTREAM.md](UPSTREAM.md) | 上游来源、许可证、删掉的文件、每个补丁的原因 |

## 目录结构

```
shortvideo-factory/
  run.py                    源码运行入口（开发用）
  svf/                      外壳：启动器、本机服务与反向代理、进程管理、使用指南、运行状态、界面
  upstream/                 上游 MoneyPrinterTurbo 源码（bbe7eb9，打了 4 处小补丁，见 UPSTREAM.md）
  fonts/                    随包的开源字体（思源黑体 Noto Sans SC Bold）和 OFL 许可证
  packaging/                打包脚本（PyInstaller + 随包 Python + NSIS / dmg）与 CI 冒烟测试
  tests/                    外壳单元测试、上游离线测试的筛选运行器、界面检查脚本
  docs/                     文档（同时是应用内「使用指南」的来源，同步到 Obsidian）
  requirements.txt          外壳依赖；requirements-runtime.txt 是生成界面后台的依赖（按上游锁定版本）
```

## 从源码运行（开发者）

```bash
cd shortvideo-factory
python3.11 -m venv .venv && . .venv/bin/activate      # Windows：.venv\Scripts\activate
pip install -r requirements.txt -r requirements-runtime.txt
python run.py              # 独立窗口（不可用时自动改用浏览器）
python run.py --browser    # 用默认浏览器打开
python -m unittest discover -s tests -v
python packaging/build.py  # 在哪个系统上运行就打出哪个系统的安装包（需要 pyinstaller pillow uv）
```

## 许可证

- 上游 MoneyPrinterTurbo：MIT（[upstream/LICENSE](upstream/LICENSE)）。
- 思源黑体 Noto Sans SC、Be Vietnam Pro、Charm：SIL Open Font License 1.1（见 `fonts/OFL-*.txt`）。
- 用本应用做出来的视频里，素材的版权归素材站作者，按 Pexels / Pixabay 的许可使用；配音、背景音乐的使用条件见 [01 使用指南](<docs/01 使用指南.md>) 的「版权与合规」。
