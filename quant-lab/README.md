# QuantLab 量化学习实验室

用你本地的 K 线做 **机器学习选股 + A 股规则回测** 的 Windows 桌面应用。双击打开，导入通达信 `vipdoc` 文件夹（或 CSV/Excel），选因子和模型，点一下就得到滚动训练、样本外评估、带手续费和涨跌停约束的组合回测，以及模型对最新交易日的选股排名。

- 方案与架构（含发散的备选方案、路线图）：[docs/方案设计.md](docs/方案设计.md)
- 安装：[docs/安装说明.md](docs/安装说明.md)
- 使用、数据格式、命令行、扩展：[docs/使用说明.md](docs/使用说明.md)

> 仅供学习研究，不构成投资建议。

## 能做什么

| 模块 | 内容 |
| --- | --- |
| 数据 | 通达信 `.day`、通达信导出 txt、CSV/Excel（中英文列名自动识别）、Tushare、Parquet、分钟线自动聚合；单位自动换算；**不复权数据自动识别送转除权**；增量合并；K 线查看器 |
| 掩码 | 股票池（板块、上市天数、流动性、ST、最低价）；可买/可卖（一字涨停买不进、一字跌停卖不出、停牌）；新股前 5 天无涨跌幅 |
| 因子 | 73 个量价因子 / 9 组；截面排名或去极值 Z 分数；单因子 RankIC 检验 |
| 模型 | LightGBM（回归 / LambdaRank 排序 / 分类）、随机森林、sklearn 梯度提升、岭回归、MLP；多选即集成 |
| 训练 | 滚动训练（walk-forward）+ 隔离带，早停看验证集 RankIC，只用样本外预测回测 |
| 回测 | T+1 开盘成交、100 股一手、佣金最低 5 元、印花税、滑点、持仓缓冲带、止损、退市清算、指数或等权基准 |
| 评估 | 年化/夏普/索提诺/最大回撤/卡玛/信息比率/Beta、IC/RankIC/ICIR、分层收益与单调性、因子重要性、月度收益、交易明细 |
| 其他 | 实验对比、最新选股、导出 CSV、配置 JSON、命令行批量运行、演示数据（模拟 A 股） |

## 下载安装包

GitHub → Actions → **quant-lab** → 最新一次运行 → Artifacts：

| 文件 | 用途 |
| --- | --- |
| `QuantLab-Windows-x64` | 安装程序 `QuantLab-Setup-1.0.0-x64.exe` + 免安装 zip |
| `QuantLab-source` | 完整源码 zip |

推一个 `quant-v*` 标签（如 `quant-v1.0.0`）会自动发布到 Releases。

## 从源码运行

```bash
pip install -r requirements.txt
python run.py              # 独立窗口（WebView2），不可用时自动改浏览器
python run.py --browser    # 浏览器
python run.py demo         # 命令行：生成演示数据（更多见 docs/使用说明.md）
```

Windows 也可以直接双击 `start-windows.bat`。

## 自己打包（在 Windows 上）

```bash
pip install -r requirements.txt pyinstaller pillow
python packaging/build.py      # 需要 NSIS 才会生成安装程序：choco install nsis
dist\QuantLab\QuantLab.exe --selftest out.json   # 自检：演示数据跑完整实验
```

## 测试

```bash
python -m unittest discover -s tests -v
```

17 项，覆盖：代码识别、各种文件格式读取与单位换算、除权识别、**因子无未来函数（截断一致性）**、标签对齐、涨跌停掩码、滚动训练隔离带、回测记账与 A 股规则、端到端实验、HTTP 接口与令牌校验。

## 时间约定

```
t 日收盘：用 t 日及以前数据算因子、打分
t+1 日开盘：先卖后买（涨停买不进 → 顺延下一只；跌停卖不出 → 次日重试）
持有 N 天：标签 = t+1 开盘 → t+1+N 开盘的收益（与回测一致）
训练：只用标签在测试开始前已经实现的样本（隔离带 = N+1 天）
```

## 目录

```
run.py                启动入口（界面 / 命令行 / --selftest）
quantlab/
  data/               readers.py 读文件 · adjust.py 除权识别 · store.py 数据集 · demo.py 模拟数据
  market.py           代码、板块、涨跌停幅度
  masks.py            股票池与交易掩码、标签
  features.py         因子库
  dataset.py          样本矩阵
  models.py           模型注册表
  trainer.py          滚动训练
  evaluate.py         IC、分层、单因子
  backtest.py         组合回测
  metrics.py          绩效指标
  experiment.py       实验流程与结果存储
  server.py / app.py  本机服务 + 窗口
  cli.py              命令行
  web/                界面（原生 JS + ECharts）
packaging/            build.py（PyInstaller + NSIS）、installer.nsi、make_icon.py
tests/                单元与端到端测试
docs/                 方案设计、安装说明、使用说明
```
