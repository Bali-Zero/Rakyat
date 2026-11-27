"""PII sovereignty gate for cloud OCR/Vision fallbacks.

SYMBIOSIS Law 2 (OSINT/PII sovereignty) + UU PDP Art. 56 (cross-border transfer):
OCR of client documents (passport / KTP / NPWP / akta / visa) must NOT send the
document image to Google's Gemini Vision when local Ollama is unavailable. Several
OCR paths historically did exactly that as a silent fallback.

Cloud Gemini Vision/OCR is PERMANENTLY DISABLED (owner order 2026-11-27): OCR is
local-only (Apple Vision on macOS + Ollama qwen2.5vl:7b). This module stays as the
single audit point every former cloud OCR chokepoint reports to:

  - `cloud_vision_allowed()` → bool : always False (there is no override flag).
  - `note_cloud_ocr_blocked(context)` : logs a WARNING (the deterministic,
    deploy-safe signal) and best-effort Telegram-alerts the operator. NEVER
    raises — alerting failure must not break OCR degradation.

When the cloud path is blocked, callers degrade to local results (tesseract /
text-layer) per the crm_guardian pattern — never hard-fail, never send to cloud.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("backend.multimodal.cloud_vision_gate")


def cloud_vision_allowed() -> bool:
    """Always False: cloud Gemini Vision/OCR is permanently disabled (local-only OCR)."""
    return False


def note_cloud_ocr_blocked(context: str) -> None:
    """Record that a cloud OCR fallback was blocked for PII sovereignty.

    Always logs WARNING (grep-able, survives in Fly logs). Best-effort Telegram
    alert so the operator knows local OCR is degraded and can restart Ollama.
    NEVER raises — this is called on the degradation path.
    """
    logger.warning(
        "🛡️ [PII-SOVEREIGNTY] Cloud OCR/Vision fallback BLOCKED (cloud Gemini OCR permanently disabled). "
        "Local OCR unavailable — degrading locally, NOT sending document to Google. Context: %s",
        context,
    )
    try:
        # scripts/ is on PYTHONPATH in the app runtime; guard so a missing module
        # or a Telegram outage never breaks the OCR degradation path.
        from scripts.sentinel_lib.alerter import send_alert

        send_alert(
            f"OCR cloud fallback blocked (PII-safe): local Ollama OCR unavailable.\n"
            f"Context: {context}\n"
            f"Action: restart local Ollama vision (qwen2.5vl:7b). "
            f"Cloud Gemini OCR is permanently disabled.",
            level="WARNING",
            # The context names WHICH surface degraded — portal document OCR,
            # vision RAG, the CRM passport extractor. 7 distinct literals reach
            # here (measured by test_the_ocr_gate_names_which_surface_degraded,
            # which counts contexts, not files — an earlier comment guessed
            # "six"). Dropping it fused them: portal blocked at 10:00 and
            # vision RAG at 11:00 would share one identity, and the second
            # degraded service never reaches Telegram. A shared remediation does
            # not make the affected surfaces interchangeable.
            condition=f"ocr-cloud-fallback-blocked:{context}",
        )
    except Exception as exc:  # alerter import error / network / anything
        logger.debug("cloud_vision_gate: Telegram alert unavailable (%s)", exc)
