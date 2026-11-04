---
id: YYYYMMDD-NN-slug
kind: ask            # ask | draft | implement（下の「kind ごとの違い」を参照）
repo: xelvon-jp/tray-tools
base_branch: main
base_sha: <この依頼書を足したコミット。依頼の1行にも書く>
branch: muse/YYYYMMDD-NN-slug
result: handoff/YYYYMMDD-NN-slug.result.md
hops: 0              # Muse→Claude→Muse と往復した回数。差し戻しのたびに1足す
author: Claude（陽太の依頼で作成）
---

<!--
使い方（Claude 向けのメモ。依頼書には残してよい）:
1. このファイルを handoff/<id>.md にコピーして埋め、main にコミット・push する
2. **同じコミットから作業ブランチも Claude が先に作る**：git push origin HEAD:refs/heads/muse/<id>
   Muse がブランチを作ると承認カードが1回増える（ブランチ作成＋push で2回）。先に作っておけば push の1回で済む
3. そのコミットの sha を base_sha に書く（書いてからもう一度コミットする必要はない。
   依頼の1行に base を書けば Muse はそちらを読む）
4. muse_post.py post --message-file で依頼の1行を投稿する。1行の例:
   [Claude依頼] tray-tools <id> ／ xelvon-jp/tray-tools の handoff/<id>.md
   （branch: main, base: <base_sha>）を読んで、書いてあるとおりに作業してください。
   ブランチ muse/<id> は作成済みです（新しく作らないでください）。結果はそこに 1コミットで push してください。
5. muse_post.py wait --repo <repo> --branch muse/<id> --path handoff/<id>.result.md で結果を待つ
   （ブランチは最初からあるので、--path なしだと即座に終わってしまう）
-->

# [Claude依頼] <一言でいうと何をしてほしいか>

## 目的
<なぜこれをやるのか。結果を何に使うのか。背景は短く>

## やること
1. <手順。読むファイル・変えるファイルを名指しする>
2. <…>

## kind ごとの違い
この依頼の kind は上の frontmatter のとおり。

| kind | してよいこと | 作るもの |
|---|---|---|
| ask | **読み取りだけ**。コードは変えない | 結果ファイルだけ |
| draft | 新しい文書の下書きを書く。既存ファイルは変えない | 下の「触ってよいパス」に挙げた新規ファイル＋結果ファイル |
| implement | 下の「触ってよいパス」**だけ**を変更する。テストを足す・回す | 変更＋結果ファイル |

implement のとき：
- Muse の環境（Linux VM）で回せるテスト（純関数・静的チェック・`QT_QPA_PLATFORM=offscreen` で済むもの）は回し、結果を書く
- Windows 実機でしか確かめられない項目（常駐の再起動、UIA、実画面の目視、ExifTool の実書きなど）は、やったふりをせず結果ファイルに「**未実施・実機確認待ち**」と書いて項目を挙げる

## 守ること
- **`main` には何も push しない**。push するのはブランチ `muse/YYYYMMDD-NN-slug` だけ
- **ブランチ `muse/YYYYMMDD-NN-slug` は Claude が作成済み**（依頼書と同じコミットを指している）。新しく作らず、そこへ push する（承認カードを1回で済ませるため）
- 触ってよいパス：
  - `handoff/YYYYMMDD-NN-slug.result.md`（新規。必ず作る）
  - <implement / draft のときだけ、ほかのパスをここに列挙する>
- それ以外のファイルは触らない
- push は**1回・1コミット**にまとめる（承認のタップを減らすため）
- コミットメッセージは日本語。1行目に要約、本文に「なぜそうしたか」
- このリポジトリは public。秘密情報（トークン・パスワード・個人のファイルパスなど）を書かない
- 改行は LF

## 作業中に main が進んでいた場合
着手したとき、main の HEAD が base_sha より先に進んでいることがある（Claude 側が並行して作業しているため）。

- **調べ物・判断は base_sha 時点の内容で行う。** 依頼書はその時点を前提に書いてある
- **ブランチは作成済みで、base_sha を指している。** そのまま使う（現在の main に合わせ直さない）
- base_sha と現在の main とで、**この依頼の対象ファイル（読むもの・変えるもの）が違っていたら**、結果ファイルにそう書く（どのファイルが、どう違ったか。blob の SHA で確かめた、など）
- implement で、変えるファイル自体が main 側で変わっていて衝突しそうなら、無理に合わせず status: blocked で止めて理由を書く

## 結果ファイルの書式
`handoff/YYYYMMDD-NN-slug.result.md` をこの形で作ってください。

```markdown
---
id: YYYYMMDD-NN-slug
status: done        # done | failed | blocked
base_sha: <読んだ main のコミット（依頼書の base_sha）>
head_sha: <ブランチを切ったときの main のコミット。base_sha と同じなら同じ値>
branch: muse/YYYYMMDD-NN-slug
approvals: <回数>（内訳: ブランチ作成 1、push 1 …）
started: <着手した日時 JST>
finished: <終えた日時 JST>
---

## 結果
（依頼された内容。implement なら、変えたファイルと変えた理由、回したテストとその結果）

## 未実施・実機確認待ち
（implement のときのみ。無ければ「なし」）

## base と現在の main の差
（対象ファイルに差があったときのみ。無ければ「なし」）

## 連携についてのメモ
（依頼の1行の分かりやすさ、依頼書で足りなかった情報、次から変えるとよい点）
```

- **status**: `done` = 依頼どおり終えた／`failed` = やってみたが目的を果たせなかった（理由を書く）／`blocked` = 判断が要る・前提が崩れていて進められない（何が決まれば進めるかを書く）
- **approvals の数え方**: この作業で**承認カードが出た書き込み操作**の回数（ブランチ作成・ファイル作成・push など）。読み取り（ファイル取得・コミット一覧など）はカードが出ないので数えない。**内訳も書く**
