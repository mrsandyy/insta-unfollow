FROM python:3.12-slim

LABEL maintainer="insta-unfollow"
LABEL description="Instagram cleanup daemon — unfollows non-followers and withdraws stale requests"

# Prevent Python from writing .pyc files and enable unbuffered stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY insta_unfollow.py .

# Create directories for persistent data
RUN mkdir -p /app/data /app/state

# Default environment — override via docker-compose or docker run -e
ENV DRY_RUN=true \
    INSTAGRAM_USERNAME="" \
    INSTAGRAM_PASSWORD="" \
    CYCLE_INTERVAL_HOURS=48 \
    MAX_UNFOLLOWS_PER_CYCLE=30 \
    MAX_WITHDRAWALS_PER_CYCLE=15 \
    DELAY_MIN=30 \
    DELAY_MAX=60 \
    LONG_BREAK_MIN=300 \
    LONG_BREAK_MAX=600 \
    ACTIONS_BEFORE_BREAK=5 \
    RATE_LIMIT_SLEEP=900 \
    PENDING_REQUEST_STALE_DAYS=3 \
    NON_FOLLOWER_GRACE_DAYS=2 \
    SESSION_FILE=/app/state/session.json \
    PROGRESS_FILE=/app/state/progress.json \
    LOG_FILE=/app/state/actions.log \
    DATA_DIR=/app/data \
    RUN_ONCE=false

# Health indicator — container stays healthy as long as the process is alive
HEALTHCHECK --interval=5m --timeout=10s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

STOPSIGNAL SIGTERM

ENTRYPOINT ["python", "insta_unfollow.py"]
