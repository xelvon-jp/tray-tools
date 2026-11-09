"""配備係の入口。見回りループと単発実行（--once）。

  python -m deploy_daemon --once     1回だけ見回って終わる
  python -m deploy_daemon            見回りループ（停止合図ファイルで新規受け取りを止める）

設定は config.load_config（config.json ＋ 環境変数）。実チェックアウトと
受け取り口 URL が未設定なら、安全のため起動しない。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .adapters import CommandTestRunner, SendScriptLiveness, SendScriptRestarter, SubprocessGit
from .config import load_config
from .digest import DigestPoster
from .intake import IntakeClient
from .log import JsonlLogger
from .pipeline import Pipeline
from .statefile import SeenStore, StateWriter


def build_pipeline(config, transport=None) -> Pipeline:
    if config.checkout_dir is None:
        raise SystemExit(
            "実チェックアウト（checkout_dir）が未設定です。config.json か "
            "環境変数 DEPLOY_DAEMON_CHECKOUT_DIR で指定してください"
        )
    if not config.intake_url:
        raise SystemExit(
            "受け取り口（intake_url）が未設定です。Worker パッチの適用後に設定してください"
        )
    send_script = config.send_script
    if send_script is None or not send_script.is_file():
        raise SystemExit(f"traytools_send.py が見つかりません: {send_script}")
    intake = IntakeClient(config.intake_url, config.intake_key, transport=transport)
    return Pipeline(
        config=config,
        intake=intake,
        git=SubprocessGit(),
        test_runner=CommandTestRunner(config.test_commands, config.test_timeout_sec),
        restarter=SendScriptRestarter(config.python_executable, send_script),
        liveness=SendScriptLiveness(config.python_executable, send_script),
        logger=JsonlLogger(config.log_path),
        state_writer=StateWriter(config.state_path),
        seen=SeenStore(config.seen_path),
    )


def _guarded_run_once(pipeline):
    """見回りを例外から守る。1件の失敗で配備係全体を落とさない。

    例外はログと状態ファイルに残し、None を返す（呼び出し側は
    そのまま次の見回りへ進む）。dd-20261109-04 の教訓。
    """
    import traceback

    try:
        return pipeline.run_once()
    except Exception:  # noqa: BLE001 - 見回りを止めないことが目的
        detail = traceback.format_exc()
        try:
            pipeline.logger.log("loop_error", detail=detail)
            pipeline.state_writer.set_daemon(
                "error", "見回りで想定外の例外が発生しました（ログ参照）"
            )
        except Exception:  # noqa: BLE001 - 記録の失敗でも見回りは続ける
            pass
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="deploy_daemon")
    parser.add_argument("--once", action="store_true", help="1回だけ見回って終わる")
    parser.add_argument("--base-dir", type=Path, default=None, help="基底フォルダの上書き")
    parser.add_argument("--interval", type=float, default=None, help="見回り間隔（秒）")
    args = parser.parse_args(argv)

    config = load_config(base_dir=args.base_dir)
    if args.interval is not None:
        config.poll_interval_sec = args.interval
    pipeline = build_pipeline(config)
    config.base_dir.mkdir(parents=True, exist_ok=True)
    # 状態ダイジェスト（仕様書 第12節）。パイプラインと同じ輸送・状態・
    # git を使い、見回りの節目に自分の状態を受け取り口へ送る。
    poster = DigestPoster(
        config, pipeline.intake.transport, pipeline.state_writer, pipeline.git
    )
    # 長いテストの途中経過もダイジェストへ出す（パイプライン側の口）。
    pipeline.progress_post = poster.post_now

    if args.once:
        records = pipeline.run_once()
        poster.maybe_post(bool(records))
        return 0
    try:
        while True:
            records = _guarded_run_once(pipeline)
            poster.maybe_post(bool(records))
            time.sleep(max(1.0, config.poll_interval_sec))
    except KeyboardInterrupt:
        pipeline.state_writer.set_daemon("stopped", "手動で止められました")
        return 0


if __name__ == "__main__":
    sys.exit(main())
