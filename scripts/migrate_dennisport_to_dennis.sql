-- One-off migration: align payroll location values to 'dennis'.
--
-- Background: the payroll/manual-check UI historically submitted the location
-- as 'dennisport', while check_config (and the rest of the app) keys on
-- 'dennis'. The exact-match check_config lookup therefore missed and silently
-- fell back to the first row (Chatham) — so Dennis payroll checks printed with
-- Chatham's name, address, and bank account number.
--
-- The code now normalizes 'dennisport' -> 'dennis', but existing rows still
-- carry the old value. This brings them in line so they (a) resolve to the
-- correct Dennis check config on reprint and (b) show up under the now-'dennis'
-- payroll filters.
--
-- SAFETY: back up the DB before running (see CLAUDE.md backup policy).
-- Idempotent — safe to run more than once.

UPDATE payroll_runs   SET location = 'dennis' WHERE location = 'dennisport';
UPDATE payroll_checks SET location = 'dennis' WHERE location = 'dennisport';
UPDATE manual_checks  SET location = 'dennis' WHERE location = 'dennisport';

-- Verify afterward (should all return 0):
--   SELECT COUNT(*) FROM payroll_runs   WHERE location = 'dennisport';
--   SELECT COUNT(*) FROM payroll_checks WHERE location = 'dennisport';
--   SELECT COUNT(*) FROM manual_checks  WHERE location = 'dennisport';
