SmartReport 1.0.67 - Windows x64 (client only; no server change)

This update is for the existing approved user profile. Keep the existing
SmartReport-1.0.9 profile and shortcut names; do not create or select a fresh
profile. The installer contains application files only.

Hours tab simplified:
- The shifts journal now takes the whole tab; a compact action/filter panel
  stays on top.
- Manual entry moved to a separate "Add shift" window (the "Add shift"
  button). All fields and validations are unchanged; a successful save
  refreshes the journal and closes the window, cancelling writes nothing.
- On a fresh start the journal shows the current calendar month in Stockholm
  time instead of a hard-coded October. The period chosen during a session is
  kept across tab switches and synchronization.
- Quick "This month" / "Last month" buttons next to the dates; a custom
  period still works.
- Shift actions, row selection, scrolling and closed-record protection are
  unchanged; the employee web and calculations are untouched.

No expense, shift, or period is confirmed by this release.

Roll back application files only; never replace newer working data with an
older database. Keep the verified rollbacks in installer/rollback/1.0.65 and
installer/rollback/1.0.66.