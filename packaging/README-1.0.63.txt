SmartReport 1.0.63 - Windows x64

This update is for the existing approved user profile. Keep the existing
SmartReport-1.0.9 profile and shortcut names; do not create or select a fresh
profile. The installer contains application files only.

Deleted invoice uploaded again (needs the matching server update):
- A draft saved after deleting and re-uploading the same PDF was shown as a sync
  conflict ("such invoice is already in the archive"). The server matched the
  soft-deleted record by invoice data before it checked the original by hash.
  Deleted records are now ignored by that data check, so the server answers
  deleted_original, and the client restores the same record (same ID, same
  client_id, current version, same original) with the user's edits. No duplicate
  is created and version checks are not bypassed.
- Assigned: shown only when at least one allocation has a chosen object or
  company. Unselected rows no longer show "Assigned".
- Type: the invoice type is stored in the queued draft; a recognised invoice
  (supplier, number, amount) is shown as invoice instead of "type not set".
- Summary: one invoice present both in the local queue and the server register
  is counted once.

No expense, shift, or period is confirmed by this release; saving a draft does
not confirm an expense and unknown objects are never chosen automatically.

Roll back application files only; never replace newer working data with an
older database. Keep the verified rollback in installer/rollback/1.0.62.
