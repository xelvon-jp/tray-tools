"""配備のパイプライン（状態機械）。

received → staged → tested →（承認待ち）→ applied → restarted → verified → reported

作業場は「配備係専用のクローン」（staging）。ユーザーの作業用チェックアウトへは
fast-forward のみで反映し、未コミット変更などで汚れていたら反映せず止める。
失敗分岐は仕様書 第6節に対応する：
  A2 テスト失敗      … 実物へ反映せず検証用コピーを破棄し、報告する（再試行しない）
  A3 再起動/生存失敗 … 反映直前の HEAD へロールバックし、再起動と再確認を行う。
                       ロールバック側の再起動もブロッカーがある間は実行しない
  A5 重複排除        … 届け出 ID で処理済みを記録し、二度がけしない
  A7 権限外・競合    … 常時承認の範囲外は受け取り拒否。ff できない・作業用
                       チェックアウトが汚れている場合は反映を止めて報告する
停止合図ファイルがある間は新規受け取りをしない。

git・テスト実行・再起動・生存確認は小さなインターフェース越しに使う
（実アダプタは adapters.py、テストではフェイクに差し替える）。
"""
from __future__ import annotations

import fnmatch
import re
import shutil
import time
from pathlib import Path

from .adapters import GitError
from .intake import IntakeError
from .model import DeliveryRecord, DeliveryState


class Pipeline:
    def __init__(
        self,
        config,
        intake,
        git,
        test_runner,
        restarter,
        liveness,
        logger,
        state_writer,
        seen,
        sleep=time.sleep,
    ):
        self.config = config
        self.intake = intake
        self.git = git
        self.test_runner = test_runner
        self.restarter = restarter
        self.liveness = liveness
        self.logger = logger
        self.state_writer = state_writer
        self.seen = seen
        self.sleep = sleep

    # ---------- 1周分 ----------

    def run_once(self) -> list:
        cfg = self.config
        if cfg.stop_file.exists():
            self.state_writer.set_daemon("stopped", "停止合図ファイルがあります")
            self.logger.log("poll_skipped_stop_file")
            return []
        self.state_writer.set_daemon("working", "受け取り口を見回り中")
        try:
            deliveries = self.intake.fetch_pending()
        except IntakeError as e:
            self.logger.log("intake_fetch_failed", detail=str(e))
            self.state_writer.set_daemon("error", f"受け取り口の取得に失敗: {e}")
            return []
        records = []
        for delivery in deliveries:
            if cfg.stop_file.exists():
                self.logger.log("poll_stopped_midway", delivery_id=delivery.id)
                break
            if self.seen.is_terminal(delivery.id):
                self.logger.log(
                    "delivery_skipped_duplicate",
                    delivery_id=delivery.id,
                    state=self.seen.terminal_state(delivery.id),
                )
                continue
            records.append(self.process(delivery))
        self.state_writer.set_daemon("idle", f"見回り完了（{len(records)}件処理）")
        return records

    # ---------- 1件の処理 ----------

    def process(self, delivery) -> DeliveryRecord:
        cfg = self.config
        rec = DeliveryRecord(delivery)
        rec.transition(DeliveryState.RECEIVED, delivery.summary)
        self._sync(rec)
        self.logger.log("delivery_received", delivery_id=delivery.id, repo=delivery.repo)

        if delivery.bundle_ref and not delivery.ref:
            return self._finish(
                rec,
                DeliveryState.REJECTED,
                "bundle 形式の届け出は第1増分では未対応です",
            )

        reason = self._scope_reason(delivery)
        if reason:
            return self._finish(rec, DeliveryState.REJECTED, reason)

        remote = cfg.remote_for(delivery.repo_name)
        if not remote:
            return self._finish(
                rec, DeliveryState.REJECTED, f"リモートが未設定です: {delivery.repo_name}"
            )

        # ステージング = 配備係専用のクローン
        staging = cfg.staging_dir_for(delivery.repo_name)
        try:
            self.git.ensure_clone(remote, staging, delivery.branch)
            self.git.fetch(staging, delivery.branch)
        except GitError as e:
            return self._defer(rec, f"ステージングの準備に失敗: {e}")
        if not self.git.has_object(staging, delivery.ref):
            return self._defer(rec, "変更（ref）がまだ GitHub 側に見えません")
        try:
            self.git.checkout(staging, delivery.ref)
        except GitError as e:
            return self._defer(rec, f"ステージングへの切り替えに失敗: {e}")
        rec.transition(DeliveryState.STAGED)
        self._sync(rec)

        # 変更ファイルの実物チェック（宣言より実 diff を正とする）
        prev_head = self._checkout_head()
        if prev_head and self.git.has_object(staging, prev_head):
            names = self.git.diff_names(staging, prev_head, delivery.ref)
            bad = [n for n in names if not self._path_allowed(n)]
            if bad:
                return self._finish(
                    rec,
                    DeliveryState.REJECTED,
                    "常時承認の範囲外のファイルが含まれています: " + "、".join(bad[:5]),
                )

        # テスト（A2）
        ok, output = self.test_runner.run(staging)
        if not ok:
            shutil.rmtree(staging, ignore_errors=True)  # 検証用コピーを破棄
            return self._finish(
                rec, DeliveryState.TEST_FAILED, "テストが失敗しました", detail=output
            )
        rec.transition(DeliveryState.TESTED)
        self._sync(rec)

        # 承認ゲート（第1段の既定モード）
        if cfg.apply_mode != "auto" and not self._approval_exists(delivery.id):
            rec.transition(
                DeliveryState.AWAITING_APPLY_APPROVAL,
                "テスト合格。承認シグナル待ち",
            )
            self._sync(rec)
            self._report(rec, terminal=False)
            self.logger.log("delivery_awaiting_approval", delivery_id=delivery.id)
            return rec

        return self._apply_and_restart(rec, prev_head)

    # ---------- 反映・再起動・ロールバック ----------

    def _apply_and_restart(self, rec: DeliveryRecord, prev_head_hint) -> DeliveryRecord:
        cfg = self.config
        delivery = rec.delivery
        checkout = cfg.checkout_dir
        if checkout is None:
            return self._finish(rec, DeliveryState.REJECTED, "実チェックアウトが未設定です")
        try:
            if self.git.is_dirty(checkout):
                return self._finish(
                    rec,
                    DeliveryState.CONFLICT,
                    "作業用チェックアウトに未コミット変更があるため、反映を止めました",
                )
            prev_head = self.git.head(checkout)
            rec.prev_head = prev_head
            self.git.fetch(checkout, delivery.branch)
            self.git.merge_ff_only(checkout, delivery.ref)
        except GitError as e:
            return self._finish(
                rec, DeliveryState.CONFLICT, f"反映できませんでした: {e}", detail=str(e)
            )
        # 反映の必須検証（dd-20261109-02 の教訓）: ff-only は届け出の ref が
        # 現在位置の祖先だと何もせず成功するので、HEAD が ref と一致するかを
        # 必ず確かめる。no-op や後戻りを成功として報告しない。承認ファイルは
        # この検証を通るまで消費せず、再起動にも進まない。
        applied_head = self.git.head(checkout)
        if not self._head_matches(delivery.ref, applied_head):
            return self._finish(
                rec,
                DeliveryState.CONFLICT,
                "反映後に HEAD が届け出の ref と一致しませんでした",
                detail=f"HEAD={applied_head} ref={delivery.ref}",
            )
        self._consume_approval(delivery.id)
        rec.transition(DeliveryState.APPLIED, f"{(prev_head_hint or prev_head)[:8]} -> {delivery.ref[:8]}")
        self._sync(rec)
        self.logger.log(
            "delivery_applied", delivery_id=delivery.id, prev_head=prev_head, ref=delivery.ref
        )

        outcome, message = self._restart_and_verify(rec)
        if outcome == "verified":
            return self._finish(
                rec,
                DeliveryState.REPORTED,
                "反映と再起動が完了しました",
                report_state="verified",
            )
        if outcome == "held":
            return self._finish(
                rec,
                DeliveryState.HELD,
                "再起動ブロッカーが続くため保留しました（反映済み・再起動は未実施）",
                detail=message,
            )
        # 再起動そのものの失敗、または生存確認の失敗 → ロールバック（A3）
        self.logger.log("rollback_start", delivery_id=delivery.id, detail=message)
        return self._rollback(rec, message)

    def _rollback(self, rec: DeliveryRecord, cause: str) -> DeliveryRecord:
        cfg = self.config
        checkout = cfg.checkout_dir
        if rec.prev_head is None:
            return self._finish(rec, DeliveryState.ROLLBACK_FAILED, "戻す先の HEAD が不明です")
        try:
            self.git.reset_hard(checkout, rec.prev_head)
        except GitError as e:
            return self._finish(
                rec, DeliveryState.ROLLBACK_FAILED, f"ロールバックに失敗しました: {e}"
            )
        rec.transition(DeliveryState.APPLIED, "ロールバック適用（再起動待ち）")
        self._sync(rec)
        # ロールバック側の再起動も、ブロッカーがある間は実行しない（A3 の競合回避）
        outcome, message = self._restart_and_verify(rec, note="ロールバック後の再起動")
        if outcome == "verified":
            return self._finish(
                rec,
                DeliveryState.ROLLED_BACK,
                "問題があったため、反映直前の状態へ戻して復旧しました",
                detail=cause,
                report_state="rolled_back",
            )
        return self._finish(
            rec,
            DeliveryState.ROLLBACK_FAILED,
            "ロールバック後の再起動・生存確認ができませんでした。人間の対応が必要です",
            detail=f"{cause} / {message}",
            report_state="rollback_failed",
        )

    def _restart_and_verify(self, rec: DeliveryRecord, note: str = ""):
        """再起動（ブロッカー待ちつき）と生存確認。-> (outcome, message)

        outcome は "verified" | "held" | "failed"。
        ブロッカーがある間は再起動を実行せず、間隔を空けて確かめ直す。
        上限まで続いたら "held"（再起動は未実施のまま）。
        """
        cfg = self.config
        for attempt in range(cfg.blocker_retries + 1):
            kind, message = self.restarter.check()
            if kind == "ok":
                break
            if kind == "error":
                return "failed", message
            # blocked
            if attempt >= cfg.blocker_retries:
                return "held", message
            self.logger.log(
                "restart_blocked",
                delivery_id=rec.delivery.id,
                attempt=attempt + 1,
                detail=message,
            )
            self.sleep(cfg.blocker_wait_sec)
        ok, message = self.restarter.restart()
        if not ok:
            return "failed", message
        rec.transition(DeliveryState.RESTARTED, (note + " " + message).strip())
        self._sync(rec)
        for _ in range(max(1, cfg.liveness_retries)):
            if self.liveness.check():
                rec.transition(DeliveryState.VERIFIED)
                self._sync(rec)
                return "verified", ""
            self.sleep(cfg.liveness_interval_sec)
        return "failed", "再起動後の生存確認ができませんでした"

    # ---------- 範囲チェック・承認シグナル ----------

    def _scope_reason(self, delivery) -> str | None:
        cfg = self.config
        if delivery.repo_name not in cfg.allowed_repos:
            return f"対象外のリポジトリです: {delivery.repo}"
        if not any(fnmatch.fnmatchcase(delivery.branch, pat) for pat in cfg.allowed_branches):
            return f"対象外のブランチです: {delivery.branch}"
        bad = [p for p in delivery.paths if not self._path_allowed(p)]
        if bad:
            return "常時承認の範囲外のファイルが含まれています: " + "、".join(bad[:5])
        return None

    def _path_allowed(self, path: str) -> bool:
        prefixes = self.config.allowed_path_prefixes
        if not prefixes:
            return True
        normalized = path.replace("\\", "/").lstrip("/")
        return any(normalized.startswith(p.replace("\\", "/").lstrip("/")) for p in prefixes)

    def _approval_path(self, delivery_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", delivery_id)
        return self.config.approval_dir / f"{safe}.approve"

    def _approval_exists(self, delivery_id: str) -> bool:
        return self._approval_path(delivery_id).exists()

    def _consume_approval(self, delivery_id: str) -> None:
        try:
            self._approval_path(delivery_id).unlink(missing_ok=True)
        except OSError:
            pass

    # ---------- 共通の締め ----------

    def _checkout_head(self) -> str | None:
        checkout = self.config.checkout_dir
        if checkout is None or not (checkout / ".git").exists():
            return None
        try:
            return self.git.head(checkout)
        except GitError:
            return None

    def _defer(self, rec: DeliveryRecord, reason: str) -> DeliveryRecord:
        """一時的な待ち（終端ではない）。上限を超えたら FETCH_FAILED で終端にする。"""
        attempts = self.seen.bump_attempt(rec.delivery.id)
        if attempts > self.config.ref_wait_retries:
            return self._finish(
                rec, DeliveryState.FETCH_FAILED, f"変更を取り込めませんでした: {reason}"
            )
        rec.transition(DeliveryState.DEFERRED, reason)
        self._sync(rec)
        self._report(rec, terminal=False)
        self.logger.log(
            "delivery_deferred", delivery_id=rec.delivery.id, attempts=attempts, detail=reason
        )
        return rec

    @staticmethod
    def _head_matches(ref: str, head: str) -> bool:
        """HEAD と届け出の ref が同一コミットか（短縮 SHA も許す）。

        from_dict で ref は小文字化済み。head は git が返す40桁の小文字。
        """
        ref = ref.strip().lower()
        head = head.strip().lower()
        return head.startswith(ref) or ref.startswith(head)

    def _finish(
        self,
        rec: DeliveryRecord,
        state: DeliveryState,
        summary: str,
        detail: str = "",
        report_state: str | None = None,
    ) -> DeliveryRecord:
        rec.transition(state, summary)
        rec.detail = detail
        self._sync(rec, summary)
        self._report(rec, terminal=True, state_override=report_state, summary=summary, detail=detail)
        self.seen.mark_terminal(rec.delivery.id, state.value)
        self.logger.log(
            "delivery_finished",
            delivery_id=rec.delivery.id,
            state=state.value,
            summary=summary,
        )
        return rec

    def _sync(self, rec: DeliveryRecord, summary: str = "") -> None:
        self.state_writer.set_delivery(
            rec.delivery.id,
            rec.state.value,
            summary or rec.delivery.summary,
            ref=rec.delivery.ref or None,
        )

    def _report(
        self,
        rec: DeliveryRecord,
        terminal: bool,
        state_override: str | None = None,
        summary: str = "",
        detail: str = "",
    ) -> None:
        try:
            self.intake.report(
                rec.delivery.id,
                state=state_override or rec.state.value,
                ok=rec.state == DeliveryState.REPORTED,
                summary=summary or rec.delivery.summary,
                detail=detail,
                terminal=terminal,
            )
        except IntakeError as e:
            # 報告の失敗で処理結果は覆らない。ログに残し、状態ファイル側で見えるようにする
            self.logger.log("report_failed", delivery_id=rec.delivery.id, detail=str(e))
