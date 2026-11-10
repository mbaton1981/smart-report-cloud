SmartReport 1.0.54 - Windows x64

Upgrade the existing application folder, keep the approved-profile shortcut
and preserve SmartReport-1.0.9. Do not start a fresh profile.

Employee: choose object/date, start/end, next-day ending if needed, unpaid
break intervals and normal/all-overtime/part-overtime mode. Use Calculate hours.
Normal evening work is not automatically overtime. No customer prices or
percentages are entered by employees.

Administrator: select the object in the desktop Objects card and open
Customer supplements. Base comes from the existing object rate. Add dated
normal/overtime rules, separate weekends and explicit holidays; copy another
object if appropriate. Empty means unknown; 0% confirms no supplement.
Priority is holiday, weekend, weekday, then explicit work mode; no stacking.
The example 520 + 20% = 624 is illustrative, never assigned to working sites.

Reports and invoicing > Other actions > Customer interval calculation:
review each time/mode/base/percentage/amount. Correct time through the native
editor. Applying terms to existing open rows requires an explicit preview.
Legacy hours-only and payroll OB records are not given invented intervals.
Closing saves the historical version and calculation. Later object edits do
not silently reprice confirmed periods. Payroll rules remain independent.

Invoice parsing retains normal local extraction, with a missing-field AI
fallback through the existing connection. Only anonymized geometry/IDs and
fixed caption labels are sent; original values are checked locally. It never
confirms costs. Saved split VAT and separate rounding survive reopening/saving.

No working databases, originals, credentials or user settings are included.
Keep the previous 1.0.53 installer/application as rollback. Roll back application
files only; never overwrite newer working data with an older database.
