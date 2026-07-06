#!/usr/bin/env bash
# Runs the scraper for up to ~5h45m (GitHub Actions jobs cap at 6h), then
# checks progress. If everything is done, drops a DONE marker so future
# scheduled runs no-op instead of re-scraping.
set -e

if [ -f DONE ]; then
  echo "Crawl already complete — nothing to do."
  exit 0
fi

echo "Starting/resuming crawl..."
timeout 5h45m python3 scrape_jharkhand.py || true

echo "---- Progress ----"
python3 scrape_jharkhand.py --stats

remaining=$(python3 scrape_jharkhand.py --stats | grep "remaining" | awk '{print $NF}')
echo "Villages remaining: $remaining"

if [ "$remaining" = "0" ]; then
  echo "All villages scraped. Marking DONE."
  touch DONE
fi
