"""「卡片 + AI 插图」：在 AI 导演的基础上，给导演标记「适合配图」的少数概念页（每个视频最多 3 张）生成插图。

文生图服务用上游已有的「OpenAI 兼容文生图」设置（设置 → 素材来源设置 → OpenAI 兼容文生图：接口地址、模型、钥匙），
调用上游 app/services/material.py 的 generate_images_openai（POST {base_url}/images/generations，按张计费，
它自带重试、限流退避和「结果不明不重提交」的计费保护）。提示词是导演写的英文 image_prompt，
如果设置里填了 openai_image_prompt_template（含 {term}），上游会套上这个模板。

生成的图片存在任务目录的 illustrations/ 下（PNG），画这一页时铺满作背景、盖一层半透明的主题底色（studio_cards）。
没配置文生图、导演没标记、或生成失败时，这一页照常由程序画，任务不会失败。
"""
from __future__ import annotations

from pathlib import Path

MAX_IMAGES = 3
NOT_CONFIGURED = "需要先在 设置 → 素材来源设置 里配置文生图（OpenAI 兼容文生图的接口地址和模型）"


def configured(app_config=None) -> bool:
    try:
        from app.services import material
        return bool(material.is_openai_image_enabled(app_config))
    except Exception:  # noqa: BLE001
        return False


def generate(pages, spec: dict, task_dir, video_aspect, log=None, generator=None, max_images: int = MAX_IMAGES) -> list[dict]:
    """按导演的标记生成插图，把图片路径写进对应页的 data["image_path"]。返回每张的结果 [{"page", "prompt", "file", "ok", "error"}]。

    generator(prompt, save_dir) → 图片路径或空串；默认用上游 generate_images_openai。"""
    log = log or (lambda m: None)
    out_dir = Path(task_dir) / "illustrations"
    want = [(i, p) for i, p in enumerate(spec.get("pages") or []) if p.get("illustrate") and p.get("image_prompt")][:max_images]
    results: list[dict] = []
    if not want:
        log("AI 插图：导演没有标记适合配图的页，这次只画卡片")
        return results
    out_dir.mkdir(parents=True, exist_ok=True)
    if generator is None:
        from app.services import material

        def generator(prompt, save_dir):
            items = material.generate_images_openai(search_term=prompt, minimum_duration=1, video_aspect=video_aspect,
                                                    save_dir=save_dir)
            return items[0].url if items else ""
    for i, item in want:
        r = {"page": i + 1, "prompt": item["image_prompt"], "file": "", "ok": False, "error": ""}
        try:
            path = generator(item["image_prompt"], str(out_dir))
        except Exception as exc:  # noqa: BLE001  结果不明（可能已扣费）时不再继续请求下一张
            r["error"] = f"{type(exc).__name__}: {exc}"
            results.append(r)
            log(f"AI 插图：第 {i + 1} 页的插图生成出错，后面的不再生成（避免重复计费）：{r['error'][:200]}")
            break
        if path and Path(path).is_file():
            pages[i].data["image_path"] = str(path)
            r.update(ok=True, file=Path(path).name)
            log(f"AI 插图：第 {i + 1} 页「{pages[i].heading}」已生成插图 {Path(path).name}")
        else:
            r["error"] = "文生图服务没有返回图片（原因见上面 openai image 开头的日志）"
            log(f"AI 插图：第 {i + 1} 页没有拿到插图，这一页照常由程序画")
        results.append(r)
    return results
