# Invoice Recognition and Net Allocations

## Draft-only automation implementation — LOCAL, NOT DEPLOYED (2026-10-21)

The isolated implementation in `feature/isolated-mail-automation` supersedes the
historical automation proposal below. No installed desktop, live mailbox,
working profile, Render configuration or existing queue is changed. The API
is absent unless explicitly enabled. The backup patch is not included.

### Components and boundaries

- `mail_intake.py`: separately authenticated `/api/mail-intake` Blueprint,
  disabled by default. A dedicated random bearer token (at least 32 characters,
  recommended `secrets.token_urlsafe(48)`) must differ from the expense API key.
  It does not authorize any expense endpoint. Browser review is read-only and
  additionally available to the existing authenticated admin session.
- The explicit absolute `SMARTREPORT_MAIL_INTAKE_DIR` owns `intake.sqlite3` and
  `originals/<sha256>.<extension>`. No expense database migration is needed.
  The queue has its own application ID/schema version; unrelated databases and
  mailbox identity changes are rejected, not overwritten.
  This is an intentional refinement of the old `/data/documents` proposal:
  intake originals stay under `/data/mail_import/originals`, entirely separate
  from confirmed expense originals. Both are on the web service's existing disk,
  never on Cron's ephemeral filesystem.
- `mail_worker.py`: one-pass IMAPS/TLS worker, explicit credentials from environment,
  read-only INBOX and `BODY.PEEK[]`. No Qt, desktop imports, flags, movement,
  deletion, CLOSE or EXPUNGE. HTTP redirects are rejected; HTTPS is mandatory
  except explicit `--allow-local` for loopback-only tests.
- Receiver validates message size (20 MiB), SHA-256, MIME/extension/file magic.
  Supported originals: PDF, PNG, JPEG, TIFF and BMP. Unknown or invalid attachments
  remain visible in receipt history without becoming drafts or expenses.
- Receipt, attachment/source rows and pending drafts commit atomically, after
  original file fsync/atomic replacement (directory fsync on POSIX). Recognition
  is a separate resumable operation. A killed recognition request leaves the
  draft pending; later passes resume it without refetching the message.
- Default recognition runs in a separate process with a 90-second timeout,
  a 20-page PDF limit and a 20-million-pixel page/image limit. Extraction uses
  the existing invoice parser, including payable/rounding/string reference and
  non-invoice safeguards. Missing OCR dependencies, corrupt/encrypted PDFs,
  limits and extraction failures are visible as failed recognition with the
  original retained. They are **not** successful invoices.
  The receiving web service uses `requirements-mail.txt`, which includes the
  existing optional `rapidocr_onnxruntime` engine in addition to normal server
  requirements. The headless forwarding worker does not need OCR.
  Verify that receiving build command/model resources in staging; never
  interpret a received scan as recognized when OCR is unavailable.
- Recognition only processes pending drafts, at most ten per pass. Already
  recognized/failed results are never automatically overwritten. Operator-driven
  correction/reprocessing of this new server queue is not provided by this API.
- Same SHA-256 reuses a draft while every distinct
  mailbox/UIDVALIDITY/UID/part retains its source. Message-ID is history, not a
  uniqueness key. Different PDFs with the same normalized supplier and invoice
  number stay separate; **both** receive a manual-review conflict flag even if
  dates/totals differ. Missing identity fields require manual inspection;
  this is candidate detection, not proof of invoice uniqueness.
- `/api/mail-intake/review` displays receipt history separately from recognition,
  original next to fields, missing values, OCR/reference and conflicts.
  An enabled server adds **Почта — черновики** to the web expense header.
  This page has **no confirmation, allocation or expense-writing controls**.
  It is not an automatic desktop-queue bridge: editing/object selection/manual
  handoff from these server drafts into the expense workflow remains a separate,
  explicitly approved integration. Existing manual desktop intake is unchanged.

### API and recovery contract

All worker calls require `Authorization: Bearer <dedicated token>`; no token in
URLs, logs or source. Successful responses use `Cache-Control: no-store`.

| Endpoint below `/api/mail-intake` | Purpose |
| --- | --- |
| `POST /lease`, JSON `mailbox` | Acquire exclusive 180-second lease with monotonic fence |
| `POST /control` | Renew, set UIDVALIDITY epoch, advance a fully scanned UID window, finish or report failure |
| `POST /messages` | Raw `message/rfc822`, receipt identity/lease query parameters, `X-Content-SHA256`; durable receipt ACK |
| `GET /messages?after=<id>` | Receipt metadata and attachment outcomes, pages of 50 |
| `GET /pending`, `POST /recognize` | Resume up to ten pending drafts; fenced result commit |
| `GET /drafts?after=<id>` | Persisted fields, recognition state, conflict flags, source history; pages of 50 |
| `GET /drafts/<id>/original` | Authenticated, checksum-verified original |
| `GET /review?after=<id>` | Read-only browser review, pages of 50 drafts |
| `GET /status` | Cursor, epoch, last attempt/success/error, backlog span, pending/failed/incomplete/conflict counts |

`SMARTREPORT_MAILBOX_ID` is the SHA-256 returned by
`mail_intake.mailbox_key(host, username, "INBOX")`, computed from the approved
mailbox configuration. The receiver pins this value in its queue; a worker
for another account cannot reuse its cursor. No credentials enter the hash.

The worker renews its lease every 30 seconds, including during recognition.
Every durable write checks owner, fence and expiry. A simultaneous worker gets
423; an expired/stale writer gets 409 and cannot commit. After a hard kill,
wait for lease expiry (at most 180 seconds), then run again.

The authoritative cursor lives on the receiver, not on Cron. The worker snapshots
UIDNEXT, searches bounded numeric UID windows (500 by default), processes ascending
UIDs and checkpoints each received message. Only fully scanned windows advance
over missing/deleted UIDs. Defaults: at most 100 messages/100 windows per pass.
There is **no seven-day cutoff**: later runs catch up every still-present missed
message. Mail deleted from the mailbox before receipt cannot be recovered by IMAP.
`backlog` is a remaining UID *span*, not a count of existing messages.

UIDVALIDITY changes reset the scan to UID 1 in a new epoch. Prior receipt history,
drafts and originals remain. Matching bytes reuse the draft with new sources;
different bytes never merge automatically. Regressing UIDNEXT in the same epoch
stops visibly rather than silently skipping data.

Oversized messages stop the pass with
`message_too_large_manual_review_required`; their cursor is **not** advanced.
Manual intervention is required before later messages can proceed. This deliberate
failure is preferable to hiding a skipped invoice. Corrupt originals similarly
fail explicitly, never acknowledge a false success or replace working data.

### Cron and monitoring preparation (inert)

`packaging/render-mail.yaml.example` is deliberately **not** `render.yaml`.
It has not been applied and creates nothing. It describes a five-minute UTC
worker and a five-minute monitor offset by two minutes. Both must be pinned to
the same separately approved commit as the API before provisioning.

Worker command: `python -m mail_worker`.
Monitor command: `python -m mail_worker --monitor --max-age 900`.
The monitor emits a privacy-safe JSON status and exits 1 for no completed pass,
last failure, age over 15 minutes, UID backlog, pending recognition, recognition
attention, conflicting PDFs or invalid attachments. `operational_healthy` and
`review_attention` explicitly distinguish intake failures from documents needing
review; either prevents an overall green `healthy` result. Configure and test Render failed-job
notifications during an approved staging launch; no notification channel is
currently connected. A monitor that itself stops needs Render's job-failure/
missing-run monitoring, not merely its own stdout.

Web-only environment, after approval:

```text
SMARTREPORT_MAIL_INTAKE_ENABLED=1
SMARTREPORT_MAIL_INTAKE_DIR=/data/mail_import
SMARTREPORT_MAIL_INTAKE_TOKEN=<new dedicated secret>
SMARTREPORT_MAILBOX_ID=<approved mailbox identity SHA-256>
```

Worker-only environment:
`SMARTREPORT_MAIL_API_URL` (HTTPS service base URL, no API suffix),
`SMARTREPORT_MAIL_INTAKE_TOKEN`, `SMARTREPORT_IMAP_HOST`,
`SMARTREPORT_IMAP_USERNAME`, `SMARTREPORT_IMAP_PASSWORD`.
The monitor needs only URL/token, never IMAP credentials. Cron needs no disk
access, expense API key, desktop profile or cloud database credentials.
Recognition requests can take 90 seconds; the approved receiving web process
needs `pip install -r requirements-mail.txt`, a Gunicorn timeout above this limit
(e.g. 120 seconds), and staging resource
checks. The worker HTTP read timeout is 150 seconds. These server settings have
**not** been changed.

### Isolated acceptance and first controlled launch

New focused suite:
`python -m unittest discover -s tests -p "test_mail_automation.py"`.
It uses generated emails/PDFs, a strict in-memory IMAP fixture and a real
loopback HTTP server; a separate subprocess also imports the actual server
against temporary cloud/intake databases. No live IMAP connection is made.

Coverage includes actual PDF extraction through the worker/API to reopened
fields and source original, a rasterized invoice through actual local OCR,
order blocking, corrupt documents, receipt-vs-blob
identity, different-PDF conflicts, lost ACK after commit, restart/catch-up across
UID gaps and limits, UIDVALIDITY, simultaneous lease acquisition, renewal/expiry/
stale result rejection, recognition resumption, checksum/auth/size/MIME limits,
read-only escaped browser review, monitor healthy/stale states, and zero expenses.
Existing accepted invoice examples/Balance/manual user tests are not repeated.

First controlled launch, **only after new explicit permission**:

1. Approve the exact local commit, hosting cost/resource/timeout settings and
   a dedicated **test mailbox**, containing generated/copied test documents only.
   Keep the working desktop/profile/old version untouched.
2. Deploy the API to a separate staging web service with its own disk/database,
   unique intake secret and mailbox identity. Verify default-off/unauthorized
   responses first, then enable only in staging. Production stays disabled.
3. Run once manually, **no Cron yet**:
   `python -m mail_worker --max-messages 5 --max-windows 1`.
   Inspect receipt history, original/fields and conflicts in the admin review.
   Verify zero expenses and unchanged message flags.
4. Repeat the pass, interrupt one pass, wait out the lease and resume. Verify no
   duplicate draft for identical bytes, separate conflicting PDFs, retained
   sources/original hashes and a green monitor with honest recognition counters.
5. Only after acceptance enable the staging Cron/monitor, test one failed run and
   actual notification delivery, and observe several scheduled/recovery runs.
6. A **separate production decision** must approve the live mailbox and initial
   history scope: a fresh queue scans from UID 1, not just recent mail. Agree that
   scope and backlog budget before enabling production; do not silently seed a
   cursor or skip historical messages. Production deployment, secrets, restart,
   API activation and Cron remain separate permission gates.

Rollback: stop/disable the worker and monitor jobs first; set the feature flag
off and restart only with permission, or deploy the previous server revision.
Retain `/data/mail_import` and all originals; do not restore an old working
database over current data. Re-enabling the same code/mailbox resumes from the
persisted cursor. Local code is isolated in its own branch/worktree; reverting
its implementation commit does not require deleting any data.

## Unified local desktop candidate 1.0.10-rc1 (2026-10-21)

This candidate combines the accepted Balance fix, document classification,
explicit payable/rounding extraction and string OCR/reference transfer. It also
connects manual mail receipt to the desktop Expenses tab: **Входящие фактуры**.
The installed 1.0.9 client is not replaced or restarted by building the candidate.

- The user explicitly starts a read-only IMAPS pass over the last seven days.
  Password entry is local and ephemeral; no background polling is enabled.
- Each desktop database has a separate queue below the user profile's
  `mail-inbox/<database-path-hash>`. Immutable originals, receipt/source history,
  recognition, INSERT-only drafts and approved handoffs remain there.
  No synthetic object catalog is seeded in desktop mode.
- Drafts do not enter the expense outbox or server register. Object selection,
  original review and a separate default-No confirmation are mandatory.
- Approved payload/client ID is durable before staging a separate original copy
  for the normal desktop expense outbox. Handoff retries reuse the same client ID;
  receiving the same bytes reuses the queued document but retains each source.
  A matching business key in approved mail documents blocks a second handoff and
  requires review of the existing card. The server's existing duplicate checks
  also apply to cards originating outside this mailbox.
- The original queue copy survives normal outbox ACK cleanup. The card's original
  can be opened through the normal register after synchronization.
- Existing drafts are not re-recognized or overwritten on receipt or opening.
  Reprocessing remains an explicit, selected-document action.
- The separate full-backup patch `dca9f9d` is **not in this candidate**.
  Historical backup sections below describe that separate development branch,
  not capabilities added by this release.

The full scenario exposed a previously untested gap: the expense API discarded
`payment_reference`. This release therefore includes a small compatible server
change: a nullable TEXT column, string validation, create/update/reprocess storage,
serialization and `payment_reference_v1` capability. Missing keys on older PUTs
preserve the existing reference; an explicit empty string clears it. No existing
records are backfilled. Desktop delivery checks that capability and the returned
reference/payable/rounding before removing its staged original.

The candidate retains the already accepted server/report baseline `df62930`,
including object reports and paired-dataset boundaries. It also preserves the
installed desktop's dataset/candidate-profile/PIN safeguards: corresponding source
methods were compared against installed bytecode without execution or database
access. The old stash was read, not applied or removed.

Server installation is required before using the complete roundtrip in production:
deploy the candidate server source based on that compatible baseline, apply the
additive reference migration during its existing initialization, and restart only
after separate approval. Net/due-date schema capabilities must also be present;
do not enable an unrelated migration blindly. Manual desktop intake uses this
authenticated expense API v2, not an automatic-mail endpoint.

The installer is side-by-side and does not close applications, replace existing
shortcuts or start the client. `packaging/launch_existing_profile.ps1` explicitly
reuses the approved existing profile, documents and connection/token directory,
checks the dataset and refuses a simultaneous SmartReport process. Installation
and running that launcher still require separate permission.

**At this desktop release, automatic Render mail was not implemented.** The next concrete implementation
is an authenticated, draft-only intake API with durable original/source/checkpoint
storage, idempotent receipt keys, a renewable mailbox lease with fencing and
UIDVALIDITY-epoch reconciliation. Then implement a headless one-pass worker and
Render Cron invoking it every five minutes. Secrets stay in Render secret
settings; the worker uses IMAPS read-only/BODY.PEEK. Confirming expenses remains a
separate user action. SHA-256 deduplicates bytes, not invoice identity; separate
receipt history and manual handling of different PDFs of one invoice are required.
No API, Cron, production import or deployment is enabled by this desktop release.

## Historical Render mail proposal — before isolated implementation (2026-10-21)

This historical section records the original design; the local implementation
and refinements are described above. No Render service, secret, schedule, deployment or
production intake has been created or enabled.

- **Schedule:** a separate Render Cron Job every five minutes, `*/5 * * * *`
  (UTC). Its start command would execute one headless intake pass and exit; it must
  not launch Qt or run a scheduler in Flask/Gunicorn workers. Use the same released
  importer version as the receiving API. At most one run of this job is active;
  additionally use an API-side mailbox lease to exclude manual/other-job overlap.
- **Storage boundary:** Render Cron Jobs cannot mount persistent disks, and a
  service cannot access another service's disk. Therefore the cron process must
  NOT open the working SQLite file or assume `/data` is shared. It would upload
  originals and recognition results over an authenticated HTTPS **draft-intake**
  API to the disk-owning SmartReport service. That API is not implemented yet.
  Existing expense-confirmation endpoints are not a substitute.
- **Durable queue:** propose canonical immutable originals under the existing
  service's `/data/documents`, with checksum/size checks, and queue/source/checkpoint
  metadata in a dedicated SQLite database under `/data/mail_import`. A received
  attachment is acknowledged only after its file and queue record are durable.
  Expense cards are created only after manual review, explicit object choice and
  confirmation; they reference the already retained original without moving it.
  Cron scratch space is temporary and never the sole retained copy.
- **Mailbox access:** IMAPS to `mailcluster.loopia.se:993` with certificate
  validation, read-only INBOX selection and `BODY.PEEK[]`. No flag changes,
  STORE/MOVE/DELETE/CLOSE/EXPUNGE. A dedicated mailbox credential, or app password
  if the provider supports one, is entered only in Render secret environment
  settings after permission. Use a separate narrowly scoped draft-intake token;
  it must not authorize confirming expenses or accessing admin functions.
  Neither credential belongs in source, command arguments, reports or logs.
- **Catch-up after failure:** the durable cursor is per mailbox/folder/UIDVALIDITY
  on the disk-owning API, not on cron's ephemeral filesystem. Each pass resumes
  unfinished UIDs and reads the subsequent UID range in bounded batches. There is
  no "last five" or rolling seven-day cutoff in production. Record the snapshot
  upper UID for the pass; messages arriving during it are handled next time.
  Advance the contiguous checkpoint only after every part of a message is either
  retained or explicitly recorded as unsupported/no attachment. Recognition
  failures retain the original and a failed-review state; transport/storage
  failures remain pending and must not be silently skipped. A UIDVALIDITY change
  starts a new source namespace and requires paginated reconciliation of INBOX
  with hash deduplication, not blind reuse of the old cursor.
- **Retries/deduplication:** retry transient IMAP/HTTP/timeouts with bounded
  backoff; retain pending work for the next scheduled pass if the time budget is
  exhausted. Permanent authorization/storage errors stop the run with an alert,
  not a successful empty report. Deduplicate by mailbox/folder/UIDVALIDITY/UID/part
  and original SHA-256; warn on supplier/invoice identity matches with different
  files. A lost HTTP acknowledgement retries the same source identity. Do not
  overwrite earlier recognition on repeat receipt; explicit selected-document
  reprocessing stays separate.
- **Visibility:** expose last successful receipt time, last attempted run,
  pending backlog, failed attachments and incomplete/unclassified recognition as
  separate metrics. Propose an alert after three missed five-minute windows or
  immediately for rejected credentials/storage failures. A missed scheduled run
  does not imply losing mail: the next pass catches up from durable checkpoints,
  provided messages still exist in INBOX. Messages removed by other mail clients
  before any fetch cannot be recovered by this importer.

Before any implementation/release: owner approval, intake API and permission tests,
bounded cursor/catch-up tests, credential setup and real acceptance without expense
confirmation. The current desktop test importer is not production-ready scheduling.

Sources verified read-only:
[Render Cron Jobs](https://render.com/docs/cronjobs),
[Persistent disks and single-service access](https://render.com/docs/disks).

### Cost, API boundaries and crash recovery — proposal clarification

**Content identity is not invoice identity.** SHA-256 only identifies identical
file bytes: multiple receiving events may reference one stored blob. Retain each
mail source/attachment occurrence separately (mailbox, folder, UIDVALIDITY, UID,
part, receive timestamp and original filename), even when content is deduplicated.
One invoice may also arrive as different PDF bytes: supplier/invoice identifiers
are review signals, not proof that files should be discarded or records merged.
Use separate blob, receipt-event, draft and confirmed-invoice identities. Preserve
all receipt history; uncertain business matches enter manual review before any
additional expense is allowed. Reconciliation after a mailbox epoch change must
retain the new source occurrence even when it links to an existing hash.

- Official Starter Cron rate: **USD 0.00016 per active minute**, billed per second,
  **USD 1 minimum per cron service/month**, not USD 1 in addition to compute.
  Every five minutes means 8,640 scheduled passes per 30-day month. At 10 or 30
  seconds/pass compute is USD 0.23/0.69, so the minimum is USD 1; at 60 seconds it
  is about USD 1.38; at five minutes about USD 6.91. Startup and retries count.
  These are estimates, not a quote: memory/CPU must be measured for PDF/OCR before
  choosing Starter. Existing web compute, disk/object storage, outbound traffic,
  workspace charges/tax and any plan upgrade are outside this cron estimate.
  Sources: [official pricing](https://render.com/pricing) and cron docs above.
- Intake API authorization must be server-enforced, before body processing:
  TLS-only, separate rotatable secret scoped to one mailbox and draft ingestion;
  no admin, expense confirmation, deletion or arbitrary file access. Keep secrets
  in Render secret environment settings and redact authorization/mail responses.
  Enforce request/attachment size, per-pass count/time quotas, MIME/signature
  allowlist, server-generated storage paths, SHA-256/length verification and JSON
  schema validation; never trust uploaded paths, hashes or recognition as approval.
  Recognized values remain untrusted draft data for manual review. Rate-limit and
  audit runs using non-sensitive IDs. Do not expose an unauthenticated "run import"
  URL. A lost response must allow identity-based acknowledgement lookup/retry.
- Render's one-active-run guarantee covers a single cron service only. API must
  also acquire an atomic lease per mailbox/folder with owner run ID, expiry,
  renewal and a monotonically increasing fencing token. Another cron/manual
  invocation sees "already running". A crashed/expired owner cannot advance
  checkpoints: all writes check lease ownership, fencing token and UIDVALIDITY.
  Combine this with DB uniqueness constraints for source identity and SHA-256.
  Manual Trigger Run cancels an existing Render run, so it must not be treated
  as a harmless extra concurrent pass.
- On UIDVALIDITY change, reject the old epoch's writes, retain its history and
  originals, and open a new epoch starting with no UID cursor. Under the lease,
  capture the new upper UID and enumerate currently present messages in bounded
  batches (do not match messages solely by UID or Message-ID). Link exact-byte
  duplicates by SHA-256, flag business-key matches with different bytes for review,
  preserve approved cards and existing draft edits. Commit reconciliation progress
  transactionally and resume it after another crash. Advance the new checkpoint
  only for durably handled messages; gaps in UIDs from expunged messages are valid.
  Until reconciliation finishes, display a changed-mailbox/backlog warning.
  UIDVALIDITY reset alone must never create expenses, discard the old queue or
  mass-replace saved recognition.

## Current local mail acceptance (2026-10-21; not published)

The current owner instruction overrides historical release notes below: **no push,
deploy, restart, production mail import, or working-data changes are authorized**.
The backup patch is preserved; no additional backup acceptance is being performed.

An opt-in local acceptance UI is available separately from the existing draft-only
mail viewer:

```powershell
python -m local_mail --data-dir "<NEW empty isolated test directory>"
```

Never point this command at an existing mail queue. It refuses a nonempty directory
without its local-test marker. The explicit receive action reads the **five latest
INBOX messages by IMAP UID**, including mail received before the window was opened,
without a seven-day cutoff. The previous first-launch marker is retained only for
directory isolation, not used to exclude existing messages. Legacy importer callers
keep their seven-day default; `latest_count` (1–20) is opt-in.
The owner enters the mailbox password in a masked local dialog for each fetch; it is
not persisted. IMAP uses read-only INBOX and BODY.PEEK, with bounded metadata search.
There is no scheduler and no production endpoint option.
The read-only report lists each selected message (including those without attachments),
attachment names/types, unsupported files, recognized fields, duplicates and failures.
It is displayed as plain text and saved locally in `runs/`; bodies and passwords are
not included. Repeated receipt does not rerun saved recognition or create expenses.
Receipt and recognition have independent outcomes: a received/stored attachment can
still be unclassified, incompletely recognized, or failed. Zero processing errors
does not mean all invoice fields are available. The queue and offline review report
show explicit document types, including **«Подтверждение заказа — не фактура»**;
missing invoice fields on an order confirmation are not an OCR failure. Order number
and order date must not be relabeled as invoice number/date. `--document <id>` opens
one saved local document for review without reprocessing it. Recognition replacement
still requires the selected-document action and default-No confirmation.
The shared review form includes editable `payment_reference` (OCR / reference),
preserved as text when saving drafts and confirmations, including leading zeros.
Older drafts without this key display the saved recognition value on reopening;
an explicitly cleared reference remains empty. No existing draft is migrated or
saved automatically. The offline review field comparison includes this reference.
The parser recognizes `Att betala (SEK)`, `OCR/Meddelande` and the explicit
`Vänligen ange referens … vid betalning` instruction. Reference remains a string;
conflicting candidates require review. Amount due comes from labeled source text
and retains page/box evidence, never from net + VAT + rounding. Missing/ambiguous
payable amounts remain empty with warnings; inconsistent explicit amounts are
retained with a reconciliation warning. Previously queued recognition and draft
revisions are not automatically refreshed by these parser changes.

Storage within that directory:

- `originals/` and `mail_queue.sqlite3`: immutable received originals, recognition,
  insert-only draft revisions, and approved pending/sent delivery metadata.
- `register/local-register.db` and `register/documents/`: separate local API register
  and its originals. The API binds only to `127.0.0.1` on a random port with newly
  generated test credentials. The working desktop application is not started.
- `card-originals/`: originals downloaded through saved local cards and SHA-256
  verified before opening.

Receiving and opening a document never create an expense. The local form reads
saved recognition, not a new implicit extraction. Reprocessing remains an explicit
per-document action with a default-No warning. First review requires explicit object
selection; draft allocations can be resumed. Confirmation is initially disabled:
the owner must enable the acceptance gate, select objects for all rows, check the
original-review checkbox, and answer **Yes** in the final confirmation dialog.
Order confirmations, tax decisions, pro formas, receipts and unclassified documents
cannot be confirmed as ordinary invoices in this acceptance UI.

Approval persists one `client_id` and payload before the HTTP request. A lost
response retains the pending delivery, which can be retried after reopening without
another expense. Sending an unapproved draft is refused; already-sent items are not
resent. Same mail-part identity and SHA-256 deduplicate repeated receipt; the existing
API also rejects a matching business identity or an already stored original.
The original remains in the queue even after acknowledgement.

The existing `mail_viewer` remains draft-only. No automatic production transfer,
production mailbox scheduling, credential storage, or working-data changes are enabled.
Automated acceptance uses only synthetic messages and temporary local API databases;
it is not evidence that the owner's new real message has been fetched.

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