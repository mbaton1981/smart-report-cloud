SmartReport 1.0.71 - Windows x64 (client only; no server change)

This update is for the existing approved user profile. Keep the existing
SmartReport-1.0.9 profile and shortcut names; do not create or select a fresh
profile. The installer contains application files only.

Expenses:
- Object-filter totals include only the selected object's allocation. The full
  invoice amount is shown separately.
- Known net amounts remain visible when allocation tax values are unknown.
  Unknown values are not replaced with zero and no tax rate is assumed.
- Per-allocation rounding is shown separately from net and VAT, and invoice
  rounding remains separate from the invoice amount.
- Re-saving unchanged incoming allocations preserves their known net, VAT, and
  rounding values. Changed document totals or allocations do not reuse stale
  tax values.

No server deployment or server-side change is part of this release.

Keep the verified rollback installer in installer/rollback/1.0.69. Roll back
application files only; never replace newer working data with an older database.
