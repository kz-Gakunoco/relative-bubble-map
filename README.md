# Relative Bubble Map — 54銘柄版

米国株のバブルマップを、PCがOFFでも毎朝自動更新できる個人利用向けシステムです。

## できること
- X軸：セクター
- Y軸：変動率 / 比較ETF超過リターン
- 期間：1D / 5D / 20D / 60D / 3M / 6M / 12M / YTD
- バブルサイズ：時価総額 / 均一
- バブルカラー：セクター / パフォーマンス
- クリックでTradingViewを開く
- API取得失敗時は既存データを維持
- PCがOFFでもGitHub Actionsで毎朝07:30 JSTに更新

## データソース
`config/settings.json` の `provider` は初期値 `auto` です。

- `FMP_API_KEY` がGitHub Secretsにある → FMPを使用
- APIキーがない → yfinanceを使用

したがって、最初はAPIキーなし・0円で動作確認できます。
FMPを使う場合も、日付範囲をまとめて1銘柄1リクエストで取得し、毎回全履歴を取り直しません。

## 初回セットアップ
1. このフォルダ一式をGitHubのPrivate Repositoryへアップロード
2. Repository Settings → Pages → Deploy from a branch
3. Branchを `main`、Folderを `/docs` に設定
4. Actionsを有効化
5. FMPを使う場合だけ `Settings → Secrets and variables → Actions` に `FMP_API_KEY` を追加
6. Actions → `Update Bubble Map` → `Run workflow` を1回実行

数分後、GitHub Pages URLでバブルマップを閲覧できます。

## 銘柄変更
`config/universe.csv` の行を追加・削除するだけです。

列：
- ticker: 表示用ティッカー
- company: 社名
- sector: X軸・色分け用セクター
- theme: Hover表示
- benchmark: 超過リターン比較先ETF
- provider_ticker: データ取得用ティッカー（例：BRK.B → BRK-B）

## API上限対策
- 取得済み履歴は `data/prices.parquet` に保存
- 毎回、最新保存日から10日だけ重ねて再取得
- FMPは期間内の複数日を1銘柄1回で取得
- `max_requests_per_run` を超える前に停止
- HTTP 429時は処理を中断し、古い正常データを残す
- 時価総額メタデータは7日ごとに更新

## ローカル実行
```bash
pip install -r requirements.txt
python update.py
python -m http.server 8000 -d docs
```
ブラウザで `http://localhost:8000` を開きます。

## 注意
`docs/snapshot.json` は最初、画面確認用のSAMPLEデータです。最初の `python update.py` またはGitHub Actions実行で実データに置き換わります。

## 現在のUniverse
直近の銘柄選定を反映し、52銘柄ベースにAMAT・INTCを追加した54銘柄で初期化しています。必要なら `config/universe.csv` をそのまま編集してください。
