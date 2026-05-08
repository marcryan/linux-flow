import concurrent.futures
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
        self.model = WhisperModel(model_name, device=device, compute_type=compute_type)

    def _transcribe_once(self, audio: np.ndarray, language: Optional[str]) -> TranscriptResult:
        if self.transcribe_tail_pad > 0:
            pad_samples = int(self.sample_rate * self.transcribe_tail_pad)
            if pad_samples > 0:
                audio = np.concatenate([audio, np.zeros(pad_samples, dtype=np.float32)])

        duration_s = len(audio) / self.sample_rate
        use_vad = duration_s >= 1.0

        segments, info = self.model.transcribe(
            audio,
            beam_size=5,
            language=language,
            vad_filter=use_vad,
            vad_parameters=dict(min_silence_duration_ms=500),
        )

        text = " ".join(seg.text.strip() for seg in segments).strip()
        return TranscriptResult(text=text, language=info.language, duration_s=duration_s)

    def transcribe(self, audio: np.ndarray, language: Optional[str] = None) -> TranscriptResult:
        last_error = None
        attempts = max(1, self.max_retries + 1)
        for attempt in range(1, attempts + 1):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self._transcribe_once, audio, language)
                try:
                    return future.result(timeout=self.request_timeout_s)
                except concurrent.futures.TimeoutError as e:
                    future.cancel()
                    last_error = TimeoutError(
                        f"ASR timed out after {self.request_timeout_s:.1f}s (attempt {attempt}/{attempts})"
                    )
                except Exception as e:
                    last_error = e

            if attempt < attempts and self.retry_backoff_s > 0:
                time.sleep(self.retry_backoff_s)

        raise RuntimeError(f"ASR failed after {attempts} attempt(s): {last_error}") from last_error
