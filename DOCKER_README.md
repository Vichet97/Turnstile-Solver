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
docker run -d -p 5000:5000 --name turnstile-solver turnstile-solver
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
- **Browser Support**: Chromium only (camoufox excluded due to large download size)

## Browser Support

**Note**: The Docker version uses only Chromium browser due to camoufox's large binary download (700MB+) causing build timeouts. For full browser support including camoufox, use the native Python installation method described in the main README.md.

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