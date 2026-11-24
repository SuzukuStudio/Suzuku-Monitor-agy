# Suzuku-Monitor-agy

Google Antigravity (Antigravity CLI を除く) 用のリアルタイム・コンテキスト & プロンプトキャッシュ監視プラグインです。**モデルの利用枠やコンテキストを消費せず（デフォルト 0 トークン）**、ローカルの会話ログから実測トークン数とキャッシュ状況を読み取ります。

> **免責事項**: 本プラグインは個人制作の非公式サンプルです。Antigravity の内部ファイル仕様変更により動作しなくなる可能性があります。また、このプラグインの使用によりいかなる問題が生じた場合でも、当方 (ShoutLiberte 及び SuzukuStudio チーム) ではその責任を負いかねます。

## 対応環境

 Linux, Windows, macOS の Antigravity (Antigravity CLI を除く)

### 動作確認済み環境

- Linux (CachyOS)
- Antigravity App ( 旧名称: Antigravity 2.0 ) 、Antigravity IDE及びVSCode拡張機能版Antigravity

## 主な機能

1. **サイドパネル / フルページ UI（MCP App）**
   - 現在のコンテキスト量・キャッシュヒット率・同モデル増分・セッション累計を表示
   - **Orchestrator 対応**：Planner / Executor など複数モデルが混在する会話でモデルごとに独立したキャッシュ鮮度・経過タイマーを追跡し、モデル切替やターン境界の再構築を Cache Miss と誤認しません
   - **Compaction 検知**：`CHECKPOINT`（`/compact` 等）を検知し、圧縮前後のトークン目安と再構築待ち状態を表示
   - **セッション表示名同期**：Antigravity 上の自動設定・手動編集タイトルを反映
   - **自動ライブ更新**：2秒間隔（設定変更可）のポーリング＋1秒ごとの放置タイマー更新
   - 推移グラフ（Cache Hit / Uncached / Cache Miss / Compaction / Model Switch）

2. **クロスプラットフォーム デスクトップ通知**
   - Cache Miss 検知、しきい値超過（注意 / Compact 推奨）、**キャッシュ切れ約1分前アラート**、放置後の再送信時に通知します(値は任意で調整可能)
   - 対応環境：Linux（`notify-send`）、macOS（`osascript`）、Windows（PowerShell Toast）

3. **スマホ Remote Control 連携（Webhook Push）**
   - パネルの「設定」に **ntfy.sh**（例: `https://ntfy.sh/<任意の秘密トピック>`）・**Discord**・**Slack**・**汎用 JSON Webhook** の URL を入力すると、スマホにも同じアラートをプッシュ通知します
   - `ntfy.sh` の通知はタップで Remote Control (`https://antigravity.google.com`) を開けるリンク付きです
   - 必要に応じて「Remote Control 画面向けチャット緊急警告」も ON にできます（デフォルト OFF）

## データの参照元と保存先

- 読み取り元（すべて読み取り専用）：
  - `~/.gemini/antigravity/brain/<conversationId>/.system_generated/logs/transcript.jsonl`
  - `~/.gemini/antigravity/conversation_summaries.db`（SQLite `mode=ro`）
  - `~/.gemini/antigravity/annotations/<conversationId>.pbtxt`
- 設定と状態の保存先：`${PLUGIN_DATA}`（`settings.json` / `active-session.json` / `notify-state.json` / `hook-state.json`）

## ライセンス

- MIT License（`LICENSE` 参照）
- サードパーティ製バンドル依存パッケージの著作権表記：`THIRD_PARTY_NOTICES.txt` 参照
