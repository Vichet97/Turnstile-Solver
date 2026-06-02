# Docker Setup for Turnstile Solver

This project has been configured to run with Docker and Docker Compose.

## Prerequisites

1. **Docker Desktop** must be installed and running on your system
   - Download from: https://www.docker.com/products/docker-desktop/
   - Make sure Docker Desktop is started before running the commands below

## Quick Start

### Build and Run with Docker Compose

```bash
# Build the Docker image (force rebuild if updating)
docker-compose build --no-cache

# Run the container (detached mode)
docker-compose up -d

# View logs
docker-compose logs -f

# Stop the container
docker-compose down

# Rebuild and restart (if you made changes)
docker-compose down && docker-compose build --no-cache && docker-compose up -d
```

### Alternative: Run with Docker directly

```bash
# Build the image
docker build -t turnstile-solver .

# Run the container
docker run -d --user 0:0 -p 5000:5000 -p 3389:3389 \
  -e RUN_API_SOLVER=true \
  -e ENABLE_RDP_WITH_API=true \
  --name turnstile-solver turnstile-solver
```

## Access the API

Once the container is running, the Turnstile Solver API will be available at:
- **URL**: http://localhost:5000
- **Health Check**: http://localhost:5000/readme (if implemented)

## API Usage

### Solve a Turnstile CAPTCHA
```bash
curl "http://localhost:5000/turnstile?url=https://example.com&sitekey=0x4AAAAAAA"
```

### Get Result
```bash
curl "http://localhost:5000/result?id=YOUR_TASK_ID"
```

## Configuration

The Docker setup includes:
- **Port**: 5000 (exposed to host)
- **Memory Limit**: 4GB
- **Shared Memory**: 2GB (for browser stability)
- **Auto-restart**: Unless stopped manually
- **Default Browser**: Chromium (`SOLVER_BROWSER_TYPE=chromium`)
- **Browser Override**: Set `SOLVER_BROWSER_TYPE` (for example `chromium` or `playwright`)
- **Virtual GUI Geometry**: `1920x1080x24` with `96 DPI` (Xvfb)
- **RDP with API mode**: enabled by default via `ENABLE_RDP_WITH_API=true` and `user: "0:0"` in `docker-compose.yml`

## Browser Support

**Note**: Docker now defaults to Chromium (`SOLVER_BROWSER_TYPE=chromium`). You can still switch to `playwright`, `seleniumbase`, or `camoufox` by overriding `SOLVER_BROWSER_TYPE` in `docker-compose.yml` or container env vars.

## Virtual GUI tuning (Turnstile visibility)

You can tune virtual display and browser viewport via env vars:

- `XVFB_SCREEN_WIDTH` (default `1920`)
- `XVFB_SCREEN_HEIGHT` (default `1080`)
- `XVFB_SCREEN_DEPTH` (default `24`)
- `XVFB_DPI` (default `96`)
- `SOLVER_VIEWPORT_WIDTH` (default follows `XVFB_SCREEN_WIDTH`)
- `SOLVER_VIEWPORT_HEIGHT` (default follows `XVFB_SCREEN_HEIGHT`)
- `SOLVER_DEVICE_SCALE_FACTOR` (default `1.0`)
- `SELENIUMBASE_PREWARM` (default `true`)
- `SELENIUMBASE_PREFETCH_DRIVER` (default `true`)
- `SELENIUMBASE_UC` optional override (`true` / `false`; default `false` for Docker stability)
- `SELENIUMBASE_GUI_CLICK` optional (`false` recommended unless `python3-tk` is installed)
- `SOLVER_DISPLAY_MODE` (`xvfb` default, `rdp` to render headed browser windows in XRDP session, `auto` to prefer XRDP if present)
- `RDP_SESSION_WAIT_SECONDS` (`0` = wait indefinitely for XRDP session when `SOLVER_DISPLAY_MODE=rdp`)
- `XRDP_PASSWORD` (sets root password used for XRDP login; default `root`)

The Docker image now installs `python3-tk` and `python3-dev` to avoid MouseInfo/tkinter `SystemExit` crashes when SeleniumBase GUI-click helpers are enabled.

Recommended baseline for Turnstile visibility:
- `1920x1080`, depth `24`, `96 DPI`, `SOLVER_DEVICE_SCALE_FACTOR=1.0`

To use RDP while API mode is running, keep:
- `RUN_API_SOLVER=true`
- `ENABLE_RDP_WITH_API=true`
- Compose user as root: `user: "0:0"`

## Troubleshooting

1. **Docker Desktop not running**: Make sure Docker Desktop is started
2. **Port already in use**: Change the port mapping in docker-compose.yml
3. **Memory issues**: Adjust memory limits in docker-compose.yml
4. **Browser crashes**: The container includes Xvfb for headless browser operation

## Files Created

- `Dockerfile`: Container configuration
- `docker-compose.yml`: Service orchestration
- `.dockerignore`: Build optimization
- `DOCKER_README.md`: This documentation
