# -*- coding: utf-8 -*-
"""アプリ全体の部品を組み立てる（GUI/Qt/COM 非依存）。"""
from __future__ import annotations

import logging
import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from finalize_queue import FinalizeQueue, Job
from history import History
from model_cache import ModelCache
from paths import data_dir
from pipeline import make_run_job
from replacements import Replacer, load_pairs
from session import Session
from transcriber import load_model, transcribe_wav


def setup_error_log() -> logging.Logger:
    """error.log（データ置き場）。例外はここへ。sys / threading の excepthook を向ける。"""
    log = logging.getLogger("kabeuchi")
    if not log.handlers:
        h = logging.FileHandler(data_dir() / "error.log", encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)

    def hook(exc_type, exc, tb):
        log.error("未捕捉の例外:\n" + "".join(traceback.format_exception(exc_type, exc, tb)))

    def thook(args):
        log.error(f"スレッド {getattr(args.thread, 'name', '?')} の未捕捉の例外:\n"
                  + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
    sys.excepthook = hook
    threading.excepthook = thook
    return log


@dataclass
class Services:
    settings: dict
    replacer: Replacer
    history: History
    cache: ModelCache
    queue: FinalizeQueue
    session: Session


def build_services(settings: dict, on_job_event: Callable[[Job], None] | None = None,
                   on_status: Callable[[str], None] = lambda m: None, sd=None,
                   transcribe: Callable = transcribe_wav, loader: Callable | None = None,
                   history: History | None = None) -> Services:
    replacer = Replacer(load_pairs())
    history = history or History()
    loader = loader or (lambda name, th: load_model(name, settings, cpu_threads=th, log=on_status))
    cache = ModelCache(loader, idle_sec=float(settings.get("model_idle_release_sec", 600)))
    run_job = make_run_job(lambda: settings, replacer, history, cache, transcribe=transcribe)
    queue = FinalizeQueue(run_job, on_event=on_job_event)
    session = Session(settings, replacer, model_cache=cache, sd=sd, on_status=on_status)
    return Services(settings, replacer, history, cache, queue, session)
