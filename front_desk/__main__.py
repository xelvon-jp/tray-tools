"""front_desk.__main__ — 窓口の入口。

使い方:
  python -m front_desk --config config.json [--once]

窓口鍵は環境変数 FRONT_DESK_KEY から読む (ファイルには書かない)。
停止は base_dir に stop という名前のファイルを置く。
"""
from __future__ import annotations

import argparse
import os
import sys

from .config import DeskConfig
from .executor import SubprocessExecutor
from .intake import UrllibTransport
from .log import JsonlLog
from .pipeline import DeskPipeline
from .statefile import DeskStateStore, ProcessedStore

ENV_DESK_KEY = "FRONT_DESK_KEY"


def build_pipeline(config: DeskConfig, desk_key: str) -> DeskPipeline:
    transport = UrllibTransport(config.worker_base_url, desk_key)
    executor = SubprocessExecutor(
        config.executor_commands, config.exec_timeout_sec, config.workdir)
    return DeskPipeline(
        config, transport, executor,
        log=JsonlLog(config.log_path),
        state_store=DeskStateStore(config.state_path),
        processed_store=ProcessedStore(config.processed_path),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="front_desk")
    parser.add_argument("--config", default=None,
                        help="設定ファイル (JSON) のパス")
    parser.add_argument("--once", action="store_true",
                        help="見回りを1回だけ行って終わる")
    args = parser.parse_args(argv)

    desk_key = os.environ.get(ENV_DESK_KEY, "")
    if not desk_key:
        print(f"環境変数 {ENV_DESK_KEY} に窓口鍵が設定されていません",
              file=sys.stderr)
        return 2
    try:
        config = DeskConfig.load(args.config)
    except (OSError, ValueError) as exc:
        print(f"設定を読めませんでした: {exc}", file=sys.stderr)
        return 2
    if not config.worker_base_url:
        print("設定 worker_base_url が空です", file=sys.stderr)
        return 2

    os.makedirs(config.base_dir, exist_ok=True)
    pipeline = build_pipeline(config, desk_key)
    if args.once:
        processed = pipeline.poll_once()
        print(f"processed: {processed}")
        return 0

    def should_stop() -> bool:
        return os.path.exists(config.stop_path)

    try:
        pipeline.run_forever(should_stop=should_stop)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
