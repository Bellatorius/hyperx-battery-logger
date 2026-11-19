# HyperX Cloud III S Wireless battery logger

The logger reads battery status from the headset's 2.4 GHz USB dongle every 5 minutes, records every check in `battery_log.csv`, and syncs queued rows to a Google Sheet. Each row has a unique ID, so retries never create duplicates. Dates are `dd/mm/yyyy`, times are `h:mm am/pm`.

## Files

| File | What it is |
| --- | --- |
| `battery_to_google_sheet.py` | The app (tray icon + background worker) |
| `battery_log.csv` | Every check; `synced` column = uploaded to the Sheet yet |
| `battery_monitor.log` | Activity log (rotates at 2 MB) |
| `dashboard.py` | Stats + charts for the Dashboard tab |
| `config.json` | Your Sheet ID (private, copy from `config.example.json`) |
| `google-service-account.json` | Service-account key (preferred login) |
| `google-oauth-client.json` | Desktop OAuth client (fallback login) |
| `google-authorized-user.json` | Saved Google sign-in (refresh token), written by the app |

Keep both JSON files private.

## Google Sheets setup

**Recommended: service account** (never expires, no browser sign-in).

1. Google Cloud Console → **IAM & Admin → Service Accounts** → **Create service account** (no roles needed).
2. Open it → **Keys → Add key → Create new key → JSON**. Save the file here as `google-service-account.json`.
3. Share the Google Sheet with the service account's email (`…@….iam.gserviceaccount.com`) as **Editor**.

If `google-service-account.json` is a service-account key, it is always used. Otherwise the app falls back to the OAuth sign-in below.

**Fallback: OAuth sign-in**

1. Google Cloud Console → **APIs & Services → Credentials** → create an **OAuth client ID**, type **Desktop app**. Download its JSON and save it here as `google-oauth-client.json`. (Google Sheets API must be enabled.)
2. **APIs & Services → OAuth consent screen**: set Publishing status to **In production**. While it is "Testing", Google expires the saved sign-in every 7 days and syncing stops.
3. Start the app. A browser opens once to sign in with the account that can edit the Sheet.

Copy `config.example.json` to `config.json` and put your Sheet's ID in it (the part of the Sheet URL between `/d/` and `/edit`). `config.json` is private and not committed.

Rows go into **one tab per month** (`Nov 2026`, `Dec 2026`, ...), created automatically with the newest month first, based on the reading's local date.

## Dashboard

`dashboard.py` builds a **Dashboard** tab (first tab) with a summary, a month-by-month table, and four charts:
battery level (last 30 days), estimated hours of use per full charge by week, hours the headset was on per day,
and the battery % when each charge started. The numbers behind the charts are on the **Dashboard data** tab (last tab).
It refreshes once a day with the full check, or from the tray: **Update dashboard now**.

How the numbers are worked out:
- *Headset on*: time between readings less than 10 min apart (the dongle only answers while the headset is on).
- *Life per charge*: % used per hour of use while not charging, scaled to 100% (needs at least 10 h of use in the period).
- *Charge*: the level climbing by 10% or more. Charge time is only measured when the headset reported during charging.

## Syncing

- New readings upload every 5 minutes.
- Once a day (and right after signing in) a full check compares every local row with the monthly tabs and re-sends anything missing.
- With no internet, rows queue locally; the app checks every 30 s and sends as soon as the connection returns.

## When syncing stops

The tray icon screen turns **red** and the menu says why. If it says *sign-in needed*, the saved login was rejected (client secret reset/deleted, or token revoked). Fix the client JSON if needed, then right-click → **Sign in to Google...**. Queued rows are never lost; they upload in batches of 500 once sign-in works.

## Install and run

```powershell
py -m pip install hidapi gspread google-auth google-auth-oauthlib pystray pillow
pyw battery_to_google_sheet.py
```

Starts at log-on via the Task Scheduler task **HyperX Battery Logger** (`pythonw.exe` + full path to the script, Start in = this folder). Only one copy runs at a time.

The dongle must be connected and the headset in 2.4 GHz mode for readings; failed reads are still logged with an empty percentage and a status.
