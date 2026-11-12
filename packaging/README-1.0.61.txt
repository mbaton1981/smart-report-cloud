SmartReport 1.0.61 - Windows x64

This update is for the existing approved user profile. Keep the existing
SmartReport-1.0.9 profile and shortcut names; do not create or select a fresh
profile. The installer contains application files only.

Manual expense upload:
- A document is accepted for any invoice date, regardless of the period shown
  in the register. The invoice date is never replaced by the import date.
- After an import or a duplicate original is detected, the matching card is
  opened by its permanent ID. A duplicate never creates a second expense.
- If the register period, object, status filter or search hides the document,
  the program says so and offers "Show document". After you agree, the filters
  are widened (the document date is not changed) and exactly that record is
  selected and opened. "Existing card opened" is shown only when it is opened.
- The selected row is kept by ID across table refreshes and background
  synchronization.
- Queued (not yet synchronized) documents use the same period rule as the
  server, so a document does not disappear when synchronization finishes.

No expense, shift, or period is confirmed by this release.

Roll back application files only; never replace newer working data with an
older database. Keep the verified rollback in installer/rollback/1.0.60.
