#!/usr/bin/env bash
# Upload mptracker.db to the GitHub Release "data" (used by .github/workflows/pages.yml),
# then trigger a site rebuild. Run after pipeline/scrape_transcripts.py + pipeline/build_db.py.
#
# Requires the GitHub CLI, signed in:  sudo apt install gh && gh auth login
set -euo pipefail
cd "$(dirname "$0")/.."

DB=mptracker.db
[ -f "$DB" ] || { echo "$DB not found — run pipeline/build_db.py first" >&2; exit 1; }

# fold the WAL into the main file so the copy is complete
python3 -c "import sqlite3; c = sqlite3.connect('$DB'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"

echo "Compressing $DB ..."
gzip -9 -c "$DB" > "$DB.gz"
ls -lh "$DB.gz"

if ! gh release view data >/dev/null 2>&1; then
  gh release create data --title "Data" --notes "Latest mptracker.db used to build the site. Updated by pipeline/publish_db.sh."
fi
gh release upload data "$DB.gz" --clobber
rm "$DB.gz"

echo "Triggering site rebuild ..."
gh workflow run pages.yml --ref main
echo "Done. Follow progress with:  gh run watch"
