---
id: 20261104-01-rules-survey
kind: ask            # 読み取りだけ。コードは変えない
repo: xelvon-jp/tray-tools
base_branch: main
base_sha: (この依頼書を足したコミット。依頼の1行に書いてある)
branch: muse/20261104-01-rules-survey
result: handoff/20261104-01-rules-survey.result.md
hops: 0
author: Claude（陽太の依頼で作成）
---

# [Claude依頼] tray-tools の作業ルールを AGENTS.md にまとめる下調べ

## 目的
Claude と Muse の両方が同じルールで tray-tools を触れるように、将来、ルールの正本をリポジトリ直下の `AGENTS.md` に置き、`CLAUDE.md` からそれを参照する形にしたい。その下調べをお願いします。

これは Claude→Muse 連携の**最初の試運転**でもあります。依頼の受け渡し・承認・push・完了検知が一通り回るかを確かめるのが一番の目的なので、中身は短くて構いません。

## やること（読み取りだけ）
1. `CLAUDE.md` と `README.md` を読む
2. 次の3つに分けて、箇条書きで書き出す
   - **A. Muse にも当てはまる作業ルール**（`AGENTS.md` に移すべきもの。例：コミットメッセージの書き方、テストの回し方、public リポジトリとしての注意）
   - **B. Claude（Claude Code）にだけ当てはまるもの**（`CLAUDE.md` に残すもの）
   - **C. Muse の環境（Linux VM・GitHub コネクタ）では守れない・意味がないもの**（例：Windows の GUI を前提にしたテスト）。守れない場合にどうするとよいかの案も添える
3. 気づいた矛盾や古くなっている記述があれば書く

## 守ること
- **`main` には何も push しない**。push するのはブランチ `muse/20261104-01-rules-survey` だけ
- 変更するのは `handoff/20261104-01-rules-survey.result.md` の新規作成**だけ**。ほかのファイルは触らない
- push は**1回・1コミット**にまとめる（承認のタップを減らすため）

## 結果ファイルの書式
`handoff/20261104-01-rules-survey.result.md` をこの形で作ってください。

```markdown
---
id: 20261104-01-rules-survey
status: done        # done | failed | blocked
base_sha: <読んだ main のコミット>
branch: muse/20261104-01-rules-survey
approvals: <この作業で承認カードが何回出たか（分かる範囲で）>
started: <着手した日時 JST>
finished: <終えた日時 JST>
---

## 結果
（上の A / B / C と、矛盾・古い記述）

## 試運転についてのメモ
（依頼の1行の分かりやすさ、依頼書で足りなかった情報、次から変えるとよい点）
```
