from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class TranscriptResult:
    text: str
    language: Optional[str]
    duration_s: float


class ASRBackend:
    """Interface for speech-to-text backends."""

    def transcribe(self, audio: np.ndarray, language: Optional[str] = None) -> TranscriptResult:
        raise NotImplementedError
