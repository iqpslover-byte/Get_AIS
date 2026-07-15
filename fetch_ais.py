# -*- coding: utf-8 -*-
"""
Get_AIS 収集スクリプト (航行警報連動)
- Get_NAVWARN の日次電文(DailyMem*.txt=現在有効な警報)から座標を抽出し、
  危険区域を囲むバウンディングボックスを自動生成。
- パス1: そのボックス内の「全船」を収集(=危険区域付近の船)。
- パス2: watchlist.json の既知船(SpaceX/遠望=回収/追跡船)を全球で収集(区域外でも捕捉)。
- 既知船は known=true でタグ(アプリ側で色分け)。既知船は last-known 引き継ぎ。
- NAVWARN が取れない時は areas.json の固定ボックスにフォールバック。
環境変数: AISSTREAM_KEY(必須), AIS_COLLECT_SECONDS(既定90=パス1:60/パス2:30),
          AIS_OUT(既定 data/vessels.json), AIS_MAX_VESSELS(既定500),
          AIS_MAX_BOXES(既定20), AIS_DEBUG=1(診断)
"""
import asyncio
import datetime
import json
import math
import os
import re
import sys
import time
import urllib.request

import websockets

API_KEY = os.environ.get("AISSTREAM_KEY")
COLLECT_SECONDS = int(os.environ.get("AIS_COLLECT_SECONDS", "90"))
OUT = os.environ.get("AIS_OUT", "data/vessels.json")
MAX_VESSELS = int(os.environ.get("AIS_MAX_VESSELS", "500"))
MAX_BOXES = int(os.environ.get("AIS_MAX_BOXES", "20"))
WS_URL = "wss://stream.aisstream.io/v0/stream"
DEBUG = os.environ.get("AIS_DEBUG") == "1"

NAVWARN_BASE = "https://iqpslover-byte.github.io/Get_NAVWARN/data/"
NAVWARN_FILES = ["DailyMemPAC.txt", "DailyMemLAN.txt", "DailyMemXII.txt", "DailyMemIV.txt", "DailyMemARC.txt"]

# ---- NAVWARN 座標抽出(アプリ parse() の主要パターンを移植) ----
_PATS = [
    # DD-MM-SS.s N  DDD-MM-SS.s E (度分秒)
    (re.compile(r"(\d{1,3})-(\d{2})-(\d{2}\.?\d*)\s*([NS])[,\s;]+(\d{1,3})-(\d{2})-(\d{2}\.?\d*)\s*([EW])"), "dms"),
    # DD-MM.mm N  DDD-MM.mm E (度-小数分・NAVWARN主流)
    (re.compile(r"(\d{1,3})-(\d{1,2}\.\d+)\s*([NS])[,\s;]+(\d{1,3})-(\d{1,2}\.\d+)\s*([EW])"), "dm"),
    # DD-MM N  DDD-MM E (度-整数分)
    (re.compile(r"(\d{1,3})-(\d{2})\s*([NS])[,\s;]+(\d{1,3})-(\d{2})\s*([EW])"), "dmi"),
    # DD°MM.m'N DDD°MM.m'E (度記号)
    (re.compile(r"(\d{1,3})[°º]\s*(\d{1,2}\.?\d*)['′]?\s*([NS])[,\s;]+(\d{1,3})[°º]\s*(\d{1,2}\.?\d*)['′]?\s*([EW])"), "deg"),
]


def _pt(match, kind):
    g = match.groups()
    if kind == "dms":
        la = int(g[0]) + int(g[1]) / 60 + float(g[2]) / 3600
        if g[3] == "S":
            la = -la
        lo = int(g[4]) + int(g[5]) / 60 + float(g[6]) / 3600
        if g[7] == "W":
            lo = -lo
    else:
        la = int(g[0]) + float(g[1]) / 60
        if g[2] == "S":
            la = -la
        lo = int(g[3]) + float(g[4]) / 60
        if g[5] == "W":
            lo = -lo
    if -90 <= la <= 90 and -180 <= lo <= 180:
        return (la, lo)
    return None


def navwarn_points():
    """5エリアの日次電文から座標点 [(lat,lon), ...] を集める。"""
    pts = []
    for fn in NAVWARN_FILES:
        try:
            with urllib.request.urlopen(NAVWARN_BASE + fn, timeout=25) as r:
                txt = r.read().decode("utf-8", "ignore").upper()
        except Exception as e:
            print("navwarn fetch fail", fn, e, file=sys.stderr)
            continue
        for pat, kind in _PATS:
            for m in pat.finditer(txt):
                p = _pt(m, kind)
                if p:
                    pts.append(p)
    return pts


def boxes_from_points(pts, cell=5.0, pad=0.6):
    """点を cell 度グリッドでまとめ、点数の多い順に MAX_BOXES 個のボックスを返す。"""
    if not pts:
        return []
    cells = {}
    for la, lo in pts:
        key = (math.floor(la / cell), math.floor(lo / cell))
        cells.setdefault(key, []).append((la, lo))
    groups = sorted(cells.values(), key=len, reverse=True)[:MAX_BOXES]
    boxes = []
    for g in groups:
        las = [p[0] for p in g]
        los = [p[1] for p in g]
        nw = [min(90.0, max(las) + pad), max(-180.0, min(los) - pad)]
        se = [max(-90.0, min(las) - pad), min(180.0, max(los) + pad)]
        boxes.append([nw, se])
    return boxes


def load_areas():
    try:
        with open("areas.json", encoding="utf-8") as f:
            a = json.load(f)
    except Exception:
        return []
    boxes = []
    for area in a.get("areas", []):
        b = area.get("box")
        if b and len(b) == 2:
            boxes.append([[float(b[0][0]), float(b[0][1])], [float(b[1][0]), float(b[1][1])]])
    return boxes


def load_watchlist():
    try:
        with open("watchlist.json", encoding="utf-8") as f:
            wl = json.load(f)
    except Exception:
        return {}
    m = {}
    for v in wl.get("vessels", []):
        mmsi = str(v.get("mmsi", "")).strip()
        if mmsi:
            m[mmsi] = {"name": (v.get("name", "") or "").strip(), "cat": (v.get("cat", "") or "").strip()}
    return m


def load_prev():
    prev = {}
    try:
        with open(OUT, encoding="utf-8") as f:
            for v in json.load(f).get("vessels", []):
                prev[str(v.get("mmsi", ""))] = v
    except Exception:
        pass
    return prev


async def _run(sub, seconds, on_position):
    end = time.time() + seconds
    async with websockets.connect(WS_URL, ping_interval=20, close_timeout=5) as ws:
        await ws.send(json.dumps(sub))
        while time.time() < end:
            remaining = end - time.time()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            except Exception as e:
                print("recv error:", e, file=sys.stderr)
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("MessageType") != "PositionReport":
                continue
            meta = msg.get("MetaData", {}) or {}
            pr = (msg.get("Message", {}) or {}).get("PositionReport", {}) or {}
            on_position(meta, pr)


async def collect(boxes, wl, seconds, mmsi_filter=None, in_zone=False):
    latest = {}

    def on_position(meta, pr):
        mmsi = str(meta.get("MMSI", ""))
        if not mmsi:
            return
        lat = pr.get("Latitude")
        lon = pr.get("Longitude")
        if lat is None or lon is None:
            return
        tag = wl.get(mmsi)
        latest[mmsi] = {
            "mmsi": mmsi,
            "name": (tag["name"] if (tag and tag["name"]) else (meta.get("ShipName", "") or "").strip()),
            "cat": (tag["cat"] if tag else ""),
            "known": bool(tag),
            "zone": in_zone,          # 危険区域付近で拾った船
            "lat": lat, "lon": lon,
            "cog": pr.get("Cog"), "sog": pr.get("Sog"), "heading": pr.get("TrueHeading"),
            "time": meta.get("time_utc", ""),
        }

    sub = {"APIKey": API_KEY, "BoundingBoxes": boxes, "FilterMessageTypes": ["PositionReport"]}
    if mmsi_filter:
        sub["FiltersShipMMSI"] = mmsi_filter
    await _run(sub, seconds, on_position)
    return latest


def _cap(merged):
    if len(merged) <= MAX_VESSELS:
        return merged
    known = [v for v in merged.values() if v.get("known")]
    others = [v for v in merged.values() if not v.get("known")]
    others.sort(key=lambda v: v.get("time", ""), reverse=True)
    keep = (known + others)[:MAX_VESSELS]
    return {v["mmsi"]: v for v in keep}


def main():
    if not API_KEY:
        print("ERROR: AISSTREAM_KEY が未設定です", file=sys.stderr)
        sys.exit(1)

    pts = navwarn_points()
    boxes = boxes_from_points(pts)
    src = "navwarn"
    if not boxes:
        boxes = load_areas()
        src = "areas.json(fallback)"
    print(f"navwarn points={len(pts)} boxes={len(boxes)} src={src}")

    if DEBUG:
        total = [0]; seen = {}
        def on_p(meta, pr):
            total[0] += 1; seen[str(meta.get("MMSI", ""))] = (meta.get("ShipName", "") or "").strip()
        if boxes:
            asyncio.get_event_loop().run_until_complete(
                _run({"APIKey": API_KEY, "BoundingBoxes": boxes, "FilterMessageTypes": ["PositionReport"]}, 60, on_p))
        print(f"[DEBUG] boxes={len(boxes)} total={total[0]} distinct={len(seen)}")
        return

    wl = load_watchlist()
    p1 = max(30, int(COLLECT_SECONDS * 2 / 3))
    p2 = max(20, COLLECT_SECONDS - p1)

    latest = {}
    # パス1: 危険区域ボックス内の全船
    if boxes:
        latest.update(asyncio.get_event_loop().run_until_complete(collect(boxes, wl, p1, in_zone=True)))
    # パス2: 既知船(回収/追跡船)を全球で
    if wl:
        known = asyncio.get_event_loop().run_until_complete(
            collect([[[90, -180], [-90, 180]]], wl, p2, mmsi_filter=list(wl.keys())))
        latest.update(known)

    # last-known は既知船だけ引き継ぐ
    merged = {}
    for m, v in load_prev().items():
        if v.get("known"):
            merged[m] = v
    merged.update(latest)
    merged = _cap(merged)

    known_n = sum(1 for v in merged.values() if v.get("known"))
    zone_n = sum(1 for v in merged.values() if v.get("zone"))
    out = {
        "updated": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "source": "AISStream.io",
        "boxes": boxes,
        "vessels": list(merged.values()),
    }
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"received {len(latest)} / kept {len(out['vessels'])} (known={known_n}, zone={zone_n}) -> {OUT}")


if __name__ == "__main__":
    main()
