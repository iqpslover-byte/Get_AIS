# Get_AIS

SpaceX ドローン船・回収船などの **AIS 位置**を定期取得して `data/vessels.json` に書き出す中継リポジトリ。
OP's LAB Maps が raw 経由でこの JSON を読み、地図に船マーカーを描く。

（Get_TLE / Get_NAVWARN / Get_LAUNCHES と同じ「GitHub Actions で取得 → JSON を commit」方式）

## 仕組み
- `fetch_ais.py` が **AISStream.io** に約90秒だけ WebSocket 接続し、`watchlist.json` の MMSI の
  最新 PositionReport を拾って `data/vessels.json` に書く。
- 今回受信できなかった船は前回値(last-known)を引き継ぐ（地図から消さない）。
- `.github/workflows/ais.yml` が **20分ごと**（＋手動）に実行し commit/push。

## セットアップ手順
1. **このフォルダの中身で新しい GitHub リポジトリ `Get_AIS` を作る**（**Public** 推奨＝アプリが raw を無認証で読むため。他のデータ用リポジトリと同じ）。
   ```
   Get_AIS/
   ├─ fetch_ais.py
   ├─ watchlist.json
   ├─ requirements.txt
   ├─ data/vessels.json      (空のひな形。Actions が上書き)
   └─ .github/workflows/ais.yml
   ```
2. **AISStream.io の無料アカウント**を作成し API キーを取得： https://aisstream.io/
3. リポジトリの **Settings → Secrets and variables → Actions → New repository secret** で
   - Name: `AISSTREAM_KEY`
   - Value: 取得したキー
4. **`watchlist.json` の各船の `mmsi`（9桁）を埋める**。MarineTraffic / VesselFinder で船名検索して確認。
   `mmsi` が空の行は追跡対象外。
5. **Settings → Actions → General** で workflow の実行を許可（Read and write permissions）。
6. **Actions タブ → Collect AIS → Run workflow** で手動実行して、`data/vessels.json` が更新されるか確認。
7. アプリ側はこの raw URL を読む（実装時に設定）：
   ```
   https://raw.githubusercontent.com/<あなた>/Get_AIS/main/data/vessels.json
   ```

## 出力フォーマット（`data/vessels.json`）
```json
{
  "updated": "2026-07-15T12:00:00Z",
  "source": "AISStream.io",
  "vessels": [
    { "mmsi": "368105000", "name": "ASOG", "lat": 28.5, "lon": -75.2,
      "cog": 210.0, "sog": 8.3, "heading": 208, "time": "2026-07-15 11:58:41.0 +0000 UTC" }
  ]
}
```

## 制約・注意
- **AISStream の受信範囲に依存**。陸上受信局の届く沿岸〜近海はよく入るが、**外洋のドローン船は衛星AIS頼みで疎になる/欠ける**ことがある。無料枠の宿命として「常に全船が映る」わけではない。
- そのため last-known 引き継ぎで直近の位置を保持する。`time` フィールドで鮮度が分かるので、アプリ側で「◯分前」等の表示や古いものの淡色化を行うとよい。
- 船は入れ替わる。`watchlist.json` は随時メンテする。
- cron 20分は目安。AISStream 無料枠・Actions 実行時間と相談して調整。
