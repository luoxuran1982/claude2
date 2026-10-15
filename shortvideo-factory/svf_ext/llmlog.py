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

绝不记 API Key：记录里不放配置；写入前把配置里所有「key / token / secret / password」类字段的值、
以及形如 sk-…、Bearer … 的串替换成「***」。没有任务的调用（界面上点「生成文案和关键词」）记到 storage/llm_calls/webui.jsonl；
之后用这段文案点「生成视频」时，把对应的记录复制进那个任务的 llm_calls.jsonl（标明「在界面上生成」）。
"""
from __future__ import annotations

import contextvars
import json
import os
import re
import threading
import time
from pathlib import Path

FILE_NAME = "llm_calls.jsonl"
STEP_LABELS = {"script": "写文案", "terms": "提取关键词", "social": "生成发布标题和话题", "test": "测试连接", "other": "其他"}
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


def read_task(task_id: str) -> list[dict]:
    try:
        return read(task_file(task_id))
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
            return keep or u
    return None


def make_record(prompt: str, output, elapsed: float, app_config, raw_list: list, ctx: dict | None,
                error: str = "") -> dict:
    ctx = ctx or {}
    info = _provider_info(app_config)
    step = ctx.get("step") or "other"
    text = output if isinstance(output, str) else ("" if output is None else str(output))
    ok = bool(text) and not text.startswith("Error:") and not error
    rec = {
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

def _with_step(fn, step: str, task_arg: bool = False):
    def wrapper(*args, **kwargs):
        cur = _ctx.get() or {}
        new = dict(cur)
        if task_arg and args:
            new["task_id"] = str(args[0])
            new["step"] = step
        elif not cur.get("step"):
            new["step"] = step
        new["attempt"] = 0
        token = _ctx.set(new)
        try:
            return fn(*args, **kwargs)
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
            record(make_record(str(prompt), out, time.perf_counter() - t0, app_config, raw, ctx, err))
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


def install() -> None:
    if _INSTALLED[0]:
        return
    _INSTALLED[0] = True
    from app.services import llm
    from app.services import task as task_service
    if not getattr(llm._generate_response, "_svf_llm", False):
        llm._generate_response = _wrap_generate_response(llm._generate_response)
    for name, step in (("generate_script", "script"), ("generate_terms", "terms"),
                       ("generate_social_metadata", "social"), ("test_connection", "test")):
        fn = getattr(llm, name, None)
        if fn and not getattr(fn, "_svf_llm", False):
            setattr(llm, name, _with_step(fn, step))
    for name, step in (("generate_script", "script"), ("generate_terms", "terms")):
        fn = getattr(task_service, name, None)
        if fn and not getattr(fn, "_svf_llm", False):
            setattr(task_service, name, _with_step(fn, step, task_arg=True))
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
        if c.get("step") == "script" and c.get("ok") and now - float(c.get("ts") or 0) < within \
                and key and key in _norm(c.get("output") or ""):
            hit = i
            break
    if hit is None:
        return 0
    picked = [calls[hit]]
    for c in calls[hit + 1:]:
        if float(c.get("ts") or 0) - float(calls[hit].get("ts") or 0) > 300:
            break
        if c.get("step") == "terms":
            picked.append(c)
    n = 0
    for c in picked:
        c = dict(c, task_id=task_id, from_ui=True)
        try:
            append(task_file(task_id), c)
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n
