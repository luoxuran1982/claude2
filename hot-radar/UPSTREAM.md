# 上游来源与改动记录

热点雷达的核心功能（热榜抓取、关键词/AI 筛选、AI 分析与翻译、HTML 报告、推送、MCP 服务）全部来自开源项目 TrendRadar。
本目录下 `upstream/` 是它的源码副本，我们只在外面加了一层桌面外壳（`hotradar/`）。

| 项目 | 内容 |
| --- | --- |
| 上游项目 | TrendRadar（作者 sansan0） |
| 地址 | <https://github.com/sansan0/TrendRadar> |
| 版本 | 主程序 v6.11.0，MCP 服务 v4.1.1，配置文件版本见 `upstream/version_configs` |
| 提交 | `ceabb02`（2026-10-13，「chore: 升级主项目至 v6.11.0、MCP 至 v4.1.1」） |
| 许可证 | GNU GPL-3.0（原文：`upstream/LICENSE`，本目录 `LICENSE` 是同一份） |
| 复制方式 | `git archive` 导出，不带 `.git` 历史 |

## GPL-3.0 对热点雷达意味着什么

- TrendRadar 按 GPL-3.0 发布。热点雷达把它的代码打包进同一个应用一起运行，属于它的**衍生作品**，所以**整个热点雷达（包括我们写的外壳、界面、打包脚本）同样按 GPL-3.0 发布**，不能改成闭源或附加更严格的条款。
- **分发安装包时必须同时提供对应的完整源码**。热点雷达在应用内「使用指南 → 源码」里可以逐个查看全部源码，并一键下载源码 zip（含 `upstream/` 全部上游代码、我们的外壳、文档和打包脚本），安装包本身就附带这份源码，满足这一要求；GitHub 仓库的 `hot-radar/` 目录也是公开的对应源码。
- 必须保留原作者的版权和许可声明：`upstream/LICENSE` 原样保留，上游文件里的署名不删；本文件列出我们改过哪些上游文件以及原因（GPL-3.0 第 5 条要求修改版注明改动）。
- 用户拿到安装包后可以自由使用、修改、再分发，但再分发时同样要附源码、保持 GPL-3.0。
- GPL-3.0 不提供任何担保（见许可证第 15、16 条）。

## 复制时没有带过来的文件

| 上游路径 | 原因 |
| --- | --- |
| `_image/`（7.8 MB） | README 用的宣传截图和二维码，应用用不到。`upstream/README.md` 里的图片因此显示不出来，文字完整 |
| `output/`（6.3 MB） | 上游作者附带的 7 天样例数据库。我们只从其中 `output/news/2025-12-27.db` 取了 3 次抓取结果，整理成离线演示夹具 `samples/newsnow-sample-2025-12-27.json`（见下文） |
| `README-EN.md`、`README-MCP-FAQ-EN.md`、`config/config.en.yaml`、`config/frequency_words.en.txt`、`config/timeline.en.yaml`、`setup-windows-en.bat` | 英文版文档和配置，与中文版内容相同。热点雷达全中文，只保留中文版 |
| `docs/`（GitHub Pages 上的可视化配置编辑器网页） | 它从 GitHub 在线读取配置；热点雷达有自己的离线配置界面 |
| `index.html`（仓库根目录） | 上游给 GitHub Pages 生成的一份报告样例，属于运行产物，不是源码 |
| `.github/ISSUE_TEMPLATE/`、`.github/workflows/issue-guard.yml` | 上游仓库自己的 Issue 管理，与程序无关 |

保留了上游的 `.github/workflows/crawler.yml` 等文件作为参考（说明上游原本怎样在 GitHub Actions 上定时运行）。它们放在 `hot-radar/upstream/.github/` 下，GitHub 只执行仓库根目录 `.github/workflows/` 里的工作流，所以不会被触发。

## 对上游代码的改动（补丁清单）

原则：尽量不改上游。桌面外壳需要的行为（不自动打开浏览器、不联网检查上游版本、离线演示数据）都在我们自己的
`hotradar/worker.py` 里运行时替换，不动上游文件。只有用户能直接看到的英文提示才改成中文。
每处改动都在行尾留了 `# 热点雷达补丁` 注释，可以用 `grep -rn "热点雷达补丁" upstream/` 找到全部。

| # | 文件 | 改动 | 原因 |
| --- | --- | --- | --- |
| 1 | `upstream/trendradar/core/frequency.py` 第 62 行 | 正则无效时的提示 `Warning: Invalid regex pattern '/…/': …` 改为中文「警告：正则表达式无效 '/…/'，已按普通关键词处理：…」 | 关键词写错正则时这句话会显示在应用的抓取日志和关键词检查结果里，界面要求全中文 |
| 2 | `upstream/trendradar/report/generator.py` 第 222 行 | 报告渲染函数缺失时的兜底页面标题 `Report` 改为「报告」，并加上 `<meta charset="utf-8">` | 兜底页面会出现在「最新报告」里；不声明编码时中文可能乱码 |

## 运行时替换（不改上游文件）

这些都写在 `hotradar/worker.py`，只在热点雷达的抓取工作进程里生效：

| 替换对象 | 做法 | 原因 |
| --- | --- | --- |
| `webbrowser.open` | 换成空函数 | 上游每次生成报告后会用系统浏览器打开它；桌面版在应用窗口里显示报告 |
| `trendradar.__main__.check_all_versions` | 换成只打印一句说明、返回「无需更新」 | 上游每次运行都去 GitHub 检查新版本并提示升级；桌面版的上游代码随热点雷达安装包一起更新，提示上游新版本会让人误以为能单独升级 |
| `DataFetcher.fetch_data`（仅「离线演示」） | 改为从 `samples/newsnow-sample-2025-12-27.json` 读取 | 离线演示和自动化测试不访问 newsnow |
| 环境变量 | 把用户在应用里填写的 AI Key、推送地址以环境变量（`AI_API_KEY`、`FEISHU_WEBHOOK_URL` 等，上游原生支持）传给工作进程；去掉 `GITHUB_ACTIONS`；设置 `LITELLM_LOCAL_MODEL_COST_MAP=True` | 密钥不写进 `config.yaml`；CI 里也按本机模式运行；LiteLLM 启动时不联网下载模型价格表 |

## 离线演示夹具

`samples/newsnow-sample-2025-12-27.json` 由上游 `output/news/2025-12-27.db` 中 21:45、22:42、23:40 三次抓取的排名生成
（11 个平台，每次约 250 条标题），格式与 newsnow 接口返回的 `items` 相同。它是上游作者当天真实抓到的热榜标题，
只用于离线演示和测试，**不是当前热点**；应用里用它生成的报告放在单独的「演示」工作区，与正式数据分开，界面上标明「离线演示」。

## 升级上游的方法

见 `docs/03 源码阅读与重建指南.md` 的「升级上游 TrendRadar」一节：重新 `git archive` 新版本到 `upstream/`，
按本文件删掉不需要的文件，`grep "热点雷达补丁"` 对照本表重新打补丁，跑测试，再更新本文件的版本号和补丁表。
