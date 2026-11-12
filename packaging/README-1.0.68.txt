SmartReport 1.0.68 - Windows x64 (client only; no server change)

This update is for the existing approved user profile. Keep the existing
SmartReport-1.0.9 profile and shortcut names; do not create or select a fresh
profile. The installer contains application files only.

Shifts journal status colors:
- "Hours confirmed" rows: light blue background with dark text in the light
  theme; muted blue with light text in the dark theme.
- "Invoiced" rows: light green background with dark text in the light theme;
  muted green with light text in the dark theme.
- A selected status row is drawn with a stronger shade of its own status
  color and stays readable; it no longer merges with the status tint.
- Ordinary ("In progress") rows and their selection are unchanged.
- Only the appearance changed: status logic, data, calculations and the
  employee web are untouched.

No expense, shift, or period is confirmed by this release.

Roll back application files only; never replace newer working data with an
older database. Keep the verified rollbacks in installer/rollback/1.0.66 and
installer/rollback/1.0.67.
