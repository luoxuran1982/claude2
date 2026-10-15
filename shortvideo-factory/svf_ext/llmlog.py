"""大模型调用记录（原汁原味）：每次调用大模型都记一行 JSON 到任务目录的 llm_calls.jsonl。

记什么：哪一步调用的（写文案、提取关键词…）、用的哪个服务和模型、接口地址、发给模型的完整提示词（system 和 user 原文）、
模型返回的完整原始输出（上游整理前的文字；OpenAI 兼容接口另存整段原始 JSON 应答）、耗时、token 用量（应答里有才记）、
第几次尝试、成功还是失败。

怎么接的（不改上游源码，只在外壳里包一层，由 svf_ext/webui.py 导入时 install()）：
- 上游所有大模型调用都经过 app/services/llm.py 的 _generate_response(prompt, app_config)。把模块里的这个函数换成
  「先记下输入、再调原函数、再记下输出」的包装；上游 generate_script / generate_terms 等按名字调用它，自然走到包装。
- 「哪一步」：包住 llm.generate_script / generate_terms / generate_social_metadata / test_connection，和任务流水线里的
  task.generate_script / generate_terms（后者还带着任务 ID），用 contextvars 传给记录函数。
- 原始应答和 token 用量：调用期间临时接住 OpenAI SDK 的 chat.completions.create 返回值（只读，不改它）。

1.1.0 起每条记录还有：
- id：记录编号；之后程序怎么用这次的输出，另写一行 {"kind": "use", "ref": id, "note": "取作文案……"}（read_task 合并进 use_notes）；
- parts：发给模型的提示词按「固定模板 / 程序填进去的变量」切成的片段（[{"t": 文字, "k": "tpl"|"var"|"prog", "n": 变量名}]），
  「模型调用」页据此着色；
- reasoning：模型的思考过程（DeepSeek 等返回的 reasoning_content，或输出里的 <think>…</think>），没有就是空。

绝不记 API Key：记录里不放配置；写入前把配置里所有「key / token / secret / password」类字段的值、
以及形如 sk-…、Bearer … 的串替换成「***」。没有任务的调用（界面上点「生成文案和关键词」）记到 storage/llm_calls/webui.jsonl；
之后用这段文案点「生成视频」时，把对应的记录复制进那个任务的 llm_calls.jsonl（标明「在界面上生成」）。
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

FILE_NAME = "llm_calls.jsonl"
STEP_LABELS = {"script": "写文案", "terms": "提取关键词", "revise": "按字数改写文案", "director": "AI 导演设计分镜",
               "social": "生成发布标题和话题", "test": "测试连接", "other": "其他"}
MAX_RAW = 400_000           # 单条原始应答最多保存多少字符
UI_MAX_BYTES = 5_000_000

_ctx: contextvars.ContextVar = contextvars.ContextVar("svf_llm_ctx", default=None)
_raw: contextvars.ContextVar = contextvars.ContextVar("svf_llm_raw", default=None)
_LOCK = threading.Lock()
_INSTALLED = [False]
SECRET_KEY_HINTS = ("key", "token", "secret", "password", "passwd", "credential")
SECRET_PATTERNS = (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"), re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"),
                   re.compile(r"(?i)(api[_-]?key|access[_-]?token)(\"?\s*[:=]\s*\"?)[A-Za-z0-9._\-]{8,}"))


# ---------------------------------------------------------------- 去掉密钥

def secret_values(cfg_sections) -> set[str]:
    out = set()

    def walk(obj, key=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, str(k))
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                walk(v, key)
        elif isinstance(obj, str) and len(obj.strip()) >= 6 and any(h in key.lower() for h in SECRET_KEY_HINTS):
            for part in re.split(r"[,\s]+", obj.strip()):   # 多个钥匙用逗号分隔时逐个去掉
                if len(part) >= 6:
                    out.add(part)
    for sec in cfg_sections:
        walk(sec)
    return out


def _config_secrets() -> set[str]:
    try:
        from app.config import config
        return secret_values([getattr(config, "_cfg", {}) or {}, config.app])
    except Exception:  # noqa: BLE001
        return set()


def scrub(obj, secrets: set[str] | None = None):
    """递归把密钥换成 ***。"""
    secrets = _config_secrets() if secrets is None else secrets
    if isinstance(obj, str):
        for s in sorted(secrets, key=len, reverse=True):
            if s in obj:
                obj = obj.replace(s, "***")
        for pat in SECRET_PATTERNS:
            obj = pat.sub(lambda m: (m.group(1) + m.group(2) + "***") if m.lastindex and m.lastindex >= 2 else "***", obj)
        return obj
    if isinstance(obj, dict):
        return {k: ("***" if any(h in str(k).lower() for h in ("api_key", "apikey", "authorization", "secret", "password"))
                    and isinstance(v, str) and v else scrub(v, secrets)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub(v, secrets) for v in obj]
    return obj


# ---------------------------------------------------------------- 写记录

def task_file(task_id: str) -> Path:
    from app.utils import utils
    return Path(utils.task_dir()) / task_id / FILE_NAME


def ui_file() -> Path:
    from app.utils import utils
    return Path(utils.storage_dir()) / "llm_calls" / "webui.jsonl"


def append(path: Path, record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.name == "webui.jsonl" and path.exists() and path.stat().st_size > UI_MAX_BYTES:
                path.replace(path.with_name("webui.old.jsonl"))
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def read(path) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        out.append(json.loads(ln))
                    except ValueError:
                        continue
    except OSError:
        pass
    return out


def merge_uses(rows: list[dict]) -> list[dict]:
    """把 {"kind": "use"} 行（程序怎么用了某次调用的输出）合并进对应调用的 use_notes，只返回调用记录。"""
    calls = [r for r in rows if r.get("kind") != "use"]
    by_id = {c.get("id"): c for c in calls if c.get("id")}
    for r in rows:
        if r.get("kind") == "use" and r.get("ref") in by_id:
            by_id[r["ref"]].setdefault("use_notes", []).append(r.get("note", ""))
    return calls


def read_task(task_id: str) -> list[dict]:
    try:
        return merge_uses(read(task_file(task_id)))
    except Exception:  # noqa: BLE001
        return []


def _provider_info(app_config) -> dict:
    info = {"provider": "", "model": "", "base_url": "", "adapter": ""}
    try:
        from app.config import config
        from app.models.llm_provider import get_llm_provider
        cfg = app_config if app_config is not None else config.app
        pid = str(cfg.get("llm_provider", "") or "").lower()
        info["provider"] = pid
        prov = get_llm_provider(pid)
        if prov is not None:
            info["provider_label"] = getattr(prov, "default_label", "") or pid
            info["adapter"] = getattr(prov, "adapter", "")
            info["model"] = prov.resolve_model_name(cfg.get(prov.config_key("model_name"), "")) or ""
            info["base_url"] = prov.resolve_base_url(cfg.get(prov.config_key("base_url"), "")) or ""
            if pid == "ollama" and not info["base_url"]:
                info["base_url"] = config.get_default_ollama_base_url()
    except Exception as exc:  # noqa: BLE001
        info["provider_error"] = f"{type(exc).__name__}: {exc}"
    return info


def _messages(prompt: str, adapter: str) -> list[dict]:
    msgs = []
    if adapter == "claude_code":
        try:
            from app.services import llm
            msgs.append({"role": "system", "content": llm.CLAUDE_CODE_SYSTEM_PROMPT})
        except Exception:  # noqa: BLE001
            pass
    msgs.append({"role": "user", "content": prompt})
    return msgs


def _usage(raw_list: list) -> dict | None:
    for r in reversed(raw_list or []):
        u = r.get("usage") if isinstance(r, dict) else None
        if isinstance(u, dict) and u:
            keep = {k: u.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens") if u.get(k) is not None}
            det = u.get("completion_tokens_details") or {}
            if isinstance(det, dict) and det.get("reasoning_tokens") is not None:
                keep["reasoning_tokens"] = det["reasoning_tokens"]
            return keep or u
    return None


THINK_RE = re.compile(r"<think>(.*?)(?:</think>|$)", re.S)


def reasoning_of(raw_list: list) -> str:
    """模型的思考过程：OpenAI 兼容应答里的 reasoning_content / reasoning 字段，或正文里的 <think>…</think>。"""
    for r in reversed(raw_list or []):
        try:
            msg = r["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            continue
        for k in ("reasoning_content", "reasoning"):
            v = msg.get(k) if isinstance(msg, dict) else None
            if isinstance(v, str) and v.strip():
                return v.strip()
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            m = THINK_RE.search(content)
            if m and m.group(1).strip():
                return m.group(1).strip()
    return ""


def make_parts(prompt: str, variables) -> list[dict]:
    """把提示词切成「固定模板」和「变量」片段。variables：[(名称, 值, 前缀或空, 类别 var/prog)]，
    值在提示词里第一次出现（有前缀时找「前缀 + 值」）的位置标成变量；找不到的变量忽略。"""
    marks: list[tuple[int, int, str, str]] = []
    for v in variables or []:
        name, value = v[0], str(v[1] or "")
        anchor = v[2] if len(v) > 2 else ""
        kind = v[3] if len(v) > 3 else "var"
        if not value.strip():
            continue
        pos = prompt.find(anchor + value) if anchor else prompt.find(value)
        while pos >= 0:
            a = pos + len(anchor)
            b = a + len(value)
            if not any(a < mb and ma < b for ma, mb, _, _ in marks):
                marks.append((a, b, name, kind))
                break
            pos = prompt.find(anchor + value if anchor else value, pos + 1)
    marks.sort()
    out, cur = [], 0
    for a, b, name, kind in marks:
        if a > cur:
            out.append({"t": prompt[cur:a], "k": "tpl", "n": ""})
        out.append({"t": prompt[a:b], "k": kind, "n": name})
        cur = b
    if cur < len(prompt):
        out.append({"t": prompt[cur:], "k": "tpl", "n": ""})
    return out


def make_record(prompt: str, output, elapsed: float, app_config, raw_list: list, ctx: dict | None,
                error: str = "") -> dict:
    ctx = ctx or {}
    info = _provider_info(app_config)
    step = ctx.get("step") or "other"
    text = output if isinstance(output, str) else ("" if output is None else str(output))
    ok = bool(text) and not text.startswith("Error:") and not error
    parts = ctx.get("parts")
    if parts is None and ctx.get("prompt_vars"):
        parts = make_parts(prompt, ctx["prompt_vars"])
    rec = {
        "id": uuid.uuid4().hex[:12],
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ts": round(time.time(), 3),
        "task_id": ctx.get("task_id") or "",
        "step": step,
        "step_label": STEP_LABELS.get(step, step),
        "attempt": ctx.get("attempt", 1),
        "provider": info.get("provider", ""),
        "provider_label": info.get("provider_label", info.get("provider", "")),
        "model": info.get("model", ""),
        "base_url": info.get("base_url", ""),
        "messages": _messages(prompt, info.get("adapter", "")),
        "output": text[:MAX_RAW],
        "reasoning": reasoning_of(raw_list)[:MAX_RAW],
        "parts": parts or [],
        "raw_response": [r if len(json.dumps(r, ensure_ascii=False, default=str)) <= MAX_RAW else {"truncated": True}
                         for r in (raw_list or [])],
        "elapsed_seconds": round(elapsed, 3),
        "usage": _usage(raw_list),
        "ok": ok,
        "error": error or (text[len("Error:"):].strip() if text.startswith("Error:") else ""),
    }
    if ctx.get("from_ui"):
        rec["from_ui"] = True
    return scrub(rec)


def record(rec: dict) -> None:
    try:
        path = task_file(rec["task_id"]) if rec.get("task_id") else ui_file()
        append(path, rec)
    except Exception as exc:  # noqa: BLE001
        try:
            from loguru import logger
            logger.warning(f"写大模型调用记录失败：{exc}")
        except ImportError:
            pass


# ---------------------------------------------------------------- 接到上游

def note_use(note: str, ctx: dict | None = None) -> None:
    """记下程序怎么用了这一步最后一次调用的输出（写一行 {"kind": "use"}）。"""
    ctx = ctx if ctx is not None else (_ctx.get() or {})
    ref = ctx.get("last_id")
    if not ref or not note:
        return
    try:
        path = task_file(ctx["task_id"]) if ctx.get("task_id") else ui_file()
        append(path, {"kind": "use", "ref": ref, "note": note, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "ts": round(time.time(), 3), "task_id": ctx.get("task_id") or ""})
    except Exception:  # noqa: BLE001
        pass


@contextlib.contextmanager
def step(name: str, task_id: str | None = None, parts=None, prompt_vars=None):
    """外壳自己调用大模型时（AI 导演、按字数改写）标明步骤：with llmlog.step("director", task_id) as ctx: …"""
    cur = _ctx.get() or {}
    new = dict(cur, step=name, attempt=0)
    new.pop("last_id", None)
    if task_id is not None:
        new["task_id"] = task_id
    new["parts"] = parts
    new["prompt_vars"] = prompt_vars
    token = _ctx.set(new)
    try:
        yield new
    finally:
        _ctx.reset(token)


def _with_step(fn, step: str, task_arg: bool = False, vars_fn=None, use_fn=None):
    def wrapper(*args, **kwargs):
        cur = _ctx.get() or {}
        new = dict(cur)
        if task_arg and args:
            new["task_id"] = str(args[0])
            new["step"] = step
        elif not cur.get("step"):
            new["step"] = step
        new["attempt"] = 0
        new.pop("last_id", None)
        new.pop("parts", None)
        if vars_fn:
            try:
                new["prompt_vars"] = vars_fn(*args, **kwargs)
            except Exception:  # noqa: BLE001
                pass
        token = _ctx.set(new)
        try:
            result = fn(*args, **kwargs)
            if use_fn:
                try:
                    note_use(use_fn(result, *args, **kwargs), new)
                except Exception:  # noqa: BLE001
                    pass
            return result
        finally:
            _ctx.reset(token)
    wrapper.__wrapped__ = fn
    wrapper.__name__ = getattr(fn, "__name__", "llm")
    wrapper._svf_llm = True
    return wrapper


def _wrap_generate_response(fn):
    def _generate_response(prompt, app_config=None, *args, **kwargs):
        ctx = _ctx.get() or {}
        if ctx:
            ctx["attempt"] = int(ctx.get("attempt", 0) or 0) + 1
        raw: list = []
        token = _raw.set(raw)
        t0 = time.perf_counter()
        out, err = None, ""
        try:
            out = fn(prompt, app_config, *args, **kwargs) if (app_config is not None or args or kwargs) else fn(prompt)
            return out
        except Exception as exc:  # noqa: BLE001  上游自己会把错误变成 "Error: …"，这里只是兜底
            err = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            _raw.reset(token)
            rec = make_record(str(prompt), out, time.perf_counter() - t0, app_config, raw, ctx, err)
            if ctx is not None and isinstance(ctx, dict):
                ctx["last_id"] = rec["id"]
            record(rec)
    _generate_response.__wrapped__ = fn
    _generate_response._svf_llm = True
    return _generate_response


def _hook_openai() -> None:
    try:
        from openai.resources.chat.completions import Completions
    except Exception:  # noqa: BLE001
        return
    if getattr(Completions.create, "_svf_llm", False):
        return
    orig = Completions.create

    def create(self, *args, **kwargs):
        resp = orig(self, *args, **kwargs)
        sink = _raw.get()
        if sink is not None and not kwargs.get("stream"):
            try:
                sink.append(resp.model_dump(mode="json"))
            except Exception:  # noqa: BLE001
                pass
        return resp
    create._svf_llm = True
    Completions.create = create


def _terms_vars(video_subject="", video_script="", amount=5, *args, **kwargs):
    try:
        from app.utils import utils
        script = utils.remove_pause_tags(video_script or "").strip()
    except Exception:  # noqa: BLE001
        script = str(video_script or "").strip()
    return [("视频主题", video_subject, "### Video Subject\n"), ("文案（写文案这一步的输出）", script, "### Video Script\n"),
            ("关键词个数", str(amount), "Generate ")]


def count_chars(text: str) -> int:
    return len(re.findall(r"[\u3400-\u9fff]", text or "")) + len(re.findall(r"[A-Za-z]+|\d+", text or ""))


def _script_use(result, *args, **kwargs):
    if not result:
        return "没有拿到可用的文案（每次失败上游会再试，几次都失败时任务停在「写文案」）。"
    lr = (_ctx.get() or {}).get("length_result") or {}
    if lr.get("revised"):
        return (f"程序数了字数：这次写了 {lr.get('first_chars')} 字，目标约 {lr.get('target_chars')} 字，偏差超过 ±15%，"
                f"所以请模型改写了一次（见下一次调用「按字数改写文案」），最终用的是改写后的 {lr.get('final_chars')} 字文案。")
    return (f"取作视频文案（{count_chars(result)} 字）：程序先去掉 * # 和方括号、圆括号里的内容，再交给配音、字幕和画面；"
            "任务目录 script.json 里的 script 就是它。")


def _terms_use(result, *args, **kwargs):
    if not result:
        return "没解析出关键词（应该返回 JSON 字符串数组）。"
    return f"解析成搜索关键词列表 {json.dumps(result, ensure_ascii=False)}，拿去素材站搜视频。"


VARS = {"terms": _terms_vars}
USES = {"script": _script_use, "terms": _terms_use}


def install() -> None:
    if _INSTALLED[0]:
        return
    _INSTALLED[0] = True
    from app.services import llm
    from app.services import task as task_service
    if not getattr(llm._generate_response, "_svf_llm", False):
        llm._generate_response = _wrap_generate_response(llm._generate_response)
    for name, step_name in (("generate_script", "script"), ("generate_terms", "terms"),
                            ("generate_social_metadata", "social"), ("test_connection", "test")):
        fn = getattr(llm, name, None)
        if fn and not getattr(fn, "_svf_llm", False):
            setattr(llm, name, _with_step(fn, step_name, vars_fn=VARS.get(step_name), use_fn=USES.get(step_name)))
    for name, step_name in (("generate_script", "script"), ("generate_terms", "terms")):
        fn = getattr(task_service, name, None)
        if fn and not getattr(fn, "_svf_llm", False):
            setattr(task_service, name, _with_step(fn, step_name, task_arg=True))
    _hook_openai()


# ---------------------------------------------------------------- 界面上生成的文案 → 复制进任务

def _norm(text: str) -> str:
    """只留文字和数字：上游会去掉模型输出里的 * # 和括号，比较前两边都这样处理。"""
    return re.sub(r"[\W_]+", "", text or "")


def adopt_ui_calls(task_id: str, script: str, within: float = 6 * 3600) -> int:
    """用户先在界面上点「生成文案和关键词」，再点「生成视频」：把那次写文案（以及紧跟着的提取关键词）的记录复制进任务。"""
    script = (script or "").strip()
    if not script or not task_id:
        return 0
    try:
        calls = read(ui_file())
    except Exception:  # noqa: BLE001
        return 0
    now = time.time()
    hit = None
    key = _norm(script)[:60]
    for i in range(len(calls) - 1, -1, -1):
        c = calls[i]
        if c.get("kind") != "use" and c.get("step") in ("script", "revise") and c.get("ok") \
                and now - float(c.get("ts") or 0) < within and key and key in _norm(c.get("output") or ""):
            hit = i
            break
    if hit is None:
        return 0
    if calls[hit].get("step") == "revise":   # 文案是「按字数改写」后的：从它前面那次写文案开始复制
        for j in range(hit - 1, -1, -1):
            if calls[j].get("kind") != "use" and calls[j].get("step") == "script":
                hit = j
                break
    picked = [calls[hit]]
    for c in calls[hit + 1:]:
        if float(c.get("ts") or 0) - float(calls[hit].get("ts") or 0) > 300:
            break
        if c.get("kind") == "use" or c.get("step") in ("terms", "revise", "script"):
            if c.get("step") == "script" and c.get("kind") != "use" and c.get("ok"):
                break   # 又写了一段新文案，不属于这一段
            picked.append(c)
    ids = {c.get("id") for c in picked if c.get("kind") != "use"}
    picked = [c for c in picked if c.get("kind") != "use" or c.get("ref") in ids]
    n = 0
    for c in picked:
        c = dict(c, task_id=task_id, from_ui=True)
        try:
            append(task_file(task_id), c)
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n
