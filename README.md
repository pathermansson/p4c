# Zaptec charging cost dashboard

A GitHub Actions job runs once a day. It logs in to the Zaptec API, fetches your charge sessions, matches each charging period to the Nord Pool spot price (hourly or 15-minute) and writes `docs/data.json`. `docs/index.html` shows that data as a dashboard (monthly totals, cost breakdown, latest sessions).

## Setup (about 10 minutes)

1. **Create a GitHub repo** and upload everything in this folder (keep the folder structure, including `.github/workflows/`).
2. **Add your Zaptec login as secrets:** Repo → Settings → Secrets and variables → Actions → New repository secret:
   - `ZAPTEC_USERNAME` – the email you use for portal.zaptec.com
   - `ZAPTEC_PASSWORD`
3. **Edit `config.json`** with your real prices (see below).
4. **Run it once:** Actions tab → "Update charging dashboard" → Run workflow. After that it runs daily at 04:30 UTC.
5. **Publish the dashboard:** Settings → Pages → Deploy from branch → `main` / `/docs`. Your dashboard appears at `https://<user>.github.io/<repo>/`.

> **Privacy:** GitHub Pages from a free private repo is not available, so the page and `data.json` (kWh, cost, charging times) would be public. If you don't want that, skip Pages, download `docs/` and open `index.html` locally (via any simple web server), or host it behind a login on a service you control.

## What goes into the price (config.json)

All values in öre/kWh **excluding VAT**, taken from your invoices:

| Field | Meaning |
|---|---|
| `supplier_markup_ore` | Your electricity company's per-kWh markup |
| `grid_fee_energy_ore` | The per-kWh part of your grid (nät) fee; set separate day/night values if your grid company uses them (`day_hours` = local hours that count as day) |
| `energy_tax_ore` | Energy tax (elskatt) per kWh |
| `vat_rate` | 0.25 |

The values in the file are **placeholders**. Replace them. Fixed monthly fees and power (effekt) charges are not included, since they aren't caused by the car alone.

The spot price comes from elprisetjustnu.se, which publishes the raw Nord Pool price (SEK/kWh, excl. VAT, converted from EUR), so it can differ slightly from what your supplier bills.

## First-run check (important)

I built the Zaptec part from Zaptec's published API definition but could not test against a real account. Run once locally with `--debug`:

```
export ZAPTEC_USERNAME=...  ZAPTEC_PASSWORD=...
python update.py --debug
```

That saves one raw session to `data/raw_session_sample.json`. Check that `Energy` matches what the Zaptec portal shows for that session. The script handles both cumulative and incremental `EnergyDetails` values, and falls back to spreading the total evenly over the session if there are none. If a month looks wrong, compare it with your invoice.

If you have several chargers, set `charger_id` in `config.json`.

## Preview without data

`python update.py --demo` writes synthetic data to `docs/data.json` so you can see the layout. Real runs overwrite it.
