# -*- coding: utf-8 -*-
"""
Get_AIS 収集スクリプト
- AISStream.io に短時間だけ WebSocket 接続し、watchlist.json の MMSI の
  最新の位置(PositionReport)を拾って data/vessels.json に書き出す。
- 今回受信できなかった船は前回の値(last-known)を引き継ぐ(地図から消さないため)。
- 環境変数:
    AISSTREAM_KEY        … AISStream.io の APIキー(必須・GitHub Secret)
    AIS_COLLECT_SECONDS  … 収集する秒数(既定90)
    AIS_OUT              … 出力先(既定 data/vessels.json)
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
WS_URL = "wss://stream.aisstream.io/v0/stream"
DEBUG = os.environ.get("AIS_DEBUG") == "1"   # 診断: フロリダ沿岸の全AISを数える(MMSIフィルタなし・json更新なし)


def load_watchlist():
    """watchlist.json -> {mmsi(str): {"name":str, "cat":str}}  (mmsi が空の行は無視)"""
    with open("watchlist.json", encoding="utf-8") as f:
        wl = json.load(f)
    m = {}
    for v in wl.get("vessels", []):
        mmsi = str(v.get("mmsi", "")).strip()
        if mmsi:
            m[mmsi] = {
                "name": (v.get("name", "") or "").strip(),
                "cat": (v.get("cat", "") or "").strip(),   # カテゴリ(アプリ側の色分け用)
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


async def debug_collect():
    """診断: フロリダ沿岸(Canaveral周辺)の全PositionReportを数える。認証/ストリームの生存確認用。"""
    sub = {
        "APIKey": API_KEY,
        "BoundingBoxes": [[[24.0, -82.0], [31.0, -78.0]]],
        "FilterMessageTypes": ["PositionReport"],
    }
    total = 0
    seen = {}
    end = time.time() + 60
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
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("MessageType") != "PositionReport":
                print("non-position msg:", str(msg)[:200], file=sys.stderr)
                continue
            total += 1
            meta = msg.get("MetaData", {}) or {}
            seen[str(meta.get("MMSI", ""))] = (meta.get("ShipName", "") or "").strip()
    print(f"[DEBUG] total PositionReports in 60s (FL coast): {total}")
    print(f"[DEBUG] distinct vessels: {len(seen)}")
    wl = load_watchlist()
    hit = [m for m in wl if m in seen]
    print(f"[DEBUG] watchlist vessels seen: {hit if hit else 'none'}")
    for i, (m, n) in enumerate(list(seen.items())[:20]):
        print(f"   {m}  {n}")


async def collect(mmsi_map):
    """収集した最新レコードを {mmsi: record} で返す"""
    latest = {}
    sub = {
        "APIKey": API_KEY,
        # BoundingBoxes は必須。MMSI フィルタと併用するので全球でよい。
        "BoundingBoxes": [[[-90, -180], [90, 180]]],
        "FiltersShipMMSI": list(mmsi_map.keys()),
        "FilterMessageTypes": ["PositionReport"],
    }
    end = time.time() + COLLECT_SECONDS
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
            mmsi = str(meta.get("MMSI", ""))
            if mmsi not in mmsi_map:
                continue
            lat = pr.get("Latitude")
            lon = pr.get("Longitude")
            if lat is None or lon is None:
                continue
            info = mmsi_map[mmsi]
            latest[mmsi] = {
                "mmsi": mmsi,
                "name": info["name"] or (meta.get("ShipName", "") or "").strip(),
                "cat": info.get("cat", ""),
                "lat": lat,
                "lon": lon,
                "cog": pr.get("Cog"),          # 対地進路(度)
                "sog": pr.get("Sog"),          # 対地速力(kn)
                "heading": pr.get("TrueHeading"),
                "time": meta.get("time_utc", ""),  # 受信UTC
            }
    return latest


def main():
    if not API_KEY:
        print("ERROR: AISSTREAM_KEY が未設定です", file=sys.stderr)
        sys.exit(1)
    if DEBUG:
        asyncio.get_event_loop().run_until_complete(debug_collect())
        return
    mmsi_map = load_watchlist()
    if not mmsi_map:
        print("WARN: watchlist.json に有効な MMSI がありません(追跡対象なし)", file=sys.stderr)

    latest = {}
    if mmsi_map:
        latest = asyncio.get_event_loop().run_until_complete(collect(mmsi_map))

    # last-known を引き継ぐ(今回未受信の船も残す)。watchlist から外れた船は落とす。
    merged = dict(load_prev())
    merged.update(latest)
    merged = {k: v for k, v in merged.items() if k in mmsi_map}

    out = {
        "updated": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "source": "AISStream.io",
        "vessels": list(merged.values()),
    }
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"received {len(latest)} / kept {len(out['vessels'])} vessels -> {OUT}")


if __name__ == "__main__":
    main()
