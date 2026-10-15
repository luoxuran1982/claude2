"""离线检查「防重复提交、取消、残留任务」（svf_ext/tasks.py）在真实的上游任务管理里是否生效。

和应用里一样经上游 webui_task.submit_generation 提交「知识讲解卡片」长任务（固定文案 + 假配音，见 cards_task_check.py），然后：
1. 同一会话、同样参数马上再提交 → 被拦下；有任务在生成时另一个会话提交 → 也被拦下；
2. 等到 FFmpeg 开始编码时取消 → 状态立即是「已取消」，FFmpeg 子进程被结束，后台线程退出，之后状态不会被写回「生成中」或「完成」；
   任务目录的 svf_status.json 是 cancelled；取消后能删除（上游 is_task_busy 为假）；
3. 残留：会话名单里有一个没提交的任务 ID、状态存储里有一个「生成中」却没人在跑的任务、任务目录里有上一个进程留下的
   running 记录 → reconcile 后前两个标成「已中断」并移出名单，第三个在任务管理里显示「已中断」。

用法同 cards_task_check.py（cwd 是上游目录）：python tasks_check.py <svf_ext 所在目录>。最后一行输出 RESULT {json}。
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.getcwd())
sys.path.insert(1, sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(2, os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    import cards_task_check as ctc
    from app.models import const
    from app.models.schema import VideoParams
    from app.services import state as sm, task as tm, voice, webui_task
    from svf_ext import tasks

    tasks.install()
    voice.tts = ctc.fake_tts
    out: dict = {}

    params = VideoParams(video_subject="逻辑门", video_script=ctc.LONG_SCRIPT, video_source="knowledge_cards",
                         video_aspect="9:16", voice_name="zh-CN-XiaoxiaoNeural-Female", bgm_type="", bgm_volume=0.0,
                         subtitle_enabled=True, font_name="NotoSansSC-Bold.ttf", n_threads=2, video_count=1)
    tid = str(uuid.uuid4())
    out["claim_first"] = tasks.claim(tid, params, "S1")
    webui_task.submit_generation(task_id=tid, params=params, capture_logs=True)
    out["claim_same_params"] = tasks.claim(str(uuid.uuid4()), params, "S1")
    out["claim_other_session"] = tasks.claim(str(uuid.uuid4()), params, "S2")
    out["busy"] = tasks.busy_task() == tid

    # 等到出现带任务 ID 的 FFmpeg 子进程（知识卡片编码或最终合成）
    deadline = time.time() + 240
    while time.time() < deadline and not (tasks.task_processes(tid) and int((sm.state.get_task(tid) or {}).get("progress") or 0) >= 40):
        time.sleep(0.2)
    out["ffmpeg_seen"] = [p.pid for p in tasks.task_processes(tid)]
    out["progress_before_cancel"] = (sm.state.get_task(tid) or {}).get("progress")
    t0 = time.time()
    out["cancel_message"] = tasks.cancel(tid, wait=20)
    out["cancel_seconds"] = round(time.time() - t0, 1)
    rec = sm.state.get_task(tid) or {}
    out["state_after_cancel"] = rec.get("state")
    out["stage_after_cancel"] = rec.get("failed_stage")
    out["live_after_cancel"] = tasks.is_live(tid)
    out["ffmpeg_left"] = [p.pid for p in tasks.task_processes(tid)]
    sm.state.update_task(tid, state=const.TASK_STATE_PROCESSING, progress=77)   # 模拟后台线程迟到的进度回报
    sm.state.update_task(tid, state=const.TASK_STATE_COMPLETE, progress=100)
    rec = sm.state.get_task(tid) or {}
    out["state_after_late_writes"] = rec.get("state")
    out["deletable"] = not tm.is_task_busy(rec)
    from app.utils import utils
    out["status_file"] = tasks.read_status(os.path.join(utils.task_dir(), tid)).get("state")
    out["busy_after_cancel"] = tasks.busy_task()
    out["claim_duplicate_after_cancel"] = tasks.claim(str(uuid.uuid4()), params, "S1")   # 同会话同参数 90 秒内

    # 残留
    leaked = str(uuid.uuid4())
    stale = str(uuid.uuid4())
    sm.state.update_task(stale, state=const.TASK_STATE_PROCESSING, progress=0, video_subject="stale")
    active = {leaked: {"subject": "x", "mtime": time.time() - 600}, stale: {"subject": "y", "mtime": time.time() - 600}}
    fixed = tasks.reconcile(sm.state, active, pending_id=None)
    out["reconciled"] = sorted(fixed) == sorted([leaked, stale])
    out["active_left"] = list(active)
    out["stale_stage"] = (sm.state.get_task(stale) or {}).get("failed_stage")
    old = str(uuid.uuid4())
    old_dir = os.path.join(utils.task_dir(), old)
    os.makedirs(old_dir, exist_ok=True)
    with open(os.path.join(old_dir, tasks.STATUS_FILE), "w", encoding="utf-8") as f:
        json.dump({"state": "running", "pid": 999999, "pid_started": 1.0}, f)
    out["history_status"] = tasks.status_of({"task_id": old, "state": None, "task_path": old_dir, "video_file": "",
                                             "source": "history"})
    out["ok"] = (out["claim_first"] is None and bool(out["claim_same_params"]) and bool(out["claim_other_session"])
                 and out["busy"] and out["ffmpeg_seen"] and out["state_after_cancel"] == const.TASK_STATE_FAILED
                 and out["stage_after_cancel"] == tasks.CANCEL_STAGE and not out["live_after_cancel"]
                 and not out["ffmpeg_left"] and out["state_after_late_writes"] == const.TASK_STATE_FAILED
                 and out["deletable"] and out["status_file"] == "cancelled" and out["busy_after_cancel"] is None
                 and "刚提交过" in (out["claim_duplicate_after_cancel"] or "")
                 and out["reconciled"] and not out["active_left"] and out["stale_stage"] == tasks.INTERRUPT_STAGE
                 and out["history_status"] == tasks.INTERRUPT_STAGE)
    print("RESULT " + json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
