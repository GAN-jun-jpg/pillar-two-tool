# -*- coding: utf-8 -*-
"""外部规则来源定点监控（发现层，纯函数 + 可注入 fetcher，不依赖 Streamlit）。

只回答一个问题：**我们盯的那几份官方文档，有没有更新？**

- 为什么不去爬 OECD 官网的页面：官网 HTML 层整站走 Cloudflare JS 挑战
  （实测主题页与 `sitemap.xml` 都是 HTTP 403），硬抓必须上无头浏览器，
  与本项目 PyInstaller onedir 的交付形态冲突。而 `content/dam/` 下的静态
  PDF 直链可以正常下载，所以采用「**关注清单 + 指纹比对**」：盯住少数几份
  决定参数的官方文件，而不是全站新闻。
- 抓到的文档**不会被自动解析成规则**：文档内容怎么变成参数，仍走
  「规则缺口 → 云端《规则解释卡》→ 两轮人工审核 → 回归测试 → 发布」的治理闭环。
  本模块只负责"发现 + 提醒 + 留痕"。
- 网络失败只是"本次未检查"：收敛成 `status="error"` 返回，不抛给调用方，
  不影响计算主流程（评委机器可能没有外网）。
- 不引入新依赖：`urllib`（标准库）+ `hashlib`；`pypdf` 可用时按**正文文本**
  比对（避免 PDF 元数据/时间戳造成假变更），不可用时退回**字节**比对。

状态语义：

    new        首次检查，建立基线（不算"有更新"）
    unchanged  与上次快照一致
    changed    与上次快照不同 —— 官方文档更新了
    error      抓取/解析失败（离线、超时、404…），只记录，不阻断

另有 `local_state`（与本地已存副本的比对）：

    same / different / missing / unknown（比对基准不同）
"""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BASE_DIR = Path(__file__).resolve().parent
LIST_PATH = BASE_DIR / "source_watchlist.json"        # 可选：覆盖内置清单
STATE_PATH = BASE_DIR / "data" / "source_snapshots.json"
DEFAULT_TIMEOUT = 20
MIN_TEXT_CHARS = 200  # 正文短于此长度视为提取失败（扫描件 / 图片版 PDF）

STATUS_LABELS = {"new": "首次建立基线", "unchanged": "无变化",
                 "changed": "⚠️ 有更新", "error": "未检查（失败）"}
LOCAL_LABELS = {"same": "与本地副本一致", "different": "本地副本落后",
                "missing": "本地副本缺失", "unknown": "无法比对"}

# 关注清单（默认内置，保证 PyInstaller 打包后依然可用）。
# 三条 URL 均在 2026-09-29 实测可下载（HTTP 200 + application/pdf）。
DEFAULT_WATCHLIST: list[dict[str, Any]] = [
    {
        "id": "handbook",
        "name": "支柱二实施手册（Pillar Two Implementation Handbook）",
        "url": ("https://www.oecd.org/content/dam/oecd/en/topics/policy-sub-issues/"
                "global-minimum-tax/minimum-tax-implementation-handbook-pillar-two.pdf"),
        "why": "各辖区 QDMTT / IIR / UTPR 的落地口径与时间表",
        "local_path": "minimum-tax-implementation-handbook-pillar-two.pdf",
    },
    {
        "id": "admin_guidance",
        "name": "行政指引（Administrative Guidance · Central Record）",
        "url": ("https://www.oecd.org/content/dam/oecd/en/topics/policy-sub-issues/"
                "global-minimum-tax/administrative-guidance-globe-rules-pillar-two-"
                "central-record-legislation-transitional-qualified-status.pdf"),
        "why": "GloBE 规则解释与过渡期合格地位认定（对应 tax_core / allocation）",
    },
    {
        "id": "central_record",
        "name": "中央记录更新（Updated Central Record · 各辖区立法状态）",
        "url": ("https://www.oecd.org/content/dam/oecd/en/topics/policy-sub-issues/"
                "global-minimum-tax/updated-central-record-for-purposes-of-the-"
                "global-minimum-tax.pdf"),
        "why": "各辖区 QDMTT / IIR 立法与生效状态，对应 qdmtt_applies / utpr_applies",
    },
]


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── 关注清单 ──

def _valid(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    url = str(item.get("url") or "")
    return bool(str(item.get("id") or "").strip()) and url.startswith(("http://", "https://"))


def load_watchlist(path: str | Path | None = None) -> list[dict[str, Any]]:
    """读取关注清单：同名 JSON 文件存在则以它为准，否则用内置默认清单。

    非法条目（缺 id / 非 http(s) URL）被跳过而不是报错 —— 清单是用户可编辑的。
    """
    target = Path(path) if path is not None else LIST_PATH
    items = DEFAULT_WATCHLIST
    if target.exists():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                loaded = loaded.get("sources")
            if isinstance(loaded, list):
                items = loaded
        except Exception:  # noqa: BLE001 - 清单损坏时回落内置清单
            items = DEFAULT_WATCHLIST

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not _valid(item):
            continue
        key = str(item["id"]).strip()
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "id": key,
            "name": str(item.get("name") or key),
            "url": str(item["url"]),
            "why": str(item.get("why") or ""),
            "local_path": item.get("local_path") or None,
        })
    return result


# ── 抓取与指纹 ──

def fetch(url: str, timeout: int = DEFAULT_TIMEOUT,
          fetcher: Callable[[str, int], tuple[str, bytes]] | None = None
          ) -> tuple[str, bytes]:
    """下载一个 URL，返回 (content_type, bytes)。`fetcher` 可注入，测试不联网。"""
    if fetcher is not None:
        return fetcher(url, timeout)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.headers.get("Content-Type", ""), response.read()


def extract_text(content_type: str, data: bytes,
                 extractor: Callable[[str, bytes], str | None] | None = None
                 ) -> str | None:
    """提取正文（目前只对 PDF 做）。拿不到时返回 None，调用方退回字节比对。"""
    if extractor is not None:
        return extractor(content_type, data)
    if "pdf" not in str(content_type).lower():
        return None
    try:
        from pypdf import PdfReader
    except Exception:  # noqa: BLE001 - 没有 pypdf 也能靠字节比对工作
        return None
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:  # noqa: BLE001 - 解析失败即退回字节比对
        return None


def snapshot_of(content_type: str, data: bytes,
                extractor: Callable[[str, bytes], str | None] | None = None) -> dict[str, Any]:
    """一份文档的指纹：优先正文文本摘要，拿不到时退回字节摘要。"""
    text = extract_text(content_type, data, extractor)
    if text:
        normalized = re.sub(r"\s+", " ", text).strip()
        if len(normalized) >= MIN_TEXT_CHARS:
            return {"basis": "text",
                    "digest": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                    "chars": len(normalized)}
    return {"basis": "bytes",
            "digest": hashlib.sha256(data).hexdigest(),
            "chars": 0}


def _file_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.exists() or not path.is_file():
        return None
    content_type = "application/pdf" if path.suffix.lower() == ".pdf" else ""
    try:
        return snapshot_of(content_type, path.read_bytes())
    except Exception:  # noqa: BLE001 - 本地副本读不了就不比对
        return None


# ── 检查 ──

def check_source(item: dict[str, Any], state: dict | None = None,
                 fetcher: Callable | None = None, extractor: Callable | None = None,
                 timeout: int = DEFAULT_TIMEOUT, checked_at: str | None = None) -> dict[str, Any]:
    """检查一个来源。任何异常都收敛成 `status="error"`，不向外抛。"""
    result: dict[str, Any] = {
        "id": str(item.get("id") or ""),
        "name": str(item.get("name") or item.get("id") or ""),
        "url": str(item.get("url") or ""),
        "why": str(item.get("why") or ""),
        "checked_at": checked_at or _now(),
        "status": "error",
        "error": "",
        "digest": "",
        "basis": "",
        "chars": 0,
        "bytes": 0,
        "previous_digest": "",
        "previous_checked_at": "",
        "local_state": None,
    }
    previous = (state or {}).get(result["id"]) or {}
    result["previous_digest"] = str(previous.get("digest") or "")
    result["previous_checked_at"] = str(previous.get("checked_at") or "")

    try:
        content_type, data = fetch(result["url"], timeout=timeout, fetcher=fetcher)
        snapshot = snapshot_of(content_type, data, extractor=extractor)
    except Exception as exc:  # noqa: BLE001 - 离线 / 超时 / 404 都只是"未检查"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    result.update({"digest": snapshot["digest"], "basis": snapshot["basis"],
                   "chars": snapshot["chars"], "bytes": len(data)})
    if not result["previous_digest"]:
        result["status"] = "new"
    elif result["previous_digest"] == result["digest"]:
        result["status"] = "unchanged"
    else:
        result["status"] = "changed"

    local_path = item.get("local_path")
    if local_path:
        local = Path(local_path)
        if not local.is_absolute():
            local = BASE_DIR / local
        local_snapshot = _file_snapshot(local)
        if local_snapshot is None:
            result["local_state"] = "missing"
        elif local_snapshot["basis"] != snapshot["basis"]:
            result["local_state"] = "unknown"
        else:
            result["local_state"] = ("same" if local_snapshot["digest"] == snapshot["digest"]
                                     else "different")
    return result


def check_all(items: list[dict[str, Any]] | None = None, state: dict | None = None,
              fetcher: Callable | None = None, extractor: Callable | None = None,
              timeout: int = DEFAULT_TIMEOUT, max_workers: int = 4,
              checked_at: str | None = None) -> list[dict[str, Any]]:
    """并发检查全部来源（默认 4 路），按清单顺序返回。空清单返回空列表。"""
    watchlist = list(items) if items is not None else load_watchlist()
    if not watchlist:
        return []
    stamp = checked_at or _now()
    workers = max(1, min(int(max_workers), len(watchlist)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(check_source, item, state, fetcher, extractor,
                               timeout, stamp) for item in watchlist]
        return [future.result() for future in futures]


# ── 快照状态 ──

def load_state(path: str | Path | None = None) -> dict[str, Any]:
    """读取上次检查的指纹；文件不存在或损坏时返回空字典。"""
    target = Path(path) if path is not None else STATE_PATH
    if not target.exists():
        return {}
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 状态损坏按首次检查处理
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_state(results: list[dict[str, Any]], state: dict | None = None,
               path: str | Path | None = None) -> dict[str, Any]:
    """把本次成功的检查并入状态并落盘；失败的来源保留旧指纹，避免误判"更新"。"""
    merged = dict(state or {})
    for result in results:
        if result.get("status") == "error" or not result.get("digest"):
            continue
        merged[result["id"]] = {
            "digest": result["digest"],
            "basis": result["basis"],
            "chars": result.get("chars", 0),
            "bytes": result.get("bytes", 0),
            "url": result.get("url", ""),
            "name": result.get("name", ""),
            "checked_at": result.get("checked_at", _now()),
        }
    target = Path(path) if path is not None else STATE_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    except Exception:  # noqa: BLE001 - 落盘失败不影响本次结果展示
        pass
    return merged


def summarize(results: list[dict[str, Any]]) -> dict[str, int]:
    """按状态计数，供界面与审计使用。"""
    counts = {"total": len(results), "new": 0, "unchanged": 0, "changed": 0,
              "error": 0, "local_outdated": 0, "checked_at": ""}
    for result in results:
        key = str(result.get("status") or "error")
        counts[key] = counts.get(key, 0) + 1
        if result.get("local_state") == "different":
            counts["local_outdated"] += 1
        counts["checked_at"] = counts["checked_at"] or str(result.get("checked_at") or "")
    return counts


def describe(result: dict[str, Any]) -> str:
    """一句话描述（界面与审计共用同一份文案）。"""
    status = str(result.get("status") or "error")
    if status == "error":
        return f"未检查：{result.get('error') or '未知错误'}"
    parts = [STATUS_LABELS.get(status, status)]
    if result.get("chars"):
        parts.append(f"正文 {result['chars']:,} 字")
    elif result.get("bytes"):
        parts.append(f"{result['bytes'] / 1024:.0f} KB（按字节比对）")
    if result.get("local_state"):
        parts.append(LOCAL_LABELS.get(result["local_state"], result["local_state"]))
    return "；".join(parts)
