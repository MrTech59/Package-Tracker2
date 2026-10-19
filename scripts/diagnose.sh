#!/usr/bin/env bash
# =====================================================================
# Package Tracker - "why is my dashboard still old?" diagnostic
#
# Run this ON THE LXC (the machine where the app is installed):
#
#     sudo bash -c "$(cat diagnose_lxc.sh)"
#
# or just:  sudo bash diagnose_lxc.sh
#
# Paste the whole output back.
# =====================================================================

INSTALL_DIR="${INSTALL_DIR:-/opt/package-tracker}"

hr() { echo; echo "==================== $* ===================="; }

hr "1. What code is DEPLOYED on disk"
echo "--- version in the deployed package ---"
grep -m1 '__version__' "$INSTALL_DIR/app/__init__.py" 2>/dev/null \
  || echo "  cannot read $INSTALL_DIR/app/__init__.py"

echo
echo "--- deployed index.html has the pairing panel? ---"
if grep -q 'btn-pair-auto' "$INSTALL_DIR/app/static/index.html" 2>/dev/null; then
  echo "  YES - pairing panel present on disk"
else
  echo "  NO  - pairing panel MISSING on disk   <-- this is the problem"
fi

echo
echo "--- deployed app.js has the pairing code? ---"
if grep -q 'pairAutomatically' "$INSTALL_DIR/app/static/app.js" 2>/dev/null; then
  echo "  YES - pairing JS present on disk"
else
  echo "  NO  - pairing JS MISSING on disk"
fi

echo
echo "--- asset version referenced by the deployed HTML ---"
grep -oE '(app\.js|styles\.css)\?v=[0-9.]+' "$INSTALL_DIR/app/static/index.html" 2>/dev/null \
  || echo "  no versioned asset reference found"

hr "2. The SOURCE repo you ran --update from"
# install.sh copies from the folder the script lives in, so this matters a lot.
CANDIDATES=()
add_candidate() { [ -d "$1" ] && CANDIDATES+=("$1"); }

# Where this script was invoked from, and where it lives.
add_candidate "$(pwd)"
# shellcheck disable=SC2005
add_candidate "$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
# Common clone locations.
add_candidate /root/package-tracker
add_candidate /root/Package-Tracker
add_candidate /root/Package-Tracker2
add_candidate "$HOME/package-tracker"
add_candidate /opt/package-tracker-src
add_candidate /tmp/package-tracker
add_candidate /srv/package-tracker

# Also search for any directory holding install.sh plus app/, a couple of
# levels deep in the usual roots, in case the clone lives somewhere unusual.
for root in /root /home /opt /srv /mnt /media /tmp; do
  [ -d "$root" ] || continue
  while IFS= read -r hit; do
    [ -n "$hit" ] && add_candidate "$(dirname "$hit")"
  done < <(find "$root" -maxdepth 3 -name install.sh -path '*/app/../*' 2>/dev/null \
           | head -5; find "$root" -maxdepth 3 -type f -name install.sh 2>/dev/null | head -5)
done

FOUND_SRC=0
SEEN=""
for d in "${CANDIDATES[@]+"${CANDIDATES[@]}"}"; do
  # Only report dirs that look like the project.
  [ -f "$d/install.sh" ] || [ -f "$d/app/__init__.py" ] || continue
  # Skip duplicates (the same dir can be found via several guesses).
  case "|$SEEN|" in *"|$d|"*) continue ;; esac
  SEEN="$SEEN|$d"
  FOUND_SRC=1
  echo "--- source dir: $d ---"
  (
    cd "$d" || exit
    echo "  git commit : $(git rev-parse HEAD 2>/dev/null || echo 'NOT a git repo')"
    echo "  git branch : $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    echo "  remote     : $(git remote get-url origin 2>/dev/null || echo 'none')"
    echo "  version    : $(grep -m1 '__version__' app/__init__.py 2>/dev/null || echo '?')"
    if grep -q 'btn-pair-auto' app/static/index.html 2>/dev/null; then
      echo "  pairing UI : PRESENT in source"
    else
      echo "  pairing UI : MISSING in source   <-- update would copy stale code"
    fi
    echo "  git status : $(git status --porcelain 2>/dev/null | wc -l) modified file(s)"
  )
done
[ "$FOUND_SRC" -eq 0 ] && echo "  no project source directory found in the usual places"

hr "3. What the RUNNING service actually serves"
echo "--- service state ---"
systemctl is-active package-tracker 2>/dev/null || echo "  service not active"

PORT=$(grep -E '^PORT=' "$INSTALL_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"' ')
PORT=${PORT:-8080}
echo "  port: $PORT"

echo
echo "--- /api/health ---"
curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/api/health" 2>/dev/null \
  || echo "  no response from /api/health"

echo
echo "--- HTML served right now ---"
if curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/" 2>/dev/null | grep -q 'btn-pair-auto'; then
  echo "  served HTML HAS the pairing panel"
  echo "  -> backend is already updated; if your browser shows the old UI, it is a CACHE problem"
else
  echo "  served HTML LACKS the pairing panel"
  echo "  -> the running process is old code"
fi

echo
echo "--- asset version served ---"
curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/" 2>/dev/null \
  | grep -oE '(app\.js|styles\.css)\?v=[0-9.]+' || echo "  no versioned asset found"

hr "4. Past update backups (what was replaced, and when)"
ls -1dt /var/backups/package-tracker/*/ 2>/dev/null | head -5 || echo "  no backups found"
newest=$(ls -1dt /var/backups/package-tracker/*/ 2>/dev/null | head -1)
if [ -n "$newest" ]; then
  echo
  echo "  newest backup: $newest"
  echo "  version it held: $(grep -m1 '__version__' "${newest}code/app/__init__.py" 2>/dev/null || echo 'no code snapshot')"
  if grep -q 'btn-pair-auto' "${newest}code/app/static/index.html" 2>/dev/null; then
    echo "  pairing UI in backup: PRESENT (so the backup was already new)"
  else
    echo "  pairing UI in backup: MISSING (backup was the old version)"
  fi
fi

hr "5. Recent service log"
journalctl -u package-tracker -n 25 --no-pager 2>/dev/null | tail -25 || echo "  no journal access"

hr "DONE"
echo "Copy everything above and paste it back."
