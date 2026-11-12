SmartReport 1.0.64 - Windows x64 (client) + server update

This update is for the existing approved user profile. Keep the existing
SmartReport-1.0.9 profile and shortcut names; do not create or select a fresh
profile. The installer contains application files only.

Completes the 1.0.63 fix for a deleted invoice uploaded again:
- The restore now happens in the same synchronization pass, so the restored
  card is available right after "Save draft" instead of waiting for the next
  synchronization; the selected row follows the restored record.
- The document type is now persisted on the updated server (document_type
  column, feature document_type_v1) and survives synchronization for all
  document kinds, not only recognised invoices.
- The invoice review dialog has a new "Fill form from proposal" button: rows
  whose PDF marking matches exactly one active object get that object; unknown
  or ambiguous markings stay for manual selection. Nothing is saved or
  confirmed by the button.

No expense, shift, or period is confirmed by this release; saving a draft does
not confirm an expense and unknown objects are never chosen automatically.

Roll back application files only; never replace newer working data with an
older database. Keep the verified rollbacks in installer/rollback/1.0.62 and
installer/rollback/1.0.63.