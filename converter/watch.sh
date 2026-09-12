#!/bin/sh
set -u

ROOT="${INPUT_ROOT:-/data/input}"
DPI="${CONVERTER_DPI:-170}"
RASTER_QUALITY="${CONVERTER_RASTER_QUALITY:-78}"
FINAL_QUALITY="${CONVERTER_JPEG_QUALITY:-70}"
MIN_AGE_MINUTES="${CONVERTER_MIN_AGE_MINUTES:-1}"
SLEEP_SECONDS="${CONVERTER_SCAN_INTERVAL_SECONDS:-15}"

log() {
  echo "[converter] $*"
}

log "watching: $ROOT"
log "settings: DPI=$DPI raster_quality=$RASTER_QUALITY jpeg_quality=$FINAL_QUALITY min_age=${MIN_AGE_MINUTES}m interval=${SLEEP_SECONDS}s"

while true; do
  find "$ROOT" -type f -iname "*.pdf" \
    ! -path "*/done/*" \
    ! -path "*/failed/*" \
    -mmin +"$MIN_AGE_MINUTES" 2>/dev/null |
  while IFS= read -r pdf; do
    dir=$(dirname "$pdf")
    base=$(basename "$pdf" .pdf)

    log "rasterizing: $pdf"

    if pdftoppm -jpeg -r "$DPI" -jpegopt quality="$RASTER_QUALITY" "$pdf" "$dir/$base" 2>/dev/null; then
      if command -v jpegoptim >/dev/null 2>&1; then
        for jpg in "$dir"/"$base"-*.jpg; do
          [ -e "$jpg" ] || continue
          jpegoptim --max="$FINAL_QUALITY" --strip-all --quiet "$jpg" || true
        done
      fi

      mkdir -p "$dir/done"
      mv -f "$pdf" "$dir/done/"
      log "finished: $base"
    else
      mkdir -p "$dir/failed"
      mv -f "$pdf" "$dir/failed/"
      log "FAILED (moved to failed/): $pdf"
    fi
  done

  sleep "$SLEEP_SECONDS"
done
