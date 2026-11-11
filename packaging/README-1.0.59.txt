SmartReport 1.0.59 - Windows x64

This update is for the existing approved user profile. Keep the existing
SmartReport-1.0.9 profile and shortcut names; do not create or select a fresh
profile. The installer contains application files only.
New fresh-profile shortcuts are not selected by default; keep and use the
existing approved-profile shortcuts.

Customer report:
- Select object and period, review the preview and Swedish translations, then
  save the compact PDF.
- The PDF separates ordinary hours, full recorded OB durations, and trips.
  It does not show unconfirmed customer prices or supplements.

Beijer incoming document proposals:
- "Fill form with proposal" selects an object only when its marking has one
  matching active object. Unmatched, archived, or ambiguous rows remain for
  manual selection.
- Applying a proposal fills the editable form only. Review every destination
  and amount; create an expense only with the separate expense action.
- For Beijer document 9, the two unique active matches are preselected, the
  unmarked 127.20 SEK row remains unassigned, and the proposed total is
  5,679.00 SEK after the stated 0.10 SEK rounding.

Invoice recognition:
- Registered issuer identity is preferred over an unlabelled buyer address.
- Invoice number/OCR text and stated rounding remain separate fields. For the
  reviewed Santander original, gross is 5,251.25 SEK, rounding is -0.25 SEK,
  and amount due is 5,251.00 SEK.

No expense, shift, or period is confirmed by this release. Historical customer
allocations are retained; supplement automation remains deferred. The employee
web form and payroll rules are unchanged.

The full test suite's known native Qt failure remains unresolved. Roll back
application files only; never replace newer working data with an older
database. Keep the verified rollback in installer/rollback/1.0.57.
