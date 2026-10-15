"""离线检查「大模型调用记录」和「目标时长」（svf_ext/llmlog.py、duration.py）接到上游以后是否生效。

起一个本机的假 OpenAI 兼容接口（/v1/chat/completions，返回固定文案、关键词和 token 用量），把上游的大模型设置指向它，
钥匙是一串假的 sk-…。然后：
1. 任务流水线里写文案（task.generate_script）和提取关键词（task.generate_terms）→ 任务目录的 llm_calls.jsonl 有两条，
   记着步骤、服务、模型、完整提示词（写文案的提示词末尾有目标时长换算的字数）、完整原始输出、原始 JSON、token 用量、耗时；
2. 记录里、日志里都找不到钥匙；
3. 界面上点「生成文案」（没有任务）→ 记到 storage/llm_calls/webui.jsonl；之后用这段文案提交任务 → 复制进任务的记录。

用法（cwd 是上游目录）：python llm_check.py <svf_ext 所在目录>。最后一行输出 RESULT {json}。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.getcwd())
sys.path.insert(1, sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

KEY = "sk-svfcheck-SECRETSECRET1234567890"
SCRIPT = "逻辑门是数字电路的基本单元。与门只有两个输入都为1时才输出1。"
SEEN: list = []


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        SEEN.append({"auth": self.headers.get("Authorization", ""), "body": body})
        prompt = body["messages"][-1]["content"]
        text = '["logic gate", "circuit board"]' if "search terms" in prompt else SCRIPT
        out = {"id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": body.get("model"),
               "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 321, "completion_tokens": 45, "total_tokens": 366}}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    from app.config import config
    config.app["llm_provider"] = "openai"
    config.app["openai_api_key"] = KEY
    config.app["openai_base_url"] = f"http://127.0.0.1:{srv.server_port}/v1"
    config.app["openai_model_name"] = "fake-model-1"
    config.ui["svf_target_seconds"] = 60
    from app.models.schema import VideoParams
    from app.services import llm, task as tm
    from app.utils import utils
    from svf_ext import duration, llmlog, webui as ext_webui   # 导入 webui 即接上上游（tasks、llmlog、目标时长）

    out: dict = {}
    tid = str(uuid.uuid4())
    params = VideoParams(video_subject="逻辑门", video_script="", video_source="pexels", voice_name="zh-CN-XiaoxiaoNeural-Female")
    ext_webui.note_submission(tid, params)
    script = tm.generate_script(tid, params)
    terms = tm.generate_terms(tid, params, script)
    calls = llmlog.read_task(tid)
    out["script_ok"] = script == SCRIPT
    out["terms"] = terms
    out["steps"] = [c["step_label"] for c in calls]
    c0 = calls[0] if calls else {}
    out["provider_model"] = [c0.get("provider"), c0.get("model")]
    out["prompt_has_length"] = "Length Requirement" in (c0.get("messages") or [{}])[-1].get("content", "") and "270" in json.dumps(c0, ensure_ascii=False)
    out["prompt_matches_sent"] = bool(SEEN) and SEEN[0]["body"]["messages"][-1]["content"] == (c0.get("messages") or [{}])[-1].get("content")
    out["output"] = c0.get("output")
    out["usage"] = c0.get("usage")
    out["raw_has_id"] = bool(c0.get("raw_response")) and c0["raw_response"][0].get("id") == "chatcmpl-1"
    out["elapsed"] = c0.get("elapsed_seconds")
    out["task_info"] = duration.read_task(os.path.join(utils.task_dir(), tid))
    text = open(llmlog.task_file(tid), encoding="utf-8").read()
    out["key_sent_to_server"] = SEEN[0]["auth"].endswith(KEY)
    out["key_in_records"] = KEY in text or "SECRETSECRET" in text

    # 界面上生成文案（没有任务）→ webui.jsonl → 提交任务时复制进去
    config.ui["svf_target_seconds"] = 0
    s2 = llm.generate_script(video_subject="逻辑门", language="", paragraph_number=1)
    ui_calls = llmlog.read(llmlog.ui_file())
    out["ui_recorded"] = bool(ui_calls) and ui_calls[-1]["step"] == "script" and not ui_calls[-1]["task_id"]
    out["ui_no_length_when_unset"] = "Length Requirement" not in ui_calls[-1]["messages"][-1]["content"] if ui_calls else False
    tid2 = str(uuid.uuid4())
    ext_webui.note_submission(tid2, VideoParams(video_subject="逻辑门", video_script=s2, video_source="knowledge_cards"))
    adopted = llmlog.read_task(tid2)
    out["adopted"] = [(c["step"], c.get("from_ui")) for c in adopted]
    out["ok"] = (out["script_ok"] and out["terms"] == ["logic gate", "circuit board"] and out["steps"] == ["写文案", "提取关键词"]
                 and out["provider_model"] == ["openai", "fake-model-1"] and out["prompt_has_length"] and out["prompt_matches_sent"]
                 and out["output"] == SCRIPT and out["usage"] == {"prompt_tokens": 321, "completion_tokens": 45, "total_tokens": 366}
                 and out["raw_has_id"] and out["key_sent_to_server"] and not out["key_in_records"]
                 and out["task_info"].get("target_seconds") == 60 and out["ui_recorded"] and out["ui_no_length_when_unset"]
                 and bool(out["adopted"]) and tuple(out["adopted"][0]) == ("script", True))
    print("RESULT " + json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
