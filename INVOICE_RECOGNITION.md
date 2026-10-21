# Invoice Recognition and Net Allocations

## Release Status

On 2026-10-17 the owner authorized completing, pushing and deploying the current
**test** system only (no important data). Real email and any future production
environment are NOT covered. See `WORK_LOG.md` for the actual state. The text below
that says "do not push/deploy" is a historical record of earlier local stages.

`migrate_invoice_net_schema(connection)` is not run by desktop initialization. The
server runs it at startup only when `SMARTREPORT_UPGRADE_NET_SCHEMA=1` is set, after
`create_database_backup()`. Due-date columns are added additively by `init_cloud_db()`.
For any future production use it still needs a verified backup and maintenance window.

## Selective rollback of the document safeguards patch

From a clean working tree at the repository root, revert only commit
`3a0bf18f9fc2cf777052d3372d2e385b7f966dd2` with:

```powershell
git revert --no-edit 3a0bf18f9fc2cf777052d3372d2e385b7f966dd2
```

This does not revert the parent Beijer rounding-test commit
`4ce094d2045d2e8e6452fcca0eaf353ecc89d58c`. It changes repository history only;
it does not publish or deploy the result.

## Future mail intake and document retention (proposal; not enabled)

The current Loopia importer and review UI are a local test-only queue. They do
not write to the working expense register. Test originals are stored under
`%LOCALAPPDATA%\SmartReport\mail_faktura_trial\originals\`, with queue state in
`mail_queue.sqlite3`; this is not a production retention location.

The intended production flow is:

1. **Receive:** store each supported attachment once in an incoming area on the
   server's persistent document volume, and record mailbox, folder, UIDVALIDITY,
   UID, MIME-part index, filename, and SHA-256 in the import queue. A unique
   source-identity constraint makes retrying the same mail part idempotent; a
   content hash also prevents storing identical bytes twice.
2. **Recognize:** read the immutable original and save extracted fields,
   classification, and recognition status in the queue only. Recognition never
   creates or confirms an expense. Failed attempts retain the original for an
   explicit retry.
3. **Review:** require a person to open the original, review/correct the fields,
   and explicitly confirm an eligible invoice or receipt. Order confirmations,
   tax decisions, pro formas, and unclassified items cannot pass this step as
   invoices; an order confirmation requires the actual invoice or receipt.
4. **Save:** create the expense record only after that confirmation. Prefer
   retaining the same immutable original on the same persistent volume and
   linking it to the expense attachment. If a copy is necessary, verify its
   SHA-256 before committing the attachment metadata. Keep the queue item and
   original until the server acknowledges the saved record.

In addition to source identity and byte-level SHA-256, the expense API's
business-key check (supplier, invoice number, date, currency, and gross amount)
blocks matching invoices with different file bytes. Such matches should be
shown for human review, not silently discarded. A same-client retry is
idempotent. This target flow is a design only; no production mail import or
automatic transfer is enabled.

## Backup and recovery findings and proposed operations

The read-only provider configuration check on 2026-10-21 confirmed
`SMARTREPORT_DOCUMENTS_DIR=/data/documents` and
`DATABASE_PATH=/data/cloud_database.db`, with a 1 GB persistent disk mounted at
`/data` on `smart-report-server`. The dashboard showed daily disk snapshots for
October 15–21. No setting was changed and no restart, deploy, or disk restore was
performed. Persistence across restart/deploy follows Render's persistent-disk
contract, not a new destructive/restart test. See `WORK_LOG.md`.

The local full-backup patch adds `GET /api/admin/backup/full` (admin only).
It returns a ZIP containing `server_snapshot.db`, `manifest.json`, and
`documents/` with original `stored_name` paths. References come from the database
snapshot, including soft-deleted cards and hidden split containers, not the
active-only, limited UI listing. Inactive object links remain in the unchanged
snapshot too. Legacy primary-original references and unlinked files under the
document root are also retained. No automatic retention cleanup is enabled.

The existing `SmartReport.exe --backup <folder>` download workflow will use this
full endpoint once client and server are updated with explicit permission. Its
output includes the ZIP and a verified expanded copy; an old server without the
endpoint returns an error, never a silently partial fallback. The database-only
endpoint still exists but is not a full documents backup.

Offline recovery from a new archive into a new or empty TEST directory:

```powershell
python -m server_backup --restore "C:\TestBackups\backup.zip" --destination "C:\TestRecovery\restored"
```

The result is `server_snapshot.db`, `documents/`, and `manifest.json`; no manual
filename mapping is needed. The command validates the entire archive in a
staging directory before making the restored directory available. It checks
SQLite integrity and foreign keys, the database hash, database-to-file
references, and every original's SHA-256 and size. Existing non-empty
destinations are rejected. It never changes service configuration or points a
client at the restored database. Older partial exports are not accepted as
full archives.

An isolated regression exercise restored active/deleted/split cards, an
allocation to an inactive object, legacy and unlinked originals. IDs, statuses
and links survived; the active restored card served its original and Qt rendered
the test PDFs. Missing or corrupt source originals cause HTTP 409 with
`complete=false`; missing/corrupt archive data aborts restoration with a nonzero
CLI exit, without publishing a partial restored database. Deleted cards remain
deleted and their originals remain inaccessible through the ordinary card API.
SHA-256 detects inconsistency against the snapshot/manifest; it does not replace
authenticated encrypted off-server storage.

Proposed backup operations, not implemented:

- Every 24 hours, create a SQLite online snapshot and a manifest of every
  retained attachment, including database/attachment IDs, stored name, byte
  size, and SHA-256. Include soft-deleted records for the chosen retention
  period, or explicitly document their expiry.
- Store the encrypted backup in a separately administered off-server location
  (separate provider/account and credentials from Render), not only on `/data`.
- Retain 30 daily and 12 month-end copies. Verify database integrity, attachment
  hashes, file counts, and the manifest after upload; alert on any missing or
  mismatched item.
- Perform a full restore into an isolated environment quarterly and after
  backup/restore changes. Suggested service targets are RPO 24 hours and RTO
  4 hours, subject to owner approval and actual backup size.

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
- Document total after rounding = net + stated invoice VAT + separately stated rounding.
- Paid amount and remaining amount due are separate values, not substitutes for
  the document total. A missing paid amount is unknown, not zero and not inferred
  from a zero balance.
- For an ordinary unpaid invoice, remaining amount due equals the document
  total. For payment workflows, balance = document total - confirmed paid amount
  only when those values and the treatment of partial/advance payments are known.
- Current `gross_amount` is before rounding and `amount_due` is the balance field;
  a separate persisted paid-amount model is NOT implemented. Prepaid receipts
  therefore remain draft-only until separate implementation and acceptance.
- Allocation totals are checked against invoice net, not amount due.
- The UI distribution counter counts only rows with a selected object or explicit
  company destination. An amount proposed without a destination stays unallocated.
- Unknown per-order VAT stays unknown; there is no proportional tax allocation.
- Company expenses use a null object ID, never a synthetic company object.
- Buyer/order markers are not object codes. Company designation is per document;
  no buyer-name-to-company rule is stored.
- PDF text and character coordinates are read locally; missing text pages use
  local OCR. No third-party document processing service is used.

## Verification Limits

On 2026-10-17 the original two-page Beijer PDF was tested through the actual
desktop upload handler, local extraction, parser and Qt review form. Both pages
had embedded text; OCR was not used. The original SHA-256 was
`755ee1b95409571121c87468f61065a60f4e167a02834bf3d4781ff5417c5755`.

The isolated run checked extracted field values, two order rows, source-page
buttons, draft reopening, user-selected company destination/category, and local
confirmation persistence without duplicating the queued document. Object
matching used a temporary catalog, not the working catalog. The real PDF is not
committed; the portable regression records its relevant label/coordinate format
and also tests changed identifiers/amounts and repeated order totals.

Real-server synchronization, another device, and working-database migration
remain unverified and unauthorized. Local confirmation in the outbox does not
mean the server accepted it. Mocked OCR tests verify routing and parsing, not
the accuracy of an OCR engine on a real scan.

## Twelve-File Batch Follow-up

The complete `C:\invoices` batch (12 PDFs, 27 pages, 10 unique documents) was
retested through upload and Qt after correcting seller-role detection, PDF
rotation, right-aligned values, tax-total columns, currency and order markers.
All 96 expected fields matched the forms. Every invoice arithmetic check and
allocation sum matched; injected invalid rounding disabled confirmation in all
12 forms. The temporary test destinations were restored before saving drafts.

Three copies of Beijer 260407197227 are byte-identical. In one temporary desktop
database the first upload was confirmed locally; two other filenames and a retry
were blocked before a new form, leaving one queued confirmed document. API
regressions separately reject a repeated original with a new client ID and keep
same-client retries idempotent. No files were removed from the source folder.

Byggbox P422 is offered only for an exact unique match in the test catalog.
Absent rounding is left empty; it is not inferred from a difference between
totals. Per-order VAT is still unknown unless stated. This batch contains only
embedded-text PDFs and does not certify real scanned-document OCR.

Detailed local artifacts are under `reports/invoice_batch_20261017_174725/`.
These results do not authorize push, deployment, production migrations or use of
working databases.

## Corrections After the Frozen Holdout

The original `reports/review_stage_20261017/` and
`reports/holdout_baseline_20261017_180120/` are preserved as historical evidence.
The eight text PDFs have now been used for fixes and are no longer an unseen
acceptance sample. Their original visual expectations were kept separate from
the parser, and before/after comparisons are in
`reports/correction_stage_20261017/index.html`.

The Windows upload streams are closed before local file deletion. A confirmed
ACK increments `synced` independently of optional cleanup; cleanup failures are
reported separately. Missing/incomplete document identity in an ACK retains the
durable queue and original. Real loopback HTTP tests cover pre-send failure,
lost response after server commit, incomplete ACK, process termination before
local ACK, and retries in new processes. The receiving client downloaded every
original, checked SHA-256 and opened the PDF. See
`reports/sync_reliability_20261017/`.

Parsing fixes include buyer-role exclusion, legal forms beyond AB, fragmented
captions/money across close baselines, column boundaries and explicit VAT-rate
summary priority. Short invoice numbers are supported. Reverse-charge evidence
does not manufacture VAT: explicit zero is read, absent numeric VAT stays
unknown and requires review. Unstated currency is blank in the form and remains
UNKNOWN in a draft, never silently SEK.

Prepaid receipts and remaining balances are recognized as a distinct context.
The document total and remaining amount due are displayed separately. This
payment workflow is not fully supported yet: only draft saving is allowed, and
the API also rejects confirmation. It must not be bypassed by replacing a zero
remaining balance with the full document total.

The final original-file run processed 20 text PDFs / 36 pages: the original 12
and eight later text PDFs. It preserved all drafts/originals and manual mixed
allocations. A separate scanned receipt was excluded from OCR testing. These
process-level tests are NOT tests of two physical devices. Two real devices and
an approved isolated test server remain a separate prerequisite, and no
production launch, push, deployment or working-database migration is authorized.

## Final ACK Evidence and Reporting Limits

The final ACK audit also checks the complete mandatory attachment manifest.
For document saves, success requires a 2xx response, matching client/document
identity, a valid newer version for updates, and every required SHA-256 with
`has_original=true`. Missing, partial, foreign, stale or malformed ACKs retain
the queue and local originals with `synced=0`. Known original hashes are retained
when editing an existing uploaded document in either current form. This uses JSON metadata only;
no database migration is introduced.

The consolidated table and sanitized synthetic synchronization results are in
`reports/final_acceptance_20261017/`. The 96+64 comparisons cover eight invoice
fields, including invoice date but NOT payment due date. Supplemental due dates
were reported separately at that frozen stage: Qt did not expose/persist them as a
dedicated field, and raw parser extraction was incomplete. They must not be
represented as covered by the prior field metric.

AJ remains draft-only because its payment workflow is not fully supported, not
because zero remaining balance is an invalid document. Lifting the block needs
an agreed paid/partial-paid model, separate document total/payment/balance,
appropriate UI/API/isolated exchange tests and a separate user acceptance.
Two physical devices and then a genuinely new independent text corpus remain
future stages; the scanned receipt is a separate OCR stage. No test operations
are to be imported into working accounts.

## Local Stage Handoff

The reproducible local-stage evidence is preserved under
`reports/local_stage_handoff_20261017/`; the original baseline remains separate.
At that handoff all 20 text PDFs contained an explicit payment due date: 9 were extracted by the
parser, 11 were missed, and none lacked the date in the original. Due-date support
in Qt was unfinished and was not covered by 96+64 field checks.
The audit is AI-assisted original-document review, not independent human signoff.

The physical-device test plan is marked BLOCKED, not passed. The next unseen
text corpus requires human-confirmed expected values before its first run; any
post-fix repeat is regression evidence. AJ remains draft-only until a separately
implemented and accepted payment data model. No handoff artifact authorizes
production operations, migration, mail access, push or deployment.

## Payment Due Date Stage

Current local due-date evidence is in `reports/due_date_stage_20261017/`.
The original handoff archives and nine-of-twenty baseline were not overwritten.
Coordinate extraction now supports due-date columns, separate value rows,
alternative captions and rotated-page coordinates. All 20 original dates match
the prior manual original-PDF audit, and the other 160 field values remain equal.

Both current Qt expense forms have a separate due-date input. Provenance is
explicit, calculated, manual, ambiguous or absent. Calculation from unambiguous
payment terms is labelled calculated and requires review; conflicting/invalid
explicit dates are not replaced by payment-term guesses. Manual edits preserve
original evidence through draft reopening and local confirmation.

Fresh server schemas contain dedicated due-date/provenance/evidence columns.
There is NO automatic ALTER of existing document tables for this feature.
`due_date_v1` is advertised only when those columns exist. Unsupported servers
leave the outbox pending. Older clients omitting due fields preserve them, but
cannot bypass a stored unresolved review requirement. The ACK must echo the saved
date and provenance before the client clears its queue.

Actual loopback HTTP testing used fresh test schemas, two client processes and
separate databases: upload, manual correction, draft reopening, confirmation,
lost response, new-process retry, receiving-client download, a second date edit
and stale-version rejection. One document and one original remained. This is NOT
a test of two physical devices. The original PDF was unchanged.

No separate paid-amount storage was introduced; AJ is still draft-only in Qt/API.
Production schemas, existing user databases, mail, push and deployment remain
unauthorized. Physical-device testing stays BLOCKED; independent unseen text
invoices and real scanned-document OCR remain separate stages.

## Non-Invoice Classification Safeguards

Documents headed `ORDERBEKRÄFTELSE` are classified as order confirmations.
Documents headed `Beslut om trängselskatt` are classified as tax decisions.
Receiver/customer details from these documents are not proposed as supplier or
invoice identity. Neither type can be confirmed as an invoice expense; an order
confirmation requires the actual invoice or receipt.

Existing saved mail-queue recognition results are not refreshed automatically.
The test viewer updates one selected result only after the user confirms the
explicit reprocess prompt. This updates recognition data only; it does not
confirm an expense, create an accounting operation, or synchronize anything.