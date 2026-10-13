# 热点雷达 1.0.0（HotRadar）

开源项目 **TrendRadar**（<https://github.com/sansan0/TrendRadar>，GPL-3.0，约 6.3 万星）的桌面版：聚合微博、知乎、抖音、B 站、百度等热榜和 RSS，按关键词或 AI 筛选，生成 HTML 报告，推送到飞书/钉钉/企业微信/Telegram/邮件等，附带 MCP 服务。我们只加了一层桌面外壳（窗口、可视化配置、任务管理、使用指南、运行状态、缩放、打包），上游代码放在 `upstream/`，改了 6 处，见 [UPSTREAM.md](<UPSTREAM.md>)。

引入目的：和用户自己的「舆情监控系统」对比，见 [docs/04 与舆情监控系统的对比](<docs/04 与舆情监控系统的对比.md>)。

- **阅读入口**：[docs/00 项目阅读入口](<docs/00 项目阅读入口.md>)
- 使用指南（启停、抓取、关键词、推送、AI Key、数据、故障、进程与内存、缩放）：[docs/01 使用指南](<docs/01 使用指南.md>)
- 软件框架与系统架构：[docs/02 软件框架与系统架构](<docs/02 软件框架与系统架构.md>)
- 源码阅读与重建：[docs/03 源码阅读与重建指南](<docs/03 源码阅读与重建指南.md>)
- 安装说明（随安装包附带）：[docs/安装说明](<docs/安装说明.md>)

## 下载安装包

从仓库的 Releases 页面下载（由 GitHub Actions 的「热点雷达」工作流构建）：

| 文件 | 平台 |
| --- | --- |
| `HotRadar-1.0.0-macOS-arm64.dmg` | macOS（Apple 芯片），拖进「应用程序」，首次右键 →「打开」 |
| `HotRadar-Setup-1.0.0-x64.exe` | Windows x64 安装程序，装到 `%LOCALAPPDATA%\Programs\HotRadar\`，不需要管理员权限 |
| `HotRadar-1.0.0-windows-x64.zip` | Windows 免安装版 |
| `HotRadar-source.zip` | 完整源码（GPL-3.0 对应源码；应用内「使用指南」也能下载） |

数据和设置在 macOS `~/Library/Application Support/HotRadar/`、Windows `%LOCALAPPDATA%\HotRadar\`，换新版本和卸载都保留。

## 数据来源

热榜来自第三方公开接口 **newsnow**（开源项目 ourongxing/newsnow 的公开实例 `newsnow.busiyi.world`），不是各平台官方接口，也不归 TrendRadar 或本项目所有；它可能限流或停止服务，失败时对应平台记为「失败」，已有数据不受影响。可以自建 newsnow 后在「配置 → 平台与 RSS」改接口地址。网络不通时「离线演示」用上游仓库附带的 2025-12-27 样例热榜跑全流程。

## 从源码运行

需要 Python 3.12。

```bash
pip install -r requirements.txt -c constraints.txt
python run.py                  # 应用窗口
python run.py --browser        # 用默认浏览器
python run.py --serve-only --port 18790   # 只起本机服务（开发、测试）
```

## 测试与打包

```bash
python -m unittest discover -s tests -v            # 外壳测试（含离线演示、MCP 看门狗等集成测试）
cd upstream && python -m pytest -q tests && cd ..   # 上游自带测试
node tests/web.test.js                              # 缩放档位、Markdown 渲染
python packaging/smoke.py --source                  # 冒烟：启动 → 接口 → 演示 → MCP → 退出
pip install pyinstaller pillow && python packaging/build.py   # 在哪个系统上运行就打哪个系统的包
```

## 目录

```text
run.py             入口（--worker 进入工作进程）
hotradar/          我们写的外壳：app.py 窗口与启停、server.py 本机服务、runner.py 任务管理与定时、
                   worker.py 工作进程、configio.py 配置读写、keywords.py 关键词检查、reports.py 报告、
                   guide.py 使用指南、procinfo.py 进程与内存、paths.py 路径、web/ 界面
upstream/          上游 TrendRadar 6.11.0 源码（GPL-3.0，6 处补丁）
samples/           离线演示夹具（取自上游样例库）
packaging/         build.py、installer.nsi、make_icon.py、smoke.py
tests/             test_hotradar.py、web.test.js
docs/              00–04 文档、安装说明、assets/ 架构图与截图
constraints.txt    由上游 uv.lock 导出的依赖版本
LICENSE            GPL-3.0
```

## 许可证

GPL-3.0。热点雷达把 TrendRadar 打包在同一个应用里运行，属于它的衍生作品，整个热点雷达（包括外壳）同样按 GPL-3.0 发布；分发安装包时附带完整源码（应用内可查看和下载）。详见 [UPSTREAM.md](<UPSTREAM.md>)。
