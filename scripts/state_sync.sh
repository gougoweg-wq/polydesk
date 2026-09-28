#!/bin/sh
# Бумажный журнал polydesk в ветке `state` (без таблицы тиков — она большая и нужна только для графиков).
set -e
REMOTE="${STATE_REMOTE:-origin}"
mkdir -p data
case "$1" in
  pull)
    if git fetch -q "$REMOTE" state 2>/dev/null; then
      git show "$REMOTE/state:polydesk.db.gz" | gunzip -c > data/polydesk.db
      echo "состояние получено: $(du -h data/polydesk.db | cut -f1)"
    else
      echo "ветки state ещё нет — чистый бумажный счёт"
    fi ;;
  push)
    [ -f data/polydesk.db ] || exit 0
    tmp=$(mktemp -d)
    python3 - data/polydesk.db "$tmp/polydesk.db" << 'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); src.close()
dst.execute("DELETE FROM ticks WHERE ts < strftime('%s','now') - 7200"); dst.commit(); dst.execute("VACUUM"); dst.close()
PY
    gzip -9 -f "$tmp/polydesk.db"
    blob=$(git hash-object -w "$tmp/polydesk.db.gz")
    tree=$(printf '100644 blob %s\tpolydesk.db.gz\n' "$blob" | git mktree)
    commit=$(GIT_AUTHOR_NAME=polydesk-bot GIT_AUTHOR_EMAIL=bot@polydesk GIT_COMMITTER_NAME=polydesk-bot \
             GIT_COMMITTER_EMAIL=bot@polydesk git commit-tree "$tree" -m "state $(date -u +%Y-%m-%dT%H:%M:%SZ)")
    git push -q --force "$REMOTE" "$commit:refs/heads/state"
    rm -rf "$tmp"
    echo "состояние отправлено: $(date -u +%H:%M) UTC" ;;
  *) echo "usage: state_sync.sh pull|push"; exit 1 ;;
esac
