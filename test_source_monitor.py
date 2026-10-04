# -*- coding: utf-8 -*-
"""外部规则来源监控测试（全部离线：抓取器与正文提取器都可注入）。

口径：
- 监控只做"发现"：指纹一样就是无变化，不一样就是官方文档有更新；
- 任何网络/解析失败都只是"本次未检查"，不能抛给调用方、不能影响计算主流程；
- 失败的检查不能覆盖上一次成功的指纹（否则会把旧文档误判成"新更新"）。
"""
import json
import urllib.error

import pytest

from source_monitor import (DEFAULT_WATCHLIST, check_all, check_source, describe,
                            extract_text, fetch, load_state, load_watchlist,
                            save_state, snapshot_of, summarize)

ITEM = {"id": "doc", "name": "测试文档", "url": "https://example.org/a.pdf",
        "why": "测试用"}
OTHER = {"id": "doc2", "name": "测试文档2", "url": "https://example.org/b.pdf",
         "why": "测试用"}


def _fetcher(payloads: dict, content_type: str = "application/pdf"):
    """按 URL 返回预设内容；值是异常实例则抛出，用于覆盖失败路径。"""
    def _fetch(url: str, timeout: int):
        data = payloads[url]
        if isinstance(data, Exception):
            raise data
        return content_type, data
    return _fetch


def _state(**digests) -> dict:
    return {key: {"digest": value, "basis": "bytes", "checked_at": "2026-09-01 00:00:00"}
            for key, value in digests.items()}


# ── 关注清单 ──

def test_default_watchlist_holds_verified_oecd_sources(tmp_path):
    items = load_watchlist(tmp_path / "不存在.json")
    assert [item["id"] for item in items] == ["handbook", "admin_guidance",
                                             "central_record"]
    for item in items:
        assert item["url"].startswith("https://www.oecd.org/content/dam/")
        assert item["url"].endswith(".pdf")
        assert item["name"] and item["why"]
    handbook = items[0]
    assert handbook["local_path"] == "minimum-tax-implementation-handbook-pillar-two.pdf"
    assert len(DEFAULT_WATCHLIST) == 3


def test_watchlist_file_overrides_builtin(tmp_path):
    path = tmp_path / "source_watchlist.json"
    path.write_text(json.dumps({"sources": [
        {"id": "mine", "name": "我的来源", "url": "https://example.org/x.pdf"}]},
        ensure_ascii=False), encoding="utf-8")
    items = load_watchlist(path)
    assert [item["id"] for item in items] == ["mine"]
    assert items[0]["local_path"] is None


def test_watchlist_skips_invalid_and_duplicate_entries(tmp_path):
    path = tmp_path / "source_watchlist.json"
    path.write_text(json.dumps([
        {"id": "ok", "url": "https://example.org/1.pdf", "name": "1"},
        {"name": "缺 id", "url": "https://example.org/2.pdf"},
        {"id": "bad_url", "url": "ftp://example.org/3.pdf"},
        "不是字典",
        {"id": "ok", "url": "https://example.org/4.pdf", "name": "重复 id"},
    ], ensure_ascii=False), encoding="utf-8")
    items = load_watchlist(path)
    assert len(items) == 1 and items[0]["id"] == "ok"
    assert items[0]["name"] == "1", "重复 id 保留第一条"
    assert items[0]["why"] == ""


def test_watchlist_falls_back_when_file_is_broken(tmp_path):
    path = tmp_path / "source_watchlist.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    assert len(load_watchlist(path)) == len(DEFAULT_WATCHLIST)


# ── 指纹 ──

def test_snapshot_prefers_text_digest(tmp_path):
    """同样的字节、不同的正文 → 指纹必须不同（证明按正文而非字节比对）。"""
    text_a = "A" * 400
    text_b = "B" * 400
    snap_a = snapshot_of("application/pdf", b"same-bytes",
                         extractor=lambda ctype, data: text_a)
    snap_b = snapshot_of("application/pdf", b"same-bytes",
                         extractor=lambda ctype, data: text_b)
    assert snap_a["basis"] == "text" and snap_a["chars"] == 400
    assert snap_a["digest"] != snap_b["digest"]


def test_snapshot_falls_back_to_bytes_when_text_unusable():
    short = snapshot_of("application/pdf", b"x" * 10,
                        extractor=lambda ctype, data: "太短了")
    assert short["basis"] == "bytes" and short["chars"] == 0
    missing = snapshot_of("application/pdf", b"y" * 10,
                          extractor=lambda ctype, data: None)
    assert missing["basis"] == "bytes"
    assert missing["digest"] != short["digest"]


def test_extract_text_without_extractor_ignores_non_pdf():
    assert extract_text("text/html", b"<html></html>") is None


def test_extract_text_reads_real_pdf_fixture():
    """真实官方 PDF（仓库自带副本）能提出正文 —— 正文比对这条路是真的能用。"""
    pytest.importorskip("pypdf")
    from pathlib import Path

    from source_monitor import BASE_DIR

    path = BASE_DIR / "minimum-tax-implementation-handbook-pillar-two.pdf"
    if not path.exists():
        pytest.skip("仓库未自带该 PDF")
    snapshot = snapshot_of("application/pdf", path.read_bytes())
    assert snapshot["basis"] == "text"
    assert snapshot["chars"] > 20_000, "真实手册实测约 8.4 万字"


# ── 检查 ──

def test_check_source_marks_first_run_as_new():
    result = check_source(ITEM, {}, fetcher=_fetcher({ITEM["url"]: b"abc"}))
    assert result["status"] == "new"
    assert result["bytes"] == 3 and result["digest"]
    assert describe(result).startswith("首次建立基线")


def test_check_source_detects_change_and_no_change():
    payloads = {ITEM["url"]: b"abc"}
    first = check_source(ITEM, {}, fetcher=_fetcher(payloads))
    same = check_source(ITEM, _state(doc=first["digest"]),
                        fetcher=_fetcher(payloads))
    assert same["status"] == "unchanged"
    payloads[ITEM["url"]] = b"abcd"
    changed = check_source(ITEM, _state(doc=first["digest"]),
                           fetcher=_fetcher(payloads))
    assert changed["status"] == "changed"
    assert changed["previous_digest"] == first["digest"]
    assert "有更新" in describe(changed)


def test_check_source_turns_failures_into_error():
    for failure in (urllib.error.URLError("离线"),
                    TimeoutError("超时"),
                    urllib.error.HTTPError(ITEM["url"], 404, "Not Found", None, None)):
        result = check_source(ITEM, {}, fetcher=_fetcher({ITEM["url"]: failure}))
        assert result["status"] == "error"
        assert result["digest"] == "" and result["error"]
        assert describe(result).startswith("未检查：")


def test_check_source_compares_local_copy(tmp_path):
    local = tmp_path / "local.pdf"
    local.write_bytes(b"abc")
    item = dict(ITEM, local_path=str(local))

    same = check_source(item, _state(doc="x"),
                        fetcher=_fetcher({ITEM["url"]: b"abc"}))
    assert same["local_state"] == "same"
    assert "与本地副本一致" in describe(same)

    local.write_bytes(b"old")
    behind = check_source(item, _state(doc="x"),
                          fetcher=_fetcher({ITEM["url"]: b"abc"}))
    assert behind["local_state"] == "different"

    missing = check_source(dict(ITEM, local_path=str(tmp_path / "nope.pdf")), {},
                           fetcher=_fetcher({ITEM["url"]: b"abc"}))
    assert missing["local_state"] == "missing"
    assert check_source(ITEM, {}, fetcher=_fetcher({ITEM["url"]: b"abc"}))["local_state"] is None


def test_check_all_preserves_order_and_marks_missing_baseline():
    results = check_all([ITEM, OTHER], {}, fetcher=_fetcher({
        ITEM["url"]: b"abc", OTHER["url"]: urllib.error.URLError("离线")}))
    assert [r["id"] for r in results] == ["doc", "doc2"]
    assert results[0]["status"] == "new"
    assert results[1]["status"] == "error"
    assert results[0]["checked_at"] == results[1]["checked_at"], "同一次检查同一时间戳"
    assert check_all([], {}) == []


def test_fetch_uses_injected_fetcher():
    content_type, data = fetch("https://example.org/a.pdf",
                               fetcher=lambda url, timeout: ("text/plain", b"hi"))
    assert (content_type, data) == ("text/plain", b"hi")


# ── 快照状态 ──

def test_state_round_trip(tmp_path):
    path = tmp_path / "snapshots.json"
    results = [check_source(ITEM, {}, fetcher=_fetcher({ITEM["url"]: b"abc"}))]
    saved = save_state(results, load_state(path), path)
    assert load_state(path)[ITEM["id"]]["digest"] == saved[ITEM["id"]]["digest"]
    assert load_state(tmp_path / "不存在.json") == {}


def test_save_state_keeps_old_fingerprint_on_error(tmp_path):
    """抓取失败不能清掉旧指纹，否则下一次会把同一份文档误报成"有更新"。"""
    path = tmp_path / "snapshots.json"
    old = _state(doc="old-digest")
    failed = [check_source(ITEM, old, fetcher=_fetcher(
        {ITEM["url"]: urllib.error.URLError("离线")}))]
    merged = save_state(failed, old, path)
    assert merged["doc"]["digest"] == "old-digest"


def test_save_state_survives_unwritable_path():
    results = [check_source(ITEM, {}, fetcher=_fetcher({ITEM["url"]: b"abc"}))]
    merged = save_state(results, {}, "\0:/bad/path/x.json")
    assert merged["doc"]["digest"], "落盘失败也要返回合并后的状态"


def test_summarize_counts_states_and_local_lag():
    results = [
        {"id": "a", "status": "changed", "local_state": "different",
         "checked_at": "2026-09-29 10:00:00"},
        {"id": "b", "status": "unchanged", "local_state": "same",
         "checked_at": "2026-09-29 10:00:00"},
        {"id": "c", "status": "error", "error": "离线"},
    ]
    counts = summarize(results)
    assert counts["total"] == 3 and counts["changed"] == 1
    assert counts["unchanged"] == 1 and counts["error"] == 1
    assert counts["local_outdated"] == 1
    assert counts["checked_at"] == "2026-09-29 10:00:00"


def test_describe_covers_every_status():
    assert "无变化" in describe({"status": "unchanged", "bytes": 4096})
    assert "正文 1,024 字" in describe({"status": "new", "chars": 1024})
    assert "未检查" in describe({"status": "error", "error": "超时"})
    assert describe({"status": "changed", "local_state": "unknown"}).startswith("⚠️")
