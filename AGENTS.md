<!-- BEGIN:obsidian-hub -->
## Obsidian Vault 連携（ハブ運用）

このリポジトリの「進捗・意思決定ログ」は Obsidian Vault に集約する。

**ハブノート: `~/notes/Dev/repitte-hotel-notifier.md`**

- **作業開始時**: ハブノートを読み、前回までの状況と未完了TODOを把握してから着手する
- **区切りが付いたとき**（機能実装完了・バグ修正完了・方針決定・調査結果が出た等）: ハブノートの `## 作業ログ` に追記する
  - `### YYYY-MM-DD` 見出しの下に「やったこと / 決めたこと（なぜ） / 次にやること」を箇条書きで
  - `## 現在の状況` と `## TODO` も実態に合わせて更新する
  - 会社名・案件名・商品名・関連リポジトリ名は `[[名前]]` のリンク記法にする（Vault内の `Projects/` と繋がる）
- **詳細な仕様書・設計書はこのリポジトリの `docs/` に置く**（Gitで版管理する）。ハブノートには要約とパスだけ書く
- 些細な変更（typo修正・依存更新・フォーマットのみ等）では追記しない。ログを薄めない
- ハブノートが無い場合は新規作成してよい（`~/notes/Dev/README.md` の一覧にも1行追加する）
- **認証情報・APIキーはハブノートに書かない**（Vault は iCloud と GitHub にバックアップされる）
- 顧客の個人情報（メール本文・署名欄の電話番号や住所）は **Vault にそのまま書いてよい。マスクしない**
  （2026-08-28 の本人判断。2026-08-27 版の「顧客個人情報は書かない」を上書きする）
- **健康情報は Vault に置かない。** 共有が必要なものは
  `~/Library/Mobile Documents/com~apple~CloudDocs/claude-shared/` 経由で各機の memory へ
- このブロックは `agent-config/repo-template/obsidian-hub.md` から配布される。**手で編集しない**

### 2台 × 2ツールで交互に触るための最低限

主機/サブ機 × Claude Code/Codex の**4通り**が順番にこのリポジトリを触る。

- **着手前に3つ確認する**
  1. `git fetch && git status -sb` — origin より遅れていたら pull してから始める
  2. `git log --oneline @{u}..HEAD` — 前のセッションの未pushが残っていないか。
     **push されていないコミットは、もう1台からは存在しないのと同じ**
  3. ハブノートの `## 現在の状況` — 何を、なぜ途中で止めたか
- **作業ログの見出しに「誰が」を入れる**（`主機CC` / `主機CX` / `サブCC` / `サブCX`）
  例: `### 2026-09-01 主機CC ── スプレッドシート同期の行作成`
- **未pushが効いてくるのは「もう1台に渡すとき」と「GitHub Actions で動かすとき」だけ。**
  同じ端末の別ツールとは同じファイルを見ているので push は要らない。
  ただし残したまま離れるなら、報告文に「未pushで N commit 残している」と書く
- **記憶ファイル（Claude Code 専用）は Codex から読めない。** ツールを跨いで効かせたいことはハブノートに書く
- **追記するまでが作業。** 完了報告の前にハブノートへ追記し、報告文にその旨を含める
<!-- END:obsidian-hub -->

## このリポジトリ

`#job_sales` の契約報告（「【契約獲得】」と「リピッテホテル」の両方を含む投稿）を `#repitte-hotel` へ転記し、
完了報告を元スレッドへ返す bot。本体は `monitor.py` 1本と `tests/`。
起動係は [[cnctor-onboarding]] の GAS（30分おきに `workflow_dispatch`）で、`schedule:` は4時間おきの保険。

ここは要点だけ。**詳細は下の表の場面が来たときに、`docs/` のそのファイルだけ読む**（全部を先に読まない）。

| こういうとき | 読むもの |
|---|---|
| state（`last_processed.txt`）・取得窓・引き直し・二重転記まわりを触る、`monitor.yml` の Save state を触る | `docs/state-design.md` |
| Actions のログを見て異常か判断する／`#repitte-hotel` に同じ報告が繰り返し並んだ | `docs/log-reading.md` |
| 定数や判定条件のテストを書く・直す | `docs/testing.md` |
| 転記文の目印・完了報告の文面を変える／cnctor-onboarding との受け渡し | `docs/downstream-report.md` |
| 実測値（実行間隔・流量）を引く、再測定した | `docs/state-design.md` の末尾「実測値」表。**コードやテストに数値を書かない** |

## 設計の前提（変えるなら理由を docs/ で読んでから）

- state に入れてよいのは「Slack が発行した ts」と「初回に決めた起点」だけ。こちらの時計から作った値を足さない
- 全部取れたと言い切れない回は 1 件も処理しない。部分取得を「取れた分」として返さず例外にする
- 本体の転記に失敗したら state を進めずに打ち切る。添付・リマインドの失敗は進めてスレッドに警告する（重複より欠落を選ぶ）
- 1 件転記するごとに、その場で state をファイルに書く。ループの外でまとめて書かない
- 読み取り側（`_read_state_raw`）は state を疑い、書き込み側（`advance_last_ts`）は処理した事実をそのまま残す。非対称は意図的
- いまは at-least-once。冪等化（元投稿の `channel + ts` を一意キーにする）は見送り中。経緯と設計案は `docs/state-design.md`、TODO はハブノート
- 完了報告は常に `#job_sales` の元スレッドへ。語尾は「!」。宛先が決まらない残タスクは行ごと出さない。
  「事業計画の反映」は cnctor-onboarding が同じスレッドへ出すので、こちらでは出さない

## 触ると壊れるもの

| 変えない | 何が起きるか |
|---|---|
| `INITIAL_LOOKBACK_SEC = 1800`（30分）を広げる | state を失った回に古い契約報告をまとめて転記する |
| `FUTURE_TS_TOLERANCE_SEC = 60` を広げる | 広げた幅ぶん、その間に届いた投稿が二度と取れない。値は `tests/test_state.py` で固定してある |
| `_read_state_raw()` の4つの検査（数値・有限で正・未来でない・90日以内）を緩める | 永久停止。緑のまま 0 件が続くか、赤で落ち続ける |
| `advance_last_ts` の「前より古い値では上書きしない」を外す | 転記済みの契約報告を丸ごと再投稿する |
| `upload_file` の失敗をログだけで戻す | 契約書が付かないまま誰にも気づかれず終わる |
| `conversations.history` を1ページで済ませる | 取得窓に51件以上あると、古い＝まだ転記していない方が二度と読まれない |
| `monitor.yml` の `concurrency`・`if: always()`・`git pull --rebase` を外す | 二重転記、または state が保存されず次回また二重転記 |
| `write_last_ts` / `write_repair_notice` の atomic 書き込みをやめる | 0 バイトの state が残り、リピッテチームへ重い確認依頼が飛ぶ |
| `monitor.yml` の `pending_repair_notice.json` の分岐を触る | pytest の対象外。`docs/state-design.md` の3ケースを手で通す |
