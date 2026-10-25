-- Retire the revision engine's two tables.
--
-- The revision system has no UI and no endpoints left (its code is dormant in
-- pipeline/revision/ for a possible revival). revision_reports /
-- revision_summary are therefore unused storage, and this file is the focused,
-- idempotent step that removes them. It is NOT part of consolidate.sql so it
-- can be run on a database that already went through the lessons-model
-- consolidation, and re-run harmlessly.
--
-- To revive the engine: recreate both tables (DDL in git history at
-- pipeline/studio/consolidate.sql before 2026-10-23) and re-add the
-- /api/revision/* endpoints; the questions/answers revision_* columns were
-- deliberately kept, so no data migration is needed.
--
-- Binding: no placeholders. consolidate.retire_revision() owns the transaction.

-- The flag first: check the audit trail before anything is removed, so a
-- reviewer sees what goes in the same log the migration prints.
DO $precheck$
DECLARE
  n_reports BIGINT;
  n_summary BIGINT;
BEGIN
  IF to_regclass('public.revision_reports') IS NULL
     AND to_regclass('public.revision_summary') IS NULL THEN
    RETURN;                       -- already retired: nothing to check
  END IF;
  SELECT count(*) INTO n_reports FROM public.revision_reports;
  SELECT count(*) INTO n_summary FROM public.revision_summary;
  RAISE NOTICE 'retiring revision_reports (% rows) and revision_summary (% rows)',
               n_reports, n_summary;
END
$precheck$;

DROP TABLE IF EXISTS public.revision_summary;
DROP TABLE IF EXISTS public.revision_reports;
