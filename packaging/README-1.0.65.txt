SmartReport 1.0.65 - Windows x64 (client only; no server change)

This update is for the existing approved user profile. Keep the existing
SmartReport-1.0.9 profile and shortcut names; do not create or select a fresh
profile. The installer contains application files only.

Incoming documents of an invoice that already exists in the register:
- "Count as expense" no longer fails with "such an invoice already exists in
  the register or queue" when the incoming PDF is byte-identical (SHA-256) to
  the original of an existing register record. The incoming card is linked to
  that record's client_id and the transfer UPDATES the existing record with a
  version check; a second expense is never created.
- Real duplicates are still blocked: the same invoice data with a different or
  unknown original keeps requiring manual reconciliation, and an original
  already linked to another incoming card is refused.

No expense, shift, or period is confirmed by this release.

Roll back application files only; never replace newer working data with an
older database. Keep the verified rollbacks in installer/rollback/1.0.63 and
installer/rollback/1.0.64.