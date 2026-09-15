#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每 30 分钟跑一次：拉 tokenfly.ai 统计快照，归档到 history.json。

合并规则（防 Render 重启丢数）：
- snapshot 带 boot_id（每次服务启动随机），days 内是该 boot 的累计值。
- 本地 .boots.json 记住每个 (date, boot) 的最后累计值；
  history.json[date] = 各 boot 累计值按 key 求和（PV 类精确，UV 为近似）。
"""
import json
import re
import subprocess
import sys
import urllib.request
from http.client import IncompleteRead
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
HERE = Path(__file__).parent
HISTORY = HERE / "history.json"
BOOTS = HERE / ".boots.json"


def get_key():
    yml = Path.home() / "workspace" / "tokenfly" / "render.yaml"
    m = re.search(r"key: STATS_KEY\n\s+value: (\S+)", yml.read_text())
    if not m:
        sys.exit("STATS_KEY not found in render.yaml")
    return m.group(1)


def fetch_snapshot(key):
    url = f"https://tokenfly.ai/api/stats/snapshot?key={key}"
    req = urllib.request.Request(url, headers={"User-Agent": "tfstats-sync/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        try:
            raw = r.read()
        except IncompleteRead as e:
            # 服务端（Cloudflare/aiohttp chunked）偶尔提前断流，但已读数据通常完整；
            # 用已读部分尝试解析，截断了会 json 报错走正常失败路径
            raw = e.partial
        j = json.loads(raw.decode())
    if not j.get("ok"):
        sys.exit(f"snapshot not ok: {j}")
    return j


def merge_dicts(dicts):
    out = {}
    for d in dicts:
        for k, v in (d or {}).items():
            cnt = v.get("count", v.get("pv", v)) if isinstance(v, dict) else v
            out[k] = out.get(k, 0) + cnt
    return out


def merge_cities(dicts):
    out = {}
    for d in dicts:
        for k, v in (d or {}).items():
            if isinstance(v, dict):
                e = out.get(k)
                if not e:
                    e = {"count": 0, "city": v.get("city", ""), "country": v.get("country", ""),
                         "cc": v.get("cc", ""), "lat": v.get("lat", 0), "lon": v.get("lon", 0)}
                    out[k] = e
                e["count"] += v.get("count", 0)
                if not e["lat"] and v.get("lat"):
                    e["lat"], e["lon"] = v["lat"], v["lon"]
            else:
                e = out.get(k) or {"count": 0, "city": k, "country": "", "cc": "", "lat": 0, "lon": 0}
                e["count"] += v
                out[k] = e
    return out


def apply_snapshot(snap, boots, history):
    """把一次 snapshot 合并进 history/boots。

    persistent 快照（Turso 模式）：days 是全量累计，直接覆盖对应日期，
    不按 boot 求和（否则重复计数）；boots 不动。
    非 persistent：旧逻辑——按 (date, boot) 记累计值，history 为各 boot 求和。
    """
    days = snap.get("days", {})
    if snap.get("persistent"):
        for date, d in days.items():
            history.setdefault("days", {})[date] = d
        return history, boots

    boot = snap["boot"]
    for date, d in days.items():
        boots.setdefault(date, {})[boot] = d

    # 修剪：只留最近 4 天的 boots
    cutoff = (datetime.now(PT) - timedelta(days=4)).strftime("%Y-%m-%d")
    for date in list(boots):
        if date < cutoff:
            del boots[date]

    # 重算 history（各 boot 求和）
    for date, per_boot in boots.items():
        vals = list(per_boot.values())
        history.setdefault("days", {})[date] = {
            "pv": sum(v.get("pv", 0) for v in vals),
            "uv": sum(v.get("uv", 0) for v in vals),
            "bots": sum(v.get("bots", 0) for v in vals),
            "api": sum(v.get("api", 0) for v in vals),
            "pages": merge_dicts(v.get("pages") for v in vals),
            "page_uv": merge_dicts(v.get("page_uv") for v in vals),
            "countries": merge_dicts(v.get("countries") for v in vals),
            "cities": merge_cities(v.get("cities") for v in vals),
            "refs": merge_dicts(v.get("refs") for v in vals),
            "devices": merge_dicts(v.get("devices") for v in vals),
        }
    return history, boots


def main():
    key = get_key()
    snap = fetch_snapshot(key)
    days = snap.get("days", {})

    boots = json.loads(BOOTS.read_text()) if BOOTS.exists() else {}
    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else {"days": {}}

    history, boots = apply_snapshot(snap, boots, history)

    old_h, old_b = HISTORY.read_text(), BOOTS.read_text() if BOOTS.exists() else ""
    new_h = json.dumps(history, ensure_ascii=False, sort_keys=True)
    new_b = json.dumps(boots, ensure_ascii=False, sort_keys=True)
    if new_h == old_h and new_b == old_b:
        print("no change")
        return
    HISTORY.write_text(new_h)
    BOOTS.write_text(new_b)

    subprocess.run(["git", "add", "history.json"], cwd=HERE, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Spark", "-c", "user.email=spark@local",
         "commit", "-qm", f"stats sync {datetime.now(PT):%Y-%m-%d %H:%M}"],
        cwd=HERE, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=HERE, check=True)
    print(f"synced: {list(days)}")


if __name__ == "__main__":
    main()
