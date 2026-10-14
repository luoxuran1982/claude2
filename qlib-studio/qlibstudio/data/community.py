"""社区 A 股数据集 chenditc/investment_data（Qlib 官方数据暂停下载后，上游 README 推荐的替代来源）。

数据集已经是 Qlib 二进制格式（qlib_bin.tar.gz，约 570 MB，解压后约 2–3 GB），每天由作者从多个公开来源
交叉校验后发布在 GitHub Releases。我们做的事：断点续传下载 → 校验 → 解压到 datasets/<编号>/ → 质量检查。
也可以先在浏览器里下载好，再用「从本地文件导入」选这个 .tar.gz。

    来源：https://github.com/chenditc/investment_data/releases/latest/download/qlib_bin.tar.gz
    上游说明：upstream/scripts/data_collector/crowd_source/README.md
"""
from __future__ import annotations

import os
import shutil
import ssl
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import convert, datasets, quality

URL = "https://github.com/chenditc/investment_data/releases/latest/download/qlib_bin.tar.gz"
PAGE = "https://github.com/chenditc/investment_data/releases"
CHUNK = 1024 * 1024


class DownloadError(RuntimeError):
    pass


def _ssl_context():
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE"):
        return ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE"))
    try:
        import certifi  # 打包后的程序里系统证书可能读不到，用 certifi 自带的根证书
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def explain(exc: BaseException, url: str) -> str:
    host = url.split("/")[2] if "://" in url else url
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return f"下载地址不存在（HTTP 404：{url}）。作者可能改了发布文件名，请到 {PAGE} 看最新一期的文件"
        if exc.code in (403, 429):
            return f"GitHub 拒绝了下载请求（HTTP {exc.code}），可能是限流，过一会儿再试"
        return f"服务器返回 HTTP {exc.code}（{exc.reason}）"
    reason = getattr(exc, "reason", exc)
    text = str(reason)
    if isinstance(reason, ssl.SSLError) or "CERTIFICATE" in text.upper():
        return f"和 {host} 的 HTTPS 证书校验失败（{text}）。公司网络或代理可能在拦截 HTTPS"
    if isinstance(reason, TimeoutError) or "timed out" in text:
        return f"连接 {host} 超时。国内网络访问 GitHub 下载常常很慢或中断：可以稍后重试（会断点续传），或开代理，或在浏览器里下载后用「从本地文件导入」"
    if "Name or service not known" in text or "getaddrinfo" in text or "nodename" in text or "11001" in text:
        return f"解析不了域名 {host}（没联网或 DNS 不通）"
    if "refused" in text.lower():
        return f"{host} 拒绝连接（{text}）。如果设置了代理，检查代理是否在运行"
    return f"下载失败：{text}"


def download(dest_dir: Path, url: str = URL, progress=lambda p, m: None, timeout: float = 30.0) -> Path:
    """断点续传下载到 dest_dir/qlib_bin.tar.gz。返回文件路径。"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / "qlib_bin.tar.gz"
    part = dest_dir / "qlib_bin.tar.gz.part"
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "QlibStudio", **({"Range": f"bytes={have}-"} if have else {})})
    ctx = _ssl_context()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)  # noqa: S310  固定的 https 地址
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and have:  # 已经下完
            part.replace(final)
            return final
        raise DownloadError(explain(exc, url)) from None
    except (urllib.error.URLError, OSError) as exc:
        raise DownloadError(explain(exc, url)) from None
    with resp:
        resumed = resp.status == 206
        if have and not resumed:
            have = 0  # 服务器不支持续传：从头下
        length = resp.headers.get("Content-Length")
        total = (int(length) + have) if length else None
        if total:
            free = shutil.disk_usage(dest_dir).free
            if free < (total - have) * 5:
                raise DownloadError(f"磁盘空间不够：数据包 {total / 1e9:.1f} GB，解压后约 {total * 4 / 1e9:.0f} GB，"
                                    f"数据目录所在磁盘只剩 {free / 1e9:.1f} GB")
        tag = resp.geturl().split("/download/")[-1].split("/")[0] if "/download/" in resp.geturl() else ""
        (dest_dir / "release-tag.txt").write_text(tag, encoding="utf-8")
        t0, done = time.time(), have
        try:
            with open(part, "ab" if resumed else "wb") as f:
                while True:
                    buf = resp.read(CHUNK)
                    if not buf:
                        break
                    f.write(buf)
                    done += len(buf)
                    speed = (done - have) / max(time.time() - t0, 1e-6)
                    msg = f"下载 {done / 1e6:.0f} MB" + (f" / {total / 1e6:.0f} MB" if total else "") + f"，{speed / 1e6:.1f} MB/s"
                    if resumed and have:
                        msg += f"（从 {have / 1e6:.0f} MB 处续传）"
                    progress(0.8 * done / total if total else 0.0, msg)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise DownloadError(explain(exc, url) + f"。已下载 {done / 1e6:.0f} MB，再点一次「下载」会从这里续传") from None
    if total and part.stat().st_size != total:
        raise DownloadError(f"下载不完整：{part.stat().st_size} / {total} 字节，再点一次会续传")
    part.replace(final)
    return final


def _find_root(base: Path) -> Path:
    for p in [base, *sorted(base.rglob("calendars"))]:
        cand = p if (p / "calendars" / "day.txt").is_file() else p.parent
        if (cand / "calendars" / "day.txt").is_file() and (cand / "features").is_dir():
            return cand
    raise ValueError("压缩包里没有 Qlib 数据（找不到 calendars/day.txt 和 features/）")


def import_archive(archive: Path, ds_dir: Path, meta: dict, progress=lambda p, m: None) -> dict:
    """解压 qlib_bin.tar.gz（或任意含 Qlib 数据目录的 .tar.gz / .tar）到 ds_dir。"""
    archive, ds_dir = Path(archive), Path(ds_dir)
    if not archive.is_file():
        raise ValueError(f"找不到文件：{archive}")
    tmp = ds_dir / "_extract"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        with tarfile.open(archive, "r:*") as tar:
            members = tar.getmembers()
            for k, m in enumerate(members):
                tar.extract(m, tmp, filter="data")  # data 过滤器：拒绝绝对路径、../、设备文件和指向外面的链接
                if k % 2000 == 0:
                    progress(0.8 + 0.1 * k / max(len(members), 1), f"解压 {k}/{len(members)}")
    except (tarfile.TarError, EOFError, OSError) as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError(f"压缩包损坏或不是 tar.gz（{exc}）。如果是下载中断造成的，删掉后重新下载") from None
    src = _find_root(tmp)
    for child in src.iterdir():
        shutil.move(str(child), str(ds_dir / child.name))
    shutil.rmtree(tmp, ignore_errors=True)
    progress(0.92, "生成股票池、质量检查（抽查 400 只）")
    convert.write_pools(ds_dir)
    if not (ds_dir / "names.json").exists():
        datasets.write_json(ds_dir / "names.json", {})
    q = quality.check(ds_dir, max_symbols=400)
    datasets.write_json(ds_dir / "quality.json", q)
    return convert.finish_meta(ds_dir, meta, q)
