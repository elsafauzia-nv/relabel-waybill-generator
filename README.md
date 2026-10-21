# Cross Border Relabel waybill capture (Operator V2 QA)

Scans a tracking ID in the Operator V2 QA **Cross Border Relabel Tool** through the real UI, captures
the `PRINTER:PRINT` event that the scan sends to the printer, fills the captured template with the
captured job data, saves the final ZPL, and renders it through Labelary as a PNG/PDF waybill.

There are two ways to use it:

* **Web UI (GitHub Pages) + local agent.** Open the page, pick country and vendor, enter the tracking ID,
  click Start, follow the live status, then view, copy and download the ZPL and waybill.
* **Terminal.** `./capture_waybill.py` asks the same questions in the terminal.

Both run the same workflow code.

* **Page:** https://elsafauzia-nv.github.io/relabel-waybill-generator/
* **Repository:** https://github.com/elsafauzia-nv/relabel-waybill-generator. The Python agent is at
  the root, and the web page is in `docs/`.

## For QA members: getting started

You need a **Mac** with **Google Chrome** (or Edge), [**uv**](https://docs.astral.sh/uv/) (`brew install uv`),
and the **nv-label-printer app**, which must be **running and signed in for every scan** (see step 5).
Node.js is optional: with it, templates are filled with the printer app's own `label.js`; without it,
with the verified Python copy, which gives byte-identical output.

1. Get the code and set it up, once:
   ```bash
   git clone https://github.com/elsafauzia-nv/relabel-waybill-generator.git && cd relabel-waybill-generator && ./setup.sh
   ```
   (Without git: *Code → Download ZIP* on GitHub, unzip, then run `./setup.sh` in that folder.)
   To update later, run `git pull` in the folder.
2. Start the agent and keep its window open while you use the page:
   ```bash
   cd <path to>/relabel-waybill-generator && ./agent.py
   ```
3. Open **https://elsafauzia-nv.github.io/relabel-waybill-generator/** in Chrome or Edge. Click
   **Allow** if Chrome asks about local network access. The status pill should say *Local agent
   connected*.
4. The first scan opens the agent's own Chrome window. Sign in to Operator V2 there with **your own**
   account. It is remembered in `.browser-profile/` inside your copy.
5. Before every scan, the **nv-label-printer app must be running and signed in**: open it, then in
   Operator V2 go to *Settings → Open label printer*. Operator V2 only sends a scan's print job after a
   printer app has answered its printer status request. Without a running, signed-in app it waits
   forever, there is nothing to capture, and the run fails at *Matching print event*. This applies to
   **Capture only** runs too.
6. Your Operator V2 account needs a **Socket Printer** as its active printer, and it must be the one
   whose address matches your printer app. Otherwise there is no print event to capture. Tick
   **Capture only** if you don't want a label printed. If no label printer is selected in the app,
   real print jobs only queue there; nothing comes out.

Waybills are saved in `output/` inside your copy. **Never share** `.browser-profile/`: it holds your
signed-in Operator V2 session. `output/` holds QA shipment data.

## Web UI and local agent

```
GitHub Pages (static page)  ──HTTP──>  agent.py on 127.0.0.1:8765  ──>  Chrome (persistent profile)
https://elsafauzia-nv.github.io/…          restricted local API            Operator V2 relabel tool
  form, live status, ZPL, preview          runs capture_waybill.execute    PRINTER:PRINT capture
                                           output/<COUNTRY>_<VENDOR>_<TID>/  → label.js → Labelary
```

* **Where it runs.** The page is static and has no secrets. Everything else happens on your computer,
  in the agent: the browser session, the captured event, the ZPL and the files.
* **Start the agent** and keep its window open:
  ```bash
  cd <path to>/relabel-waybill-generator && ./agent.py
  ```
  By default it accepts requests from `https://elsafauzia-nv.github.io` only. Add other origins with
  `--allow-origin`, for example a locally served copy of the page:
  `--allow-origin http://127.0.0.1:8766`, served with `cd docs && python3 -m http.server 8766 --bind 127.0.0.1`.
* **Open the page** (GitHub Pages URL, see *Deploying the page*), select the country and vendor (MY
  offers JNT only), enter the tracking ID, and click **Start**. The first run opens the agent's Chrome
  window; sign in to Operator V2 there if asked, and the run continues by itself.
* **Capture only.** Ticking *Capture only, don't send the job to the printer* is the same as `--no-print`.
* **Stages shown live:** local agent → authenticated browser → submitting tracking ID → scan result →
  matching print event → generating ZPL → validating ZPL → rendering waybill → saving files. Each stage
  turns green only once it has actually completed. A failure shows Operator V2's own message, or the
  timeout or capture error, together with what to do next.
* **Results.** The **Generated ZPL** panel shows the populated ZPL, with *Copy ZPL* and *Download ZPL*.
  The **Waybill** panel shows the PNG rendered from that same ZPL, with *Download waybill* and *Download
  PDF*. Both come from the agent's saved files, the same bytes as in `output/`. If rendering fails, the
  ZPL is still shown together with the rendering error, and no fake preview is drawn.
* **Duplicate tracking IDs.** The page asks *Generate it again* or *Replace with new ID*, the same
  choices as the terminal script.

### Local API (agent.py)

| method | path | purpose |
|---|---|---|
| GET | `/api/health` | agent available, busy or not, countries and vendors |
| POST | `/api/jobs` | start a run: `{country, vendor, tracking_id, no_print, duplicate?, replace_with?}` |
| GET | `/api/jobs/<id>` | stages, log, result (ZPL text, field checks, file links) |
| GET | `/api/jobs/<id>/files/zpl\|png\|pdf` | that job's saved waybill files only |

Security:
* **Network:** the agent binds to `127.0.0.1` only, and the `Host` header must be
  `127.0.0.1:<port>` or `localhost:<port>`, which blocks DNS rebinding.
* **Origins:** only the allowed web UI origins get CORS access. Requests from other sites are refused
  (403), and job requests without an `Origin` are refused too.
* **Input:** the request body is limited to 2 KB, and only the listed fields are accepted. Country,
  vendor and tracking ID are validated with the same rules as the terminal script; FLASH is refused
  for MY.
* **What the API exposes:** no file paths, shell commands, browser control or arbitrary files. Files
  are served only by job ID and kind, from `output/`. The page never receives the access token, cookies,
  the captured `PRINTER:PRINT` payload or local paths. Only one run at a time.
* **Chrome permission:** Chrome may ask *"Allow this site to access other apps and services on this
  device / your local network"* the first time the GitHub Pages site calls the agent. Choose **Allow**.
  The agent also answers Chrome's *Private Network Access* preflight
  (`Access-Control-Allow-Private-Network: true`).
* **Browsers:** Chrome or Edge on the same computer as the agent. Safari blocks calls from an
  `https://` page to `http://127.0.0.1` and is not supported.

### Repository layout and GitHub Pages

The repository https://github.com/elsafauzia-nv/relabel-waybill-generator is **public**. It holds:

* **Root:** the Python agent and terminal script, `setup.sh`, `requirements.txt` and this README.
* **`docs/`:** the web page (`index.html`, `app.js`, `style.css`, `.nojekyll`).

GitHub Pages serves `docs/` from the `main` branch, at `https://elsafauzia-nv.github.io/relabel-waybill-generator/`.
A push to `main` that changes `docs/` republishes the page within about a minute.

`.gitignore` keeps `.browser-profile/` (your signed-in Operator V2 session), `output/` (QA shipment data),
`.venv/` and caches out of the repository. **Check `git status` before every commit**: never commit
those folders, passwords, tokens or captured payloads.

The page's origin, `https://elsafauzia-nv.github.io`, is the agent's default allowed origin. If the
agent runs on another port, open the page with `?agent=http://127.0.0.1:<port>`, or set the address
under *Agent address* on the page.

### Troubleshooting

| symptom | what to do |
|---|---|
| "Local agent unavailable" | Start `./agent.py` and keep it running, then click the status pill. Check that the page's agent address matches the agent's port. Allow Chrome's local-network prompt. |
| 403 "origin not allowed" in the agent | The page was opened from another address: add it with `--allow-origin`. |
| Stuck at "Authenticated browser" | Sign in to Operator V2 in the Chrome window the agent opened (10 minutes allowed). |
| Scan result failed | Shows Operator V2's own message, e.g. `<TID> not found`, or `No relabel label found` when the vendor is wrong. |
| "Matching print event" timeout | Operator V2 accepted the ID but sent no `PRINTER:PRINT` within 45 s. Most often, the **nv-label-printer app is not running or not signed in**: Operator V2 waits for a printer app to answer before it sends any print job. Open the app, sign it in (Operator V2 *Settings → Open label printer*), then scan again. Otherwise check the relabel tool's print log in the agent's Chrome window. |
| Print-event socket failure | Operator V2 has no Socket Printer selected, or `wss-qa.ninjavan.co` is unreachable. |
| Generating ZPL failed: unresolved placeholders | The event lacks a field the template needs; the message names it. This is an Operator V2 / template mismatch, not a capture problem. |
| Validating ZPL warnings | Remarks over the template's box, or font notes for characters the template font can't display. The waybill shows what would print. |
| Rendering failed | Labelary unreachable or rate-limited (it retries 429/5xx). The ZPL is still shown. Try again later, or paste it into labelary.com/viewer.html. |
| "busy" | Another run is in progress; one at a time. |

## Setup (once)

Requirements: macOS with Google Chrome, [uv](https://docs.astral.sh/uv/), and the **nv-label-printer**
app (`/Applications/nv-label-printer-ason.app`), **running and signed in for every scan**. Operator V2
only sends a scan's print job after a printer app has answered. Node.js is optional; with it, the
script fills the template with the app's own `label.js`, otherwise with the verified Python copy.

```bash
cd <path to>/relabel-waybill-generator
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
# only if you don't use Google Chrome (--channel ""):
.venv/bin/playwright install chromium
```

## Run

```bash
cd <path to>/relabel-waybill-generator && ./capture_waybill.py
```

The script is executable and switches to its own `.venv` by itself, so `./capture_waybill.py`, its full path from any folder, and `python3 capture_waybill.py` all work. The folder itself runs too: `cd <path to the folder containing relabel-waybill-generator> && python3 relabel-waybill-generator`.

You enter **country** (PH or MY), **vendor** (PH: JNT or FLASH; MY: JNT, chosen automatically) and
**tracking ID** once. The browser opens on the relabel page. If Operator V2 asks you to sign in, sign
in in that window and the run continues by itself; the session is kept in `.browser-profile/`.

```
Country: PH
Vendor: JNT
Tracking ID: EL4PL1562310318577590272
...
[OK] PRINTER:PRINT event captured (wss://wss-qa.ninjavan.co/socket.io/?EIO=3&transport=websocket /printers)
[OK] Matching job found (nv_tracking_id=EL4PL…, tracking_id=JT0000010917158, template=hub_shipping_label_100X150_cross_border_jnt)
[OK] Template populated (14 placeholders with the agent's label.js)
[OK] No unresolved placeholders
[OK] Duplicate tracking ID check completed (no duplicate)
[OK] ZPL saved: output/PH_JNT_EL4PL1562310318577590272.zpl
[OK] Waybill image saved: output/PH_JNT_EL4PL1562310318577590272.png
[OK] Waybill PDF saved: output/PH_JNT_EL4PL1562310318577590272.pdf
```

Flags (all optional):

| flag | effect |
|---|---|
| `--country PH --vendor JNT --tid <TID>` | skip the prompts (needed when not in a terminal) |
| `--no-print` | capture the print job but don't send it to the printer, so no label is wasted |
| `--replace-with <NEW TID>` | if `--tid` already has a waybill, replace it with this tracking ID |
| `--force` | if the tracking ID already has a waybill, generate it again |
| `--timeout 45` | seconds to wait for the `PRINTER:PRINT` event after scanning |
| `--format png\|pdf\|both` | waybill format (default both) |
| `--blank-missing-fields` | if required template fields are missing, also render a marked **preview** (debug only) |

## Output (`output/`)

Each tracking ID gets its own folder, `output/<COUNTRY>_<VENDOR>_<TRACKING_ID>/`, holding only the
waybill:

```
output/PH_JNT_EL4PL1562310318577590272/
    PH_JNT_EL4PL1562310318577590272.zpl   final ZPL, every placeholder filled
    PH_JNT_EL4PL1562310318577590272.png   Labelary render of that ZPL
    PH_JNT_EL4PL1562310318577590272.pdf
```

No JSON files are stored. A run works in a temporary `output/.staging/` folder and moves the three
files into place only after the waybill has been filled, validated, rendered and verified. A failed
run never touches an existing waybill and saves nothing, except the marked preview described below.

### Duplicate tracking IDs

Before scanning, the script checks whether `output/<COUNTRY>_<VENDOR>_<TID>/` already exists. If it
does, you choose:

1. **Replace it with a new tracking ID.** The new ID is scanned. Only when it produces a valid
   waybill is the old folder renamed to `<COUNTRY>_<VENDOR>_<NEW TID>/`, its old files deleted and the
   new waybill written into it. The script then checks that the old tracking ID is gone from the folder
   and file names and from the new ZPL. Nothing in the ZPL is search-and-replaced: the new waybill is
   generated from the new scan's captured job. If the new ID fails, the old folder stays untouched and
   the run says "Replacement NOT done".
2. **Scan the same ID again.** The **folder is kept**; only the files inside it are deleted and
   replaced by the new waybill. That happens after the new waybill succeeded. Without `--no-print`,
   scanning again prints another label.

Flat files of the tracking ID left directly in `output/` by older versions of this script are removed
at the same time. Folders of other tracking IDs, or of the same ID under another country or vendor,
are never touched.

**When the print event lacks a field the template needs,** the run fails (exit code 14) and names the
missing placeholder. With `--blank-missing-fields` it also writes
`<name>.PREVIEW-NOT-PRINTED.zpl/.png/.pdf` into the folder. That is the template with the missing
field left blank, plus a "PREVIEW - NOT THE PRINTED LABEL" banner. These files never overwrite a
waybill, and the next successful scan clears them.

## How the waybill is produced (verified on QA, 2026-10-21)

* **UI:** the tool is an iframe (`operator-react-qa.ninjavan.co/order-management/cross-border-relabel-tool.html`).
  * Vendor select: `[data-testid="cross-border-relabel-tool-testid.vendor.select"]`, options `.ant-select-item-option`.
  * Tracking ID input: `[data-testid="cross-border-relabel-tool-testid.nv-tid.input"]`. Enter submits, the same as a scanner.
* **Lookup:** `GET /<country>/3pl-oms/v1/external-xdock-order-mappings?tracking_id=…`. An unknown ID
  returns 404, and the tool shows a notification for about 2 seconds, e.g. `NVPHINVALID000 not found`
  or `No relabel label found`. The script records that text as it appears and prints it together with
  the API status.
* **Print event:** a Socket.IO frame on namespace `/printers`, sent over
  `wss://wss-qa.ninjavan.co/socket.io/?EIO=3&transport=websocket&auth=…`:
  `42/printers,<ack>["message", <room>, {"event":"PRINTER:PRINT","jobs":[…]}]`.
  * The script searches the payload structure for `PRINTER:PRINT` rather than assuming a fixed path.
  * It accepts only the job whose `nv_tracking_id` or `tracking_id` is this run's scanned ID, arriving
    after this scan.
  * Nothing from earlier runs is reused, and the access token is never logged.
* **Filling the template:** `jobs[n].template_string` is filled with that job's fields by the
  **installed agent's own `label.js#generateZpl`**, the code that prints the real label. That covers
  `{{barcode}}`, `{{sort_code[uppercase]}}`, `{{…[convert_to_zpl_hex]}}`, `limit:N`, `{{font[…]}}`,
  the agent's built-ins, and any other placeholder in the template. Only placeholders are replaced:
  ZPL commands, line breaks, barcodes, graphics and the label size are untouched.
  * **Required placeholder missing from the job:** the run fails with `unresolved template
    placeholders` and names it. The agent would print `undefined` there, or crash on a filter.
  * **Optional placeholder:** `cod_watermark_zpl` is sent by the relabel tool only for COD parcels.
    For COD it is included; for non-COD it is left empty, as the template intends. The agent itself
    would write the literal word `undefined` there.
  * **Final checks:** no `{{…}}`, `undefined` or `[object Object]` may remain, and the ZPL must run
    from `^XA` to `^XZ`.
  * **Encoding:** the template declares UTF-8 (`^CI28`), so the ZPL is saved as UTF-8 and Chinese and
    Thai text is kept intact. The agent itself cuts each character to 8 bits, so on the **physical**
    label that text is garbled; the run warns about it (agent bug).
* **Field rules.** These come from Operator V2's relabel tool; the script never changes a value, it
  checks it:
  * **Remarks:** at most 250 characters; the tool cuts longer remarks to 247 characters + `...`.
  * **Fourth sorting code:** at most 25 characters, uppercase, on its own single-line field.
  * **Goods and pieces:** goods = the item names joined by `", "`; pieces = the number of items, at
    least 1.

  A value that is incomplete in the ZPL, or over those limits, fails validation. A box in the template
  that's too small (from Labelary's lint) is a warning, because the printer would cut the text the same
  way. The current J&T template's remarks box (`^FB430,6`, font 22) can be smaller than 250 characters
  need.
* **Rendering:** the label size comes from the template name (`…_100X150_…` = 100×150 mm), and the
  resolution from `^PW` divided by the width: 800 dots ÷ 100 mm = 8 dpmm, i.e. 203 dpi.

Exit codes: 0 OK, 2 invalid tracking ID, 3 vendor mismatch, 4 Operator V2 error, 5 timeout, 6 missing
print event, 7 WebSocket failure, 8 non-socket printer, 9 ZPL extraction, 10 Labelary, 11 sign-in, 12
browser, 13 input, 14 unresolved placeholders, 15 duplicate (non-interactive, no
`--force`/`--replace-with`), 16 verification. No waybill is saved unless every step succeeded.

## Code layout

| module | responsibility |
|---|---|
| `capture_waybill.py` | terminal entry point; `execute()` runs the workflow for both the terminal and the agent |
| `agent.py` | local agent: restricted HTTP API for the web UI |
| `report.py` | progress output: terminal lines, or stage events for the web UI |
| `field_checks.py` | remarks / fourth sorting code / goods and pieces checks |
| `docs/` | the GitHub Pages UI (static HTML, CSS, JS) |
| `prompts.py` | country / vendor / tracking ID prompts, validation, duplicate decision |
| `browser.py` | browser profile, sign-in, vendor selection, scan, error notifications, lookup watcher |
| `ws_capture.py` | Socket.IO parsing, printer-socket monitoring, matching `PRINTER:PRINT` to the scan |
| `zpl_extract.py` | filling the template with the agent's `label.js`, placeholder rules, validation, encoding |
| `render_with_agent.js` | runs the installed agent's `label.js` under Node |
| `agent_label_port.py` | Python port of `label.js` (only with `--allow-python-renderer`) |
| `labelary.py` | label size and resolution from the template, Labelary rendering, lint |
| `artifacts.py` | managed output: records, staging, commit and replace, failed runs, verification |
| `errors.py` | failure kinds and exit codes |

## Limitations

* **Chinese and Thai render blank in Goods and Remarks.** The J&T template prints those fields with
  ZPL font `^A0`, which has no CJK or Thai glyphs. Labelary reports "Field data contains characters
  which the field font cannot display", and a real Zebra printer using `^A0` has the same limit. The
  text is intact in the saved ZPL; fixing the output needs a template font change.
* **MY is not verified end to end.** The page and its pre-selected JNT vendor were checked, but no MY
  tracking ID was available.
* **Only Socket Printers (version 5) can be captured.** HTTP printer boxes build the ZPL inside the
  box; the script detects that and stops with exit code 8.
* **`--no-print` blocks only `PRINTER:PRINT`.** The rest of the socket traffic passes through.
* **Labelary is external.** The label content (names and addresses) is sent to `api.labelary.com`.
