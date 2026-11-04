-- Migration 334: official names for E30A / E30B in the legacy visa_types.name column.
-- Law (families/law/E30/E30A.json, E30B.json): E30A = primary/secondary education,
-- E30B = higher education. The old labels (Research / Training) were wrong.
-- Guarded by the old value: re-runnable, never clobbers a manual edit.

SET lock_timeout = '5s';
SET statement_timeout = '60s';

UPDATE visa_types
SET name = 'E30A - Student KITAS (Primary & Secondary Education)', last_updated = now()
WHERE code = 'E30A' AND name = 'E30A - Student KITAS (Research)';

UPDATE visa_types
SET name = 'E30B - Student KITAS (Higher Education)', last_updated = now()
WHERE code = 'E30B' AND name = 'E30B - Student KITAS (Training)';

-- === ROLLBACK ===

UPDATE visa_types
SET name = 'E30A - Student KITAS (Research)', last_updated = now()
WHERE code = 'E30A' AND name = 'E30A - Student KITAS (Primary & Secondary Education)';

UPDATE visa_types
SET name = 'E30B - Student KITAS (Training)', last_updated = now()
WHERE code = 'E30B' AND name = 'E30B - Student KITAS (Higher Education)';
