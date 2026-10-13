"""配置读写：界面里的改动写回上游原生的配置文件格式。

- config/config.yaml：用 ruamel.yaml 往返读写，保留上游文件里的全部注释、引号和顺序，只改界面上列出的字段
  （白名单 FIELDS），其他内容原样不动；写之前把旧文件备份成 config.yaml.bak。
- config/frequency_words.txt、config/ai_interests.txt：纯文本，原样保存。
- 密钥（AI Key、推送地址、Token、邮箱密码）不写进 config.yaml，单独存在数据目录的 secrets.json，
  抓取时以上游原生支持的环境变量（AI_API_KEY、FEISHU_WEBHOOK_URL……）传给工作进程。界面只看到「已填写/未填写」和末 4 位。
"""
from __future__ import annotations

import copy
import io
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, SingleQuotedScalarString

from . import paths

DEFAULT_FILES = (
    "config.yaml", "frequency_words.txt", "ai_interests.txt", "timeline.yaml",
    "ai_analysis_prompt.txt", "ai_translation_prompt.txt",
    "ai_filter/prompt.txt", "ai_filter/extract_prompt.txt", "ai_filter/update_tags_prompt.txt",
)
VERSION_FILES = ("version", "version_mcp", "version_configs")  # MCP 服务从工作目录读 version


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)  # 与上游 config.yaml 的缩进一致
    y.width = 4096
    return y


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- 工作区初始化
PROMPT_FILES = ("ai_analysis_prompt.txt", "ai_translation_prompt.txt",
                "ai_filter/prompt.txt", "ai_filter/extract_prompt.txt", "ai_filter/update_tags_prompt.txt")
MARKER = ".hotradar-version"


def ensure_workspace(ws: Path, defaults: Path | None = None) -> list[str]:
    """第一次使用时把安装包里的上游默认配置复制到工作区；用户能在界面里改的文件（config.yaml、关键词、兴趣描述、
    timeline.yaml）已存在时一律不覆盖。界面里不能改的 AI 提示词随安装包版本更新：版本变化时覆盖，旧文件内容不同则先备份成 .bak。
    返回新复制的文件。"""
    from . import __version__
    defaults = defaults or paths.upstream_dir()
    marker = ws / MARKER
    try:
        old_version = marker.read_text(encoding="utf-8").strip()
    except OSError:
        old_version = ""
    copied = []
    for rel in DEFAULT_FILES:
        src, dst = defaults / "config" / rel, ws / "config" / rel
        if not src.is_file():
            continue
        if dst.exists() and rel in PROMPT_FILES and old_version != __version__ and dst.read_bytes() != src.read_bytes():
            shutil.copy2(dst, dst.with_name(dst.name + ".bak"))
            dst.unlink()
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append("config/" + rel)
    if old_version != __version__:
        atomic_write(marker, __version__.encode("utf-8"))
    for d in ("custom/keyword", "custom/ai"):
        (ws / "config" / d).mkdir(parents=True, exist_ok=True)
    for rel in VERSION_FILES:  # 版本号跟随安装包，每次覆盖
        src = defaults / rel
        if src.is_file():
            atomic_write(ws / rel, src.read_bytes())
    (ws / "output").mkdir(parents=True, exist_ok=True)
    return copied


def prepare_demo_workspace(main_ws: Path, demo_ws: Path) -> None:
    """离线演示工作区：复制正式工作区当前的关键词和配置，然后关掉联网项（RSS、AI、推送），避免演示时误发消息。"""
    ensure_workspace(demo_ws)
    for rel in ("frequency_words.txt", "ai_interests.txt", "config.yaml", "timeline.yaml"):
        src = main_ws / "config" / rel
        if src.is_file():
            shutil.copy2(src, demo_ws / "config" / rel)
    cfg = demo_ws / "config" / "config.yaml"
    y = _yaml()
    data = y.load(cfg.read_text(encoding="utf-8"))
    _set(data, "rss.enabled", False)
    _set(data, "notification.enabled", False)
    _set(data, "ai_analysis.enabled", False)
    _set(data, "ai_translation.enabled", False)
    _set(data, "filter.method", "keyword")
    _set(data, "advanced.crawler.request_interval", 50)
    _set(data, "schedule.enabled", False)
    buf = io.StringIO()
    y.dump(data, buf)
    atomic_write(cfg, buf.getvalue().encode("utf-8"))


# ---------------------------------------------------------------- config.yaml 字段白名单
# 路径 → (类型, 选项/范围)。界面只能改这里列出的字段。
ENUMS = {
    "report.mode": ("current", "daily", "incremental"),
    "report.display_mode": ("keyword", "platform"),
    "filter.method": ("keyword", "ai"),
    "ai_analysis.mode": ("follow_report", "daily", "current", "incremental"),
    "notification.channels.wework.msg_type": ("markdown", "text"),
    "schedule.preset": ("always_on", "morning_evening", "office_hours", "night_owl", "custom"),
}
FIELDS: dict[str, tuple] = {
    "app.timezone": ("str",),
    "schedule.enabled": ("bool",),
    "schedule.preset": ("enum",),
    "platforms.enabled": ("bool",),
    "platforms.api_url": ("url",),
    "platforms.sources": ("platforms",),
    "rss.enabled": ("bool",),
    "rss.freshness_filter.enabled": ("bool",),
    "rss.freshness_filter.max_age_days": ("int", 0, 3650),
    "rss.feeds": ("feeds",),
    "report.mode": ("enum",),
    "report.display_mode": ("enum",),
    "report.sort_by_position_first": ("bool",),
    "report.rank_threshold": ("int", 1, 100),
    "report.max_news_per_keyword": ("int", 0, 1000),
    "filter.method": ("enum",),
    "filter.priority_sort_enabled": ("bool",),
    "ai_filter.min_score": ("float", 0.0, 1.0),
    "ai_filter.batch_size": ("int", 1, 1000),
    "display.regions.hotlist": ("bool",),
    "display.regions.new_items": ("bool",),
    "display.regions.rss": ("bool",),
    "display.regions.standalone": ("bool",),
    "display.regions.ai_analysis": ("bool",),
    "display.standalone.platforms": ("idlist",),
    "display.standalone.rss_feeds": ("idlist",),
    "display.standalone.max_items": ("int", 0, 500),
    "notification.enabled": ("bool",),
    "notification.channels.wework.msg_type": ("enum",),
    "notification.channels.email.smtp_server": ("str",),
    "notification.channels.email.smtp_port": ("str",),
    "notification.channels.ntfy.server_url": ("url",),
    "storage.formats.txt": ("bool",),
    "storage.local.retention_days": ("int", 0, 36500),
    "ai.model": ("str",),
    "ai.api_base": ("url",),
    "ai.timeout": ("int", 5, 1800),
    "ai.temperature": ("float", 0.0, 2.0),
    "ai.max_tokens": ("int", 0, 1000000),
    "ai_analysis.enabled": ("bool",),
    "ai_analysis.language": ("str",),
    "ai_analysis.mode": ("enum",),
    "ai_analysis.max_news_for_analysis": ("int", 1, 2000),
    "ai_analysis.include_rss": ("bool",),
    "ai_analysis.include_standalone": ("bool",),
    "ai_analysis.include_rank_timeline": ("bool",),
    "ai_translation.enabled": ("bool",),
    "ai_translation.language": ("str",),
    "ai_translation.scope.hotlist": ("bool",),
    "ai_translation.scope.rss": ("bool",),
    "ai_translation.scope.standalone": ("bool",),
    "advanced.crawler.request_interval": ("int", 50, 60000),
    "advanced.crawler.use_proxy": ("bool",),
    "advanced.crawler.default_proxy": ("url",),
    "advanced.rss.timeout": ("int", 1, 300),
    "advanced.rss.use_proxy": ("bool",),
    "advanced.rss.proxy_url": ("url",),
}

NONEMPTY = {"app.timezone": "时区", "ai_analysis.language": "AI 分析语言", "ai_translation.language": "翻译目标语言"}

# 密钥：secrets.json 里的键 → 上游读取的环境变量
SECRETS = {
    "ai_api_key": ("AI_API_KEY", "AI 接口密钥（API Key）"),
    "feishu_webhook_url": ("FEISHU_WEBHOOK_URL", "飞书机器人 Webhook 地址"),
    "dingtalk_webhook_url": ("DINGTALK_WEBHOOK_URL", "钉钉机器人 Webhook 地址"),
    "wework_webhook_url": ("WEWORK_WEBHOOK_URL", "企业微信机器人 Webhook 地址"),
    "telegram_bot_token": ("TELEGRAM_BOT_TOKEN", "Telegram Bot Token"),
    "telegram_chat_id": ("TELEGRAM_CHAT_ID", "Telegram Chat ID"),
    "email_from": ("EMAIL_FROM", "发件邮箱"),
    "email_password": ("EMAIL_PASSWORD", "邮箱密码或授权码"),
    "email_to": ("EMAIL_TO", "收件邮箱"),
    "ntfy_topic": ("NTFY_TOPIC", "ntfy 主题"),
    "ntfy_token": ("NTFY_TOKEN", "ntfy 访问令牌"),
    "bark_url": ("BARK_URL", "Bark 推送地址"),
    "slack_webhook_url": ("SLACK_WEBHOOK_URL", "Slack Webhook 地址"),
    "generic_webhook_url": ("GENERIC_WEBHOOK_URL", "通用 Webhook 地址"),
    "generic_webhook_template": ("GENERIC_WEBHOOK_TEMPLATE", "通用 Webhook 消息模板（JSON）"),
}
# config.yaml 里与密钥对应的字段：用来提醒「配置文件里也写了，环境变量优先」
SECRET_YAML = {
    "ai_api_key": "ai.api_key",
    "feishu_webhook_url": "notification.channels.feishu.webhook_url",
    "dingtalk_webhook_url": "notification.channels.dingtalk.webhook_url",
    "wework_webhook_url": "notification.channels.wework.webhook_url",
    "telegram_bot_token": "notification.channels.telegram.bot_token",
    "telegram_chat_id": "notification.channels.telegram.chat_id",
    "email_from": "notification.channels.email.from",
    "email_password": "notification.channels.email.password",
    "email_to": "notification.channels.email.to",
    "ntfy_topic": "notification.channels.ntfy.topic",
    "ntfy_token": "notification.channels.ntfy.token",
    "bark_url": "notification.channels.bark.url",
    "slack_webhook_url": "notification.channels.slack.webhook_url",
    "generic_webhook_url": "notification.channels.generic_webhook.webhook_url",
    "generic_webhook_template": "notification.channels.generic_webhook.payload_template",
}


def _get(data, dotted: str, default=None):
    cur = data
    for k in dotted.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _like(old, new):
    """沿用原来的引号风格：原值是双引号字符串，新值也写成双引号。"""
    if isinstance(new, str):
        if isinstance(old, DoubleQuotedScalarString) or old is None:
            return DoubleQuotedScalarString(new)
        if isinstance(old, SingleQuotedScalarString):
            return SingleQuotedScalarString(new)
    return new


def _set(data, dotted: str, value) -> None:
    keys = dotted.split(".")
    cur = data
    for k in keys[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            cur[k] = CommentedMap()
        cur = cur[k]
    cur[keys[-1]] = _like(cur.get(keys[-1]), value)


def _tail_comment(seq):
    """列表最后一项后面跟着的注释（通常是下一节的分隔注释，如「# 3. 数据源 - RSS 订阅」），
    重建列表时要挪到新的最后一项后面，否则新增的项会排到这段注释下面。行尾注释留在原项上。"""
    if not isinstance(seq, CommentedSeq) or not seq or not isinstance(seq[-1], CommentedMap) or not seq[-1]:
        return None
    last = seq[-1]
    entry = last.ca.items.get(list(last.keys())[-1])
    if not entry or len(entry) < 3 or entry[2] is None:
        return None
    tok = entry[2]
    i = tok.value.find("\n")
    if i < 0:
        return None
    head, rest = tok.value[:i + 1], tok.value[i + 1:]
    if not rest.strip():
        return None
    tail = copy.copy(tok)
    tail.value = rest
    if head.strip():
        tok.value = head
    else:
        entry[2] = None
    return tail


def _put_tail_comment(seq, tail) -> None:
    if tail is None or not seq or not isinstance(seq[-1], CommentedMap) or not seq[-1]:
        return
    last = seq[-1]
    key = list(last.keys())[-1]
    entry = last.ca.items.get(key)
    if entry and len(entry) > 2 and entry[2] is not None:
        entry[2].value = entry[2].value.rstrip("\n") + "\n" + tail.value
    else:
        tail.value = "\n" + tail.value
        last.ca.items[key] = [None, None, tail, None]


def _rebuild_list(data, dotted: str, items: list[dict], fields: tuple[str, ...]) -> None:
    """按 id 合并列表：已存在的项原地更新（保留它们的注释），新项追加，删掉的项移除。"""
    old = _get(data, dotted)
    old_seq = old if isinstance(old, CommentedSeq) else CommentedSeq()
    tail = _tail_comment(old_seq)
    by_id = {str(it.get("id")): it for it in old_seq if isinstance(it, CommentedMap)}
    new = CommentedSeq()
    for item in items:
        cm = by_id.get(item["id"]) or CommentedMap()
        for f in fields:
            if f not in item:
                continue
            v = item[f]
            if v in (None, "") and f not in ("id", "name"):
                cm.pop(f, None)
            else:
                cm[f] = _like(cm.get(f), v) if isinstance(v, str) else v
        new.append(cm)
    _put_tail_comment(new, tail)
    _set_raw(data, dotted, new)


def _set_raw(data, dotted, value):
    keys = dotted.split(".")
    cur = data
    for k in keys[:-1]:
        cur = cur.setdefault(k, CommentedMap())
    cur[keys[-1]] = value


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class ConfigError(ValueError):
    pass


def _check_url(v: str, label: str) -> str:
    v = (v or "").strip()
    if v and not re.match(r"^(https?|socks5h?)://[^\s]+$", v):
        raise ConfigError(f"{label} 不是有效的网址（需以 http:// 或 https:// 开头）")
    return v


def validate(dotted: str, value):
    if dotted not in FIELDS:
        raise ConfigError(f"不允许修改的配置项：{dotted}")
    spec = FIELDS[dotted]
    kind = spec[0]
    if kind == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{dotted} 应为 是/否")
        return value
    if kind in ("int", "float"):
        try:
            num = int(value) if kind == "int" else float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{dotted} 应为数字") from None
        lo, hi = spec[1], spec[2]
        if not lo <= num <= hi:
            raise ConfigError(f"{dotted} 应在 {lo} 到 {hi} 之间")
        return num
    if kind == "enum":
        if value not in ENUMS[dotted]:
            raise ConfigError(f"{dotted} 只能是 {'、'.join(ENUMS[dotted])}")
        return value
    if kind == "str":
        if not isinstance(value, str) or len(value) > 500 or "\n" in value:
            raise ConfigError(f"{dotted} 应为一行文字")
        if dotted in NONEMPTY and not value.strip():
            raise ConfigError(f"{NONEMPTY[dotted]}不能为空")
        if dotted == "app.timezone":
            try:
                import pytz
                pytz.timezone(value.strip())
            except ImportError:
                pass
            except Exception:  # noqa: BLE001
                raise ConfigError(f"时区「{value}」无效，例：Asia/Shanghai") from None
        return value.strip()
    if kind == "url":
        return _check_url(str(value or ""), dotted)
    if kind == "idlist":
        if not isinstance(value, list) or not all(isinstance(x, str) and _ID_RE.match(x) for x in value):
            raise ConfigError(f"{dotted} 应为 ID 列表")
        return list(dict.fromkeys(value))
    if kind == "platforms":
        return _validate_items(value, "热榜平台", ("id", "name", "expected_domain", "enabled"))
    if kind == "feeds":
        items = _validate_items(value, "RSS 源", ("id", "name", "url", "enabled", "max_age_days"))
        for it in items:
            if not re.match(r"^https?://\S+$", it.get("url") or ""):
                raise ConfigError(f"RSS 源「{it['name'] or it['id']}」的订阅地址无效")
        return items
    raise ConfigError(f"未知类型 {kind}")


def _validate_items(value, label: str, fields: tuple[str, ...]) -> list[dict]:
    if not isinstance(value, list) or len(value) > 200:
        raise ConfigError(f"{label}列表格式不对")
    out, seen = [], set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ConfigError(f"{label}列表格式不对")
        item = {}
        for f in fields:
            if f not in raw:
                continue
            v = raw[f]
            if f == "enabled":
                if not isinstance(v, bool):
                    raise ConfigError(f"{label}的 enabled 应为 是/否")
                item[f] = None if v else False  # 启用时不写 enabled（上游默认启用），保持文件简洁
            elif f == "max_age_days":
                if v in (None, ""):
                    item[f] = None
                else:
                    try:
                        item[f] = int(v)
                    except (TypeError, ValueError):
                        raise ConfigError(f"{label}的保留天数应为整数") from None
            else:
                item[f] = str(v or "").strip()
        if not _ID_RE.match(item.get("id", "")):
            raise ConfigError(f"{label} ID「{item.get('id', '')}」只能用字母、数字、- _ .")
        if item["id"] in seen:
            raise ConfigError(f"{label} ID「{item['id']}」重复")
        seen.add(item["id"])
        if "name" in fields and not item.get("name"):
            item["name"] = item["id"]
        dom = item.get("expected_domain")
        if dom and not re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", dom):
            raise ConfigError(f"{label}「{item['name']}」的校验域名格式不对（例：baidu.com）")
        out.append(item)
    return out


def config_path(ws: Path) -> Path:
    return ws / "config" / "config.yaml"


def load_yaml(ws: Path):
    return _yaml().load(config_path(ws).read_text(encoding="utf-8"))


def view(ws: Path) -> dict:
    """给界面用的配置视图：白名单字段的当前值（纯 JSON）。"""
    data = load_yaml(ws)
    values = {}
    for dotted, spec in FIELDS.items():
        v = _get(data, dotted)
        if spec[0] == "platforms":
            v = [{"id": str(p.get("id", "")), "name": str(p.get("name", p.get("id", ""))),
                  "expected_domain": str(p.get("expected_domain", "") or ""), "enabled": p.get("enabled", True) is not False}
                 for p in (v or []) if isinstance(p, dict)]
        elif spec[0] == "feeds":
            v = [{"id": str(f.get("id", "")), "name": str(f.get("name", f.get("id", ""))), "url": str(f.get("url", "")),
                  "enabled": f.get("enabled", True) is not False,
                  "max_age_days": f.get("max_age_days") if isinstance(f.get("max_age_days"), int) else None}
                 for f in (v or []) if isinstance(f, dict)]
        elif spec[0] == "idlist":
            v = [str(x) for x in (v or [])]
        elif isinstance(v, str):
            v = str(v)
        values[dotted] = v
    return {"values": values, "enums": {k: list(v) for k, v in ENUMS.items()}}


def update(ws: Path, changes: dict) -> dict:
    """校验后把改动写回 config.yaml。全部校验通过才写；写前备份旧文件。返回新的视图。"""
    if not isinstance(changes, dict) or not changes:
        raise ConfigError("没有要保存的改动")
    clean = {k: validate(k, v) for k, v in changes.items()}
    path = config_path(ws)
    y = _yaml()
    text = path.read_text(encoding="utf-8")
    data = y.load(text)
    for dotted, value in clean.items():
        kind = FIELDS[dotted][0]
        if kind == "platforms":
            _rebuild_list(data, dotted, value, ("id", "name", "expected_domain", "enabled"))
        elif kind == "feeds":
            _rebuild_list(data, dotted, value, ("id", "name", "url", "enabled", "max_age_days"))
        elif kind == "idlist":
            seq = CommentedSeq([DoubleQuotedScalarString(x) for x in value])
            seq.fa.set_flow_style()
            old = _get(data, dotted)
            _set_raw(data, dotted, seq)
            if isinstance(old, CommentedSeq):
                seq.ca.comment = old.ca.comment
            # 行尾注释挂在父映射上，原样保留
        else:
            _set(data, dotted, value)
    buf = io.StringIO()
    y.dump(data, buf)
    out = buf.getvalue()
    _yaml().load(out)  # 再读一遍，确认写出的是合法 YAML
    shutil.copy2(path, path.with_name("config.yaml.bak"))
    atomic_write(path, out.encode("utf-8"))
    return view(ws)


# ---------------------------------------------------------------- 文本配置
TEXT_FILES = {"keywords": "frequency_words.txt", "interests": "ai_interests.txt"}


def read_text(ws: Path, which: str) -> str:
    return (ws / "config" / TEXT_FILES[which]).read_text(encoding="utf-8")


def write_text(ws: Path, which: str, text: str) -> None:
    if not isinstance(text, str) or len(text) > 2_000_000:
        raise ConfigError("内容过大或格式不对")
    path = ws / "config" / TEXT_FILES[which]
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    atomic_write(path, text.replace("\r\n", "\n").encode("utf-8"))


def timeline_presets(ws: Path) -> list[dict]:
    """timeline.yaml 里的预设模板（名称 + 说明），给「调度」下拉框用。"""
    try:
        data = _yaml().load((ws / "config" / "timeline.yaml").read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return []
    out = []
    for name, body in (data.get("presets") or {}).items():
        desc = ""
        if isinstance(body, dict):
            desc = str(body.get("description") or body.get("name") or "")
        out.append({"id": str(name), "description": desc})
    if "custom" in data:
        out.append({"id": "custom", "description": "完全自定义（编辑数据目录里的 config/timeline.yaml）"})
    return out


# ---------------------------------------------------------------- 密钥
def secrets_path(root: Path) -> Path:
    return root / "secrets.json"


def load_secrets(root: Path) -> dict:
    try:
        data = json.loads(secrets_path(root).read_text(encoding="utf-8"))
        return {k: str(v) for k, v in data.items() if k in SECRETS and v}
    except (OSError, ValueError, AttributeError):
        return {}


def save_secrets(root: Path, changes: dict) -> dict:
    """changes：{键: 新值}；值为 None 或空串表示清除。只认 SECRETS 里列出的键。"""
    if not isinstance(changes, dict):
        raise ConfigError("格式不对")
    cur = load_secrets(root)
    for k, v in changes.items():
        if k not in SECRETS:
            raise ConfigError(f"未知的密钥项：{k}")
        v = "" if v is None else str(v).strip()
        if len(v) > 4000 or ("\n" in v and k != "generic_webhook_template"):
            raise ConfigError(f"{SECRETS[k][1]} 格式不对")
        if v:
            cur[k] = v
        else:
            cur.pop(k, None)
    path = secrets_path(root)
    atomic_write(path, json.dumps(cur, ensure_ascii=False, indent=1).encode("utf-8"))
    try:
        os.chmod(path, 0o600)  # 只有当前用户能读（Windows 上无效果，数据目录本身在用户目录下）
    except OSError:
        pass
    return secrets_view(root)


def _mask(v: str) -> str:
    if len(v) <= 8:
        return "已填写"
    return f"已填写（共 {len(v)} 个字符，末尾 …{v[-4:]}）"


def secrets_view(root: Path, ws: Path | None = None) -> dict:
    cur = load_secrets(root)
    yaml_data = None
    if ws is not None:
        try:
            yaml_data = load_yaml(ws)
        except Exception:  # noqa: BLE001
            yaml_data = None
    out = {}
    for k, (env, label) in SECRETS.items():
        in_yaml = bool(yaml_data is not None and str(_get(yaml_data, SECRET_YAML[k], "") or "").strip())
        out[k] = {"label": label, "env": env, "set": k in cur, "display": _mask(cur[k]) if k in cur else "未填写",
                  "in_config_yaml": in_yaml}
    return out


def secrets_env(root: Path) -> dict:
    """给工作进程用的环境变量（只含已填写的项）。"""
    return {SECRETS[k][0]: v for k, v in load_secrets(root).items()}
