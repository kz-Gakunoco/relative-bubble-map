# 3年履歴バックフィル追加パッチ

Yahoo Finance / yfinanceから、Universe全体 + 比較ETF + ES=F の日足を
**一度だけ過去3年分まとめて取得**する手動GitHub Actionです。

通常の毎朝更新フローは変更しません。

## 追加するファイル

- `backfill_3y.py`
- `.github/workflows/backfill_3y.yml`

## GitHubでの追加手順

### 1. backfill_3y.py
リポジトリ直下へアップロードしてください。

### 2. backfill_3y.yml
`.github` はWindowsで隠しフォルダ扱いされやすいので、
GitHub上で **Add file → Create new file** を使い、

`.github/workflows/backfill_3y.yml`

というファイル名で作成し、このZIP内の同名ファイルの内容を貼り付ける方法が確実です。

## 実行方法

GitHubの

**Actions → Backfill 3Y History → Run workflow**

を1回だけ実行してください。

処理内容：
1. 54銘柄 + 比較ETF + ES=F を過去3年取得
2. 既存 `data/prices.parquet` と結合
3. 重複日を除去
4. `update.py` を実行
5. ES乖離Zスコア等を含む `docs/snapshot.json` を再生成
6. GitHubへ自動Commit

## データ取得上の工夫

- 20銘柄ずつのバッチ取得
- Yahooがバッチ内の一部銘柄を落とした場合、その銘柄だけ単独再取得
- 既存データは削除せずマージ
- 失敗しても既存データを壊さない
- APIキー不要

## 補足

3年の日足なら、
- 20D × 過去252営業日のZスコア
- 60D / 3M × 過去504営業日のZスコア

を概ねカバーできます。

6M / 12M × 756営業日は、3年だと銘柄や上場時期によって基準観測数が不足する場合があります。
まずは20Dをメイン用途として使う前提なら、3年バックフィルで十分です。
