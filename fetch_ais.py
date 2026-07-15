# -*- coding: utf-8 -*-
"""
Get_AIS 収集スクリプト (エリアスキャン方式)
- AISStream.io に短時間だけ WebSocket 接続し、areas.json のバウンディングボックス内に
  いる「全ての船」の最新位置(PositionReport)を拾って data/vessels.json に書き出す。
- watchlist.json に MMSI がある船は name/cat をそれで上書きし known=true にする
  (既知の宇宙関連船=SpaceX/遠望 をアプリ側で目立たせるため)。
- 既知船は前回値(last-known)を引き継ぐ(港でAIS停波中でも急に消さない)。一般船は今回分のみ。
環境変数:
    AISSTREAM_KEY        … APIキー(必須・GitHub Secret)
    AIS_COLLECT_SECONDS  … 収集する秒数(既定90)
    AIS_OUT              … 出力先(既定 data/vessels.json)
    AIS_MAX_VESSELS      … 出力する最大隻数(既定400・既知船を優先的に残す)
    AIS_DEBUG=1          … 診断(ボックス内の受信数を数えるだけ・json更新なし)
"""
import asyncio
import datetime
import json
import os
import sys
import time

import websockets  # requirements.txt

API_KEY = os.environ.get("AISSTREAM_KEY")
COLLECT_SECONDS = int(os.environ.get("AIS_COLLECT_SECONDS", "90"))
OUT = os.environ.get("AIS_OUT", "data/vessels.json")
MAX_VESSELS = int(os.environ.get("AIS_MAX_VESSELS", "400"))
WS_URL = "wss://stream.aisstream.io/v0/stream"
DEBUG = os.environ.get("AIS_DEBUG") == "1"


def load_areas():
    """areas.json -> [[[latNW,lonNW],[latSE,lonSE]], ...]"""
    with open("areas.json", encoding="utf-8") as f:
        a = json.load(f)
    boxes = []
    for area in a.get("areas", []):
        b = area.get("box")
        if b and len(b) == 2 and len(b[0]) == 2 and len(b[1]) == 2:
            boxes.append([[float(b[0][0]), float(b[0][1])], [float(b[1][0]), float(b[1][1])]])
    return boxes


def load_watchlist():
    """既知船のタグ付け用 {mmsi(str): {"name":str, "cat":str}}"""
    try:
        with open("watchlist.json", encoding="utf-8") as f:
            wl = json.load(f)
    except Exception:
        return {}
    m = {}
    for v in wl.get("vessels", []):
        mmsi = str(v.get("mmsi", "")).strip()
        if mmsi:
            m[mmsi] = {
                "name": (v.get("name", "") or "").strip(),
                "cat": (v.get("cat", "") or "").strip(),
            }
    return m


def load_prev():
    """前回の vessels.json -> {mmsi: record}"""
    prev = {}
    try:
        with open(OUT, encoding="utf-8") as f:
            for v in json.load(f).get("vessels", []):
                prev[str(v.get("mmsi", ""))] = v
    except Exception:
        pass
    return prev


async def _run(sub, seconds, on_position):
    """sub を購読し seconds 秒間 PositionReport を on_position(meta, pr) に流す。"""
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
                if DEBUG:
                    print("non-position msg:", str(msg)[:200], file=sys.stderr)
                continue
            meta = msg.get("MetaData", {}) or {}
            pr = (msg.get("Message", {}) or {}).get("PositionReport", {}) or {}
            on_position(meta, pr)


async def collect(boxes, wl):
    """boxes 内の全船の最新レコードを {mmsi: record} で返す。"""
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
            "lat": lat,
            "lon": lon,
            "cog": pr.get("Cog"),          # 対地進路(度)
            "sog": pr.get("Sog"),          # 対地速力(kn)
            "heading": pr.get("TrueHeading"),
            "time": meta.get("time_utc", ""),  # 受信UTC
        }

    sub = {"APIKey": API_KEY, "BoundingBoxes": boxes, "FilterMessageTypes": ["PositionReport"]}
    await _run(sub, COLLECT_SECONDS, on_position)
    return latest


async def debug_collect(boxes):
    """診断: areas.json のボックス内の受信数を数える。認証/ストリーム生存確認用。"""
    total = [0]
    seen = {}

    def on_position(meta, pr):
        total[0] += 1
        seen[str(meta.get("MMSI", ""))] = (meta.get("ShipName", "") or "").strip()

    sub = {"APIKey": API_KEY, "BoundingBoxes": boxes, "FilterMessageTypes": ["PositionReport"]}
    await _run(sub, 60, on_position)
    print(f"[DEBUG] total PositionReports in 60s (areas): {total[0]}")
    print(f"[DEBUG] distinct vessels: {len(seen)}")
    wl = load_watchlist()
    hit = [(m, seen[m]) for m in wl if m in seen]
    print(f"[DEBUG] watchlist(既知) vessels seen: {hit if hit else 'none'}")
    for m, n in list(seen.items())[:25]:
        print(f"   {m}  {n}")


def _cap(merged):
    """MAX_VESSELS を超えたら既知船を優先し、残りは受信時刻の新しい順で残す。"""
    if len(merged) <= MAX_VESSELS:
        return merged
    items = list(merged.values())
    items.sort(key=lambda v: (0 if v.get("known") else 1, v.get("time", "")), reverse=False)
    # known を先頭に集めつつ、known優先で新しいものを残す
    known = [v for v in items if v.get("known")]
    others = [v for v in items if not v.get("known")]
    others.sort(key=lambda v: v.get("time", ""), reverse=True)
    keep = (known + others)[:MAX_VESSELS]
    return {v["mmsi"]: v for v in keep}


def main():
    if not API_KEY:
        print("ERROR: AISSTREAM_KEY が未設定です", file=sys.stderr)
        sys.exit(1)
    boxes = load_areas()
    if not boxes:
        print("WARN: areas.json にボックスがありません(収集対象なし)", file=sys.stderr)

    if DEBUG:
        if boxes:
            asyncio.get_event_loop().run_until_complete(debug_collect(boxes))
        return

    wl = load_watchlist()
    latest = {}
    if boxes:
        latest = asyncio.get_event_loop().run_until_complete(collect(boxes, wl))

    # last-known は「既知船」だけ引き継ぐ(一般船は流動的なので今回分のみ)。
    merged = {}
    for m, v in load_prev().items():
        if v.get("known"):
            merged[m] = v
    merged.update(latest)
    merged = _cap(merged)

    known_n = sum(1 for v in merged.values() if v.get("known"))
    out = {
        "updated": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "source": "AISStream.io",
        "vessels": list(merged.values()),
    }
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"received {len(latest)} / kept {len(out['vessels'])} (known={known_n}) -> {OUT}")


if __name__ == "__main__":
    main()
