# Qlib 量化研究台 1.0.1（QlibStudio）

微软开源量化投资平台 **Qlib**（<https://github.com/microsoft/qlib>，MIT，约 4.9 万星）的全中文桌面版。Qlib 本身是 Python 库加命令行，没有界面；
研究台把它的 Alpha158 / Alpha360 因子、LightGBM / XGBoost / CatBoost / 线性模型、滚动训练和组合回测装进一个 macOS / Windows 应用窗口，
再补上数据导入（通达信、CSV、富途 / moomoo OpenD、社区 A 股数据集）、数据质量检查、报告图表，以及和用户自己的
「QuantLab 量化学习实验室」并排对比。上游代码放在 `upstream/`（固定在 v0.9.8，只补了 1 个文件），见 [UPSTREAM.md](<UPSTREAM.md>)。

> 仅供学习研究，不构成投资建议。

- **阅读入口**：[docs/00 项目阅读入口](<docs/00 项目阅读入口.md>)
- 使用指南（启停、数据、实验、报告、对比、故障、进程与内存、缩放）：[docs/01 使用指南](<docs/01 使用指南.md>)
- 软件框架与系统架构：[docs/02 软件框架与系统架构](<docs/02 软件框架与系统架构.md>)
- 源码阅读与重建：[docs/03 源码阅读与重建指南](<docs/03 源码阅读与重建指南.md>)
- 与 QuantLab 的对比：[docs/04 与 QuantLab 的对比](<docs/04 与 QuantLab 的对比.md>)
- 安装说明（随安装包附带）：[docs/安装说明](<docs/安装说明.md>)

## 下载安装包

从仓库的 Releases 页面下载（由 GitHub Actions 的「Qlib 量化研究台」工作流构建）：

| 文件 | 平台 |
| --- | --- |
| `QlibStudio-1.0.1-macOS-arm64.dmg` | macOS（Apple 芯片），拖进「应用程序」，首次右键 →「打开」 |
| `QlibStudio-Setup-1.0.1-x64.exe` | Windows x64 安装程序，装到 `%LOCALAPPDATA%\Programs\QlibStudio\`，不需要管理员权限 |
| `QlibStudio-1.0.1-windows-x64.zip` | Windows 免安装版 |
| `QlibStudio-source.zip` | 完整源码（应用内「使用指南」也能下载） |

数据集、富途缓存、实验结果和设置在 macOS `~/Library/Application Support/QlibStudio/`、Windows `%LOCALAPPDATA%\QlibStudio\`，换新版本和卸载都保留。

## 能做什么

| 模块 | 内容 |
| --- | --- |
| 数据 | 演示数据（模拟 A 股，和 QuantLab 同一生成器）；通达信 `vipdoc` / 导出 txt、CSV / Excel / Parquet（读取逻辑与 QuantLab 相同）；富途 / moomoo OpenD 日 K（显示历史 K 线额度、本地缓存只补缺的日期）；社区数据集 chenditc/investment_data（断点续传）。统一用上游 `dump_bin` 转成 Qlib 格式，自动做质量检查（缺失/停牌、复权提示、数据中断） |
| 实验 | Alpha158 / Alpha360；LightGBM、XGBoost、CatBoost、线性（OLS / Ridge）；股票池；训练 / 验证 / 测试区间或滚动训练；在独立工作进程里运行，有进度，可取消。深度模型（需 PyTorch）不打包，写明为可选 |
| 报告 | 净值与基准、超额（Qlib 口径）、回撤、IC / RankIC、五分组收益与多空、每日持仓、成交明细（真实股数、不复权价、费用）、训练过程与因子重要性 |
| 对比 | 读取 QuantLab 导出的「实验结果交换格式」JSON，与本应用的实验并排：指标（可按共同区间重算）、净值、超额、IC、设置对照 |
| 其他 | 应用内「使用指南」（全部文档 + 源码只读查看 + 源码 zip）、运行状态（逐进程 PID / 内存）、70%–120% 缩放、版本号、退出时结束全部工作进程 |

## 从源码运行

需要 Python 3.12 和 C++ 编译器（编译上游两个 Cython 扩展，只需一次）。

```bash
pip install -r requirements.txt
python packaging/build_ext.py           # 编译 upstream/qlib/data/_libs/*.pyx
python run.py                           # 应用窗口
python run.py --browser                 # 用默认浏览器
python run.py --serve-only --port 18800 # 只起本机服务（开发、测试）
```

## 测试与打包

```bash
python -m unittest discover -s tests -v                       # 外壳测试（含真实跑 Qlib 实验、模拟 OpenD、本机 HTTP 下载）
cd upstream/tests && python -m pytest -q misc security ... && cd ../..   # 上游离线测试（完整清单见工作流）
node tests/web.test.js                                        # 缩放档位、Markdown 渲染
python packaging/smoke.py --source                            # 冒烟：启动 → 接口 → 文档 → 实验 → 取消 → OpenD → 对比 → 退出
pip install pyinstaller pillow && python packaging/build.py   # 在哪个系统上运行就打哪个系统的包
```

## 目录

```text
run.py             入口（--worker 进入工作进程）
qlibstudio/        我们写的外壳：app.py 窗口与启停、server.py 本机服务、jobs.py 任务管理、worker.py 工作进程、
                   experiment.py 实验流程（调用上游 Qlib）、compare.py 对比格式、guide.py 使用指南、procinfo.py 进程与内存、
                   data/ 数据（readers/adjust/market/demo 来自 QuantLab；convert、quality、futu、community、datasets）、web/ 界面
upstream/          上游 Qlib v0.9.8 源码（MIT，1 处补丁）
samples/           对比交换格式样例（随机数，不是真实结果）
packaging/         build_ext.py、build.py、installer.nsi、make_icon.py、smoke.py
tests/             test_qlibstudio.py、web.test.js
docs/              00–04 文档、安装说明、assets/ 架构图与截图
```

## 许可证

MIT（外壳）+ 上游 Qlib 的 MIT 许可声明（`upstream/LICENSE`）。图表库 ECharts 为 Apache-2.0。详见 [LICENSE](<LICENSE>) 和 [UPSTREAM.md](<UPSTREAM.md>)。
