# Car Scraper

A small local web app that scrapes used-car listings from Czech sites (sauto.cz, auto.bazos.cz by default),
lets you filter and sort them in one place, and works with any other listing site you paste in.

## Run

```
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5000 (on Windows you can double-click `run.bat`).

## Features

- Filters: brand, model (with suggestions per brand), max km, year from/to, max price (Kč),
  type (sedan, hatchback, wagon, SUV, ...), fuel, gearbox.
- Sort by price, kilometers or year. 20 results per page.
- Add a site by pasting a search-results URL. No selectors needed; listings are detected automatically.
  Tip: set the site's own filters first, then paste that page's URL.
- Follows every "next page" link until the site runs out. Results stream in live; press Stop any time.
- Optional "Read the whole ad" mode opens each matching ad to read the real mileage, year, fuel and gearbox.
- "Hide parts & accessories" removes spare parts and anything under 50 000 Kč.
- Understands Czech wording: `Najeto`, `Stav tachometru: 260 xxx km`, `132tkm`, `nafta`, `automatická`, `kombi`, ...

## Notes

- Prices are shown in Kč; listings priced in EUR are converted at a rough 25 Kč per EUR.
- Sites that need JavaScript to show their listings, or that block scrapers, will show an error instead of cars.
- Respect each site's terms of use and don't hammer sites with repeated full scans. For personal use only.
- Your site list is stored in `sites.json`.
