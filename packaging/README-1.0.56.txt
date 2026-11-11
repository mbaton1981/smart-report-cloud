SmartReport 1.0.56 - Windows x64

Upgrade the existing application folder, keep the approved-profile shortcut
and preserve SmartReport-1.0.9. Do not start a fresh profile.

The employee web form and payroll rules are unchanged.

Desktop > Reports and invoicing > select object and period > Calculate
supplements. Select individual open records, choose Evening shift - OB or
Overtime - Overtid, and distribute the EXISTING hours across the fixed rows.
Percentages are supplied by the scheme; the historical customer base is kept.
Ordinary remainder must be explicitly assigned. OB is already inside hours.

OB: 16-17 +0%, 17-19 +20%, 19-22 +40%, 22-06 +70%.
Overtime: 17-19 +50%, 19-22 +70%, 22-06 +100%; 16-17 is unspecified.
Unspecified conditions are not invented. Evening is not automatically overtime.
Weekend +100% requires an explicit customer-condition checkbox; it does not
stack with OB/overtime and is not payroll or an automatic 40-hour rule.

Preview applies one distribution to EACH selected record. Different durations
are rejected, not silently rescaled. Actual detailed intervals can suggest a
distribution; otherwise enter quantities without inventing start/end or schedules.
For base 520 and OB quantities 1/2/1 in 16-17/17-19/19-22:
base 2080 + supplement 416 = 2496 SEK excluding VAT.

Confirm the preview, then use the usual Close period action. No averaged OB1
price is required after confirmation. The exact allocation is audited, synced,
reopens and appears in PDF. Later price changes do not reprice closed history.
Changing actual hours/time clears stale allocation and requires another review.
Transport and payroll are separate; closed shifts remain protected.

Previous object rules and schedules remain available under Previous conditions
for compatibility/history, but are no longer the main calculation workflow.
Old clients cannot silently discard a confirmed allocation on the server.

Known unresolved issue: the combined desktop test process can crash/hang in
Qt theme application. Separate-module checks are not a fix for this native issue.

No working databases, originals, credentials or user settings are included.
Keep the previous 1.0.55 application and installer as rollback. Roll back
application files only; never overwrite newer working data with an older DB.
