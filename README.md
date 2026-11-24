# Suzuku-Monitor-agy (Antigravity Plugin)

Google Antigravity (Antigravity CLIを除く) 向けのリアルタイム・コンテキスト使用量 & プロンプトキャッシュ監視プラグインです。
**モデルの利用枠やコンテキストを消費せず（デフォルト 0 トークン）**、ローカルに保存されている会話ログから実測トークン数とキャッシュ鮮度を読み取って可視化・通知します。

> **注意**: 本プラグインは個人による**非公式のサンプルプラグイン**です。Antigravity がローカルに出力する内部ファイル（`transcript.jsonl` / `conversation_summaries.db` / `annotations/*.pbtxt`）を**読み取り専用**で参照しているため、将来の Antigravity アップデートによる内部仕様変更で動作しなくなる場合があります。あらかじめご了承ください。

---

## 主な機能

1. **サイドパネル / フルページ UI（MCP App）**
   - 現在のコンテキスト使用量、キャッシュヒット（Cache Hit） / 新規入力（Uncached）、同モデル増分、セッション累計を表示
   - **Orchestrator 対応**：複数モデルが混在する会話でモデルごとに独立したキャッシュ鮮度・経過タイマーを追跡し、モデル切替やターン境界の再構築を Cache Miss と誤認しません（トップの放置タイマーは Planner 優先。/swarmは**未検証**）
   - **Compaction 検知**：`CHECKPOINT`（`/compact` 等）を検知し、圧縮前後のサイズと再構築待ち状態を正確に表示
   - **セッション表示名同期**：Antigravity 側の自動タイトル・手動リネーム名をリアルタイムで反映
   - **自動ライブ更新**：2秒間隔（設定変更可）のポーリング＋1秒ごとの放置タイマー更新
   - **推移グラフ**：直近 30 回のモデル呼び出しについて Cache Hit / Uncached / Cache Miss / Compaction / Model Switch を可視化

2. **クロスプラットフォーム・デスクトップ通知**
   - Cache Miss 検知、しきい値超過（注意 / Compact 推奨。任意で値を変更可能）、**キャッシュ切れ約1分前アラート**、放置後の再送信時に通知
   - **Linux**: `notify-send`
   - **macOS**: `osascript`（標準通知センター）
   - **Windows**: PowerShell 経由の Windows Toast 通知

3. **スマホ Remote Control 連携（Webhook Push）** (この機能は未検証です。何かあったらIssueやPRで報告していただけると助かります！)
   - パネル内の「設定」に **ntfy.sh**（例: `https://ntfy.sh/<任意の秘密トピック>`）・**Discord**・**Slack**・**汎用 JSON Webhook** の URL を入力すると、スマホにも同じアラートをプッシュ通知できます
   - `ntfy.sh` 通知はタップで Remote Control（`https://antigravity.google.com`）を開けるリンク付き
   - 必要に応じて「Remote Control 画面向けチャット緊急警告」も ON にできます（デフォルト OFF）

---

## 動作要件

- **Antigravity**: Antigravity App (旧名称 Antigravity 2.0)、Antigravity IDE、またはVSCode拡張機能版Antigravity
- **Python 3.10 以上**（`python3` コマンドとして実行可能であること。標準ライブラリのみ使用・追加パッケージ不要）

- **Linux デスクトップ通知を使う場合**: `notify-send`（`libnotify`）

---

## インストール方法

### 方法 1: GitHub Releases から `.zip` をダウンロードして導入（推奨）

1. Releases ページから最新版の `suzuku-monitor-agy-v1.x.x.zip` をダウンロードします。
2. Antigravity のプラグインディレクトリに展開し、`suzuku-monitor/plugin.json` が直下に配置されるようにします。

   - **Linux / macOS**:

     ```bash
     mkdir -p ~/.gemini/config/plugins
     unzip suzuku-monitor-agy-v1.x.x.zip -d ~/.gemini/config/plugins/
     ```

     配置後の構成例: `~/.gemini/config/plugins/suzuku-monitor/plugin.json`

   - **Windows (PowerShell)**:

     ```powershell
     New-Item -ItemType Directory -Force "$env:USERPROFILE\.gemini\config\plugins" | Out-Null
     Expand-Archive -Path .\suzuku-monitor-agy-v1.x.x.zip -DestinationPath "$env:USERPROFILE\.gemini\config\plugins\" -Force
     ```

     配置後の構成例: `%USERPROFILE%\.gemini\config\plugins\suzuku-monitor\plugin.json`
3. すでに Antigravity を開いている場合は、プラグイン設定画面または MCP Servers 設定から再読み込みしてください（新規追加は自動検出されます）。

> **ヒント**: `agy` CLI が導入されている環境では、ローカルの `.zip` または展開済みディレクトリに対して `agy plugin validate --strict` で事前チェックもできます。

### 方法 2: リポジトリをクローンして導入

```bash
git clone <このリポジトリのURL>
cd Suzuku-Monitor-agy
mkdir -p ~/.gemini/config/plugins/suzuku-monitor
cp -R plugin/. ~/.gemini/config/plugins/suzuku-monitor/
chmod +x ~/.gemini/config/plugins/suzuku-monitor/scripts/*.py
```

または `agy` CLI を使う場合:

```bash
agy plugin install ./plugin
```

### Windows で `python3` が見つからない場合

環境によってはコマンド名が `python3` ではなく `python` のみの場合があります。MCP Server や Hook が起動しないときは、配置先の以下 2 ファイル内の `"python3"` を `"python"` に書き換えてください。

- `mcp_config.json`（`"command": "python3"`）
- `hooks.json`（`"command": "python3 ./scripts/context_hook.py"`）

---

## 使い方

1. Antigravity のサイドパネル右上 **「+」メニュー**、または **左サイドバー** から **Suzuku Monitor** を開きます。
2. 上部のドロップダウンで **「自動追従 (アクティブ / 最新)」** を選ぶと、現在の会話に自動で追従します。過去の会話も一覧から選択できます。
3. 下部の **「設定 (しきい値 · デスクトップ通知 · スマホ連携)」** を開くと、注意 / Compact 推奨のトークン数、キャッシュ TTL 目安、更新間隔、デスクトップ通知の ON/OFF、Webhook URL 等を設定でき、**「テスト通知を送信」** ボタンで動作確認も行えます。

---

## プライバシーとセキュリティ

- **読み取り専用**: `~/.gemini/antigravity/brain/`、`conversation_summaries.db`、`annotations/` を読み取り専用（SQLite は `mode=ro`）で参照するのみで、Antigravity の会話ログや設定ファイルを書き換えることはありません。
- **保存されるデータ**: プラグイン独自の設定と通知重複防止用の状態のみが `${PLUGIN_DATA}`（通常 `~/.gemini/antigravity/plugin_data/suzuku-monitor/`）に保存されます。
- **外部通信なし（デフォルト）**: 設定で Webhook URL を明示的に入力しない限り、外部ネットワークへの通信は一切行いません。
- **トークン消費ゼロ（デフォルト）**: デフォルト状態では Hook は空レスポンス `{}` のみを返し、チャット履歴やコンテキストにテキストを一切注入しません（`Remote Control 画面向けチャット緊急警告` を ON にした場合のみ、危険時に短いシステムメッセージを注入します）。

---

## 開発・ビルド・テスト・パッケージ生成

フロントエンド（`ui-src/`）のみ `@modelcontextprotocol/ext-apps` と `esbuild` を使用して単一 HTML（`plugin/ui/app.html`）へバンドルしています。通常利用だけならビルド済み `plugin/ui/app.html` が同梱されているため Node.js は不要です。

```bash
# UI のビルド（TypeScript + CSS -> plugin/ui/app.html）
npm --prefix ui-src install
npm --prefix ui-src run build

# 自動テストの実行
python3 tests/test_plugin.py

# GitHub Releases 用の配布 zip 生成（dist/suzuku-monitor-agy-v<version>.zip）
python3 scripts/package_release.py

# マニフェスト・構成の厳格検証
agy plugin validate --strict ./plugin
agy plugin validate --strict ./dist/suzuku-monitor-agy-v1.x.x.zip
```

---

## 免責事項

- 本プラグインは個人制作の非公式サンプルであり、本プラグインの使用によるいかなる損害が生じた場合でも、当方 (ShoutLiberte 及び SuzukuStudio チーム) ではその責任を負いかねます。
- Antigravity 本体の内部ログ形式や仕様変更に伴い、数値の算出やセッション名の取得が予告なく動作しなくなる可能性があります。

---

## ライセンス

- 本リポジトリのコード: [MIT License](./LICENSE)（Copyright (c) 2026 ShoutLiberte and contributors）
- バンドルされたフロントエンド依存パッケージの著作権表記・ライセンス全文: [THIRD_PARTY_NOTICES.txt](./THIRD_PARTY_NOTICES.txt)
