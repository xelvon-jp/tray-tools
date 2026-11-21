# -*- coding: utf-8 -*-
"""清書ジョブの実体（GUI/Qt/COM 非依存）。

WAV を清書 → 置換辞書を適用 → txt/md 保存 → 履歴へ登録（やり直しなら更新）→ タイムライン反映。
FinalizeQueue の run_job として使う。
"""
from __future__ import annotations

from typing import Callable

from finalize_queue import Job
from history import History
from paragraph import format_for_output, plain_text
from transcriber import transcribe_wav


def make_run_job(get_settings: Callable[[], dict], replacer, history: History, cache,
                 transcribe: Callable = transcribe_wav,
                 on_status: Callable[[Job, str], None] = lambda j, m: None):
    def run_job(job: Job):
        s = get_settings()
        name = job.model or s.get("final_model", "small")
        threads = int(s.get("final_cpu_threads", 0))

        def prog(p: float) -> None:
            job.progress = p

        def status(m: str) -> None:
            job.message = m
            on_status(job, m)

        with cache.use(name, threads) as model:
            r = transcribe(job.wav, s, model_name=name, on_progress=prog, on_status=status,
                           cancel=job.cancel, model=model, postprocess=replacer.apply,
                           formatter=lambda segs, gaps=None: format_for_output(segs, s, gaps))
        if job.cancel.is_set():
            return None  # 中断。txt/md は途中まで保存されるが履歴には載せない
        # 履歴には元の区間（時刻つき・無加工）と、整形しない本文を残す。整形は出力（txt/md/クリップボード）だけ
        raw = plain_text(r.segments)
        if job.history_id:
            history.update_final(job.history_id, text=raw, segments=r.segments,
                                 duration=r.audio_seconds, model=name,
                                 txt_path=r.txt_path or "", md_path=r.md_path or "")
        else:
            job.history_id = history.add(text=raw, segments=r.segments, duration=r.audio_seconds,
                                         model=name, wav_path=job.wav, txt_path=r.txt_path or "",
                                         md_path=r.md_path or "")
        if job.timeline is not None:
            job.timeline.set_final(r.segments, r.audio_seconds, gaps=getattr(r, "gaps", None))
        return r
    return run_job
