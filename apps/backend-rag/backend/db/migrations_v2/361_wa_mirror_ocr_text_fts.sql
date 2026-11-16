-- ============================================================
-- 361_wa_mirror_ocr_text_fts.sql
-- Full-text index over the text extracted from WhatsApp media
-- (whatsapp_message_context.ocr_result->>'text'), so media content is
-- searchable like message bodies are (idx_wmc_body_fts, migration 193).
-- Date: 2026-11-16
--
-- Partial on ocr_status = 'done': pending/empty/error rows carry no usable
-- text and are never indexed, so the index stays small. Building it still
-- scans the whole table once and holds a SHARE lock (writes wait) for the
-- duration, even when no row qualifies (prod today: OCR all pending).
-- 'simple' config = no stemming, multilingual safe (IT/EN/ID), same as 193.
-- Plain CREATE INDEX (not CONCURRENTLY): the runner applies each migration
-- in one transaction, where CONCURRENTLY is refused.
--
-- Consumer: GET /api/wa/messages/search (routers/wa_mirror_messages.py);
-- its WHERE expression must stay byte-identical to the one below.
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_wmc_ocr_text_fts
  ON whatsapp_message_context
  USING GIN (to_tsvector('simple', COALESCE(ocr_result->>'text', '')))
  WHERE ocr_status = 'done';

COMMENT ON INDEX idx_wmc_ocr_text_fts IS
  'Full-text search over extracted media text for /api/wa/messages/search. simple config, partial on ocr_status=done.';

-- === ROLLBACK ===
DROP INDEX IF EXISTS idx_wmc_ocr_text_fts;
