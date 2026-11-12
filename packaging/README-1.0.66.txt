SmartReport 1.0.66 - Windows x64 (client only; no server change)

This update is for the existing approved user profile. Keep the existing
SmartReport-1.0.9 profile and shortcut names; do not create or select a fresh
profile. The installer contains application files only.

Completes 1.0.65 for invoices kept with net allocations:
- A register invoice stored with net allocation (allocation_basis=net) that
  also has an incoming PDF copy now opens in its own register card ("Check
  invoice" / assignment), where draft saving and confirmation work with net
  amounts. The incoming review window cannot confirm such an invoice (the
  server rejects gross/object-cost payloads for net records with "update the
  client"), so it is no longer offered for them.
- The incoming window is still used for object-cost documents and for incoming
  PDFs that have no synchronized register record.

No expense, shift, or period is confirmed by this release.

Roll back application files only; never replace newer working data with an
older database. Keep the verified rollbacks in installer/rollback/1.0.64 and
installer/rollback/1.0.65.