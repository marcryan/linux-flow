import atexit
import multiprocessing as mp
import time
from typing import Optional

import numpy as np
from faster_whisper import WhisperModel

from .base import ASRBackend, TranscriptResult


class FasterWhisperBackend(ASRBackend):
    """faster-whisper backend adapter."""

    def __init__(
        self,
        model_name: str,
        device: str = "cpu",
        compute_type: str = "int8",
        sample_rate: int = 16000,
        transcribe_tail_pad: float = 0.25,
        request_timeout_s: float = 30.0,
        max_retries: int = 1,
        retry_backoff_s: float = 0.2,
    ) -> None:
        self.sample_rate = sample_rate
        self.transcribe_tail_pad = transcribe_tail_pad
        self.request_timeout_s = request_timeout_s
        self.max_retries = max_retries
        self.retry_backoff_s = retry_backoff_s
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self._ctx = mp.get_context("spawn")
        self._worker_proc = None
        self._parent_conn = None
        self._request_seq = 0
        atexit.register(self.close)

    @staticmethod
    def _transcribe_once_static(
        model: WhisperModel,
        audio: np.ndarray,
        language: Optional[str],
        sample_rate: int,
        transcribe_tail_pad: float,
    ) -> TranscriptResult:
        if transcribe_tail_pad > 0:
            pad_samples = int(sample_rate * transcribe_tail_pad)
            if pad_samples > 0:
                audio = np.concatenate([audio, np.zeros(pad_samples, dtype=np.float32)])

        duration_s = len(audio) / sample_rate
        use_vad = duration_s >= 1.0

        segments, info = model.transcribe(
            audio,
            beam_size=5,
            language=language,
            vad_filter=use_vad,
            vad_parameters=dict(min_silence_duration_ms=500),
        )

        text = " ".join(seg.text.strip() for seg in segments).strip()
        return TranscriptResult(text=text, language=info.language, duration_s=duration_s)

    @staticmethod
    def _worker_main(
        conn,
        model_name: str,
        device: str,
        compute_type: str,
        sample_rate: int,
        transcribe_tail_pad: float,
    ) -> None:
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        try:
            while True:
                try:
                    message = conn.recv()
                except EOFError:
                    break

                if not isinstance(message, dict):
                    conn.send({"ok": False, "error": "Invalid worker request payload"})
                    continue

                op = message.get("op")
                if op == "shutdown":
                    break
                if op != "transcribe":
                    conn.send({"ok": False, "error": f"Unsupported op: {op!r}"})
                    continue

                req_id = message.get("req_id")
                audio = message.get("audio")
                language = message.get("language")
                try:
                    result = FasterWhisperBackend._transcribe_once_static(
                        model=model,
                        audio=audio,
                        language=language,
                        sample_rate=sample_rate,
                        transcribe_tail_pad=transcribe_tail_pad,
                    )
                    conn.send(
                        {
                            "ok": True,
                            "req_id": req_id,
                            "text": result.text,
                            "language": result.language,
                            "duration_s": result.duration_s,
                        }
                    )
                except Exception as e:
                    conn.send({"ok": False, "req_id": req_id, "error": f"{type(e).__name__}: {e}"})
        finally:
            conn.close()

    def _ensure_worker(self) -> None:
        if self._worker_proc is not None and self._worker_proc.is_alive() and self._parent_conn is not None:
            return
        self._stop_worker()
        parent_conn, child_conn = self._ctx.Pipe(duplex=True)
        worker = self._ctx.Process(
            target=FasterWhisperBackend._worker_main,
            args=(
                child_conn,
                self.model_name,
                self.device,
                self.compute_type,
                self.sample_rate,
                self.transcribe_tail_pad,
            ),
            daemon=False,
        )
        worker.start()
        child_conn.close()
        self._parent_conn = parent_conn
        self._worker_proc = worker

    def _stop_worker(self) -> None:
        if self._parent_conn is not None:
            try:
                self._parent_conn.send({"op": "shutdown"})
            except Exception:
                pass
            try:
                self._parent_conn.close()
            except Exception:
                pass
            self._parent_conn = None

        if self._worker_proc is None:
            return

        proc = self._worker_proc
        self._worker_proc = None

        proc.join(timeout=0.8)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=1.5)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=2.0)
        try:
            close = getattr(proc, "close", None)
            if close is not None:
                close()
        except Exception:
            pass

    def _restart_worker(self) -> None:
        self._stop_worker()
        self._ensure_worker()

    def close(self) -> None:
        self._stop_worker()

    def transcribe(self, audio: np.ndarray, language: Optional[str] = None) -> TranscriptResult:
        last_error = None
        attempts = max(1, self.max_retries + 1)
        for attempt in range(1, attempts + 1):
            try:
                self._ensure_worker()
                self._request_seq += 1
                req_id = self._request_seq
                self._parent_conn.send(
                    {
                        "op": "transcribe",
                        "req_id": req_id,
                        "audio": np.asarray(audio, dtype=np.float32),
                        "language": language,
                    }
                )
                if not self._parent_conn.poll(self.request_timeout_s):
                    self._restart_worker()
                    last_error = TimeoutError(
                        f"ASR timed out after {self.request_timeout_s:.1f}s (attempt {attempt}/{attempts})"
                    )
                else:
                    response = self._parent_conn.recv()
                    if not isinstance(response, dict):
                        raise RuntimeError("Invalid worker response payload")
                    if response.get("req_id") != req_id:
                        raise RuntimeError("ASR worker response id mismatch")
                    if response.get("ok"):
                        return TranscriptResult(
                            text=response.get("text", ""),
                            language=response.get("language"),
                            duration_s=float(response.get("duration_s", 0.0)),
                        )
                    last_error = RuntimeError(response.get("error", "ASR worker failed"))
            except Exception as e:
                self._restart_worker()
                last_error = e

            if attempt < attempts and self.retry_backoff_s > 0:
                time.sleep(self.retry_backoff_s)

        raise RuntimeError(f"ASR failed after {attempts} attempt(s): {last_error}") from last_error
