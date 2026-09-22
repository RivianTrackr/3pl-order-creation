# 3PL Order Sync: Syncore PO → 3PL Central orders

Every 5 minutes this service checks [Syncore](https://docs.syncore.app) for new purchase orders. Each PO from the configured vendor becomes an order in [3PL Central / Extensiv 3PL Warehouse Manager](https://developer.3plcentral.com) for the configured warehouse, so inventory orders don't have to be re-keyed by hand.

The vendor, the warehouse, and which SKUs count as inventory are all settings.

A password-protected admin UI manages the settings, each client's 3PL Central credentials, and shows what happened to every PO.

## What it does

For each PO created after go-live:

1. **Waits** until the PO hasn't changed for the settle time (default 15 minutes), so a PO still being filled in isn't sent half-finished.
2. **Filters.** The vendor must match the setting, and only lines whose SKU matches the SKU pattern (default `INV|OD`) are sent. POs with no matching lines are skipped. If a skipped PO is edited later, it's checked again.
3. **Finds the client.** The PO's job names a Syncore contact; that contact's **client group** identifies the company, so store billing, employees and dropship contacts all map to one client on the **Clients** page. A PO from a client that isn't set up is **skipped** (no alert). The client still appears on the Clients page, and once it's finished a skipped PO can be sent with **Process** on the dashboard.
4. **Creates the 3PL order.**
   - The reference number is `<job number>-<PO number>`, e.g. `12345-3`. If the client already has an order with that reference number **or** PO number, it's reused instead of creating a second one.
   - Shipping comes from each PO: the ship-to address, the customer email and phone from Critical Comments, and the carrier and service from Ship Via. The Ship Via is matched against the warehouse's carrier list in 3PL Central ("USPS Priority Mail" becomes carrier `USPS` plus that service's code). If there's no confident match, the order is **not** created; it's reported as an error, alerted, and retried after an override is added. The PO's shipping instructions and critical comments go into the order's Warehouse Instructions; Carrier Instructions is left empty.
5. **Checks the SKUs and inventory.**
   - Each PO SKU is matched to the client's item in 3PL Central: exactly when the SKU is the same, otherwise by base SKU plus size, since Syncore writes the size into the SKU (`ABC123-2XL`) where 3PL Central uses a variant code with the size in the description (`ABC123-15570`, "... - XXL"). Sizes like 2XL/XXL and M/Medium are treated as the same.
   - A SKU with no clear match stops the order before it's created, with an alert naming the SKU and the sizes that do exist.
   - If every line has enough stock, the order is **Completed**.
   - If any line is short, the order is left **Open**, and an alert email lists the short SKUs. Every later run re-checks it and completes it once stock arrives (switch off with "Complete open orders when stock arrives").
6. **Records the Transaction Number** (the 3PL Central order id) against the PO here: on the dashboard, in the PO's timeline and in any alert about it. Nothing is written back to Syncore.

Once a day (after the hour set in Settings) a **summary email** goes out: orders created and completed, anything waiting for stock, anything needing attention, and clients still to be set up. The same run makes a **backup** of the database, kept for 14 days by default.

Any error sends one alert email per distinct problem and is retried on the next run. After 20 failed attempts it stops retrying; use **Retry** on the dashboard once the cause is fixed. Each step is recorded, so a retry resumes where it stopped.

## Admin UI

| Page | What's there |
|---|---|
| Dashboard | Setup checklist, counts, every PO's status and transaction number (click a reference for its timeline), Retry, Run now, "Test a purchase order" (preview or process one PO; opens its live log), Go live |
| Clients | Every client shipping through the warehouse, with what each still needs (Syncore client ID, 3PL Customer ID, Client ID, Client Secret) before it can be activated. Clients are added automatically when their first qualifying PO arrives and linked by name to an existing entry; more can be added by name. Test connection, pause/activate. |
| Ship Via | The warehouse's carriers and service codes loaded from 3PL Central (refreshed automatically every 6 hours), a tester that shows how any Ship Via text will be matched, and overrides for your team's shorthand (e.g. `UPS GRND` → UPS / Ground). A shipping account number written as the last word (`UPS GRND C713X7`) becomes the order's account; a last word that names a service in 3PL Central (`UPS 3DAY`) stays part of the shorthand. |
| Settings | Syncore API key, vendor, SKU pattern, 3PL user login and warehouse, SendGrid and alert recipients, daily summary, pause, dry run, settle time, backups, time zone. Includes a Syncore connection test, a test email and "Send summary now". |
| Logs | **Runs:** every scheduled run, Run now and PO test, with its result counts and full log output (each API call with status and timing, and the bodies of requests that create or change data). **PO events:** a searchable timeline of every step taken on each PO. **Emails:** every alert with recipients, body and whether SendGrid accepted it. Kept for `Keep logs (days)`, 90 by default. |
| Users | Admin logins, change password, activity log |

**Security:**
- Passwords are hashed with bcrypt, and failed logins are throttled.
- Sessions use signed, HttpOnly cookies that expire after 12 idle hours. Changing a password signs that user out everywhere.
- Every form is CSRF-protected.
- API keys and client secrets are encrypted in the database with `TPLSYNC_ENCRYPTION_KEY` and are never shown again once saved.
- The UI listens on 127.0.0.1 only. Serve it through Nginx with HTTPS.

## Deploy (Linode, Debian/Ubuntu)

```bash
git clone <repo> ~/3pl-order-creation && cd ~/3pl-order-creation
sudo bash deploy/setup.sh                         # installs to /opt/tplsync, generates keys, starts the admin
sudo -u tplsync /opt/tplsync/venv/bin/python -m tplsync create-user yourname
```

Then:

1. **Nginx and HTTPS:** copy `deploy/nginx-tplsync.conf`, set your domain, and run `certbot --nginx -d <domain>`.
2. **Configure:** sign in, fill in Settings and add each client.
3. **Preview a real PO:** use "Test a purchase order" on the dashboard.
4. **Process one test PO for real** and check the "First live test" list below.
5. **Go live:** click **Go live now** on the dashboard, then start the timer:
   ```bash
   sudo systemctl enable --now tplsync.timer
   ```

To update, pull and re-run `deploy/setup.sh`. `.env` and the database are left alone.

**Backups:** the database is copied to `/opt/tplsync/backups` once a day (change the location with `BACKUP_DIR`, the retention in Settings). Copy those off the server periodically. Back up `/opt/tplsync/.env` separately and keep it safe: without `TPLSYNC_ENCRYPTION_KEY` the saved secrets can't be decrypted, and a backup alone won't restore them.

Logs: `journalctl -u tplsync -f` (runs) and `journalctl -u tplsync-admin -f` (UI).

## First live test

Some API behaviour is documented but hasn't been tested against the live systems yet. Before starting the timer, process one test PO and check:
- **Completion:** the 3PL order shows as Completed, not Open, and was not confirmed or shipped.
- **Stock check:** it reflects real stock. Try a PO with more quantity than is on hand.

## Command line

```bash
python -m tplsync gen-keys                        # print new .env keys
python -m tplsync create-user NAME                # add a login or reset its password
python -m tplsync admin                           # run the admin UI
python -m tplsync run [--dry-run]                 # one polling run (the timer does this)
python -m tplsync run --job 12345 --po 67890      # process one PO now
python -m tplsync match-clients                   # link clients to Syncore client groups by name
python -m tplsync backup                          # back up the database now
python -m tplsync summary                         # send the daily summary now
python -m tplsync status                          # recent POs
python -m tplsync reset 67890                     # retry a PO that stopped retrying
```

## Development

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements-dev.txt
./venv/bin/python -m tplsync gen-keys > .env && echo "ADMIN_COOKIE_SECURE=false" >> .env
./venv/bin/python -m tplsync create-user admin
./venv/bin/python -m tplsync admin                # http://127.0.0.1:8120
./venv/bin/python -m pytest
```
