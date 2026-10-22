# Spyne Image Resequencer

Re-orders catalog images for many VINs to one label sequence, using the same API call the console's **Images > Resequence > Save Sequence** button makes (`POST /inventory/v2/media/image-sequencing`).

## Run
- **Windows:** double-click `run_windows.bat`
- **Mac:** double-click `run_mac.command` (first time: right-click > Open; if it says no permission run `chmod +x run_mac.command` in Terminal)
- Either OS manually: `python3 app.py` — the browser opens at http://127.0.0.1:8765

Needs Python 3.8+ (python.org). No packages to install.
The server listens on 127.0.0.1 only.

## Use
1. **Console link + token** – paste the console inventory link (enterprise_id and team_id are read from it) and your Bearer token. Token lives in memory only.
2. **Sequence** – one label per line, first line = first image. Default is Front Right, Front, Front Left, Left, Rear Left, Rear, Rear Right, Right, Interior, Focus Shoot. Upload a .txt/.csv to replace it.
3. **Scan inventory** – choose a **Source** first: All VINs, Spyne App (iOS + Android), or a single source such as FTP or Console (the list is built from what your inventory actually contains, with counts). VINs with no processed images are skipped instantly, failed reads are retried automatically, and results appear live. Use **Re-scan failed** for any that still time out. Pick **Speed** (2/4/8 parallel) - lower it if you see 504s; **Stop** cancels a running scan. Errors show in a red banner with counts; an expired token (401) stops the scan with a clear message. Nothing is changed yet.
   - **VIN view**: table with a Status filter in the column header, search, and hero (first image) now → new.
   - **Hero angle view**: card grid like the console, with a 'Hero now' filter. Click any row/card for before/after strips.
4. **Fix selected** – posts the new order, re-reads the VIN to verify, and saves `backups/<VIN>_<time>.json`.
5. **Undo** – pick backups and restore the original order.

## Learn the sequence from a correct VIN (no typing needed)
You can also just enter one or more VINs already correct in the console and click 'Learn from these VINs'.

In step 2 upload the correctly sequenced VIN folder (or its .zip). Files named like `1_7_Exterior_<imageid>.jpg` inside a folder named with the VIN are read; each image id is looked up in the live console record to get its angle, and the label order is built from the correct order and put in the sequence box. Checks shown: files matched, category mismatches, and whether a label sort reproduces your folder. During the scan, reference VINs are fixed to the exact folder order and every VIN is also checked for positions missing versus the reference.

## How labels are derived
From the image's classification in the VIN record: every exterior angle goes to the nearest of 8 positions (0 front = plain "Exterior", 45 Front Left, 90 Left, 135 Rear Left, 180 Rear, 225 Rear Right, 270 Right, 315 Front Right), plus Interior, Focus Shoot, Miscellaneous. Images sharing a label (all interior shots, for example) keep their current relative order.

## Safety notes
- Always run Preview first; try one VIN before the batch.
- Token expires; reconnect if you see HTTP 401.
- Rotate any token you have pasted into chat or email.
