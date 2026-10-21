# OB intervals: migration and rules

## Windows editing fix (1.0.18-rc1)

The Windows distribution includes `tzdata` and its IANA resources. The configured
`Europe/Stockholm` zone is unchanged; Windows must not substitute a fixed offset
because summer/winter transitions affect actual interval length.

Editing an existing OB shift without changing its date, employee or interval
retains its stored hours, base rate and segment rows on both client and server.
Changing the interval still calculates it using the current rules. Transport is
one checkbox per shift (work and OB), never per calculated segment. Existing
nonbinary transport values retain their numeric editor and value. Numeric
editors have explicit visible up/down arrows in the dark theme.

No additional database migration is required for this fix. Shift pull responses
include the last accepted operation ID so a lost push acknowledgment can be
recognized on retry without a duplicate or false conflict; unrelated remote
operations still cause a version conflict.

`tests/test_shift_edit.py` exercises the actual Qt editor, retained segment rows,
server edits, sync roundtrips, lost acknowledgments and genuine conflicts.
The packaged local-only `--diagnostic shift-edit` action also verifies the
installed timezone database and spring/autumn transitions against an isolated
local server. It must never use the working profile or cloud server.

The OB schema migration is intentionally not called by `app.py` or `server.py` at startup. Existing work, ATF, sick, salary, and report paths continue to work before migration. OB create/edit/sync endpoints return an explicit error until the target database has been migrated.

## Desktop SQLite

From the desktop project directory, first close Smart Report and run:

```powershell
python migrate_ob.py --database smart_report.db --backup-dir backups/ob-migration
```

If `SMARTREPORT_DB_FILE` points elsewhere, pass that exact file as `--database`. The script writes a SQLite-consistent backup named `<database-stem>_before_ob_<UTC-date>.db` before opening a schema transaction. For `/data/cloud_database.db` on 2026-10-12 this is the requested `cloud_database_before_ob_2026-10-12.db`. It is safe to rerun on a later date; it does not rewrite old shift rows. If that day's backup already exists, the command stops rather than overwriting it.

## Server SQLite

Do not run this command against production until the Render database path and a restorable backup have been confirmed. In a one-off shell for the server environment, pass the exact `DATABASE_PATH` value and a backup directory on persistent storage:

```sh
python migrate_ob.py --database "$DATABASE_PATH" --backup-dir /path/to/persistent/backups
```

This project currently uses SQLite. The migration does not run automatically during deploy. No Render deploy or production migration is performed by this change.

## Rollback

Stop writers, preserve the post-migration database for diagnosis, then restore the pre-migration backup created by the command. Restoring the backup is the rollback; the migration never drops or renames an existing field.

## Schema additions

The migration adds nullable `start_time`, `end_time`, and `ob_base_rate` columns to existing `shifts` and/or `incoming_submissions` tables when present. It creates `ob_segments`, keyed to the stable `sync_id` as `shift_id`, plus a unique segment key and lookup index. It also records migration version/time in `schema_migrations`. Old rows and old payloads remain valid.

## Initial rules

`ob_rules.json` is the single configurable source for the desktop and server calculator. The supplied weekday evening and weekend percentages are implementation inputs, not a certification of a Swedish kollektivavtal. Weekday 00:00–07:00 is explicitly unpaid pending confirmation (`paid: false`, multiplier 0). Confirm all periods, holidays, rounding, and rates against the applicable agreement before using results for payroll or invoicing. Holiday overrides are configured by ISO date. Time calculations use `Europe/Stockholm`; nonexistent local DST times are rejected and ambiguous times follow the configured `earlier` policy.