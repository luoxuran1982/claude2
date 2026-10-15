"""任务管理的外壳扩展：防重复提交、取消生成中的任务、识别「已中断」的残留任务。

用户反馈（1.0.2，Windows）：连按三次「生成视频」，同时起了 3 个任务；多出来的两个一直显示「生成中 0%」，
删除按钮是灰的，删不掉也停不下来。原因有两层：
1. 上游的按钮每点一次就在 on_click 里新建一个任务 ID 并记进本会话的「生成中」名单；连点时，前一次页面运行
   被下一次点击打断，记下的 ID 没有提交、也不会被移除，于是永远显示「生成中 0%」（上游删除时又把名单里的任务当成忙碌）；
   没被打断的几次则真的各提交了一个任务。
2. 上游没有取消功能：任务在后台线程里跑，FFmpeg 是它的子进程。

这里的做法（全部在外壳里，上游只在 [补丁 P7] 几处调用）：
- 「活任务」登记表：提交时登记、后台线程结束时注销。只有登记表里的任务才算真的在生成；
- 防重复：有活任务时「生成视频」按钮置灰（显示「生成中…」）；提交前再检查一次（同时点的几次只放行一个）；
  同一会话、同样参数在 90 秒内重复提交只保留第一个；
- 取消：做标记 → 立即把状态写成「已取消」→ 结束这个任务的 FFmpeg 子进程（命令行里带任务 ID 的）→ 流水线在下一步开始前、
  进度回报时发现标记就抛出 TaskCancelled 退出。之后状态不会再被写回「生成中」或「完成」；
- 残留：状态是「生成中」但不在登记表里的任务（比如程序重启前没结束的、被打断的点击），标成「已中断」，可以删除。
  任务目录里的 svf_status.json 记着由哪个进程在跑，程序重启后也能认出来。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

CANCEL_STAGE = "cancelled"
INTERRUPT_STAGE = "interrupted"
CANCEL_MSG = "已取消（在任务管理里点了「取消」）"
INTERRUPT_MSG = "已中断：生成它的后台已经不在了（程序重启、崩溃，或连点按钮时没有真正提交），不会再继续，可以删除"
BUSY_LABEL = "生成中…"
DUP_WINDOW = 90.0          # 同一会话、同样参数，多少秒内重复提交只保留一个
CLAIM_TTL = 60.0           # 登记了却没提交（页面运行被打断）的条目，多久后作废
PENDING_GRACE = 120.0      # 刚点下按钮、还没提交的那个任务，在这段时间内不算残留
STATUS_FILE = "svf_status.json"


class TaskCancelled(Exception):
    """任务被用户取消。继承 Exception：上游流水线的异常处理会把它当成失败收尾，状态由本模块写成「已取消」。"""


_LOCK = threading.RLock()
_LIVE: dict[str, dict] = {}        # task_id -> {"phase": claimed/queued/running, "since", "session"}
_CANCELLED: set[str] = set()
_RECENT: dict[tuple, tuple] = {}   # (session, 参数指纹) -> (时间, task_id)
_INSTALLED = [False]


# ---------------------------------------------------------------- 登记表

def is_live(task_id: str) -> bool:
    with _LOCK:
        return task_id in _LIVE


def is_cancelled(task_id: str) -> bool:
    with _LOCK:
        return task_id in _CANCELLED


def _purge(now: float) -> None:
    for tid, v in list(_LIVE.items()):
        if v["phase"] == "claimed" and now - v["since"] > CLAIM_TTL:
            _LIVE.pop(tid, None)
    for k, (t, _) in list(_RECENT.items()):
        if now - t > DUP_WINDOW:
            _RECENT.pop(k, None)


def busy_task(now: float | None = None) -> str | None:
    """正在生成（或排队）且没被取消的任务 ID；没有返回 None。"""
    now = time.time() if now is None else now
    with _LOCK:
        _purge(now)
        for tid, v in _LIVE.items():
            if tid not in _CANCELLED:
                return tid
    return None


def fingerprint(params) -> str:
    try:
        data = params.model_dump(mode="json")
    except AttributeError:
        data = dict(vars(params))
    return hashlib.sha1(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def claim(task_id: str, params, session_id: str = "", now: float | None = None) -> str | None:
    """提交前的检查。放行时登记并返回 None；不放行返回给用户看的原因。同一时刻只有一个能通过（加锁）。"""
    now = time.time() if now is None else now
    fp = fingerprint(params)
    with _LOCK:
        _purge(now)
        other = next((t for t in _LIVE if t != task_id and t not in _CANCELLED), None)
        if other:
            return (f"已有任务在生成（任务 {other[:8]}），这次点击没有新建任务。"
                    "等它完成，或在「任务管理」里点「取消」后再生成。")
        prev = _RECENT.get((session_id, fp))
        if prev and now - prev[0] < DUP_WINDOW and prev[1] != task_id:
            return (f"同样的参数 {now - prev[0]:.0f} 秒前刚提交过（任务 {prev[1][:8]}），这次没有重复提交。"
                    "要再做一条一样的视频，请稍等一会儿再点。")
        _RECENT[(session_id, fp)] = (now, task_id)
        _LIVE[task_id] = {"phase": "claimed", "since": now, "session": session_id}
    return None


def confirm(task_id: str) -> None:
    """提交调用返回以后：如果它没经过上游的 submit_generation（被别的代码替换掉了，例如上游测试里打了桩），
    登记就停在 claimed，撤掉，免得一直挡着后面的提交。正常情况下这时已经是 queued / running，什么也不做。"""
    with _LOCK:
        v = _LIVE.get(task_id)
        if v and v.get("phase") == "claimed":
            _LIVE.pop(task_id, None)


def release(task_id: str) -> None:
    """提交失败时撤销登记。"""
    with _LOCK:
        _LIVE.pop(task_id, None)


def _set_phase(task_id: str, phase: str) -> None:
    with _LOCK:
        v = _LIVE.setdefault(task_id, {"since": time.time(), "session": ""})
        v["phase"] = phase


# ---------------------------------------------------------------- 任务目录里的运行记录（重启后用来认出「已中断」）

def _task_dir(task_id: str) -> Path | None:
    try:
        from app.utils import utils
        return Path(utils.task_dir()) / task_id
    except Exception:  # noqa: BLE001
        return None


def _proc_started() -> float | None:
    try:
        import psutil
        return psutil.Process().create_time()
    except Exception:  # noqa: BLE001
        return None


def write_status(task_id: str, state: str, directory: Path | None = None) -> None:
    d = directory or _task_dir(task_id)
    if not d:
        return
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (STATUS_FILE + ".tmp")
        tmp.write_text(json.dumps({"state": state, "pid": os.getpid(), "pid_started": _proc_started(),
                                   "time": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, d / STATUS_FILE)
    except OSError:
        pass


def read_status(task_dir) -> dict:
    try:
        return json.loads((Path(task_dir) / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def status_from_dir(task_id: str, task_dir) -> str | None:
    """任务目录里的记录 → "cancelled" / "interrupted" / None。running 但不是本进程的活任务 → 已中断。"""
    rec = read_status(task_dir)
    st = rec.get("state")
    if st == "cancelled":
        return "cancelled"
    if st == "interrupted":
        return "interrupted"
    if st == "running" and not is_live(task_id):
        return "interrupted"
    return None


# ---------------------------------------------------------------- 状态写入

def _state_backend(state_obj=None):
    if state_obj is not None:
        return state_obj
    from app.services import state as sm
    return sm.state


def _write_terminal(task_id: str, stage: str, msg: str, state_obj=None) -> None:
    from app.models import const
    st = _state_backend(state_obj)
    try:
        rec = st.get_task(task_id) or {}
    except Exception:  # noqa: BLE001
        rec = {}
    progress = int(rec.get("progress", 0) or 0)
    update = getattr(st, "_svf_orig_update", None) or st.update_task
    try:
        update(task_id, const.TASK_STATE_FAILED, progress, failed_stage=stage, error=msg)
    except Exception:  # noqa: BLE001
        pass


def guard_state(state_obj) -> None:
    """包一层状态后端的 update_task / patch_task：已取消的任务，之后的写入一律保持「已取消」，
    免得后台线程收尾时把它写回「生成中」（上游的进度回报每次都带 state=生成中）或「完成」。"""
    if getattr(state_obj, "_svf_guarded", False):
        return
    from app.models import const
    orig_update, orig_patch = state_obj.update_task, state_obj.patch_task

    def update_task(task_id, *args, **kwargs):
        if is_cancelled(task_id):
            rec = {}
            try:
                rec = state_obj.get_task(task_id) or {}
            except Exception:  # noqa: BLE001
                pass
            return orig_update(task_id, const.TASK_STATE_FAILED, int(rec.get("progress", 0) or 0),
                               failed_stage=CANCEL_STAGE, error=CANCEL_MSG)
        return orig_update(task_id, *args, **kwargs)

    def patch_task(task_id, **kwargs):
        if is_cancelled(task_id):
            kwargs = {k: v for k, v in kwargs.items() if k not in ("state", "progress", "failed_stage", "error")}
            if not kwargs:
                return True
        return orig_patch(task_id, **kwargs)

    state_obj._svf_orig_update = orig_update
    state_obj.update_task = update_task
    state_obj.patch_task = patch_task
    state_obj._svf_guarded = True


# ---------------------------------------------------------------- 取消

def check(task_id: str) -> None:
    if is_cancelled(task_id):
        raise TaskCancelled(CANCEL_MSG)


def task_processes(task_id: str) -> list:
    """本进程的子孙进程里，命令行带着这个任务 ID（任务目录）的：FFmpeg 编码、拼接、知识卡片编码等。"""
    try:
        import psutil
        procs = psutil.Process().children(recursive=True)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for p in procs:
        try:
            if any(task_id in str(a) for a in p.cmdline()):
                out.append(p)
        except Exception:  # noqa: BLE001
            continue
    return out


def kill_task_processes(task_id: str) -> list[int]:
    procs = task_processes(task_id)
    pids = []
    for p in procs:
        try:
            p.kill()
            pids.append(p.pid)
        except Exception:  # noqa: BLE001
            pass
    return pids


def cancel(task_id: str, state_obj=None, wait: float = 8.0) -> str:
    """取消任务：排队中的不再开始，正在跑的停在当前这一步。返回给用户看的说明。"""
    from loguru import logger
    with _LOCK:
        _CANCELLED.add(task_id)
        live = task_id in _LIVE
    _write_terminal(task_id, CANCEL_STAGE, CANCEL_MSG, state_obj)
    write_status(task_id, "cancelled")
    killed = kill_task_processes(task_id) if live else []
    logger.warning(f"用户取消了任务 {task_id}" + (f"，已结束它的子进程 {killed}" if killed else ""))
    deadline = time.time() + wait
    while live and is_live(task_id) and time.time() < deadline:
        time.sleep(0.2)
        kill_task_processes(task_id)
    if is_live(task_id):
        return ("已取消。后台还在等当前这一步（比如一次网络请求）结束，之后会自动停下；"
                "这期间任务目录里的文件可能还被占用，删除失败的话稍等再删。")
    return "已取消，现在可以删除这个任务。"


# ---------------------------------------------------------------- 残留任务

def mark_interrupted(task_id: str, state_obj=None) -> None:
    from loguru import logger
    _write_terminal(task_id, INTERRUPT_STAGE, INTERRUPT_MSG, state_obj)
    d = _task_dir(task_id)
    if d and d.is_dir():
        write_status(task_id, "interrupted", d)
    logger.warning(f"任务 {task_id} 显示「生成中」，但后台已经没有在跑它，标成「已中断」")


def reconcile(state_obj, active: dict, pending_id: str | None = None, now: float | None = None) -> list[str]:
    """打开任务管理时调用：清掉本会话「生成中」名单里的残留，把「生成中」但没人在跑的任务标成「已中断」。返回处理了哪些。"""
    from app.models import const
    now = time.time() if now is None else now
    fixed = []
    for tid in list(active or {}):
        if is_live(tid):
            continue
        if tid == pending_id and now - float((active[tid] or {}).get("mtime") or now) < PENDING_GRACE:
            continue   # 刚点下按钮，这次页面运行马上就会提交
        try:
            rec = state_obj.get_task(tid)
        except Exception:  # noqa: BLE001
            rec = None
        if rec and rec.get("state") == const.TASK_STATE_PROCESSING:
            mark_interrupted(tid, state_obj)
        active.pop(tid, None)
        fixed.append(tid)
    try:
        records, _ = state_obj.get_all_tasks(1, 500)
    except Exception:  # noqa: BLE001
        records = []
    for rec in records:
        tid = rec.get("task_id")
        if not tid or tid in fixed or is_live(tid) or tid == pending_id:
            continue
        if rec.get("state") == const.TASK_STATE_PROCESSING:
            mark_interrupted(tid, state_obj)
            fixed.append(tid)
    return fixed


def status_of(task: dict) -> str | None:
    """任务管理每一行：已取消 / 已中断 / None（照上游的状态显示）。"""
    stage = task.get("failed_stage")
    if stage in (CANCEL_STAGE, INTERRUPT_STAGE):
        return stage
    tid = task.get("task_id") or ""
    try:
        state = int(task.get("state"))
    except (TypeError, ValueError):
        state = None
    if state == -1:
        return lookup_stage(tid)
    if state is None and task.get("task_path") and not task.get("video_file"):
        return status_from_dir(tid, task["task_path"])
    return None


def lookup_stage(task_id: str) -> str | None:
    try:
        from app.services import state as sm
        rec = sm.state.get_task(task_id) or {}
    except Exception:  # noqa: BLE001
        return None
    return rec.get("failed_stage") if rec.get("failed_stage") in (CANCEL_STAGE, INTERRUPT_STAGE) else None


# ---------------------------------------------------------------- 接到上游

STAGE_FUNCTIONS = ("generate_script", "generate_terms", "generate_audio", "generate_subtitle", "get_video_materials",
                   "generate_final_videos")


def _wrap_stage(fn):
    def wrapper(task_id, *args, **kwargs):
        check(task_id)
        out = fn(task_id, *args, **kwargs)
        check(task_id)
        return out
    wrapper.__wrapped__ = fn
    wrapper.__name__ = getattr(fn, "__name__", "stage")
    wrapper._svf_stage = True
    return wrapper


def install() -> None:
    """接到上游：只装一次。上游 webui/Main.py 每次刷新都重新执行，但 svf_ext 模块只导入一次。"""
    if _INSTALLED[0]:
        return
    _INSTALLED[0] = True
    from app.services import state as sm
    from app.services import task as task_service
    from app.services import webui_task

    guard_state(sm.state)
    for name in STAGE_FUNCTIONS:
        fn = getattr(task_service, name, None)
        if fn and not getattr(fn, "_svf_stage", False):
            setattr(task_service, name, _wrap_stage(fn))

    orig_reporter = task_service._stage_progress_reporter

    def stage_progress_reporter(task_id, stage_start, stage_end):
        report = orig_reporter(task_id, stage_start, stage_end)

        def guarded(fraction):
            check(task_id)      # 片段处理、下载等循环里每次回报进度时检查取消
            report(fraction)
        return guarded
    task_service._stage_progress_reporter = stage_progress_reporter

    orig_submit = webui_task.submit_generation
    orig_run = webui_task._run_generation

    def submit_generation(task_id, *args, **kwargs):
        _set_phase(task_id, "queued")
        try:
            return orig_submit(task_id, *args, **kwargs)
        except BaseException:
            release(task_id)
            raise

    def run_generation(task_id, *args, **kwargs):
        if is_cancelled(task_id):        # 排队时就被取消了：不再开始
            release(task_id)
            write_status(task_id, "cancelled")
            return {"task_id": task_id, "state": -1, "failed_stage": CANCEL_STAGE, "error": CANCEL_MSG}
        _set_phase(task_id, "running")
        write_status(task_id, "running")
        result = None
        try:
            result = orig_run(task_id, *args, **kwargs)
            return result
        finally:
            release(task_id)
            final = "cancelled" if is_cancelled(task_id) else (
                "done" if isinstance(result, dict) and result.get("state", 1) != -1 and not result.get("failed_stage") else "failed")
            write_status(task_id, final)

    webui_task.submit_generation = submit_generation
    webui_task._run_generation = run_generation
