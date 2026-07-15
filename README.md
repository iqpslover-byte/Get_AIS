# Get_AIS

**航行警報（NAVWARN）の危険区域にいる船**の AIS 位置を定期取得して `data/vessels.json` に書き出す中継リポジトリ。
OP's LAB Maps が raw 経由でこの JSON を読み、警報と同じ地図に船マーカーを描く。
既知の宇宙関連船（SpaceX ドローン船・回収船／中国 遠望 追跡船）は色分けして目立たせる。

（Get_TLE / Get_NAVWARN / Get_LAUNCHES と同じ「GitHub Actions で取得 → JSON を commit」方式）

## 仕組み
`fetch_ais.py` が **AISStream.io** に WebSocket 接続して2パスで収集：
- **パス1（危険区域）**：Get_NAVWARN の日次電文 `DailyMem*.txt`（＝現在有効な警報）から座標を抽出し、
  危険区域を囲むバウンディングボックスを自動生成 → **その中の全船**を拾う。
- **パス2（既知船）**：`watchlist.json` の MMSI（SpaceX/遠望）を**全球**で拾う（区域外でも捕捉）。
- 既知船は `known:true`＋`cat` でタグ。既知船は前回値(last-known)を引き継ぐ。
- NAVWARN が取れない時は `areas.json` の固定ボックスにフォールバック。
- `.github/workflows/ais.yml` が **20分ごと**（＋手動）に実行し commit/push。

## ファイル構成
```
Get_AIS/
├─ fetch_ais.py                 収集本体(航行警報連動・2パス)
├─ watchlist.json              既知船(SpaceX/遠望)の MMSI とカテゴリ(色分け用)
├─ areas.json                  NAVWARN が取れない時のフォールバック箱
├─ requirements.txt            websockets
├─ data/vessels.json           出力(Actions が上書き)
└─ .github/workflows/ais.yml    20分ごと + 手動(debug入力あり)
```

## セットアップ手順
1. この中身で GitHub リポジトリ `Get_AIS` を作る（**Public** 推奨＝アプリが raw を無認証で読むため）。
2. **AISStream.io** の無料アカウントで API キー取得： https://aisstream.io/apikeys
3. リポジトリの **Settings → Secrets and variables → Actions** で secret `AISSTREAM_KEY` を登録。
4. **Settings → Actions → General** で Read and write permissions を許可。
5. **Actions → Collect AIS → Run workflow** で手動実行し、`data/vessels.json` が更新されるか確認。
   - `AIS_DEBUG=1`（workflow_dispatch の debug=1）で診断（区域内の受信数を数えるだけ・json更新なし）。
6. アプリが読む raw URL：`https://raw.githubusercontent.com/<あなた>/Get_AIS/main/data/vessels.json`

## 出力フォーマット（`data/vessels.json`）
```json
{
  "updated": "2026-07-15T13:00:20Z",
  "source": "AISStream.io",
  "boxes": [ [[33.0,-81.5],[25.0,-72.0]], ... ],
  "vessels": [
    { "mmsi": "413289000", "name": "Yuanwang 5", "cat": "China", "known": true,
      "zone": true, "lat": 28.5, "lon": -75.2, "cog": 210.0, "sog": 8.3,
      "heading": 208, "time": "2026-07-15 12:58:41.0 +0000 UTC" }
  ]
}
```
- `known` … watchlist の既知船か（アプリで色分け）
- `zone`  … 危険区域ボックス内で拾った船か
- `boxes` … 今回購読した危険区域ボックス（参考）

## 監視リスト（watchlist.json・2026-07 時点で確認済み）
| 船 | MMSI | cat |
|---|---|---|
| ASOG (A Shortfall of Gravitas) | 368219910 | SpaceX |
| JRTI (Just Read the Instructions) | 368219920 | SpaceX |
| Doug（回収船） | 368485000 | SpaceX |
| Bob（回収船） | 368456000 | SpaceX |
| Yuanwang 5/6/7（遠望 追跡船） | 413289000 / 413326000 / 413379290 | China |

OCISLY は西海岸バージで MMSI 未確定（空欄）。船は入れ替わるので随時メンテ。

## 制約・注意
- **AISStream の受信範囲に依存**。米沿岸（フロリダ/カリフォルニア）の区域はよく入るが、
  **外洋・非西側沿岸の区域はスカスカ/空**になりがち（無料枠の宿命）。全区域で必ず船が出るわけではない。
- 環境変数：`AIS_COLLECT_SECONDS`(既定90=パス1:60/パス2:30)、`AIS_MAX_BOXES`(既定20)、
  `AIS_MAX_VESSELS`(既定500)、`AIS_OUT`。
- cron 20分は目安。AISStream 無料枠・Actions 実行時間と相談して調整。
