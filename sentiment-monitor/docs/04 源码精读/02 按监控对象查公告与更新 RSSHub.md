---
author: Claude
created: 2026-10-22
updated: 2026-10-22
version: 0.3.1
tags: [Claude, 舆情监控, 源码精读]
---

> v0.3.1 新增两块：按监控对象查公告（`sources/hub.py` 第 152–307 行），以及检查/更新本机 RSSHub（`rsshub.py` 第 169–322 行）。代码片段原样截取，行号是真实行号。

## 本篇会遇到的新词

| 词 | 一句话解释 |
|---|---|
| 代码对照表 | 「股票代码 → 网站内部编号」的映射。巨潮叫 orgId（如 `gssh0600519`），SEC 叫 CIK（如英伟达 `1045810`）。网站查公告认的是内部编号，不认股票代码 |
| TTL（存活时间） | 缓存多久之后算过期、要重新取 |
| 闭包 / 回调 | 把一个函数当参数交给别人，让对方在需要时调用，例如 `_DailyCache(_load_cninfo_orgs)` |
| `git pull --ff-only` | 只在「本机没有自己的新提交、可以直接快进到远端最新」时才更新，否则拒绝，防止产生合并冲突 |
| 编译（build） | 把源代码（TypeScript）转换成可以直接运行的程序（JavaScript），放进 `dist/` |
| 退出码 | 程序结束时返回的数字，0 表示成功，非 0 表示失败 |

## 1. 这两块负责什么

- **按监控对象查公告**：对每个带代码的监控对象，直接用代码到巨潮（A 股、港股）或 SEC（美股）查**这家公司自己的**公告。比「订阅全市场公告、再按标题里的名字筛」更准，也不会因为两次抓取之间公告太多而漏掉。
- **更新 RSSHub**：本机 RSSHub 是快照，不会自己更新。这一块把「拉代码 → 装依赖 → 编译 → 重启」做成一个按钮，失败时自动还原。

## 2. 函数清单

| 函数 | 位置 | 输入 | 输出 | 被谁调用 | 一句话职责 |
|---|---|---|---|---|---|
| `_DailyCache.get` | hub.py:160 | — | 对照表 | 两个新数据源 | 一天取一次对照表 |
| `_load_cninfo_orgs` | hub.py:168 | — | `{(市场, 代码): orgId}` | `_DailyCache` | 取巨潮 3 张对照表 |
| `_cninfo_target` | hub.py:184 | 监控对象 | `(市场, 代码)` 或 None | `CninfoStockSource.fetch` | 判断该不该查、查哪个市场 |
| `CninfoStockSource.fetch` | hub.py:204 | 参数、对象列表 | 资讯列表 | collector | 按代码查巨潮公告 |
| `_load_sec_ciks` | hub.py:243 | — | `{代码: CIK}` | `_DailyCache` | 取 SEC 对照表 |
| `SecFilingsSource.fetch` | hub.py:275 | 参数、对象列表 | 资讯列表 | collector | 按代码查 SEC 申报 |
| `source_dir` | rsshub.py:178 | — | 目录或 None | 本模块 | 找本机 RSSHub 源码 |
| `_env` / `_run` | rsshub.py:184/191 | 命令 | 输出 | 本模块 | 带上 Node 路径运行命令，失败时附最后 8 行输出 |
| `update_status` | rsshub.py:201 | — | 进度字典 | 接口 | 页面轮询进度 |
| `check_behind` | rsshub.py:210 | — | 落后几个提交 | 接口 | `git fetch` 后比较 |
| `start_update` | rsshub.py:227 | — | `{ok}` | 接口 | 开后台线程执行更新 |
| `_step` / `_finish` | rsshub.py:239/302 | 步骤名 | — | `_do_update` | 记进度 |
| `_do_update` | rsshub.py:246 | 源码目录 | — | 后台线程 | 四步更新 + 失败还原 |
| `_stop_local` | rsshub.py:307 | — | — | `_do_update` | 停掉监听 1200 端口的进程 |

## 3. 关键代码逐段讲解

### 3.1 一天一刷新的缓存

`monitor/sources/hub.py` 第 152–165 行：

```python
152  class _DailyCache:
153      """简单的「一天一刷新」缓存：代码对照表（股票代码 → 巨潮 orgId / SEC CIK）一天取一次就够。"""
154  
155      def __init__(self, loader, ttl=24 * 3600):
156          self.loader, self.ttl = loader, ttl
157          self.value, self.at = None, 0.0
158          self.lock = threading.Lock()
159  
160      def get(self):
161          with self.lock:
162              if self.value is None or time.time() - self.at > self.ttl:
163                  self.value = self.loader()  # 失败时抛出，下次再试；不缓存失败结果
164                  self.at = time.time()
165              return self.value
```

- **为什么要缓存**：巨潮的 A 股对照表有 6000 多只股票、几百 KB；SEC 的对照表有 1 万多家公司。每次抓公告都重新下载一遍很浪费，而股票代码和内部编号几乎不变，一天取一次足够。
- **第 155 行** `loader` 是一个**函数**，作为参数传进来（这叫「回调」）。缓存不关心怎么取数据，只负责「什么时候该重新取」。巨潮和 SEC 各传一个自己的取数函数，复用同一个缓存类。
- **第 163 行** 失败时不缓存：`self.loader()` 抛错，`self.value` 保持原样，下次调用再试。
- **第 161 行起用锁**：几个抓取线程可能同时第一次用到对照表，锁保证只下载一次。

### 3.2 判断监控对象属于哪个市场

`monitor/sources/hub.py` 第 184–194 行：

```python
184  def _cninfo_target(e: dict):
185      """监控对象 → (市场, 代码)。只处理 A 股和港股；没有代码的对象跳过。"""
186      code = (e.get("code") or "").strip().upper()
187      market = (e.get("market") or "").upper()
188      if not code.isdigit():
189          return None
190      if market == "HK" or (not market and len(code) == 5):
191          return "HK", code.zfill(5)
192      if market in ("A", "") and len(code) == 6:
193          return "A", code
194      return None
```

- 只认**纯数字代码**：美股字母代码、没有代码的主题词（「人工智能」）都返回 None，被跳过。
- 港股代码补成 5 位：你填 `700`，巨潮的对照表里是 `00700`。`zfill(5)` 就是「左边补 0 到 5 位」。
- 市场没填时靠位数猜：5 位当港股，6 位当 A 股。

### 3.3 按代码查巨潮公告

`monitor/sources/hub.py` 第 204–240 行：

```python
204      def fetch(self, params, entities):
205          out = []
206          for e in entities:
207              target = _cninfo_target(e)
208              if not target:
209                  continue  # 美股、主题词等：这个数据源不管
210              org = _cninfo_orgs.get().get(target)
211              if not org:
212                  raise netutil.FetchError(f"巨潮找不到代码 {target[1]}（{e['name']}）")
213              market, code = target
214              form = {
215                  "pageNum": "1", "pageSize": str(params.get("size") or 20), "column": "hke" if market == "HK" else "szse",
216                  "tabName": "fulltext", "plate": "", "stock": f"{code},{org}", "searchkey": "", "secid": "",
217                  "category": "", "trade": "", "seDate": "", "sortName": "", "sortType": "", "isHLtitle": "true",
218              }
219              text = netutil.fetch(
220                  "http://www.cninfo.com.cn/new/hisAnnouncement/query",
221                  {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "Referer": "http://www.cninfo.com.cn/"},
222                  data=urllib.parse.urlencode(form).encode(),
223              )
224              try:
225                  data = json.loads(text)
226              except json.JSONDecodeError as ex:
227                  raise netutil.FetchError("巨潮资讯返回的不是 JSON（可能改版或限流）") from ex
228              for d in data.get("announcements") or []:
229                  title = strip_html(d.get("announcementTitle") or "")
230                  if not title:
231                      continue
232                  out.append({
233                      "title": title if e["name"] in title else f"{e['name']}：{title}",
234                      "summary": f"{code} {d.get('secName') or ''} 公告（巨潮资讯）",
235                      "url": f"http://static.cninfo.com.cn/{d['adjunctUrl']}" if d.get("adjunctUrl") else "",
236                      "media": "巨潮资讯",
237                      "published_at": parse_time(d.get("announcementTime")),
238                      "entity_ids": [e["id"]],
239                  })
240          return out
```

- **第 210 行** 先查对照表拿到 orgId；**第 216 行** `stock=代码,orgId` 是巨潮网页自己按公司筛选时发的参数（在巨潮网站点进某家公司的公告页，开发者工具里能看到）。
- **第 215 行** 港股用 `column=hke`，A 股用 `szse`（沪市股票也能用 szse 查到，2026-10-22 实测茅台两种都返回相同结果）。
- **第 238 行** `entity_ids: [e["id"]]`：直接告诉入库流程「这条属于哪个监控对象」，不再靠标题去匹配。这就是比「全市场 + 只存命中」更准的原因。

### 3.4 按代码查 SEC 申报

`monitor/sources/hub.py` 第 275–307 行：

```python
275      def fetch(self, params, entities):
276          forms = {f.strip().upper() for f in (params.get("forms") or SEC_DEFAULT_FORMS).split(",") if f.strip()}
277          since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - float(params.get("days") or 90) * 86400))
278          out = []
279          for e in entities:
280              code = (e.get("code") or "").strip().upper()
281              market = (e.get("market") or "").upper()
282              if not code or market not in ("US", "") or not code.replace(".", "").replace("-", "").isalpha():
283                  continue  # 只处理美股代码
284              cik = _sec_ciks.get().get(code.replace(".", "-"))
285              if not cik:
286                  raise netutil.FetchError(f"SEC 找不到股票代码 {code}（{e['name']}）")
287              sub = netutil.fetch_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
288              r = (sub.get("filings") or {}).get("recent") or {}
289              for i, form in enumerate(r.get("form") or []):
290                  date = r["filingDate"][i]
291                  if date < since:
292                      break  # 按时间倒序排列，再往后都更旧
293                  if form.upper().removesuffix("/A") not in forms:
294                      continue
295                  acc = r["accessionNumber"][i]
296                  doc = r["primaryDocument"][i]
297                  desc = r.get("primaryDocDescription", [""] * (i + 1))[i] or ""
298                  cn = SEC_FORMS.get(form.upper().removesuffix("/A"), "")
299                  out.append({
300                      "title": f"{e['name']}：{form}{'（' + cn + '）' if cn else ''}" + (f" {desc}" if desc and form.upper() not in desc.upper() else ""),
301                      "summary": f"{sub.get('name') or code} 向 SEC 提交 {form}，申报日 {date}" + ("（修订版）" if form.upper().endswith("/A") else ""),
302                      "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}",
303                      "media": "SEC EDGAR",
304                      "published_at": parse_time(r["acceptanceDateTime"][i] or date),
305                      "entity_ids": [e["id"]],
306                  })
307          return out
```

- **第 282 行** 只处理字母代码的美股；`BRK.B` 这类带点的也算（去掉点再判断）。
- **第 284 行** SEC 对照表里伯克希尔写作 `BRK-B`，所以把点换成横杠再查。
- **第 287 行** `CIK{cik:010d}`：格式化成 10 位、左边补 0，例如 `CIK0001045810`。这是 SEC 接口规定的文件名格式。
- **第 289–292 行** SEC 返回的申报按时间**倒序**排列，遇到第一条早于「最近 90 天」的就 `break`（跳出循环），后面的不用再看。这比全部看一遍再筛快，属于「利用数据已排序」的常见技巧。
- **第 293 行** `removesuffix("/A")`：`10-Q/A` 是 10-Q 的修订版，按 10-Q 的类型判断要不要收。
- **默认不含 Form 4**（第 261 行的 `SEC_DEFAULT_FORMS`）：Form 4 是公司高管、董事买卖自家股票的申报，大公司一个月几十份，会把资讯流淹没。开发时自查发现，最初版本在没填参数时会把全部类型都放进来（包括 Form 4），已改为用默认清单，并加了测试 `test_sec_filings_default_forms`。

### 3.5 更新 RSSHub：四步 + 失败还原

`monitor/rsshub.py` 第 246–299 行：

```python
246  def _do_update(d: Path):
247      """拉代码 → 装依赖 → 编译 → 重启。编译前备份 dist/，编译失败就还原，旧版照常能用。"""
248      global _started_at
249      backup = d / "dist.bak-update"
250      try:
251          before = _run(["git", "rev-parse", "--short", "HEAD"], d).strip()
252          with _update_lock:
253              _update["before"] = before
254          dirty = [ln for ln in _run(["git", "status", "--porcelain", "--untracked-files=no"], d).splitlines() if ln.strip()]
255          if dirty:
256              raise RuntimeError("本机 RSSHub 有未提交的修改，为避免覆盖，不自动更新：\n" + "\n".join(dirty[:5]))
257          _step("1/4 拉取最新代码", "git pull --ff-only")
258          _run(["git", "pull", "--ff-only", "--quiet"], d, timeout=300)
259          after = _run(["git", "rev-parse", "--short", "HEAD"], d).strip()
260          with _update_lock:
261              _update["after"] = after
262          if after == before and (d / "dist" / "index.mjs").exists():
263              _step("已是最新", f"版本 {after}，不需要编译和重启")
264              _finish(True)
265              return
266          _step("2/4 安装依赖", "pnpm install（可能要几分钟）")
267          _run(["pnpm", "install", "--frozen-lockfile"], d, timeout=1200)
268          _step("3/4 重新编译", "pnpm build（可能要几分钟）")
269          if backup.exists():
270              shutil.rmtree(backup)
271          if (d / "dist").exists():
272              shutil.copytree(d / "dist", backup)
273          try:
274              _run(["pnpm", "build"], d, timeout=1800)
275          except Exception:
276              if backup.exists():  # 编译失败：还原旧的 dist，RSSHub 仍可用旧版启动
277                  shutil.rmtree(d / "dist", ignore_errors=True)
278                  shutil.move(str(backup), str(d / "dist"))
279              raise
280          shutil.rmtree(backup, ignore_errors=True)
281          _step("4/4 重启 RSSHub")
282          _stop_local()
283          _started_at = 0
284          r = start()
285          if not r.get("ok"):
286              raise RuntimeError(r.get("error") or "启动失败")
287          for _ in range(60):  # 最多等 60 秒
288              time.sleep(1)
289              if status()["ok"]:
290                  break
291          else:
292              raise RuntimeError("已重启，但 60 秒内 RSSHub 没有响应，请查看 RSSHub/logs/claude-start.log")
293          with _cache_lock:
294              _cache["at"] = 0  # 路由目录要重新读
295          _step("完成", f"{before} → {after}")
296          _finish(True)
297      except Exception as e:  # noqa: BLE001
298          _step("失败", str(e)[:500])
299          _finish(False, str(e)[:800])
```

- **第 254–256 行** 先检查本机 RSSHub 有没有被人改过（`git status`）。有改动就拒绝更新：自动更新不能覆盖用户自己的修改。测试 `test_refuses_when_dirty` 验证了这一点。
- **第 258 行** `--ff-only`：只做「快进」。打个比方：远端是一条一直往前修的路，本机停在路上某处；快进就是沿着同一条路往前走到终点。如果本机自己另修过一段岔路，就拒绝，避免两条路要「合并」时出冲突。
- **第 262–265 行** 拉完发现没有新东西，就不编译、不重启，省几分钟。
- **第 267 行** `--frozen-lockfile`：严格按仓库里锁定的依赖版本安装，不擅自升级，保证和社区测试过的组合一致。
- **第 269–279 行** 编译前把 `dist/` 复制一份备份；编译失败就删掉写坏的 `dist/`，把备份放回去，然后把错误抛出去。这样 RSSHub 至少还能用旧版启动。这种「先备份、失败回滚」的做法在数据库、部署里都很常见。
- **第 282–292 行** 停掉旧进程 → 启动 → 每秒检查一次，最多等 60 秒。`for … else`：Python 特有写法，循环**没有**被 `break` 打断（60 秒都没响应）时才执行 `else`。
- **第 293–294 行** 把路由目录缓存标为过期，下次打开订阅目录会重新读，新增的路由才会出现。
- **为什么放在后台线程**：整个过程可能要几分钟，如果在处理网页请求的线程里直接跑，页面会一直转圈直到超时。放后台线程后，页面每 3 秒问一次进度（`GET /api/rsshub/update`）。

2026-10-22 实际运行：从 `eb7d2c7`（10-18）更新到 `f2ef406`，48 个社区更新，全程约 40 秒；更新后路由从 4008 条变成 4015 条。

## 4. 错误处理与边界

| 情况 | 处理 |
|---|---|
| 巨潮或 SEC 对照表下载失败 | 本次抓取报错，不缓存失败结果，下次再试 |
| 代码在对照表里找不到 | 报「找不到代码 XXX（对象名）」，其他对象照常 |
| 没填 SEC 联系邮箱 | 访问 `data.sec.gov`、`/files/` 时直接提示去设置里填 |
| 本机 RSSHub 有本地修改 | 拒绝更新 |
| `pnpm build` 失败 | 还原旧 `dist/`，报错显示最后 8 行输出 |
| 重启后 60 秒没响应 | 报错，提示查看 RSSHub 日志 |
| 更新中途关掉应用窗口 | 后台线程随应用退出而中断；已编译完成的部分保留。下次打开再点更新即可（`git pull` 不会重复拉） |

## 5. 动手练习

1. **让 SEC 默认也看内部人交易**：只改第 261 行 `SEC_DEFAULT_FORMS`，在末尾加 `,4`。验证：`test_sec_filings_default_forms` 会失败，想一想为什么，再改测试里的期望值。
2. **把对照表缓存改成 12 小时**：改 `_DailyCache` 的默认 `ttl`。想一想：对照表多久变一次？什么时候需要更频繁（新股上市）？
3. **给更新加一步「更新后自动跑一次实测」**：在 `_do_update` 第 295 行之前调用 `catalog.all_items(force=True)`，看看会不会造成循环导入（提示：`catalog` 导入了 `rsshub`）。

## 读完你应该懂了什么

- 网站查公告认的是内部编号，所以要先有「代码 → 内部编号」对照表，并做缓存。
- 直接按代码查，比「全市场 + 按名字匹配」更准、更不容易漏。
- 自动更新一个外部程序要考虑：别覆盖用户的修改、失败能回滚、慢操作放后台、让页面看得到进度。
