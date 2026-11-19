"""HyperX Cloud III S battery logger.

Polls the headset's 2.4 GHz dongle every few minutes, records each check in a
local CSV, and syncs queued rows to a Google Sheet. Runs as a tray icon.
"""
import csv
import ctypes
import json
import logging
import os
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import gspread
import hid
import pystray
import requests
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from PIL import Image, ImageDraw

import dashboard

# pythonw has no console; libraries that print would crash without this.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

# Google Sheet ID from its URL and the tab name to write into.
CONFIG_FILE = Path(__file__).resolve().parent / "config.json"  # private; see config.example.json
try:
    SPREADSHEET_ID = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("spreadsheet_id", "")
except (OSError, ValueError):
    SPREADSHEET_ID = ""
# One tab per month, named like "Nov 2026". (Before 2026-11-19 everything went
# into a single "Hyperx Battery script" tab; it was deleted after being copied.)
MONTH_TAB_FORMAT = "%b %Y"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
BASE_DIR = Path(__file__).resolve().parent
# Preferred: a service-account key (never expires; share the Sheet with its email).
SERVICE_ACCOUNT_FILE = BASE_DIR / "google-service-account.json"
# Fallback: Desktop OAuth client + browser sign-in (expires weekly while the app is in "Testing").
OAUTH_CLIENT_FILE = BASE_DIR / "google-oauth-client.json"
OAUTH_TOKEN_FILE = BASE_DIR / "google-authorized-user.json"
SIGN_IN_STATE_FILE = BASE_DIR / "google-sign-in-state.json"
SYNC_STATE_FILE = BASE_DIR / "google-sync-state.json"   # remembers the last daily full check
OFFLINE_RETRY_SECONDS = 30                              # how often to look for the internet while offline
NETWORK_ERRORS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout, TransportError)
# Google expires OAuth sign-ins after 7 days while the consent screen is in "Testing".
OAUTH_LOGIN_DAYS = 7
EXPIRY_WARNING_HOURS = 24
SIGN_IN_NOTICE_DELAY_SECONDS = 1 * 3600  # first "sign-in needed" alert after the login breaks
SIGN_IN_REMINDER_SECONDS = 2 * 3600      # then repeat this often until signed in
CHECK_INTERVAL_SECONDS = 300
SYNC_BATCH_SIZE = 500
SIGN_IN_TIMEOUT_SECONDS = 300

HYPERX_VENDOR_ID = 0x03F0
PRODUCT_IDS = {0x02CC, 0x06BE}
HID_REPORT_LENGTH = 64
CSV_FILE = BASE_DIR / "battery_log.csv"
LOG_FILE = BASE_DIR / "battery_monitor.log"
CSV_COLUMNS = ["check_id", "date", "time", "timestamp_utc", "battery_percent", "status", "synced"]
SHEET_HEADERS = ["Check ID", "Date", "Time", "Timestamp (UTC)", "Battery (%)", "Status"]

stop_event = threading.Event()
sync_event = threading.Event()
full_sync_requested = False  # tray "Update dashboard now" asks the worker for a full check
csv_lock = threading.RLock()   # guards every read/write of the CSV
sync_lock = threading.Lock()   # one sync or sign-in at a time
latest_text = "No battery reading yet"
sync_text = "Google Sheets: not synced yet"
sync_ok = True
auth_needed = False
offline = False
auth_needed_since = 0.0
last_sign_in_notice = None
tray_icon = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8")],
)
# Its INFO line would write the one-time OAuth code into the log.
logging.getLogger("google_auth_oauthlib").setLevel(logging.WARNING)


class AuthRequired(Exception):
    """Google rejected the saved sign-in; the user must sign in again."""


# ---------------------------------------------------------------- battery --

def format_date_time(dt):
    date_text = dt.strftime("%d/%m/%Y")
    time_text = dt.strftime("%I:%M %p").lstrip("0").lower()
    return date_text, time_text


def read_battery():
    """Query the Cloud III S 2.4 GHz USB dongle. Return (percentage, status)."""
    devices = [
        item for item in hid.enumerate(HYPERX_VENDOR_ID, 0)
        if item.get("vendor_id") == HYPERX_VENDOR_ID and item.get("product_id") in PRODUCT_IDS
    ]
    if not devices:
        return None, "device_not_found"

    # Cloud III S dongle battery request and reply format.
    request = bytes([0x0C, 0x02, 0x03, 0x01, 0x00, 0x06] + [0] * (HID_REPORT_LENGTH - 6))
    for info in devices:
        device = hid.device()
        try:
            device.open_path(info["path"])
            device.set_nonblocking(1)
            device.write(request)
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                response = device.read(HID_REPORT_LENGTH)
                if response and len(response) >= 7 and response[0] == 0x0C and response[5] == 0x06:
                    value = int(response[6])
                    if value != 0xFF and 0 <= value <= 100:
                        return value, "ok"
                time.sleep(0.05)
        except (OSError, hid.HIDException) as exc:
            logging.debug("HID interface unavailable: %s", exc)
        finally:
            try:
                device.close()
            except Exception:
                pass
    return None, "no_battery_response"


# -------------------------------------------------------------------- CSV --

def ensure_csv():
    with csv_lock:
        if not CSV_FILE.exists() or CSV_FILE.stat().st_size == 0:
            with CSV_FILE.open("w", newline="", encoding="utf-8-sig") as file:
                csv.DictWriter(file, fieldnames=CSV_COLUMNS).writeheader()
            return

        # Migrate the original CSV layout while preserving all old checks and sync states.
        with CSV_FILE.open("r", newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            old_columns = reader.fieldnames or []
            if old_columns == CSV_COLUMNS:
                return
            old_rows = list(reader)
        migrated = []
        for old in old_rows:
            local_stamp = old.get("timestamp_local", "")
            try:
                local_dt = datetime.fromisoformat(local_stamp)
                date_text, time_text = format_date_time(local_dt)
            except (ValueError, TypeError):
                date_text, time_text = "", ""
            migrated.append({
                "check_id": old.get("check_id") or str(uuid.uuid4()),
                "date": date_text,
                "time": time_text,
                "timestamp_utc": old.get("timestamp_utc", ""),
                "battery_percent": old.get("battery_percent", ""),
                "status": old.get("status", ""),
                "synced": old.get("synced", "no"),
            })
        _rewrite_csv(migrated)


def _rewrite_csv(rows):
    temp = CSV_FILE.with_suffix(".tmp")
    with temp.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, CSV_FILE)


def append_local(row):
    with csv_lock:
        ensure_csv()
        with CSV_FILE.open("a", newline="", encoding="utf-8") as file:
            csv.DictWriter(file, fieldnames=CSV_COLUMNS).writerow(row)
            file.flush()
            os.fsync(file.fileno())


def all_rows():
    with csv_lock:
        ensure_csv()
        with CSV_FILE.open("r", newline="", encoding="utf-8-sig") as file:
            return list(csv.DictReader(file))


def pending_rows():
    return [row for row in all_rows() if row.get("synced") != "yes"]


def mark_synced(check_ids):
    if not check_ids:
        return
    wanted = set(check_ids)
    with csv_lock:
        with CSV_FILE.open("r", newline="", encoding="utf-8-sig") as source:
            rows = list(csv.DictReader(source))
        for row in rows:
            if row["check_id"] in wanted:
                row["synced"] = "yes"
        _rewrite_csv(rows)


# ------------------------------------------------------------ Google auth --

def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def service_account_info():
    data = _read_json(SERVICE_ACCOUNT_FILE) if SERVICE_ACCOUNT_FILE.exists() else None
    return data if data and data.get("type") == "service_account" else None


def client_file():
    """OAuth client JSON (older setups saved it under the service-account filename)."""
    for path in (OAUTH_CLIENT_FILE, SERVICE_ACCOUNT_FILE):
        data = _read_json(path) if path.exists() else None
        if data and (data.get("installed") or data.get("web")):
            return path
    return None


def load_client_config():
    path = client_file()
    if path is None:
        raise AuthRequired(f"OAuth client JSON not found; save it as {OAUTH_CLIENT_FILE.name}")
    config = json.loads(path.read_text(encoding="utf-8"))
    info = config.get("installed") or config.get("web")
    if not info:
        raise AuthRequired(f"{path.name} is not a Desktop OAuth client file")
    return config, info


def save_token(creds):
    OAUTH_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")


def load_credentials():
    """Return valid credentials without user interaction, or raise AuthRequired."""
    sa_info = service_account_info()
    if sa_info:
        return service_account.Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    _, info = load_client_config()
    if not OAUTH_TOKEN_FILE.exists():
        raise AuthRequired("not signed in to Google yet")
    data = json.loads(OAUTH_TOKEN_FILE.read_text(encoding="utf-8"))
    # A token minted for an older client (e.g. before a secret reset) can never refresh.
    if data.get("client_id") != info["client_id"] or data.get("client_secret") != info["client_secret"]:
        raise AuthRequired("OAuth client file changed; sign in again")
    creds = Credentials.from_authorized_user_info(data, SCOPES)
    if creds.valid:
        return creds
    if not creds.refresh_token:
        raise AuthRequired("saved sign-in has no refresh token")
    try:
        creds.refresh(Request())
    except RefreshError as exc:
        # invalid_client = client secret reset/deleted; invalid_grant = token revoked/expired.
        raise AuthRequired(f"Google rejected the saved sign-in: {exc}") from exc
    save_token(creds)
    return creds


def interactive_sign_in():
    """Open the browser for Google consent and save a fresh refresh token."""
    config, _ = load_client_config()
    flow = InstalledAppFlow.from_client_config(config, SCOPES)
    creds = flow.run_local_server(
        port=0,
        authorization_prompt_message="",
        timeout_seconds=SIGN_IN_TIMEOUT_SECONDS,
        prompt="consent",
        access_type="offline",
    )
    save_token(creds)
    write_sign_in_state({"signed_in_at": datetime.now().astimezone().isoformat(timespec="seconds")})
    logging.info("Signed in to Google; token saved")


def read_sign_in_state():
    return (_read_json(SIGN_IN_STATE_FILE) if SIGN_IN_STATE_FILE.exists() else None) or {}


def write_sign_in_state(state):
    SIGN_IN_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def login_expires_at():
    """Estimated expiry of the OAuth sign-in, or None (service account / unknown)."""
    if service_account_info():
        return None
    try:
        signed_in = datetime.fromisoformat(read_sign_in_state()["signed_in_at"])
    except (KeyError, ValueError, TypeError):
        return None
    return signed_in + timedelta(days=OAUTH_LOGIN_DAYS)


def warn_if_login_expiring():
    expires = login_expires_at()
    if expires is None:
        return
    state = read_sign_in_state()
    left = expires - datetime.now().astimezone()
    if timedelta(0) < left <= timedelta(hours=EXPIRY_WARNING_HOURS) and not state.get("expiry_warned"):
        day = "today" if expires.date() == datetime.now().date() else "tomorrow"
        notify(
            "Google login expires " + day,
            f"Uploads to the Sheet will stop around {format_date_time(expires)[1]} {day}. "
            "Click the tray icon to sign in again now.",
        )
        state["expiry_warned"] = True
        write_sign_in_state(state)


# ---------------------------------------------------------------- syncing --

_tab_cache = {}  # tab title -> worksheet, so each sync doesn't re-check headers


def month_of(row):
    """First day of the row's local month (from the dd/mm/yyyy date, else the UTC timestamp)."""
    try:
        return datetime.strptime(row.get("date") or "", "%d/%m/%Y").date().replace(day=1)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(row.get("timestamp_utc") or "").astimezone().date().replace(day=1)
    except ValueError:
        return None


def tab_title(month):
    return month.strftime(MONTH_TAB_FORMAT) if month else "Unknown date"


def open_spreadsheet(creds):
    if not SPREADSHEET_ID:
        raise RuntimeError(f"no spreadsheet_id in {CONFIG_FILE.name}; copy config.example.json and fill it in")
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID)


def month_worksheet(spreadsheet, title):
    worksheet = _tab_cache.get(title)
    if worksheet is not None:
        return worksheet
    try:
        worksheet = spreadsheet.worksheet(title)
        if not worksheet.acell("A1").value:
            worksheet.update([SHEET_HEADERS], "A1", value_input_option="RAW")
    except gspread.WorksheetNotFound:
        # index=0 puts the newest month first.
        worksheet = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(SHEET_HEADERS), index=0)
        worksheet.update([SHEET_HEADERS], "A1", value_input_option="RAW")
        worksheet.freeze(rows=1)
        logging.info("Created monthly tab '%s'", title)
    _tab_cache[title] = worksheet
    return worksheet


def sheet_values(row):
    percent = row["battery_percent"]
    percent = int(percent) if str(percent).isdigit() else percent
    return [row["check_id"], row["date"], row["time"], row["timestamp_utc"], percent, row["status"]]


def sync_pending(full_check=False):
    """Send unsent rows. full_check compares *every* local row with the Sheet
    and re-sends any that are missing there, even ones marked as sent."""
    with sync_lock:
        rows = all_rows() if full_check else pending_rows()
        if not rows:
            set_sync_status(True, f"Google Sheets: up to date ({now_text()})")
            return
        creds = load_credentials()
        try:
            spreadsheet = open_spreadsheet(creds)
        except RefreshError as exc:
            raise AuthRequired(f"Google rejected the credentials: {exc}") from exc
        except (PermissionError, gspread.SpreadsheetNotFound) as exc:
            who = getattr(creds, "service_account_email", "the signed-in account")
            raise AuthRequired(f"no access to the Sheet; share it with {who} as Editor") from exc

        by_month = {}
        for row in rows:
            by_month.setdefault(month_of(row), []).append(row)
        sent = 0
        try:
            # Oldest month first, so with index=0 the newest tab ends up leftmost.
            for month in sorted(by_month, key=lambda m: m or datetime.min.date()):
                month_rows = by_month[month]
                worksheet = month_worksheet(spreadsheet, tab_title(month))
                # ID lookup makes retries safe if a prior append succeeded but its reply was lost.
                existing = set(filter(None, worksheet.col_values(1)[1:]))
                mark_synced([r["check_id"] for r in month_rows if r["check_id"] in existing and r.get("synced") != "yes"])
                to_send = [r for r in month_rows if r["check_id"] not in existing]
                # Batch appends: one API call per chunk instead of one per row (Sheets allows ~60 writes/min).
                for start in range(0, len(to_send), SYNC_BATCH_SIZE):
                    chunk = to_send[start:start + SYNC_BATCH_SIZE]
                    worksheet.append_rows([sheet_values(r) for r in chunk], value_input_option="RAW", table_range="A1")
                    mark_synced([r["check_id"] for r in chunk])
                sent += len(to_send)
        except Exception:
            _tab_cache.clear()  # a tab may have been deleted or renamed; look it up again next time
            raise
        if full_check:
            logging.info("Full check: %d local row(s), %d were missing from the Sheet and were sent", len(rows), sent)
            update_dashboard(spreadsheet)  # a network error here retries the whole daily check later
            write_sync_state({"last_full_check": datetime.now().date().isoformat()})
        else:
            logging.info("Synced %d queued check(s)", sent)
        set_sync_status(True, f"Google Sheets: synced {sent} row(s) ({now_text()})")


def update_dashboard(spreadsheet):
    """Recompute the stats and rebuild the Dashboard tab's charts."""
    try:
        dashboard.publish(spreadsheet, dashboard.compute(all_rows()))
        logging.info("Dashboard updated")
    except NETWORK_ERRORS:
        raise
    except Exception:
        logging.exception("Dashboard update failed; the data sync itself is fine")


def write_sync_state(state):
    SYNC_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def full_check_due():
    state = (_read_json(SYNC_STATE_FILE) if SYNC_STATE_FILE.exists() else None) or {}
    return state.get("last_full_check") != datetime.now().date().isoformat()


def internet_up():
    try:
        socket.create_connection(("sheets.googleapis.com", 443), timeout=5).close()
        return True
    except OSError:
        return False


def try_sync(full_check=None):
    """Sync and report the outcome: "ok", "auth" (sign-in needed), "offline" or "error".
    By default the once-a-day full check runs whenever it's due."""
    global auth_needed, auth_needed_since, last_sign_in_notice, offline
    if full_check is None:
        full_check = full_check_due()
    try:
        sync_pending(full_check)
    except NETWORK_ERRORS as exc:
        if not offline:
            logging.warning("No internet; rows stay queued and will be sent when it returns: %s", exc)
        offline = True
        set_sync_status(False, f"Offline: {len(pending_rows())} queued, sends when internet returns")
        return "offline"
    except AuthRequired as exc:
        logging.error("Google Sheets sync needs sign-in; rows remain queued locally: %s", exc)
        queued = len(pending_rows())
        set_sync_status(False, f"Google sign-in needed, {queued} queued (click icon)")
        now = time.monotonic()
        if not auth_needed:
            auth_needed_since, last_sign_in_notice = now, None
        # First alert an hour after it breaks, then every two hours until fixed.
        due = (
            now - auth_needed_since >= SIGN_IN_NOTICE_DELAY_SECONDS
            if last_sign_in_notice is None
            else now - last_sign_in_notice >= SIGN_IN_REMINDER_SECONDS
        )
        if due:
            notify(
                "Google sign-in needed",
                f"Battery readings aren't reaching your Google Sheet ({queued} waiting). "
                "Click the HyperX tray icon to sign in again.",
            )
            last_sign_in_notice = time.monotonic()
        auth_needed, offline = True, False
        return "auth"
    except Exception as exc:
        logging.error("Google Sheets sync failed; rows remain queued locally: %s", exc)
        set_sync_status(False, f"Google Sheets: sync failed, {len(pending_rows())} queued")
        return "error"
    if offline:
        logging.info("Internet is back; queued rows sent")
    auth_needed, offline = False, False
    warn_if_login_expiring()
    return "ok"


def sign_in_then_sync():
    def run():
        set_sync_status(False, "Google sign-in: waiting for browser...")
        try:
            with sync_lock:
                interactive_sign_in()
        except Exception as exc:
            logging.error("Google sign-in failed: %s", exc)
            set_sync_status(False, f"Google sign-in failed: {exc}"[:120])
            return
        # Right after sign-in, send everything the Sheet is missing.
        try_sync(full_check=True)
    threading.Thread(target=run, name="google-sign-in", daemon=True).start()


# ------------------------------------------------------------------ worker --

def now_text():
    return format_date_time(datetime.now())[1]


def check_once():
    global latest_text
    now = datetime.now().astimezone()
    utc_now = now.astimezone(timezone.utc)
    date_text, time_text = format_date_time(now)
    percent, status = read_battery()
    latest_text = f"Latest: {percent}% at {date_text} {time_text}" if percent is not None else f"Latest: unavailable ({status})"
    append_local({
        "check_id": str(uuid.uuid4()),
        "date": date_text,
        "time": time_text,
        "timestamp_utc": utc_now.isoformat(timespec="seconds"),
        "battery_percent": "" if percent is None else percent,
        "status": status,
        "synced": "no",
    })
    logging.info("Battery check: %s (%s) at %s %s", "unavailable" if percent is None else f"{percent}%", status, date_text, time_text)
    refresh_tray()


def worker():
    global full_sync_requested
    logging.info("Battery monitor started; polling every %d seconds", CHECK_INTERVAL_SECONDS)
    first = True
    while not stop_event.is_set():
        try:
            check_once()
        except Exception:
            # e.g. the CSV is open and locked in Excel; keep the worker alive and try next time.
            logging.exception("Battery check could not be saved")
        result = try_sync()
        if first and result == "auth" and not service_account_info() and client_file() is not None:
            sign_in_then_sync()  # prompt once at startup; later only from the tray menu
        first = False
        deadline = time.monotonic() + CHECK_INTERVAL_SECONDS
        while not stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # While offline, wake every 30 s to see if the internet is back.
            wait = min(remaining, OFFLINE_RETRY_SECONDS) if offline else remaining
            if sync_event.wait(timeout=wait):  # tray "Sync now" wakes us early
                sync_event.clear()
                full, full_sync_requested = full_sync_requested, False
                if not stop_event.is_set():
                    try_sync(full_check=True if full else None)
            elif offline and internet_up():
                logging.info("Internet connection detected; sending queued rows")
                try_sync()


# -------------------------------------------------------------------- tray --

def make_icon_image(ok=True):
    screen = (60, 145, 220, 255) if ok else (215, 60, 50, 255)
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((13, 8, 51, 57), radius=8, outline=(35, 35, 35, 255), width=3, fill=(235, 245, 255, 255))
    draw.rectangle((20, 17, 44, 43), fill=screen)
    draw.rounded_rectangle((24, 46, 40, 51), radius=2, fill=(80, 80, 80, 255))
    return image


def notify(title, message):
    """Windows notification from the tray icon (also kept in the log)."""
    logging.info("Notification: %s - %s", title, message)
    if tray_icon is None or not getattr(tray_icon, "HAS_NOTIFICATION", False):
        return
    try:
        tray_icon.notify(message, f"HyperX Battery Logger: {title}"[:63])
    except Exception as exc:
        logging.error("Could not show notification: %s", exc)


def request_dashboard_update(icon, item):
    global full_sync_requested
    full_sync_requested = True
    sync_event.set()


def open_sheet(icon, item):
    os.startfile(f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit")


def primary_action(icon, item):
    """Left-click on the tray icon: sign in when needed, otherwise sync now."""
    if auth_needed:
        sign_in_then_sync()
    else:
        sync_event.set()


def primary_text(item):
    if auth_needed:
        return "Sign in to Google (required)"
    expires = login_expires_at()
    if expires is not None:
        return f"Sync now (login good until {expires:%d/%m} {format_date_time(expires)[1]})"
    return "Sync queued rows now"


def set_sync_status(ok, text):
    global sync_ok, sync_text
    changed = ok != sync_ok
    sync_ok, sync_text = ok, text
    if tray_icon is not None and changed:
        try:
            tray_icon.icon = make_icon_image(ok)
        except Exception:
            pass
    refresh_tray()


def refresh_tray():
    if tray_icon is None:
        return
    try:
        tray_icon.title = f"HyperX Battery Logger\n{latest_text}\n{sync_text}"[:127]
        tray_icon.update_menu()
    except Exception:
        pass


def open_file(path):
    try:
        os.startfile(str(path))
    except Exception as exc:
        logging.exception("Could not open %s: %s", path, exc)


def quit_app(icon, item):
    logging.info("Battery monitor stopped from tray menu")
    stop_event.set()
    sync_event.set()
    icon.stop()


def already_running():
    """Named mutex so two copies never write the same CSV."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    already_running.handle = kernel32.CreateMutexW(None, False, "Local\\HyperXBatteryLogger")
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def main():
    global tray_icon
    if already_running():
        logging.info("Another instance is already running; exiting")
        return
    ensure_csv()
    menu = pystray.Menu(
        pystray.MenuItem(lambda item: latest_text, None, enabled=False),
        pystray.MenuItem(lambda item: sync_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(primary_text, primary_action, default=True),
        pystray.MenuItem("Open Google Sheet", open_sheet),
        pystray.MenuItem("Update dashboard now", request_dashboard_update),
        pystray.MenuItem("Sign in to Google...", lambda icon, item: sign_in_then_sync()),
        pystray.MenuItem("Open activity log", lambda icon, item: open_file(LOG_FILE)),
        pystray.MenuItem("Open CSV data", lambda icon, item: open_file(CSV_FILE)),
        pystray.MenuItem("Quit", quit_app),
    )
    tray_icon = pystray.Icon("HyperXBatteryLogger", make_icon_image(), "HyperX Battery Logger", menu)
    threading.Thread(target=worker, name="battery-worker", daemon=True).start()
    tray_icon.run()
    stop_event.set()


if __name__ == "__main__":
    main()
