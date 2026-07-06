#!/usr/bin/env bash
# Runs the scraper for up to 5h45m (20700s) — GitHub Actions jobs cap at 6h.
# Checks progress afterward. Only marks DONE if villages were actually
# discovered AND none remain — avoids false-completing on an empty/failed run.
set -e

if [ -f DONE ]; then
  echo "Crawl already complete — nothing to do."
  exit 0
fi

echo "Starting/resuming crawl..."
timeout 20700s python3 scrape_jharkhand.py || true

echo "---- Progress ----"
python3 scrape_jharkhand.py --stats

total=$(python3 scrape_jharkhand.py --stats | grep "^villages:" | awk '{print $NF}')
remaining=$(python3 scrape_jharkhand.py --stats | grep "remaining" | awk '{print $NF}')
echo "Total villages found: $total | Remaining: $remaining"

if [ "$total" -gt 0 ] && [ "$remaining" = "0" ]; then
  echo "All villages scraped. Marking DONE."
  touch DONE
else
  echo "Not done yet (or nothing scraped this run) — will resume next scheduled run."
fi
