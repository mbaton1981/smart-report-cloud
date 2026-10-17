# Invoice Recognition and Net Allocations

## Deployment Is Not Authorized Yet

This change has only been exercised against temporary databases and local test
clients. Do not push, deploy, run the working desktop client, or migrate working
databases without the owner's separate approval.

`migrate_invoice_net_schema(connection)` is deliberately NOT called by
`init_cloud_db()` or desktop initialization. It requires an explicitly selected
connection, no active transaction, a verified backup, and an approved maintenance
window before any production use.

The migration adds document rounding, amount due, and allocation basis. It rebuilds
the allocation table so a net allocation can have unknown gross/VAT, and adds an
explicit destination and optional category. Existing IDs, monetary values and
object links are copied unchanged in one transaction; a foreign-key violation
rolls the transaction back. SQLite table rebuilding requires an exclusive writer
window and enough disk space for both the backup and the replacement table.

The server advertises `net_allocations_v1` only when this schema is present. Until
then the desktop retains net documents and originals in the existing outbox; it
does not silently convert net amounts to gross or apply a migration remotely.

## Financial Contract

- Invoice gross before rounding = net + stated invoice VAT.
- Amount due = net + stated invoice VAT + separately stated rounding.
- Allocation totals are checked against invoice net, not amount due.
- Unknown per-order VAT stays unknown; there is no proportional tax allocation.
- Company expenses use a null object ID, never a synthetic company object.
- Buyer/order markers are not object codes. Company designation is per document;
  no buyer-name-to-company rule is stored.
- PDF text and character coordinates are read locally; missing text pages use
  local OCR. No third-party document processing service is used.

## Verification Limits

The Beijer regression fixture is generated synthetic PDF data using the owner's
control values. It is NOT the original PDF. The original attachment/path was not
available during implementation, so original-file acceptance and real-device
synchronization remain separate checks. Mocked OCR verifies routing and parsing,
not the accuracy of an OCR engine on the owner's scan.