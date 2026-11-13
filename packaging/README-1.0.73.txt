SmartReport 1.0.73 - Windows x64 (client only; no server change)

This update corrects receipt parsing for compact dates, separated receipt/order
identifiers, Swedish net/VAT/gross captions, USD receipts and receipt headings
that are missing from the PDF text layer. Unknown VAT remains unknown and is
shown for review. Allocation proposals keep unrecognized text markers visible
without assigning them to an object or company costs.

Install the application files only. Keep the existing SmartReport-1.0.9 data
profile and update the two existing SmartReport shortcuts; do not create or
select a fresh profile. Only the explicit review/transfer action confirms a
cost. No server deployment or server-side change is part of this release.

Keep the verified rollback installer in installer/rollback/1.0.69. Roll back
application files only; never replace newer working data with an older database.
