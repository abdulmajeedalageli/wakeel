# Google Sheets setup (manual steps)

The bot reads your property sheet with a Google **service account**: a robot Google account that only you create and control.
You share the sheet with it as **Viewer**, and the bot also asks Google for **read-only** access.
So the bot can never change or delete anything in your sheet.

## 1. Create a Google Cloud project and enable the Sheets API
1. Go to <https://console.cloud.google.com> and sign in with your Google account.
2. Use the project picker at the top to create a **New project** (e.g. `frame-bot`). No billing is needed for this.
3. Go to **APIs & Services → Library**, search for **Google Sheets API**, and click **Enable**.

## 2. Create the service account and its key
1. Go to **IAM & Admin → Service Accounts → Create service account**.
   - Name: `frame-sheet-reader`. Click **Create and continue**. Skip the optional roles and user access, then click **Done**.
2. Open the new service account → **Keys** tab → **Add key → Create new key → JSON** → **Create**.
   A `.json` file downloads.
3. In the project folder, create a `secrets` folder, then move the file there and rename it to `service-account.json`:
   `whatsapp-bot\secrets\service-account.json`
   The `secrets/` folder is git-ignored. **Never share this file or commit it.**
4. Copy the service account's email address. It looks like `frame-sheet-reader@frame-bot.iam.gserviceaccount.com`.

> If key creation is blocked with an "organization policy" message, your Google account belongs to an
> organization that disables service-account keys. Use a personal Google account, or ask your admin.

## 3. Prepare the sheet
1. Upload `test_data/sample_properties.xlsx` to Google Drive and open it.
2. **File → Save as Google Sheets.** This step is required, because the bot can't read `.xlsx` files, only real Google Sheets.
3. Click **Share**, paste the service account email, choose **Viewer**, untick "Notify people", then click **Share**.
4. Copy the sheet ID from the address bar:
   `https://docs.google.com/spreadsheets/d/`**`1AbC...xyz`**`/edit`
   The ID is the part between `/d/` and `/edit`.

## 4. Fill in `.env`
```
GOOGLE_SERVICE_ACCOUNT_FILE=secrets/service-account.json
GOOGLE_SHEET_ID=1AbC...xyz
SHEET_TAB_NAME=Properties
SHEET_REFRESH_MINUTES=10
```

## 5. Check it works
```
python -m scripts.try_search
```
With the sample sheet, you should see `Loaded 33 properties (30 available), 2 rows skipped.`
The two skipped rows are the deliberate TEST rows (sheet rows 33 and 34), and the log line above them says why each was skipped.

## Using your real sheet later
- **Different or Arabic headers:** change the right-hand side of `"columns"` in `config/sheet.json` to match your header row exactly. Capital letters and extra spaces don't matter.
- **Arabic values** (e.g. شقة, إيجار, متاح): common ones are already listed under `"values"` in `config/sheet.json`. Add more if the log warns about an unknown status or type.
- **New districts:** add them to `config/locations.json` with their Arabic spelling. The log lists any district it doesn't recognise.
- Restart the app after editing either file. Sheet **data** changes don't need a restart, because they're picked up every `SHEET_REFRESH_MINUTES`.
