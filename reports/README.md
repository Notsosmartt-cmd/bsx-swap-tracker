# Reports

`index.html` charts the six-month datasets in `datasets/` and the sample scan in `samples/2026-11-20/`. `png/` has each chart as
a separate image, 2000 pixels wide.

How they are made:

1. `python3 reports/make_reports.py` reads the CSV and JSON files and writes `index.html`: one self-contained page with every
   chart drawn as inline SVG by the script itself (standard library only, no plotting package, no JavaScript, no network). Each
   bar and point carries its exact value in a tooltip.
2. The PNGs are screenshots of that page: headless Chromium (Playwright) opens it at a 2100-pixel-wide window with the page
   zoomed to 200%, and saves each chart's `<figure>` element on its own. For example:

   ```
   await page.setViewportSize({ width: 2100, height: 1200 });
   await page.goto('http://127.0.0.1:8000/reports/index.html');      // python3 -m http.server 8000 in the repo root
   await page.evaluate(() => { document.body.style.zoom = '2'; });
   await page.locator('#daily').screenshot({ path: 'reports/png/daily.png' });
   ```

3. `python3 reports/verify_reports.py` reads the values back out of `index.html` and recomputes each one from the source files:
   every daily bar, size share, pair count, monthly refund share, settlement-time bin, every point on the trade tape (amount,
   outcome, transaction id), the trade in the timeline and the accuracy figures. CI runs it on every push.

Two numbers in the charts are not in the data files: the size chart's dollar labels assume about $70,000 per Bitcoin (the median
of the project's priced trades since October was about $74,000, and about $64,000 in mid-November), and the accuracy chart's
confidence intervals come from `datasets/validation.json`.
