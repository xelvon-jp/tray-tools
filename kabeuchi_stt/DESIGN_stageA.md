# AI対話モード 段階A 設計（2026-11-22）

正本の仕様は `SPEC_dialogue_mode.md`、実測は `stage0/RESULTS.md`。ここは**実装の分担と境界**を決める文書。段階Aの範囲: 宛先の指定（クリック）＋セッション固定＋プリセット保存／選択＋配達の仕組み＋確定方式（ヘッドセット単押しトグル／窓のボタン／ホットキー）＋区切りごとの small 認識＋メイン画面の対話パネルでのバッファ表示＋未配達の保持。**送信（送信ボタン・Enter）は、2026-11-22 の陽太さんの要望で作る方針に変えた（`dialogue.send_after_deliver`、既定 OFF。読み戻しで入力を確認できたときだけ、1回だけ試す）。下の「送信」「仮認識と確定」「自動配達しない」を参照。**

## プロセス構成（COM と GC の教訓）
- `kabeuchi_stt` 本体（PySide6、kabeuchi-stt venv）は **comtypes を import しない**。UIA は別プロセスの**常駐ワーカー** `dialogue_worker.py` が担う。
- ワーカーは **tray-tools の venv の python**（`%USERPROFILE%\.venvs\tray-tools\Scripts\python.exe`。comtypes あり。設定 `dialogue.worker_python` で変更可）で、セッション開始時に1本起こし、終了時に止める。標準入出力で **JSON 1行 = 1リクエスト／1レスポンス**（`copilot_io_worker` と同流儀。stdout は応答 JSON 以外書かない、診断は stderr、UTF-8 バイト列で読み書き）。ワーカーは **PySide6・faster-whisper・numpy を import しない**、kabeuchi_stt 直下の `settings.py` `hotkeys.py` 等も import しない（同名衝突）。ワーカーが import してよいのは `dialogue_*.py`（Qt/COM 非依存の純部分）と標準ライブラリ・comtypes・ctypes のみ。
- **前面化ロック対策**: ヘッドセット／ホットキー入力を受けるのは本体プロセス。配達を頼む直前に、本体が `AllowSetForegroundWindow(ASFW_ANY)` を呼んでワーカーに前面化の権利を渡す（実測: ボタン受信はそのプロセスの直近の入力になり、前面化が通る。別プロセスのワーカーには権利を渡す必要がある）。窓のボタン・無入力のタイマー起動は別条件（前面化に失敗しうる）なので、失敗は「未配達」へ倒す。

## モジュール（kabeuchi_stt 直下、フラット）
| ファイル | 担当 | 備考 |
|---|---|---|
| `dialogue_profile.py` | 宛先プロファイル辞書（PHAL 互換: process_name / window_class / window_title_contains / 入力欄の手がかり / click_rel / window_size）、プリセットの保存・一覧・削除（`%LOCALAPPDATA%\kabeuchi_stt\dialogue_targets\*.json`。実パスをリポジトリに書かない）、純関数の入力欄選択 `choose_input`、読み戻し判定 `judge_readback`、テキスト等価 | 純関数。`stage0/s0_core.py` を土台に移植 |
| `dialogue_worker.py` | UIA・Win32 の実作業（常駐ワーカー） | ops: `ping` / `pick`（クリックを待って宛先プロファイルを返す）/ `resolve`（プロファイルから窓を探してハンドル握る）/ `deliver` / `cleanup` / `shutdown`。`stage0/s0_win.py` `s0_uia.py` を土台に移植 |
| `dialogue_deliver.py` | 配達の段階制御（純ロジック＋ワーカーとのやりとりの抽象） | 下記「配達の順」。ワーカーの `deliver` op から呼ぶ純関数を中心に、段階の選択・結果の整形をテスト可能に |
| `dialogue_client.py` | 本体側のワーカー管理（起動・停止・タイムアウト・再起動・`AllowSetForegroundWindow`） | Qt 非依存。`subprocess` と JSON 行 |
| `dialogue_segmenter.py` | 無音（既定1.2秒）で発話を区切る（音量しきい値は既存の `silence.py`／`mic_check` の dBFS 基準を再利用） | 純ロジック、合成波形でテスト |
| `dialogue_buffer.py` | メッセージ・バッファ、確定、**未配達キュー**（保持・再試行・コピー・破棄・古い順配達）、セッション履歴（時刻つき）の状態機械 | 純ロジック |
| `dialogue_session.py` | セッション（録りっぱなし）: マイク→区切り→small 認識→バッファ→確定→配達依頼。`session.py` の `Session` を土台にし、既存の壁打ち録音と同居（WAV も残す） | Qt 非依存 |
| `dialogue_headset.py` | ヘッドセットボタンの検出経路の併用（`dialogue.headset_mode`: auto/hid/hotkey）。①HID 直接読み ②`RegisterHotKey(VK_MEDIA_PLAY_PAUSE)` の独占（モード有効な間だけ）。どちらか一方でも成功すれば受付中・両方失敗で初めて警告。別経路の重複（0.5 秒以内）は 1 回に間引く。経路ごとの状態を診断へ | 本体プロセス内。`hotkeys.py` の流儀 |
| `dialogue_hid.py` | ヘッドセットの HID（VID/PID 一致・Consumer 0x0C の Play/Pause を持つ `MI_00&Col01` 相当）を SetupAPI で探し、共有・読み取り専用・overlapped で開いて専用スレッドで ReadFile。押下（`01 01`）を通知。純関数（レポート解釈・パス絞り込み・候補順位・caps 判定）はテスト可能 | ctypes のみ（comtypes 不使用）。書き込み系 API を使わない。`HidD_GetPreparsedData`/`HidP_GetCaps`/`HidP_GetButtonCaps`/`HidP_GetValueCaps` で caps を確認、確認できなければパス名の `col01` で保険 |
| `ui_dialogue.py` | 1列の画面（1行ヘッダー・下書き欄・未配達の行・下段・⚙メニュー・確定履歴ダイアログ）。2026-11-22 再設計 | `ui_main.py` への結線（1列画面の出し入れ） |
| `ui_draft.py` | 下書き欄 `DraftView`（確定済み＋発話ごとの波形／黄色を1つの `QTextEdit` に。ブロック単位の置き換え・入力の守り・「仮」の印の描画） | 純ロジックは `ui_dialogue_logic`（`build_tail` / `plan_tail`） |
| `dialogue_log.py` | 診断ログ（データ置き場の `dialogue.log`。発話の内容は書かない） | ヘッドセット・コントローラが書く |

## 配達の順（`deliver`、仕様どおり）
入力: `{profile, text, hwnd(任意), options}`。出力: `{status: "delivered"|"undelivered", stage, reason, readback, timings}`。
1. 宛先の窓をプロファイルで探す（握ったハンドルを優先。見つからない・複数曖昧なら止める＝別の窓へは送らない）。UIA の入力欄を毎回探し直す（自動化ID → 名前＋種類 → クラス名 → 保存位置付近。Chromium は `wake` 後）。値パターンの無い要素が当たったら、その矩形内の Edit/ComboBox/Document（値パターンあり）の最小のものへ降りる。
2. **UIA SetValue → 読み戻し**。書きかけがある場合は設定 `existing_text`（追記／上書き／中止、既定は追記）に従う。一致すれば delivered（stage=`uia`）。**Copilot 型は成功と返っても入らないので、読み戻しの一致だけを成功とする。**
3. 不一致なら **前面化**: UIA `SetFocus` → `GetForegroundWindow` の一致確認 →（最小化なら SW_RESTORE）→ だめなら `SetForegroundWindow`。**前面確認が取れるまでキー送信は一切しない**（`expected_root` を関数内で再検証する形にする）。入力中の他作業を奪わない設定（直近のキー入力が数秒以内なら待つ／通知のみ）。
4. 前面確認後、入力欄のフォーカスを確認 → **クリップボード貼り付け**（Ctrl+V）→ 読み戻し。クリップボードは**全形式を退避して復元**（画像等を失わない。できなければ貼り付けを諦めて Unicode 打鍵を検討、それも不可なら未配達）。
5. 前面化できないとき、点の下が宛先の窓であることを確認したうえで**本物のクリック**（実測で Copilot 成功。事前に宛先と一致しなければクリックしない）→ 前面確認 → Ctrl+V → 読み戻し。クリック後にマウス位置を戻す。
6. どれも不可: 中止して `undelivered`（理由つき）。タスクバーの点滅と画面通知で知らせる。**確定したテキストは捨てない。**
- 配達の前後で、前面窓の元の情報を記録（「元の窓へ戻す」は設定、既定は戻さない）。

## 認識（逐次と清書の間）
- 区切り: 無音 `dialogue.utterance_gap_sec`（既定1.2）で発話を確定 → small 認識（直前1〜2文を文脈、hotwords・置換辞書を適用）。結果はバッファ（コンパクト窓）に溜め、入力先へは書かない。
- メッセージの確定: ①ヘッドセット単押し／窓のボタン／ホットキー（段階Aで実装）。②無音での自動確定は**採用しない（2026-11-22 確定。末尾の「自動配達しない」）。以前の記述: 設定で選べる（既定 OFF）**。送信はしない。
- セッションは録りっぱなし（開始・終了は窓のボタン／ホットキー。ヘッドセットでは操作しない）。無音が続けばセッション自動終了（設定）。録音中を窓に常に表示。**保留**（取り込みを止める）あり。

## 確定の動作（ヘッドセット単押しトグル）
- 通常: 1回押す＝バッファを確定して配達（入力のみ）。未配達が溜まっているときの次の1回は「再試行」（新規の確定ではない）。
- ヘッドセットの検出はモード有効中だけ。HID 直接読み（読み取り専用）と RegisterHotKey の独占を併用し、取得失敗は画面で知らせる（両方失敗のときだけ警告。独占できず HID だけのときは「動画の再生/一時停止も同時に反応する場合があります」の弱い注意）。「受付中」を窓に出す。
- **経緯**: `RegisterHotKey` は登録に成功（code=0）しても WM_HOTKEY が 1 回も届かない状況が実機で起きた（独立の観測でも押下が見えない。原因は不明）。RegisterHotKey に頼らない経路として、HID の直接読みを併用する。Raw Input は実測でこの機器を拾えなかったので使わない。
- **副作用**: HID 直接読みは押下を横取りしないので、同じ押下が Windows にも届く（再生中のメディアが反応しうる）。ホットキーの独占が効いているときは抑えられる。

## 設定（`settings.json` の `dialogue` 節。既定値は `settings.py` の既定に追加。丸ごと書き戻さない）
`enabled`（既定 false）、`worker_python`、`utterance_gap_sec`(1.2)、`headset_button`(false)、`headset_mode`("auto"。auto/hid/hotkey)、`headset_vid_pid`("3511:2EF2")、`send_after_deliver`(false)、`wait_while_busy`(true)、`busy_wait_sec`(120)、`punctuate_on_commit`(true)、`provisional`(true)、`provisional_interval_sec`(2.0)、`commit_use_provisional`(true)、`provisional_wait_sec`(5.0)、`existing_text`("append")、`keep_idle_foreground`(false)、`return_focus`(false)、`copy_undelivered_to_clipboard`(false)、`quiet_input_sec`(3)、`last_target`。

## テストと検証
- 純ロジックは単体テスト（`kabeuchi_stt\tests\`、venv `%USERPROFILE%\.venvs\kabeuchi-stt` で `tests\run_all.py --fast` が通ること）。ワーカーの Win32/UIA 実動作は、実機でしか確かめられない部分は「未実施・実機確認待ち」と書く。
- **実機でのキー・マウス送信、窓の前面化、ワーカーの起動を、実装担当は行わない。** 受け入れの実機確認は陽太さんの立ち会いで、見えるコンソール／窓の案内つき。
- 版表示は `version.py` に従い更新（`20261122-NN` 形式）。ファイルは LF、UTF-8。公開リポジトリに入るため、実パス・個人データ・業務の話題をコードや例文に入れない（`tests` に見張りあり）。

## 画面（2026-11-22 再設計: 1列）
- **1列の小さな画面（幅 約400px）**。`ui_dialogue.DialoguePanel`（QWidget）は `ui_main.MainWindow` の `dialogue_box`（メイン列の `split` の下。通常は隠れている）に入る。［AI対話］（トレイの「AI対話モード…」）で `set_dialogue_view(True)`: タイムライン・右パネル・凡例・清書キューを隠し、`dialogue_box` を出す。幅は `win_geometry_dialogue`（別保存）、最小幅 360px。対話セッション中は壁打ちの画面へ戻れない。`dialogue.enabled` のときだけ作る（部品の組み立てだけで、録音・ワーカー・ヘッドセットは始めない）。
- 上段（録音中の表示・時間・メーター・マイクの指摘）はメイン窓のものをそのまま使う。パネルは: 1行ヘッダー（宛先コンボ・選び直す・送信までする・⚙）／警告（あるときだけ）／下書き欄／未配達の行（あるときだけ）／下段（確定して届ける・全部消す）／通知1行＋保留＋セッション。確定履歴は⚙メニューの別ダイアログ。
- **下書き欄（`ui_draft.DraftView`、1つの `QTextEdit`）**: 前から 確定済み（白。ブロックに属性なし）→ 発話 ID 順の「波形」（画像＋「変換待ち」。読み取り専用）／「黄色」（仮認識。斜体・オレンジ・左端に「仮」）。ブロックの種類・発話 ID は書式の属性（`PROP_KIND` / `PROP_UID` / `PROP_ENV`）。不変条件は「確定済みが前・波形／黄色が後ろ（連続）」で、波形への入力・確定済みとの境目をまたぐ編集は受け付けない。同じ発話が 波形→黄色→（白）とブロック単位で置き換わる（`ui_dialogue_logic.plan_tail`: 同じなら何もしない／remove・insert・update）。確定済み部分の追記・置き換えは従来の `plan_draft_sync`（版 `draft_rev`＋`base_rev`）。
- **黄色を直したら白**: 直す操作（キー・貼り付け・IME）の直前に、その黄色と上の黄色を確定済みの書式へ変える（文字は動かさない）→ `adopted` シグナル → パネルが保持し、このあとの `textChanged`／同期で `DialogueSession.adopt_provisional(uids, 欄の確定済み全文, base_rev)`。バッファは仮認識を取り除き、その発話を `_adopted` に入れる（以後の仮認識・清書は下書きを上書きしない。届いた清書は確定したメッセージの `clean` へ）。
- 波形の元: `DialogueSession.pending_waves()`（清書待ちの発話と、いま話している途中の発話の音声から棒の高さを計算。発話 ID は仮認識・清書と同じ）。
- **通知**: `ui_dialogue_logic.NoticeLine`（成功は時間で薄く→消える、警告は残る。常時の案内・未配達件数は通知が無いときの代わり）。確定操作（`pressed`）は結果より先に出す。
- **（以前の記述）**タイムラインは壁打ちと同じ `Timeline`。`DialogueSession` が `Session.timeline` へ、区切った発話（`add_pending`）・仮認識（`set_draft`）・清書での置き換え・確定（`commit_drafts`）を流す。発話の ID は区切りの開始秒（`uid_of`）で、話している途中の仮認識と区切ったあとの清書で同じになる。
- 下書き欄（`DraftEdit`）は確定済み部分（清書＋手修正）だけを持つ。バッファの版 `draft_rev` を追い、`plan_draft_sync` で「追記／末尾 1 文字の置き換え（「、」→「。」）／全部入れ替え」を決める。利用者の編集は `textChanged` ごとに `set_draft_text(text, base_rev)` で渡し、版が食い違えば（見ていない認識結果が先に届いていた）渡さず、追いついてからやり直す。確定は欄の内容を先に渡してから行う（`flush_edit`）。（2026-11-22 再設計で変更: 仮認識は欄の下の別ラベルではなく、欄の中の黄色のブロックとしてインラインで出す。直し始めると白＝確定扱い。上の「下書き欄（DraftView）」を参照。）

## 句点と改行（`dialogue_text.py`、純関数）
- 壁打ちの清書（`paragraph.py`）と同じ規則を、発話の間に適用する。間 = 次の開始 − 直前の終了 に、区切り器の preroll/tail の 0.6 秒を足したもの（すき間 0.05 秒未満＝最長での強制の区切りは 0）。時刻が無ければ従来どおりの連結。設定は `paragraph_enabled` `paragraph_gap_sec` `paragraph_force_gap_sec`（壁打ちと共有）。
- 手修正した文（`edited`）には規則を再適用しない（「。」を補わない）。直したあとの追記分との境目は、間が長ければ改行だけ入れる。
- 確定時（`finalize_text`）: 仮認識を含めた全体の末尾が句読点で終わっていなければ「。」（`dialogue.punctuate_on_commit`）。

## 送信（`dialogue_deliver._send_phase`）
- `deliver` の `options.send`。入力欄への配達が**読み戻し一致で確認できたあとだけ**。1回だけ試す（自動再試行しない）。①回答中（`busy_button`）が見えている間は待つ（`wait_while_busy`、`busy_wait_sec`。超えたら送らず `busy_timeout`）②送信ボタン（`send_button`）を UIA Invoke（前面化・フォーカス不要。ボタン名は完全一致・大文字小文字無視・入力欄に近いもの）③ボタンが無い／無効／Invoke 失敗なら、前面確認→入力欄のフォーカス確認→キー送信直前の前面再確認→Enter（`expected_root` 検証つき）④入力欄が空（または元の書きかけだけ）に戻ったかを読み戻して `sent`（True / False / "unknown"）。
- 結果に `sent` `send_stage`（button / enter）`send_reason` を足す。入力に成功して送信に失敗しても `status` は `delivered`（再試行で二重入力しない）。`DialogueBuffer` の `Message.sent`（"" / sent / not_sent / unknown）と `send_reason` に記録し、履歴に「送信済み／未送信／送信は未確認」、画面に「送信できませんでした（Enter／送信ボタンを押してください）」。クライアントの `deliver` の待ち時間は、送信ありのとき `busy_wait_sec` ぶん延ばす。

## 仮認識と確定（`DialogueSession`）
- `dialogue.provisional`（既定 ON）。別スレッド・低優先度の仮認識ループが、(a) `UtteranceSegmenter.current_audio()`（いま話している途中の音声のコピー）と (b) 清書待ちの発話の音声を、逐次認識のモデル（`make_whisper_recognizer(kind="live")`＝`live_model`・`live_cpu_threads`、モデルキャッシュ共有）で認識し、`DialogueBuffer.set_provisional(uid, text)` へ。間隔は `provisional_interval_sec`（既定 2 秒）。清書が遅れる（待ちが増える）と間隔を伸ばす（最大 5 倍）。清書が済んだ発話・確定済み・破棄済み・保留中の発話の仮認識は受け付けない。`LiveTranscriber`（ストリーム型）は再利用せず、同じモデル設定とキャッシュを使う認識器を作った。
- 下書き全体 = 確定済み部分（句点・改行の規則つき）＋ 仮認識（ID 順に素朴に連結）。確定（`commit`）は別スレッド（`_commit_flow`）で、`flush` → 仮認識の文字がまだ無い発話を最大 `provisional_wait_sec` 待つ（`commit_use_provisional`=OFF なら清書待ちのすべてを待つ。仮認識を使わない設定・認識器なしは従来どおり清書が揃うのを待つ）→ `buffer.commit()`。仮認識を含めて確定した発話の ID は `_closed[uid]=メッセージ id` に記録され、あとから届く清書は `Message.clean`（清書版）へ入り、次の下書きへは入らない（二重に届いても追記されない）。仮認識の文字が無いまま確定に含まれなかった発話の清書は、次の下書きへ入る（失わない）。
- 新しいセッションの開始時（`begin_session`）に ID の記録を捨てる（ID は録音の頭からの秒で、セッションごとに 0 から数え直すため）。持ち越した仮認識は確定済みの下書きの文字として残す。

## 自動配達しない（絶対条件）と、自動確定を採用しない理由
- **下書きに表示された内容を、利用者が何も操作していないのに自動で宛先へ入力・送信してはならない。** 必ず明示の操作（ボタン・ヘッドセット・ショートカット）を1つ挟む。仮認識・清書のどちらも「下書きに溜まる」だけ。
- 無音による自動確定（`auto_commit` / `message_gap_sec`）は**採用しない**。`DialogueSession` は自分では `commit` を呼ばない（コードを削除。設定ファイルに書かれていても使われず、既定値からも外した）。理由（陽太さんの方針）: 考え中の沈黙で早まる、誤爆で周囲の声が外部の AI へ行く。テスト `test_never_commits_or_delivers_without_user_operation`（auto_commit を書いても配達されない）と `test_nothing_is_ever_delivered_without_commit`（仮認識あり）で保証。無音による**セッションの自動終了**（`idle_stop_sec`）は従来どおり。

## 20261122-07 の補足（カーソル・句点の改行・波形の出し方）
- **カーソルの守り**（`ui_draft.DraftView`）: `apply_tail_ops` は、確定済み（白）の範囲にあるカーソル・選択を、後ろ側ブロックの挿入・削除・差し替えのあとで数値の位置へ復元する（Qt は挿入位置のカーソルを挿入のあとへ進めるため）。`_keep_out_of_wave`（`cursorPositionChanged` とコードからの書き換えの後）は、波形の中にあるカーソルを手前の白／黄色の末尾へ戻す。白の末尾にカーソルがあるときの認識結果の追記だけは、追記のあとへ進む（打ち続けられる）。
- **文章の規則**（`dialogue_text`）: `break_sentences`（文中の句点系の直後に改行）・`compose_draft(..., newline_after_sentence, blank_line)`（区切りは空行）・`is_noise_text`（幻覚の定番）。バッファの `display_text` が黄色の表示用（行区切りは U+2028）。確定済みの全文（`final_text`）では改行へ戻す。
- **波形の出し方**（`DialogueSession.pending_waves`）: 声のある発話だけ。区切り器の `current_audio(max_silence_sec)` は最短長に満たない塊・無音が続いている間は返さない。セグメンタは立ち上がり約1秒（`WARMUP_FRAMES`）は絶対値の基準だけで声を判定する。
