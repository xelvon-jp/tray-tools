"""front_desk — 窓口。

Muse からの依頼を Worker 経由で受け取り、実行モデル目安 (tier) に
応じて Claude Code セッションを起動し、結果を返す小さな常駐。
設計は collab-workspace の docs/front-desk-design.md、契約は
docs/front-desk/worker-patch/README.md。
"""
