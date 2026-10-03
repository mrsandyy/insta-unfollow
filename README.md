# Instagram Cleanup Daemon 🧹

A Dockerized daemon that automatically cleans up your Instagram following list:

- **Unfollows non-followers** after a configurable grace period (default: 2 days)
- **Withdraws stale follow requests** older than 3 days
- **Never touches** people who follow you (even if you don't follow them back)
- Runs continuously on a configurable cycle (default: every 48 hours)

## How It Works

```
┌─────────────────────────────────────────────────────┐
│  Every 48h cycle:                                   │
│                                                     │
│  1. Fetch followers & following via Instagram API   │
│  2. Detect non-followers (following − followers)    │
│  3. Track first-seen timestamp for new non-followers│
│  4. Unfollow those past the 2-day grace period      │
│  5. Withdraw pending requests older than 3 days     │
│  6. Sleep until next cycle                          │
└─────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Configure credentials

```bash
cp .env.example .env
# Edit .env with your Instagram username and password
```

### 2. (Optional) Mount your data export

For withdrawing pending follow requests, download your Instagram data:

> Instagram → Settings → Your Activity → Download Your Information

Unzip and place the contents into the `./data/` directory so that
`./data/connections/followers_and_following/pending_follow_requests.json` exists.

### 3. Build & Publish Automatically to GitHub Container Registry (ghcr.io) / Docker Hub

A preconfigured GitHub Actions workflow is included at `.github/workflows/docker-publish.yml`.

1. Create a repository on GitHub (e.g. `insta-unfollow`)
2. Link your local repo and push:
   ```bash
   git remote add origin https://github.com/<YOUR_GITHUB_USERNAME>/insta-unfollow.git
   git push -u origin main
   ```
3. GitHub Actions will automatically:
   - Build a multi-architecture Docker image (`linux/amd64`, `linux/arm64`)
   - Publish it to **GitHub Container Registry** at:
     `ghcr.io/<YOUR_GITHUB_USERNAME>/insta-unfollow:latest`
   - *(Optional)* If you add `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` in your GitHub repo **Settings → Secrets and variables → Actions**, it will also automatically push to Docker Hub!

### 4. Run the Container Anywhere

#### Option A: Docker Compose (Recommended)
Edit `DOCKER_IMAGE` or set it in `.env`, then start:
```bash
# Pull and start the daemon in background
docker compose up -d

# View live logs
docker compose logs -f
```

#### Option B: Single `docker run` command
```bash
docker run -d \
  --name insta-cleanup \
  --restart unless-stopped \
  --env-file .env \
  -e DRY_RUN=false \
  -v "$(pwd)/state:/app/state" \
  -v "$(pwd)/data:/app/data" \
  ghcr.io/<YOUR_GITHUB_USERNAME>/insta-unfollow:latest
```

### 5. Monitor logs

```bash
docker logs -f insta-cleanup

# Or inspect persistent log file
cat ./state/actions.log
```

## Configuration

All settings are configurable via environment variables in `docker-compose.yml` or `.env`:

| Variable | Default | Description |
|---|---|---|
| `DRY_RUN` | `true` | Set `false` to perform real actions |
| `CYCLE_INTERVAL_HOURS` | `48` | Hours between each cleanup cycle |
| `RUN_ONCE` | `false` | Run a single cycle and exit |
| `MAX_UNFOLLOWS_PER_CYCLE` | `30` | Max unfollows per cycle (safe for new accounts) |
| `MAX_WITHDRAWALS_PER_CYCLE` | `15` | Max request withdrawals per cycle |
| `DELAY_MIN` / `DELAY_MAX` | `30` / `60` | Random delay range (seconds) between actions |
| `LONG_BREAK_MIN` / `LONG_BREAK_MAX` | `300` / `600` | Long break range (seconds) |
| `ACTIONS_BEFORE_BREAK` | `5` | Take a long break every N actions |
| `RATE_LIMIT_SLEEP` | `900` | Sleep time (seconds) when rate limited |
| `NON_FOLLOWER_GRACE_DAYS` | `2` | Days to wait before unfollowing a non-follower |
| `PENDING_REQUEST_STALE_DAYS` | `3` | Days before a pending request is considered stale |

### Rate Limit Guidelines

| Account Age | Suggested Daily Limit |
|---|---|
| New (< 6 months) | 30–50 unfollows |
| Established (6–12 months) | 100–150 unfollows |
| Mature (1+ years) | 150–200 unfollows |

## Directory Structure

```
insta-unfollow/
├── insta_unfollow.py      # Main daemon script
├── Dockerfile             # Container image
├── docker-compose.yml     # Orchestration config
├── requirements.txt       # Python dependencies
├── .env                   # Your credentials (git-ignored)
├── .env.example           # Template
├── state/                 # Persistent volume (auto-created)
│   ├── session.json       #   Login session
│   ├── progress.json      #   Unfollow/withdraw tracking
│   └── actions.log        #   Activity log
└── data/                  # Mount your IG data export here
    └── connections/
        └── followers_and_following/
            └── pending_follow_requests.json
```

## Stopping

```bash
docker compose down        # Graceful shutdown (finishes current action)
```

The daemon handles `SIGTERM` gracefully — it finishes the current action, saves
session and progress, then exits.
