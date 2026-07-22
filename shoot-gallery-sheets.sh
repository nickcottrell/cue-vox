#!/usr/bin/env bash
# Screenshot the 3 views (storyboard / contact-sheet / transcription) for each
# gallery in cue-vox. Galleries must already exist in vault.db (drop a folder or
# POST /api/gallery/from-keeper first). Output PNGs to ~/Desktop/sol-gallery-sheets/.
# Usage: ./shoot-gallery-sheets.sh  q01_is-it-real-whose q02_what-is-it ...
#        (no args -> the 10 Sol Q&A galleries)
set -u
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HOST="http://127.0.0.1:3000"
OUT="$HOME/Desktop/sol-gallery-sheets"
mkdir -p "$OUT"
FLAGS="--headless --disable-gpu --no-sandbox --hide-scrollbars --no-first-run \
--no-default-browser-check --disable-background-networking --disable-component-update \
--disable-sync --disable-crash-reporter --metrics-recording-only --no-pings \
--disable-features=Translate,OptimizationHints"
GALLERIES=${@:-"q01_is-it-real-whose q02_what-is-it q03_congress-role \
q04_whistleblower-protection q05_retaliation-meaning q06_what-disclosure-costs \
q07_why-academia-resists q08_how-to-respond q09_why-it-matters q10_personal-conclusions"}
i=0
for g in $GALLERIES; do
  i=$((i+1)); n=$(printf "%02d" $i)
  for pair in "storyboard:case-study" "contactsheet:contact-sheet" "transcription:transcription"; do
    label=${pair%%:*}; route=${pair##*:}
    f="$OUT/${n}_${g}__${label}.png"
    ( "$CHROME" $FLAGS --user-data-dir=/tmp/cshot --window-size=1280,5200 \
        --virtual-time-budget=6000 --screenshot="$f" "$HOST/$route/$g" >/dev/null 2>&1 ) &
    cp=$!; ( sleep 40 && kill $cp 2>/dev/null ) & wp=$!
    wait $cp 2>/dev/null; kill $wp 2>/dev/null
    [ -s "$f" ] && echo "OK   ${n}_${g}__${label}.png" || echo "FAIL ${n}_${g}__${label}"
  done
done
# crop each PNG down to its actual content length (trim bottom whitespace)
python3 "$(dirname "$0")/trim-sheets.py" "$OUT"
echo "Done -> $OUT"
