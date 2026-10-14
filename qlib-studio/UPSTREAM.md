# 上游来源与改动记录

Qlib 量化研究台的计算核心（数据格式与表达式引擎、Alpha158 / Alpha360 因子、LightGBM / XGBoost / CatBoost / 线性模型、
IC 计算、组合回测、风险指标、`dump_bin` 数据转换脚本）全部来自微软开源项目 **Qlib**。
本目录下 `upstream/` 是它的源码副本，我们只在外面加了一层桌面外壳（`qlibstudio/`）。

| 项目 | 内容 |
| --- | --- |
| 上游项目 | Qlib（Microsoft，「AI-oriented Quantitative Investment Platform」） |
| 地址 | <https://github.com/microsoft/qlib> |
| 版本 | 标签 **v0.9.8** |
| 提交 | `54355232463878d2eebb91fe0ee5fa7fa1f5976c`（2026-10-08，「fix(security): constrain config-driven code execution (#2340)」，当时 main 分支的最新提交） |
| 许可证 | MIT（原文：`upstream/LICENSE`；本目录 `LICENSE` 是研究台外壳的 MIT 许可，并附上游版权声明） |
| 复制方式 | `git archive` 导出，不带 `.git` 历史 |
| PyPI 轮子 | 截至 2026-10-14，PyPI 上 `pyqlib` 最新是 0.9.7（2025-08）；0.9.8 没有现成轮子，所以两个 Cython 扩展由 `packaging/build_ext.py` 在 CI 里编译（见下文） |

## MIT 许可对研究台意味着什么

- 可以自由使用、修改、打包分发（包括闭源），唯一要求是**保留上游的版权和许可声明**：`upstream/LICENSE` 原样保留，
  安装包内附带（应用内「使用指南 → 附件 / 源码」可以看到），上游文件里的版权头不删。
- 研究台自己的代码也按 MIT 发布（`LICENSE`）。
- MIT 不提供任何担保。Qlib 和本研究台都只是研究工具，回测结果不构成投资建议。

## 复制时没有带过来的文件

| 上游路径 | 原因 |
| --- | --- |
| `docs/_static/img/`（约 3.8 MB） | 上游文档网站用的插图和动图，应用用不到。`upstream/docs/` 里的 .rst 文字完整，图片显示不出来 |
| `examples/benchmarks/GRU/*.pkl`、`examples/benchmarks/LSTM/*.pkl`（4 个，约 0.7 MB） | 上游附带的预训练深度模型（pickle 格式）。研究台不打包 PyTorch，用不到；pickle 文件加载时可以执行任意代码，不随安装包分发更安全 |
| `examples/benchmarks/TCTS/workflow.png` | 示意图 |

上游的 `.github/workflows/` 保留在 `upstream/.github/` 下作为参考；GitHub 只执行仓库根目录 `.github/workflows/` 里的工作流，所以它们不会被触发。
`upstream/.gitignore` 也原样保留：它让编译出来的 `*.so` / `*.pyd` / `*.cpp` 不进仓库。

## 对上游代码的改动（补丁清单）

原则：尽量不改上游。桌面版需要的行为都在我们自己的代码里做「运行时替换」（见下一节），不动上游文件。
改动处都有「Qlib 量化研究台补丁」注释，可以用 `grep -rn "Qlib 量化研究台补丁" upstream/` 找到全部。

| # | 文件 | 改动 | 原因 |
| --- | --- | --- | --- |
| 1 | `upstream/qlib/_version.py`（新增文件） | 写死 `version = "0.9.8"` | 上游这个文件由 setuptools-scm 在 `pip install` 时根据版本库历史生成，仓库里不提交。源码副本没有 `.git`，没有它时 `import qlib` 会去调 setuptools-scm 推算版本并失败 |

只有这一处。上游的 `setup.py`、`pyproject.toml` 没改：编译 Cython 扩展用我们自己的 `packaging/build_ext.py`（在临时目录里编，不经过 setuptools-scm），见下文。

## 运行时替换（不改上游文件）

| 替换对象 | 在哪做 | 做法 | 原因 |
| --- | --- | --- | --- |
| `scripts/dump_bin.py` 里的 `ProcessPoolExecutor` | `qlibstudio/data/convert.py` `_dump_bin_module()` | 换成 `ThreadPoolExecutor` | 上游用多进程并行写二进制文件；打包后的程序里每开一个子进程都会把整个应用再启动一遍（PyInstaller 的限制），换成线程池结果相同 |
| `qlib.workflow.QlibRecorder.log_metrics` | `qlibstudio/experiment.py` `silence_recorder()` | 换成空函数 | 上游 `LGBModel.fit` 每一轮都调用 `R.log_metrics` 写 MLflow；第一次调用会自动开一个 MLflow 实验记录，并用 shell 执行 `git diff / git status` 记录代码版本——在桌面版里会在实验目录外建 `mlruns/`，Windows 上还会闪出命令行窗口。研究台自己保存训练损失曲线（`evals_result`），不需要 MLflow |
| `qlib.init(...)` 参数 | `qlibstudio/experiment.py` `init_qlib()` | `kernels=1`、`expression_cache=None`、`dataset_cache=None` | 在工作进程里顺序计算因子，不再开多进程（同上，打包后多进程会重启应用）；不用磁盘缓存和 Redis |
| 回测的涨跌停规则 | `qlibstudio/experiment.py` `limit_threshold()` | 传上游原生支持的「表达式二元组」`("$close/Ref($close,1)-1 >= $limit_rate-0.002", …)` | 上游 A 股示例用统一的 ±9.5%，不分板块。研究台导入数据时写入每只股票每天的涨跌停幅度 `$limit_rate`（主板 10%、ST 5%、创业板注册制后/科创板 20%、北交所 30%）。也可以在界面上切回上游口径 |

另外，`NumpyOrderIndicator.to_series()` 在 pandas 2.x 下会报错（把 qlib 自己的 `Index` 对象直接交给 `pd.Series`）。研究台取成交明细时改用每个字段的 `to_dict()`（`experiment.extract_trades`），没有改上游。

## 研究台没有用到的上游部分

为了让安装包保持在 300–500 MB，下面这些上游代码随源码一起保留（使用指南里可以读），但不打进程序、不装依赖：

- 深度模型 `qlib/contrib/model/pytorch_*.py`（LSTM、GRU、ALSTM、Transformer、TRA、HIST 等）——需要 PyTorch（约 1 GB）。
  从源码运行并 `pip install torch` 后，可以按 `upstream/examples/benchmarks/<模型>/` 里的 YAML 用上游 `qrun` 命令跑。
- 强化学习下单 `qlib/rl/`、元学习 `qlib/contrib/meta/`、报告绘图 `qlib/contrib/report/`（plotly）、在线服务 `qlib/contrib/online/`、
  高频数据、组合优化器（cvxpy）、`qrun` 命令行（jinja2）、`qlib.workflow` 的 MLflow 实验管理界面。

## 编译上游 Cython 扩展

`upstream/qlib/data/_libs/rolling.pyx`、`expanding.pyx` 是滚动/扩展窗口算子（Alpha158 的 `Mean`、`Std`、`Slope`、`Rsquare` 等都用它们）。
`python packaging/build_ext.py` 把它们编成 `rolling.cpython-312-*.so`（macOS/Linux）或 `.pyd`（Windows），放回原目录。
需要 Cython、numpy 和 C++ 编译器；GitHub Actions 的 macOS、Windows 机器自带编译器，CI 每次构建前都会编一次。

## 升级上游的方法

见 `docs/03 源码阅读与重建指南.md` 的「升级上游 Qlib」一节：重新 `git archive` 新版本到 `upstream/`，
按本文件删掉不需要的文件，补 `qlib/_version.py`，检查「运行时替换」的几个对象是否还在，跑测试，再更新本文件和 `qlibstudio/__init__.py` 的版本号。
