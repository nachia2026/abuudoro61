KISAUNI PRIMARY SCHOOL - RESULT MANAGEMENT SYSTEM
====================================================

HOW TO RUN (Windows)
---------------------

STEP 1 - First time only:
  Double-click "setup.bat"
  This installs everything the system needs. Wait for it to
  say "Setup complete!" then press any key to close it.
  You only need to do this ONCE on this computer.

STEP 2 - Every time you want to use the system:
  Double-click "run.bat"
  A black window will open (this is the server running - do
  not close it while you are using the system) and your web
  browser will open automatically to the login page.

  When you are done, go back to the black window and press
  CTRL+C, or simply close the window, to stop the server.

DEFAULT LOGIN
-------------
  Username: headmaster
  Password: admin123
  (You will be asked to set a new password the first time.)

NOTES
-----
- All data (students, marks, results, users) is saved in the
  file "kisauni.db" inside this same folder (the same folder
  as run.bat/setup.bat). Do not delete it. If your file manager
  is set to hide file extensions or "system" files, kisauni.db
  may look invisible even though it is there - turn on "Show
  hidden items" / "Show file extensions" in Windows Explorer's
  View menu to confirm you can see it.
- You do NOT need to back it up manually anymore - the system
  now backs itself up automatically:
    * once every time you start it (run.bat / python app.py)
    * once a day while it keeps running
    * automatically, right before "Promote Students" (year-end)
  Backups are saved inside a "backups" folder next to kisauni.db,
  and can be downloaded, restored, or deleted from the Settings
  page inside the system (Headmaster login only).
- IMPORTANT: automatic backups above still live on this same
  computer. For real safety against this computer's hard drive
  failing, occasionally open Settings -> Database Backups inside
  the system and click "Download" to save a copy somewhere else
  entirely (Google Drive, email to yourself, or a USB drive).
- If you ever see "ModuleNotFoundError" when running run.bat,
  just run setup.bat again.
- The system works fully offline - no internet is required to
  use it, only to install packages the first time.

HOSTING THIS ONLINE (e.g. Railway) - DO NOT SKIP THIS
------------------------------------------------------
If this system is deployed to online hosting instead of running on a
school computer, the hosting platform's own disk is normally WIPED
every time the app is redeployed/rebuilt - which would delete the
database, backups, AND any uploaded school logo along with it.

To prevent this:
  1. On your hosting platform, create a "Persistent Volume" (on
     Railway: Project -> your service -> Settings -> Volumes ->
     "New Volume", mount path e.g. /data).
  2. Set an environment variable DATA_DIR=/data (matching the mount
     path you chose) on that same service.
  3. That's it - the app automatically stores kisauni.db, all
     automatic backups, and any uploaded school logo inside DATA_DIR,
     so all three survive every redeploy/restart from then on.
Without steps 1-2, the app still runs fine, but everything is only
as safe as the hosting platform's temporary disk - which is NOT
safe for a school's permanent records.
