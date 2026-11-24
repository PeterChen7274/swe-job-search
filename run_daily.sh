
#!/bin/bash

PROJECT_DIR="$HOME/swe-job-search"
STATE_DIR="$PROJECT_DIR/data"
LAST_RUN_FILE="$STATE_DIR/last_run_date"
LOCK_DIR="$STATE_DIR/job_search.lock"

mkdir -p "$STATE_DIR"

TODAY=$(/bin/date +%Y-%m-%d)

# Prevent simultaneous executions.
if ! /bin/mkdir "$LOCK_DIR" 2>/dev/null; then
    exit 0
fi

cleanup() {
    /bin/rmdir "$LOCK_DIR" 2>/dev/null
}
trap cleanup EXIT

# Check again after acquiring the lock.
if [ -f "$LAST_RUN_FILE" ] && \
   [ "$(/bin/cat "$LAST_RUN_FILE")" = "$TODAY" ]; then
    exit 0
fi

cd "$PROJECT_DIR" || exit 1

# Find Docker (Apple Silicon or Intel Homebrew installation).
if [ -x /opt/homebrew/bin/docker ]; then
    DOCKER=/opt/homebrew/bin/docker
elif [ -x /usr/local/bin/docker ]; then
    DOCKER=/usr/local/bin/docker
else
    echo "Docker not found"
    exit 1
fi

if "$DOCKER" run --rm \
    --env-file "$PROJECT_DIR/.env" \
    -v "$HOME/Downloads:/output" \
    -v "$STATE_DIR:/data" \
    simplify-job-search; then

    echo "$TODAY" > "$LAST_RUN_FILE"
fi

