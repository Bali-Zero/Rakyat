"""Visual — image QA pipeline for War Room 2.0.

Modules:
- fireworks_fallback: Flux.1 Dev image generation
- vision_qa: qwen2.5vl:7b Ollama JSON-structured flags
- qa_judge: Claude Haiku CLI final decision (pass|retry|reject)
- prompt_builder: 4-layer prompt assembly (scene + brand + style + negative)

The Google Imagen client and the VisualGenerator orchestrator were removed
(2026-11-27): no live caller, 0 calls in 60 days.
"""

from backend.services.visual.prompt_builder import (
    BRAND_SUFFIX,
    NEGATIVE_PROMPT,
    build_imagen_prompt,
)
from backend.services.visual.qa_judge import (
    QADecision,
    QAJudge,
    QAVerdict,
)
from backend.services.visual.vision_qa import (
    OllamaVisionClient,
    VisionFlags,
)

__all__ = [
    "BRAND_SUFFIX",
    "NEGATIVE_PROMPT",
    "OllamaVisionClient",
    "QADecision",
    "QAJudge",
    "QAVerdict",
    "VisionFlags",
    "build_imagen_prompt",
]
