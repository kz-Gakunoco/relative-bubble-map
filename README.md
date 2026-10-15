# Relative Bubble Map — 54銘柄 / 米国時価総額上位300社

米国株のバブルマップを、PCがOFFでも毎朝自動更新できる個人利用向けシステムです。

## できること
- 現在の54銘柄と米国時価総額上位300社を画面上で切り替え
- ティッカー・会社名検索、11セクターの絞り込み
- X軸：セクター
- Y軸：変動率 / 比較ETF超過リターン
- 期間：1D / 5D / 20D / 60D / 3M / 6M / 12M / YTD
- バブルサイズ：時価総額 / 均一
- バブルカラー：セクター / パフォーマンス
- クリックでTradingViewを開く
- API取得失敗時は既存データを維持
- PCがOFFでもGitHub Actionsで毎朝07:30 JSTに更新

## データソース
300社版はNasdaq Stock Screenerのcountry=United States、正の時価総額を持つ普通株候補を時価総額降順に並べ、株式クラスをまとめた上位300社です。ETF・ADR・優先株・ワラント等を除外します。GOOGL、BRK.Bなどは代表クラスを明示します。ランキングはNasdaq配信時点の値で、厳密なリアルタイム順位ではありません。選定日時・出典は画面とsnapshotのuniverseに記録します。

比較ETFは既存54銘柄の手動設定を優先し、それ以外はNasdaqのセクター・業種を11セクターETFに対応させます（半導体はSOXX）。全銘柄に厳密な業種ETFを割り当てる分類ではありません。

300社版は独立した`Update Top 300`で毎朝07:50 JSTに取得し、価格を`data/top300/`、公開データを`docs/snapshot_top300.json`に保存します。価格はYahooから25銘柄ずつ取得し、失敗時は最大3回試します。更新失敗時は前回の公開データを維持し、Actionsとstatusを失敗にします。54銘柄版の更新とは独立です。300社の取得元を変更する場合は`update_top300.py`を変更します。

画面の加重平均線は選択した全54/300銘柄を対象とし、検索・セクター絞り込みでは変化しません。300社版は混雑を避けるためティッカーラベルを初期状態で非表示にします。上場直後の銘柄などは長期リターン・Zスコアが欠損する場合があり、画面に表示します。

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


