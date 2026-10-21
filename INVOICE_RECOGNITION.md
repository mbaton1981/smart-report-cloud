# Invoice Recognition and Net Allocations

## Isolated Loopia Mail Trial (2026-10-20)

Development branch: `feature/mail-two-inboxes` (historical branch name).
The only supported mailbox is `faktura@byggerochrenoverar.se`.
`info` is not connected or inspected. Forwarding is done by the owner.
Stable 1.0.8, its installer, the working database and production are not changed.

Run from a **local interactive PowerShell terminal** in the project directory:

```powershell
python .\mail_import.py fetch
python .\mail_import.py review
python .\mail_import.py retry
```

`fetch` prompts for the Loopia password without echo; there is no command-line,
environment, settings-file or saved-password option. Do not paste the password
in chat or a shell command. No password is persisted. Each fetch requires entry
again. There is no background scheduler or automatic startup.

Connection: `mailcluster.loopia.se:993`, IMAP over TLS with certificate and hostname
verification. Only `INBOX` is selected, using read-only `EXAMINE`; body downloads
use `BODY.PEEK[]`. The importer never requests flag changes, MOVE, DELETE, CLOSE
or EXPUNGE. It does not send mail.

Each fetch is limited to the rolling last seven days at its start, based on the
server's `INTERNALDATE`, **not** the sender's Date header. IMAP's day-granular search
includes one extra day for timezone boundaries; exact arrival-time bounds are
checked before body download. A forwarded old invoice arriving this week is in
scope. A message over 30 MiB is reported and skipped, with other messages
continuing. There is no arbitrary limit on the number of messages within the window.

The default isolated directory is:

```text
%LOCALAPPDATA%\SmartReport\mail_faktura_trial\
    mail_queue.sqlite3
    originals\<SHA-256>.<extension>
    runs\<timestamp>_<unique-id>.json
```

Every `fetch` prints a per-run summary and saves the same counters in `runs`,
including interrupted/failed runs. Counters distinguish checked messages (metadata
requests), messages inside the exact seven-day window, all found attachments
(including unsupported types), supported PDF/image parts, newly saved documents,
exact-hash duplicates and processing errors in this run. A new document whose
recognition failed counts as both added and errored: its original remains saved.
Unconfirmed VAT/review warnings are not processing errors. Existing failed
documents are also shown separately in the overall queue totals.

For the repeat-fetch check, run `fetch` again using the same data directory.
With unchanged mailbox contents, the second summary must show zero added
documents; previously obtained attachments count as skipped duplicates.
Newly arrived messages may legitimately add documents. The seven-day interval
is rolling, so its boundaries are recorded in each private run report.
Run reports contain no password or message body.

An optional `--data-dir` must point to a new empty directory or an existing mail
trial queue. Other SQLite databases are rejected by application ID/schema version.
Desktop database/profile/document environment overrides are not used. There is
no import of `app.py`, `server.py`, accounting outbox or synchronization code.
Originals and queue contents are private financial data; do not commit or share
them, and do not redirect `review` into public logs.

`review` prints local JSON cards with recognition results, originals, all source
occurrences and possible duplicate IDs. Sources retain mailbox, folder,
UIDVALIDITY/UID, MIME-part index, Message-ID, sender, subject, arrival date,
original attachment filename and MIME type. Exact attachment hashes share one
document, including after repeated fetches, renaming, forwarding and UIDVALIDITY
changes. Different files with the same nonempty supplier/invoice number/date/
gross/currency key stay separate and receive symmetric review warnings.

Extraction reuses `invoice_processing.recognize_invoice` and the existing
PDF/local OCR implementation, with no parser or monetary-formula changes.
Proforma, tax decisions and order confirmations are classified separately in
this staging queue. Receiver/customer identity is not retained as supplier data
for tax decisions or order confirmations. All successful documents remain
`requires_review`; an order confirmation is not an approved invoice. In desktop
review, order-confirmation detection blocks confirmation as an expense; the
actual invoice or receipt must be imported and checked. Errors retain the
original and are visible as `failed` or source issues. `retry` retries failed/pending recognition locally,
without logging into mail. Re-running `fetch` retries source-download issues.
Network/filesystem/database failures stop explicitly rather than claiming success.
Exit codes: 0 = no current failures, 1 = run stopped, 2 = document/source failures.

There is deliberately **no confirmation-to-accounting action**, expense creation,
production API call or synchronization. Local review is not accounting approval.
This first-stage queue is a CLI trial, not a change to the installed desktop UI.

Verification uses fake IMAP and temporary databases/originals, plus the real
existing extractor on generated PDF, PNG scan, receipt, signed credit invoice
and proforma fixtures. A repeated/forwarded attachment produces one document and
multiple sources. The synthetic scan extracts invoice number, net and amount due,
but its OCR joins `Total VAT` into `TotalVAT`: VAT/gross remain unknown with a
review reason, not invented. This does not certify accuracy on real supplier
scans.

### Offline original-file audit

The owner subsequently ran two real Loopia imports with local password entry.
The saved successful run reports each show 21 checked candidates, 18 messages
inside the exact window and 20 supported attachments; first import added 20,
repeat added zero/skipped 20, both completed with zero errors. A separate earlier
aborted attempt (`UnicodeEncodeError`, before any messages were read) is retained
as historical evidence, not counted as an error in those successful runs.

`mail_review.py` creates a read-only offline HTML/JSON comparison, with a strict
field allowlist and optional SHA-bound assessments from a local JSON file.
It validates queue integrity/source links, unique hashes and every original.
It never logs into mail, imports accounting code or modifies the queue/originals.
The output deliberately omits raw OCR, email addresses/subjects/Message-ID,
shipping addresses, phone numbers, card/account details and vehicle identifiers.
Commercial document fields remain private. No remote assets, scripts or forms
are used. Full originals are only accessible via an explicit local file link.
The integrated VS Code browser may reject files outside trusted folders; open
the generated HTML in the ordinary system browser instead, without changing trust.

The initial 20 originals (33 PDF pages plus a logo image) were visually inspected
locally and compared with their stored extraction. The private assessment file
and report are outside the repository, in the isolated mail trial directory:
`review_assessment.json`, `review/index.html`, `review/audit.json`.
The original extraction stays unchanged. Corrected/verified values appear in
a separate report column, not as silent edits to the test or working database.

Findings: 11 ordinary invoices have matching requested fields, including explicit
rounding where present; three receipts need non-invoice review, four tax decisions
were mistaken for invoices with the buyer as supplier, one order confirmation
was mistaken for an invoice and one signature logo was mistaken for a document.
The existing parser misses some Bygghemma/Power receipt fields; these were verified
in the report, not claimed as parser fixes. The ELWO order number is not invented
as a receipt number. Unknown VAT on tax decisions remains unknown, not zero.
Receipt totals do not become unpaid balances.

Mail-only classification now distinguishes tax-decision/order-confirmation
headings and leaves uninformative images unclassified for review; nothing is
discarded. Shared financial parsing and stable application/server code are
unchanged. Existing 20 queue results are preserved to show before/after evidence.

Development-only report regeneration:

```powershell
python .\mail_review.py --assessment "$env:LOCALAPPDATA\SmartReport\mail_faktura_trial\review_assessment.json"
python -m unittest discover -s tests -p "test_mail*.py" -v
```

The generated report groups 11 checked invoices, eight documents requiring a
decision before any future accounting (three receipts/four tax decisions/one
order confirmation), and one non-document logo. This is extraction verification,
not accounting approval, proof of payment or cross-check against the working ledger.

### Receipt parser fixes and multipage review

The shared existing parser now handles the observed receipt layout causes:

- Bygghemma: the product-table `Totalt` heading is not a document-total candidate;
  `Följesedeldatum` is recognized even when layout grouping joins its cell to
  adjacent buyer text. The unique actual total is used with stated VAT by the
  existing gross-minus-VAT rule. No receipt number is invented from an order ID.
- Power: `KONTANTNOTA` is recognized as a receipt and its adjacent receipt number
  is read; date-plus-time values are allowed for receipt document dates. A
  `TOTAL (Inkl. moms SEK …)` row provides explicitly printed VAT and gross, not
  an inferred rate. A footer-only company name can identify the cash-receipt
  seller when an organization-number caption is also present. No unpaid debt is
  created from a paid receipt total.

Portable coordinate tests use changed suppliers, dates and amounts as well as
missing VAT and conflicting totals. Reprocessing all 20 local originals confirms
that Bygghemma/Power now match the visually verified fields; requested fields
on all 11 ordinary invoices are unchanged. Originals and initial queue results
are never overwritten.

`recognize_invoice` already passed all pages of each attachment into
`parse_invoice_pages`, which already retained coordinate-based order proposals.
Previously the mail CLI did not show these proposals and did not invoke the
existing `allocation_vat_from_items` verification. It now reuses that helper:
when every item, order net and document net/VAT reconcile, item evidence is
attached to the existing proposals. Missing/conflicting evidence yields visible
warnings, never proportional VAT or a guessed object.

The existing three-page Beijer 260407197227 was verified as one invoice:
five orders, 17 item rows across all three pages, including one order continuing
from page 2 to page 3. Order net totals sum to 2,206.00 SEK and item VAT to
551.50 SEK; document gross is 2,757.50, explicitly printed rounding +0.50,
amount due 2,758.00. Repeated summary totals are not added as items/orders.
The viewer uses existing `net_invoice_issues` for validation. It shows the
original project markers, each order/items/source pages and totals. No working
object catalog is read: all object destinations remain explicitly unselected.
Unprefixed markers are not silently normalized to existing object codes.
There is no confirmation action or accounting transfer in this trial.

The local review now displays four columns (initial extraction, current parser,
verified original values, field label), with all rendered original pages beside
them. Order-page links navigate the original pane. A compact viewport stacks
the panes; a desktop-width viewport shows them side by side. Local page images
in `review/pages/` contain the full private original and must not be published.
They are derivative previews; original hashes are checked before rendering.

Every review export appends an immutable timestamped JSON snapshot under
`review/history/`. The previous standalone report is preserved as the initial
snapshot. The view lists timestamped initial extraction, verified corrections
and refreshed parser field changes; the latest report can be rebuilt without
losing its current recognition. Reviewed values do not automatically change when
the parser changes. Full order/item evidence is retained in the snapshots.
`--refresh` reprocesses only saved originals and does not connect to IMAP:

```powershell
python .\mail_review.py --refresh --assessment "$env:LOCALAPPDATA\SmartReport\mail_faktura_trial\review_assessment.json"
```

Verification: full isolated unittest suite, 144 tests passed; actual 20-original
regression comparison; local browser check of 20 cards, the three Beijer page
images, desktop side-by-side layout, page-3 navigation and visible history.
The temporary loopback-only browser verification server is stopped afterwards;
the persistent report opens directly in the system browser without a server.

```powershell
python -m unittest discover -s tests -p "test_mail_import.py" -v
```

## Release Status

### Isolated mail allocation editor (2026-10-20)

Acceptance was subsequently repeated on a **separate complete copy** of the
20-document queue. Each real document was opened, saved as a trial-only draft,
closed and reopened. The source queue and originals remained unchanged. The
trial editor now retains document type in draft revisions and shows deterministic,
type-specific notices. The report displays the same notices without exporting
arbitrary OCR or mail error messages. Unknown VAT and missing amount due remain
unknown; receipt totals are not converted into debt. No default desktop save
behavior or accounting formulas were changed by these notices.

Actual coverage: 11 text invoices, three text receipts, four text tax decisions,
one text order confirmation and a logo PNG. **No real financial scan, credit
invoice or proforma is present.** OCR of the logo is not a financial scan
acceptance sample. Synthetic scan/credit/proforma tests pass but do not establish
recognition quality for real documents.

Existing accounting transport was independently exercised on new temporary
databases with synthetic originals and actual loopback HTTP: normal send,
offline failure, lost post-commit response, incomplete ACK and process termination
before local ACK. New-process retries produced five single server documents;
the receiver verified every original hash and PDF. The server was stopped and
temporary databases cleaned up. The mail-to-accounting adapter remains disabled.
Full unittest regression: **149 tests passed**.

`mail_viewer.py` opens the saved mail trial queue using the **existing** desktop
invoice review form. `InvoiceReviewMixin` in `invoice_review.py` and document
gallery classes in `document_preview.py` are extracted shared components, not a
parallel allocation implementation. The desktop retains its original default
save flow; the trial adapter intercepts draft saving before desktop storage,
outbox or sync. The viewer never imports `app.py` or `server.py`.

The trial queue alone gains `trial_objects` (synthetic, visibly test-labelled
catalog) and append-only `trial_allocation_revisions` (document ID, original SHA,
timestamp, revision and draft payload). Reopening loads the latest draft;
optimistic revision checks reject stale writers. Imported document results,
source envelopes and original attachments remain unchanged. No working catalog
or working database is opened.

The shared form provides object/company selection, category and field editing,
add/remove allocation rows, original page navigation and existing net/VAT/due
validation. Exact unique marking matches only are preselected; unmatched or
duplicate codes need manual selection. A saved explicit unselected destination
stays unselected on reopen. Changing row net invalidates its VAT/item evidence,
without guessing replacement VAT. Incomplete drafts can be saved with visible
issues, but confirmation is disabled in both UI and trial storage.

The static report remains a report, not an editor. It now displays saved trial
destinations, a separate draft-value column and revision history beside originals.
The separate Qt queue window opens the common editable form:

```powershell
python .\mail_viewer.py --document 16
```

This is a developer entry point; the user need not execute commands when the
assistant opens the viewer. Closing the window does not delete drafts.

The actual three-page Beijer was assigned through the shared form, saved, closed
and reopened twice, including a company-destination revision. Its five orders
and 17 item records reconcile: net 2206.00 + VAT 551.50 + printed rounding 0.50 =
amount due 2758.00 SEK. P1022/P1034 matched exactly; 1035/1024 required manual
test-catalog choices. The final example includes company expenses for demonstration
only, not an approved real-world allocation. All initial import tables and 20
original SHA hashes were checked unchanged against a fresh pre-write backup.
Nothing is transferred to accounting or production.

On 2026-10-17 the owner authorized completing, pushing and deploying the current
**test** system only (no important data). Real email and any future production
environment are NOT covered. See `WORK_LOG.md` for the actual state. The text below
that says "do not push/deploy" is a historical record of earlier local stages.

`migrate_invoice_net_schema(connection)` is not run by desktop initialization. The
server runs it at startup only when `SMARTREPORT_UPGRADE_NET_SCHEMA=1` is set, after
`create_database_backup()`. Due-date columns are added additively by `init_cloud_db()`.
For any future production use it still needs a verified backup and maintenance window.

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