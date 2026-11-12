SmartReport 1.0.62 - Windows x64

This update is for the existing approved user profile. Keep the existing
SmartReport-1.0.9 profile and shortcut names; do not create or select a fresh
profile. The installer contains application files only.

Light/dark theme switch:
- Switching the theme took about 3-3.5 seconds in the installed 1.0.61. The
  time went into Qt re-measuring every cell of the content-sized tables
  (shift table: about 24,000 cell size calculations) on each style change.
- The re-measuring is now suspended while the style is applied and done once
  afterwards. Colours and appearance of both themes are unchanged; the selected
  tab, filters, selection, scroll position and unsaved input are kept.

No expense, shift, or period is confirmed by this release.

Roll back application files only; never replace newer working data with an
older database. Keep the verified rollback in installer/rollback/1.0.61.