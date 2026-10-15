# ShiftPatch

スタッフのシフト、メニュー別の作業時間、在庫、注文を組み合わせて、カフェの当日の販売方針を調整するサンプルアプリです。利用者ごとに **喫茶 こもれび** のサンプル店舗を作成します。管理画面は日本語です。

**Google Cloud プロジェクトがなくても動作します。** 初期設定の `calculation_only` では、固定の比較案をシミュレーションし、店長が選択します。Gemini モードでは、モデルが必要なデータを調べ、計算結果を見ながら候補を修正します。認証やモデルの設定がない状態を、AI の実行成功として表示することはありません。

最初の画面で **サンプル店舗を体験** を押してください。ゲストの変更内容は同じブラウザーで引き継がれます。Firebase を設定して Google ログインを有効にすると、別の端末からもアカウントのサンプル店舗を開けます。公開に必要な設定は [設定・デプロイガイド](docs/USER_SETUP.md) を参照してください。

## ローカルですぐに起動する

すでに依存パッケージをインストール済みの場合は、**README.md があるプロジェクトのルート**で次のコマンドを実行します。仮想環境を手動で有効化する必要はありません。

```bash
python3 scripts/dev.py
```

API と Web の起動完了が表示されたら、[http://127.0.0.1:5173/](http://127.0.0.1:5173/) を開きます。終了するときは、実行中のターミナルで **Ctrl+C** を押します。すでにサーバーが動作している場合は、そのサーバーを利用してください。

`.env` が Gemini モードの場合は、その認証設定で起動します。モデルを呼び出さずに操作を試したい場合は、`.env` の `SHIFTPATCH_AI_MODE=calculation_only` に変更してサーバーを再起動してください。初めてセットアップする場合は、次の手順から進めます。

## 開発環境のセットアップ

Python 3.11 以上、Node.js 22、npm が必要です。ローカルのサンプル体験には、Docker や Google ログインの設定は必要ありません。

### 1. 初回インストール

macOS / Linux では、次の順に実行します。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.lock -e './backend[dev,gemini,cloud]'
npm ci --prefix apps/web
```

`uv` を利用する場合は、Python 関連の 2 行を次のコマンドで置き換えられます。フロントエンドの `npm ci --prefix apps/web` も実行してください。

```bash
uv venv --python python3 .venv
uv pip install --python .venv/bin/python -r backend/requirements.lock -e './backend[dev,gemini,cloud]'
```

Windows の PowerShell では、Python のコマンドを次のように実行します。

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.lock -e './backend[dev,gemini,cloud]'
npm.cmd ci --prefix apps/web
```

以降の `python3` / `.venv/bin/python` は、Windows では `.\.venv\Scripts\python.exe` に、`npm` は `npm.cmd` に置き換えます。仮想環境の実行ファイルを直接使うため、PowerShell の有効化スクリプトを実行する必要はありません。

### 2. 環境変数を設定する

`.env` がない場合は、`.env.example` をコピーします。**既存の `.env` は上書きしないでください。** ファイルがなくても、初期設定の計算モードで起動できます。

macOS / Linux:

```bash
if [ ! -f .env ]; then cp .env.example .env; fi
```

Windows PowerShell:

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

初期設定は次のとおりです。

```dotenv
SHIFTPATCH_ENV=development
SHIFTPATCH_STORAGE=sqlite
SHIFTPATCH_AI_MODE=calculation_only
```

Gemini を使用する場合は、後述の [Gemini の接続と実モデル検証](#gemini-の接続と実モデル検証) に従って設定します。`.env` は Git の管理対象から除外しています。

### 3. 開発サーバーを起動する

macOS / Linux:

```bash
python3 scripts/dev.py
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe scripts/dev.py
```

このコマンドは、パッケージとポートの状態を確認し、FastAPI と Vite をまとめて起動します。両方のサーバーが応答すると、ターミナルに以下の URL と AI の設定が表示されます。起動メッセージの一部は韓国語です。

```text
http://127.0.0.1:5173  |  API: http://127.0.0.1:8000/docs
AI: calculation_only  |  Ctrl+C to stop
```

ターミナルは開いたままにします。`Ctrl+C` で API と Web の両方を終了できます。

### 4. ブラウザーで確認する

| 画面 | URL | 操作 |
| --- | --- | --- |
| 体験入口・店舗管理 | [http://127.0.0.1:5173/](http://127.0.0.1:5173/) | **サンプル店舗を体験** を押して、自分の店舗を開く |
| サンプル注文 | [http://127.0.0.1:5173/demo/order](http://127.0.0.1:5173/demo/order) | 同じブラウザーで開き、自分の店舗に注文を入れる |
| API ドキュメント | [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) | リクエストとレスポンスの形式を確認する |
| API ヘルスチェック | [http://127.0.0.1:8000/api/health](http://127.0.0.1:8000/api/health) | API サーバーの稼働を確認する |

注文画面は、管理画面の **サンプル注文画面** リンクからも開けます。同じブラウザーのタブでは同じ店舗を共有します。別のブラウザーやシークレットウィンドウでは、別の店舗が作成されます。再接続時も `127.0.0.1` を使用してください。`localhost` に切り替えると Cookie が変わり、別の体験として扱われる場合があります。

これらの URL には、サーバーを起動したコンピューターからアクセスします。チームメンバーがローカルで試す場合は、それぞれのコンピューターでセットアップしてください。共通の公開 URL を提供する手順は [Cloud Run デプロイガイド](docs/USER_SETUP.md) にあります。

店舗 API にはセッションと自分の店舗 ID が必要です。まず同じホストの体験入口からサンプル店舗を開き、`/api/session` の `store.id` を確認します。変更リクエストには `Idempotency-Key` ヘッダーも必要です。詳しくは [API 仕様](docs/API_CONTRACT.md) を参照してください。

### セットアップ・接続で困ったとき

- **`pip` の更新通知:** パッケージのインストールに成功していれば、そのまま起動できます。
- **`ENOTEMPTY` や `lucide-react` のフォルダー整理エラー:** 開発サーバーと別の npm インストールを終了してから再インストールしてください。macOS / Linux では、既存のパッケージフォルダーを一時ディレクトリーに退避できます。

```bash
shiftpatch_backup_dir=$(mktemp -d)
mv apps/web/node_modules "$shiftpatch_backup_dir/shiftpatch-node_modules"
npm ci --prefix apps/web
```

`node_modules` がない場合は、退避の 2 行を省略します。ソースとローカル DB はこの操作で変更されません。インストールが完了してからサーバーを起動してください。

- **フロントエンドのパッケージ不足・`vite: command not found`:** プロジェクトのルートで `npm ci --prefix apps/web` を完了させます。
- **8000 / 5173 番ポートを使用中:** 既存の ShiftPatch のターミナルを確認します。起動済みのサーバーを使うか、そのターミナルで `Ctrl+C` を押して終了してから再起動します。
- **ブラウザーから接続できない:** 起動完了の表示と [API ヘルスチェック](http://127.0.0.1:8000/api/health) を確認します。サーバーが終了している場合は `python3 scripts/dev.py` を再実行します。
- **Google ログインボタンが無効:** Firebase の設定前はゲストで体験できます。設定方法は [設定ガイド](docs/USER_SETUP.md) を参照してください。
- **`AI: calculation_only` と表示される:** 初期設定の計算モードです。Gemini を使う場合は `.env` の接続設定を変更して再起動します。
- **エディターに `Cannot find module app.settings` と表示される:** プロジェクトのルートを開いてください。`pyrefly.toml` が `backend` / `scripts` の検索パスと `.venv` の Python を設定します。依存パッケージをインストール済みか確認し、古い診断が残る場合はファイルを開き直してください。

### サーバーを個別に起動する場合

次のコマンドをそれぞれ別のターミナルで実行します。以下の Python のパスは macOS / Linux 向けです。Windows では `.\.venv\Scripts\python.exe` に、`npm` は `npm.cmd` に置き換えてください。

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```bash
npm --prefix apps/web run dev -- --port 5173 --strictPort
```

API サーバーもプロジェクトのルートにある `.env` を読みます。すでにプロセスに設定された環境変数が優先されます。

ローカルデータは、初期設定では `backend/data/shiftpatch.db` に保存され、サーバーを再起動しても残ります。リセットは現在の利用者の店舗だけを初期状態に戻します。すべての店舗データは合成データです。

## 利用できる機能

| 画面 | 機能 |
| --- | --- |
| スタッフ・シフト | 当日・翌日の確定シフト、欠勤、メニュー別の作業時間、勤務可能日の編集 |
| 仕込み・在庫 | 需要・人員能力・材料量の計算、仕込み・発注候補、実在庫の補正、入荷確定 |
| 営業管理 | 注文と作業の状態、担当者の割り当て、候補比較、店長承認、販売制限と見直し時刻の管理 |
| 振り返り・翌日 | 完了注文の売上・販売数、予測と実績、未完了注文と予約材料、販売記録の修正、翌日の準備 |
| サンプル注文 | 6 メニューの注文受付、在庫・人員・残り販売枠・営業時間の確認 |
| エージェント | 営業前・営業中・営業後の分析、ツール呼び出し、計算結果を踏まえた候補修正、根拠と実行記録 |

注文受付と材料予約は、同じ SQLite トランザクションに保存されます。同じリクエストを再送しても注文は重複しません。作業開始時にはスタッフと作業台の重複を確認し、完了時に予約材料を一度だけ消費します。提案後に条件が変わった場合は再分析が必要です。販売制限は見直し時刻を過ぎても継続し、店長が解除します。

## デモの試し方

1. サンプル店舗をリセットします。初期の営業時計は **2026-10-11 11:00 JST**、営業時間は **11:00〜13:00** です。スタッフ 4 人、メニュー 6 種、材料 8 種、作業台 2 台、既存注文 3 件が入っています。
2. シフト画面で欠勤を登録するか、在庫画面で実在庫を変更します。
3. 営業画面で分析・計算を実行し、候補ごとの完了数・待ち時間・売上を比較します。計算モードでは、AI の推薦を付けずにシミュレーション結果を表示します。
4. 候補を承認した後、サンプル注文画面から新しい注文を入れます。承認した販売制限と材料予約が反映されます。
5. 注文の作業を開始し、**+5分** などで営業時計を進めてから完了させます。初期の手動時計は実時間では進みません。作業時間が経過する前に完了できず、時刻を進めるだけで作業が自動完了することもありません。
6. 開始済みの作業をすべて完了させてから営業を締めます。未着手の注文が残る場合は、その注文と予約材料を記録したまま締めます。早期終了や販売制限があった日は、販売機会が制限された実績として扱います。
7. 翌日のシフトを修正し、翌日の準備案を分析します。発注数量は提案であり、取引先への自動発注は行いません。

## エージェントの構成

**1 つのメイン Gemini エージェント**が、業務ツールと計算モジュールを呼び出します。照会・予測・シミュレーションごとに別の LLM を起動する構成ではありません。

Gemini モードでは、モデルが必要な照会を選び、販売上限の候補を作り、サーバーの計算結果を受け取って候補を比較・修正します。最終提案は、保存済みの計算結果 ID を参照し、サーバーが検証します。販売設定を直接変更するツールはモデルに提供しません。

`calculation_only` では、サーバーが決めた 3 つの比較案を計算します。実行記録の出所を `server` として残し、AI の推薦は空にします。この経路は自律エージェントの実行としては扱いません。詳細は [エージェント実装ガイド](docs/AGENT_IMPLEMENTATION.md) を参照してください。

## Gemini の接続と実モデル検証

### GCP Vertex AI を使う場合

GCP プロジェクトを利用する場合は、**Vertex AI + ADC 認証**を設定します。プロジェクトのルートにある `.env` を次のように編集してください。

```dotenv
SHIFTPATCH_AI_MODE=gemini
SHIFTPATCH_GEMINI_PROVIDER=vertex
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=global
SHIFTPATCH_GEMINI_MODEL=gemini-3.8-flash
SHIFTPATCH_AI_TIMEOUT_SECONDS=120
SHIFTPATCH_GEMINI_REQUEST_TIMEOUT_SECONDS=60
```

`your-project-id` は、表示名ではなく実際のプロジェクト ID に置き換えます。Vertex AI API の有効化、利用アカウントの `roles/aiplatform.user` 権限、プロジェクトの課金設定が必要です。クレジットの適用条件は、利用中の課金アカウントで確認してください。[Google Cloud の接続ガイド](https://cloud.google.com/vertex-ai/generative-ai/docs/start/quickstart)

ローカルでは、プロジェクトにアクセスできるアカウントで認証してから、短い応答を確認します。

```bash
gcloud auth application-default login
python3 scripts/check_gemini.py
```

### Developer API キーを使う場合

Developer API キーで接続する場合は、`.env` を次のように設定します。

```dotenv
SHIFTPATCH_AI_MODE=gemini
SHIFTPATCH_GEMINI_PROVIDER=developer
GEMINI_API_KEY=your-api-key
SHIFTPATCH_GEMINI_MODEL=gemini-3.8-flash
```

接続確認スクリプトとアプリは、同じ `.env` の provider・モデル・認証設定を使用します。接続確認スクリプトは、プロジェクトの `.venv` があれば自動的にその Python で実行します。`auto` はプロジェクトが設定されていれば Vertex を優先するため、Developer API キーを使う場合は `developer` を明示してください。

公式 API では endpoint を省略します。ネイティブ API 対応のプロキシを使う場合は、`SHIFTPATCH_GEMINI_ENDPOINT` にベース URL を設定します。OpenAI 互換 URL やモデルの完全な URL は指定できません。[SDK の設定](https://googleapis.github.io/python-genai/)

### アプリで分析する

`.env` を変更した後は、開発サーバーを `Ctrl+C` で停止し、`python3 scripts/dev.py` で再起動します。画面上部の **AI運営アシスタント → AIで分析する** から実行できます。

分析時間は `SHIFTPATCH_AI_TIMEOUT_SECONDS` で 10〜180 秒に設定できます。未指定時は 40 秒で、実モデルの複数段階の分析には 120 秒を推奨します。API の実行期限、モデル待機、結果保存は同じ時間枠で管理します。モデル呼び出し 6 回、ツール実行 12 回、候補 5 件の上限は、時間枠を延ばしても変わりません。

各モデルリクエストの待機時間は `SHIFTPATCH_GEMINI_REQUEST_TIMEOUT_SECONDS` で 5〜120 秒に設定できます。初期値は 60 秒です。Vertex AI の HTTP 504 は、応答がリクエストの期限内に完了しなかったことを示します。認証が成功していても発生し、画面には接続・権限エラーと区別して表示します。リクエストの待機時間を延ばしても、分析全体の上限は維持されます。

営業前・営業中・営業後の状態に応じて分析します。営業中の自動分析を有効にすると、欠勤、実在庫の変更、入荷差異、注文数・待ち時間のしきい値、見直し時刻などをきっかけに再分析します。自動リクエストは管理画面を開いている間に動作します。

調査・計算・説明・検証の進行と、実行したツールを表示します。質問への回答や業務データの修正後は、新しいスナップショットで分析を続けられます。検証済みの候補は店長の承認後に反映します。続行に失敗しても入力済みの回答を保持し、失敗・期限切れの結果から再分析できます。

### エージェントの実モデル検証

開発サーバーとは別に実行します。現在の店舗 DB を変更せず、毎回独立した合成店舗を使います。以下の `.venv/bin/python` は macOS / Linux 向けです。Windows では `.\.venv\Scripts\python.exe` に置き換えます。

```bash
# まず正常ケース 1 件で、実接続と結果の契約を確認する
.venv/bin/python scripts/verify_gemini.py --scenario normal

# 全 6 ケース：正常・欠勤・パン不足・既存注文のリスク・営業前・営業後
.venv/bin/python scripts/verify_gemini.py
```

`test-results/gemini-live/summary.json` と各ケースの JSON に、実際のモデル ID、ツール引数、結果 ID、親候補と修正候補、所要時間、トークン使用量を記録します。認証情報やモデルの内部思考は保存しません。

全件検証では 6 件の独立した分析を実モデルにリクエストします。結果契約、状況に合った照会、異なる調査経路、**先行する計算結果を参照した候補修正**がすべて確認できた場合に終了コード 0 になります。修正を観測できない場合や実行上限に達した場合は未通過として記録します。`--rounds 3` で繰り返し評価できます。

接続確認、エージェント検証、開発サーバー、直接起動した uvicorn は、いずれも `.env` を読みます。既存のプロセス環境変数が優先されます。エージェント検証の終了コードは、設定エラーが 2、実行・行動基準の未通過が 1 です。API キーはサーバーの `.env` に保存し、ブラウザーやリポジトリーには含めません。詳細は [実モデル検証ガイド](docs/GEMINI_VALIDATION.md) を参照してください。

## ローカルの自動テスト

プロジェクトのルートで実行します。次の回帰テスト・ビルド・シミュレーション評価では、Gemini を実際には呼び出しません。

```bash
# バックエンドの回帰テスト
.venv/bin/python -m pytest backend/tests -q

# フロントエンドの回帰テスト
npm --prefix apps/web test

# TypeScript の検査と本番用ビルド
npm --prefix apps/web run build

# 合成シナリオのシミュレーション評価
python3 scripts/evaluate.py
```

Windows では、Python のコマンドをそれぞれ `.\.venv\Scripts\python.exe -m pytest backend/tests -q` と `.\.venv\Scripts\python.exe scripts/evaluate.py` に置き換え、`npm` は `npm.cmd` を使ってください。

バックエンドでは、最後の材料への同時注文、重複防止、在庫不足、スタッフ・作業台の競合、古い提案の承認拒否、制限の継続、営業締めの記録、シミュレーションの再現性、モデルのツール選択・候補修正・参照検証を確認します。エージェントの回帰テストにはスクリプト化した応答と実 SDK の型を使用し、実モデルの品質検証は別に行います。

クライアントでは、応答の消失、サーバーエラー、ページの再読み込み後も、同じ変更リクエストの重複防止キーが維持されることを確認します。正常応答の後に新しい注文を送る場合は、新しいキーを使います。

評価スクリプトでは **12 状況 × 5 シード × 3 固定方針 × 作業時間 3 水準 = 540 回**を比較します。再現性、入力の不変性、スタッフ・作業台の重複、在庫・勤務制約を検証し、`test-results/evaluation/` に JSON・CSV・Markdown のレポートを作成します。作業時間は基準の 0.7・1.0・1.3 倍です。この評価は実際の売上改善や Gemini の判断品質を立証するものではありません。

## コード構成

```text
apps/web/src/             React・TypeScript・TanStack Query の画面
backend/app/main.py       FastAPI・入力検証・提案の実行と保存
backend/app/operations.py 注文・在庫・スタッフ・承認・営業締め
backend/app/repository.py SQLite の永続化とトランザクション
backend/app/auth.py       ゲスト・Firebase 認証・セッション・店舗所有権
backend/app/cloud_repository.py Firestore・セッション・使用量・TTL
backend/app/web_host.py   Cloud Run 上で Web と API を配信
backend/app/simulation/   スタッフ・作業台・予約材料を反映した計算
backend/app/planning/     過去の販売記録に基づく予測・準備量
backend/app/agent/        Gemini SDK・ツール・実行ループ・結果検証
backend/app/settings.py  共通の .env 読み込み・Gemini 接続設定
backend/tests/           計算・API・エージェントの回帰テスト
scripts/dev.py           ローカルの API・Web 一括起動
scripts/check_gemini.py  アプリと同じ設定で接続を確認
scripts/verify_gemini.py 合成シナリオの実モデル検証
scripts/evaluate.py      合成シナリオ・感度分析レポート
scripts/deploy.py        Cloud Build・Firestore Rules/TTL・Cloud Run
pyrefly.toml             Python エディターの検索パスと仮想環境
docs/                    実装・API・設定ガイド
ARCHITECTURE.md           アーキテクチャとハッカソン向け設計
```

## ローカル実装とクラウド公開の検証状況

ゲストセッション、利用者別のサンプル店舗、任意の Firebase Google ログイン、Firestore アダプター、Cloud Run コンテナー、デプロイスクリプトを実装しています。クラウドでは Web と API を 1 つの Cloud Run サービスで提供します。ローカルは SQLite を使い、公開環境では Firestore と許可 Origin の設定が必要です。API が店舗の所有権を確認し、ブラウザーから Firestore への直接アクセスは deny-all ルールで制限します。

GCP Vertex AI の実モデル呼び出しを確認済みです。2026-10-15 に、正常・欠勤・在庫不足・既存注文のリスク・営業前・営業後の **合成 6 ケースすべてで結果契約を検証**し、観測結果を参照した候補修正も確認しました。実際の Google ログイン、リモート Firestore、Cloud Run 公開には、別途設定と実環境での検証が必要です。ハッカソンの「Cloud Run 上での実行」と「Google Cloud AI の実利用」のうち、公開デプロイについては実行記録を確保する必要があります。[設定・デプロイガイド](docs/USER_SETUP.md)、[アーキテクチャ](ARCHITECTURE.md)、[API 仕様](docs/API_CONTRACT.md) を参照してください。

需要は、通常営業日の直近の販売平均と時間帯別の記録から計算します。時間帯ごとの標本が不足している場合は、均等配分したことを明示します。在庫はロットごとの有効期限・予約量と FIFO 予約を扱い、スタッフのメニュー別作業時間には登録値を使います。実 POS、決済、自動発注、月間シフトの自動生成は今後の範囲です。売上と待ち時間は、これらの前提に基づく比較値です。
