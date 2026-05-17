# Docker Deployment Guide

Run Polymarket MCP Server with Docker in one command - no Python installation required!

## Why Use Docker?

- **Zero Dependencies**: No need to install Python, pip, or manage virtual environments
- **Consistent Environment**: Works the same on macOS, Linux, and Windows
- **Isolated**: Doesn't interfere with your system Python or other projects
- **Production-Ready**: Same setup for development and production
- **Easy Updates**: Pull new images without dependency conflicts
- **Resource Control**: Limit CPU and memory usage
- **Quick Start**: Get running in under 2 minutes

## Prerequisites

### Install Docker Desktop

**macOS:**
```bash
brew install --cask docker
# Or download from: https://www.docker.com/products/docker-desktop
```

**Linux:**
```bash
# Ubuntu/Debian
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Or follow: https://docs.docker.com/engine/install/
```

**Windows:**
Download and install from: https://www.docker.com/products/docker-desktop

**Verify installation:**
```bash
docker --version
docker compose version
```

## Quick Start (1 Command!)

### Option 1: Using the Start Script (Recommended)

```bash
chmod +x docker-start.sh
./docker-start.sh
```

The script will:
1. Check Docker installation
2. Create `.env` if needed
3. Validate configuration
4. Build the image
5. Start the server
6. Show logs and status

### Option 2: Manual Start

```bash
# 1. Create .env file
cp .env.example .env
# Edit .env with your credentials

# 2. Start with docker-compose
docker compose up -d

# 3. View logs
docker compose logs -f
```

## Configuration

### Environment Variables

Create a `.env` file in the project root:

```bash
# Required: Polygon wallet credentials
POLYGON_PRIVATE_KEY=0x1234...  # Your wallet private key
POLYGON_ADDRESS=0xABCD...      # Your wallet address

# Optional: CLOB/relayer login credentials (preferred names)
POLYMARKET_RELAYER_KEY=         # Leave empty to auto-generate
POLYMARKET_RELAYER_SECRET=      # Leave empty to auto-generate
POLYMARKET_RELAYER_PASSPHRASE=  # Leave empty to auto-generate

# Optional: legacy aliases for the same triplet
POLYMARKET_API_KEY=            # Leave empty to auto-generate
POLYMARKET_API_SECRET=         # Leave empty to auto-generate
POLYMARKET_PASSPHRASE=         # Leave empty to auto-generate

# Operating mode
DEMO_MODE=false                # Set to true for testing without real funds

# Configuration
LOG_LEVEL=INFO                 # DEBUG, INFO, WARNING, ERROR
POLYMARKET_CHAIN_ID=137        # Polygon mainnet (137) or Amoy testnet (80002)

# Safety limits
MAX_ORDER_SIZE_USD=1000
MAX_TOTAL_EXPOSURE_USD=10000
REQUIRE_CONFIRMATION_ABOVE_USD=100
```

### Getting Your Credentials

**Polygon Wallet:**
1. Use MetaMask, Trust Wallet, or any Polygon-compatible wallet
2. Export your private key (Settings > Security > Export Private Key)
3. Copy your wallet address (0x...)

**Polymarket API Keys (Optional):**
- Leave empty - the server will auto-generate them on first run
- Or create manually at: https://polymarket.com/settings/api

**Important trading flow requirement:**
- Use a MetaMask-linked wallet in Polymarket's **deposit wallet flow**.
- Social-login-only accounts can be blocked from placing orders.
- `POLYMARKET_RELAYER_*` and `POLYMARKET_API_*` are treated as the same CLOB login triplet (relayer names are preferred).

### MetaMask + Funder/Proxy Variables (Detailed)

If trading/portfolio shows `0` while Polymarket UI shows funds, the issue is almost always
**wallet role mapping** (`POLYGON_ADDRESS` signer vs `POLYMARKET_FUNDER` deposit wallet) or
wrong `POLYMARKET_SIGNATURE_TYPE`.

Use this exact checklist:

#### 1) `POLYGON_PRIVATE_KEY` (signer private key)
- Open MetaMask → account menu (top-right) → **Account details** / **Export private key**.
- Export the private key for the account that signs transactions.
- Put it in `.env` (without quotes). Both with/without `0x` are accepted by this project.
- Example:
  ```env
  POLYGON_PRIVATE_KEY=abc123...64hex...
  ```

#### 2) `POLYGON_ADDRESS` (signer EOA address)
- In MetaMask, copy the current account address (starts with `0x`).
- This must match the private key above.
- Example:
  ```env
  POLYGON_ADDRESS=0xYourMetaMaskEOA
  ```

#### 3) `POLYMARKET_FUNDER` (deposit/proxy wallet used in Polymarket UI)
- Open Polymarket and go to your wallet/deposit section (the address where your USDC/positions appear).
- Copy that **deposit/funder** address.
- If UI balance is under a different address than your MetaMask signer, set it here.
- If signer and UI wallet are the same, you may leave empty (it falls back to `POLYGON_ADDRESS`).
- Example:
  ```env
  POLYMARKET_FUNDER=0xYourPolymarketDepositWallet
  ```

#### 4) `POLYMARKET_SIGNATURE_TYPE` (auth mode)
- This server supports:
  - `0` for EOA direct mode (funder must be the signer EOA)
  - `3` for MetaMask + deposit-wallet (`POLY_1271`) setups
- Example:
  ```env
  POLYMARKET_SIGNATURE_TYPE=3
  ```

#### 5) CLOB login credentials (`POLYMARKET_RELAYER_*` preferred; `POLYMARKET_API_*` legacy aliases)
- `POLYMARKET_RELAYER_KEY` (UUID from Polymarket UI) is required.
- If `POLYMARKET_RELAYER_SECRET` and `POLYMARKET_RELAYER_PASSPHRASE` are both set, startup uses **Static Relayer Credentials** (no key derivation).
- If either secret or passphrase is missing/empty, startup uses **Derived Wallet Credentials** and derives secret/passphrase from `POLYGON_PRIVATE_KEY`.
- Container logs show which mode was selected at startup.

#### 6) Enable debug logs to troubleshoot auth mapping
- Set:
  ```env
  LOG_LEVEL=DEBUG
  ```
- Restart container and inspect logs:
  ```bash
  docker compose down && docker compose up -d
  docker compose logs -f polymarket-mcp
  ```
- You should see wallet-auth diagnostics including signer/funder/signature_type.

#### 7) Known-good MetaMask template
```env
DEMO_MODE=false
POLYGON_PRIVATE_KEY=<metamask_private_key>
POLYGON_ADDRESS=<metamask_eoa_address>
POLYMARKET_FUNDER=<polymarket_deposit_wallet_address>
POLYMARKET_SIGNATURE_TYPE=3
POLYMARKET_RELAYER_KEY=
POLYMARKET_RELAYER_SECRET=
POLYMARKET_RELAYER_PASSPHRASE=
POLYMARKET_API_KEY=
POLYMARKET_API_SECRET=
POLYMARKET_PASSPHRASE=
LOG_LEVEL=DEBUG
```

## Auto-Generated API Credentials

If `POLYMARKET_RELAYER_KEY` is set but secret/passphrase are not fully set, the server
**automatically derives secret + passphrase** from your wallet private key at startup.

### When does this happen?

- **First run** – relayer key exists but secret/passphrase are empty.
- **HTTP 401 from Polymarket** – the stored credentials have expired or become invalid and
  the server re-derives them automatically.

### What to do when new credentials appear in the logs

When either scenario occurs you will see a prominent banner in the container logs:

```
docker compose logs -f
```

The banner looks like this:

```
======================================================================
⚠️  NEW POLYMARKET API CREDENTIALS GENERATED  ⚠️
Reason: No API credentials were configured — generated automatically on first run
======================================================================
Copy the values below into your docker-compose.yml (or .env file):

  POLYMARKET_RELAYER_KEY=<full-key-value>
  POLYMARKET_RELAYER_SECRET=<full-secret-value>
  POLYMARKET_RELAYER_PASSPHRASE=<full-passphrase-value>

Then RESTART the container so the new credentials are picked up:
  docker compose down && docker compose up -d
======================================================================
```

> ⚠️ **Important:** Persisting the credentials avoids creating a brand-new key on every
> restart (Polymarket imposes per-wallet key limits).  Copy the three values printed in the
> banner into your docker-compose environment and restart as instructed.

### Step-by-step

1. Start the server for the first time (or after a 401 error):
   ```bash
   docker compose up -d
   ```
2. Open the logs and look for the credential banner:
   ```bash
   docker compose logs -f
   ```
3. Copy the three printed values into your `docker-compose.yml`:
   ```yaml
   environment:
     - POLYMARKET_RELAYER_KEY=<value from log>
     - POLYMARKET_RELAYER_SECRET=<value from log>
     - POLYMARKET_RELAYER_PASSPHRASE=<value from log>
    ```
    Or, if you use a `.env` file:
    ```bash
    POLYMARKET_RELAYER_KEY=<value from log>
    POLYMARKET_RELAYER_SECRET=<value from log>
    POLYMARKET_RELAYER_PASSPHRASE=<value from log>
    ```
4. Restart the container:
   ```bash
   docker compose down && docker compose up -d
   ```



## Usage

### Start Server

```bash
docker compose up -d
```

This runs the server in detached mode (background).

### View Logs

```bash
# Follow logs in real-time
docker compose logs -f

# View last 50 lines
docker compose logs --tail=50

# View logs for specific service
docker compose logs polymarket-mcp
```

### Stop Server

```bash
# Stop gracefully
docker compose down

# Stop and remove volumes (clean slate)
docker compose down -v
```

### Restart Server

```bash
# Restart
docker compose restart

# Rebuild and restart
docker compose up -d --build
```

### Check Status

```bash
# Container status
docker compose ps

# Health check
docker compose exec polymarket-mcp python -c "import sys; sys.exit(0)"

# Resource usage
docker stats polymarket-mcp
```

### Update to Latest Version

```bash
# Pull latest code
git pull

# Rebuild and restart
docker compose up -d --build
```

## Integration with Claude Desktop

### Using Docker Container in MCP

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "polymarket-trading": {
      "command": "docker",
      "args": [
        "compose",
        "-f",
        "/Users/your-username/polymarket-mcp/docker-compose.yml",
        "run",
        "--rm",
        "polymarket-mcp"
      ]
    }
  }
}
```

Or use direct Docker run:

```json
{
  "mcpServers": {
    "polymarket-trading": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "-i",
        "--env-file",
        "/Users/your-username/polymarket-mcp/.env",
        "polymarket-mcp:latest"
      ]
    }
  }
}
```

## Advanced Usage

### Custom Docker Compose

Create `docker-compose.override.yml` for custom settings:

```yaml
version: '3.8'

services:
  polymarket-mcp:
    environment:
      - LOG_LEVEL=DEBUG
    volumes:
      - ./custom-config:/app/config
    deploy:
      resources:
        limits:
          cpus: '2.0'
          memory: 1G
```

### Multi-Architecture Build

Build for multiple platforms (Intel and ARM):

```bash
# Enable buildx
docker buildx create --use

# Build for multiple platforms
docker buildx build --platform linux/amd64,linux/arm64 -t polymarket-mcp:latest .
```

### Production Deployment

For production use, consider:

```yaml
# docker-compose.prod.yml
version: '3.8'

services:
  polymarket-mcp:
    build:
      context: .
      dockerfile: Dockerfile
    restart: always
    environment:
      - LOG_LEVEL=WARNING
    deploy:
      resources:
        limits:
          cpus: '2.0'
          memory: 1G
        reservations:
          cpus: '0.5'
          memory: 512M
    logging:
      driver: "json-file"
      options:
        max-size: "50m"
        max-file: "5"
    healthcheck:
      interval: 15s
      timeout: 5s
      retries: 5
```

Start with: `docker compose -f docker-compose.prod.yml up -d`

## Troubleshooting

### Container Won't Start

```bash
# Check logs for errors
docker compose logs polymarket-mcp

# Verify environment variables
docker compose config

# Test manually
docker compose run --rm polymarket-mcp python -c "from polymarket_mcp.config import load_config; print(load_config())"
```

### Permission Denied

```bash
# Fix script permissions
chmod +x docker-start.sh

# Fix log directory permissions
mkdir -p logs
chmod 777 logs
```

### Out of Memory

```bash
# Increase memory limit in docker-compose.yml
deploy:
  resources:
    limits:
      memory: 1G  # Increase this
```

### Port Conflicts

MCP servers use stdio (not ports), but if you add HTTP endpoints:

```yaml
ports:
  - "8080:8080"  # host:container
```

### Connection Issues

```bash
# Test network connectivity
docker compose exec polymarket-mcp ping -c 3 google.com

# Check DNS
docker compose exec polymarket-mcp nslookup polymarket.com

# Verify credentials
docker compose exec polymarket-mcp env | grep POLYGON
```

### Image Size Too Large

```bash
# Check current size
docker images polymarket-mcp

# Clean up
docker system prune -a

# Rebuild with optimizations
docker compose build --no-cache
```

### Logs Not Appearing

```bash
# Check logging driver
docker compose config | grep logging

# Inspect container
docker inspect polymarket-mcp | grep LogPath

# Direct log access
docker logs polymarket-mcp
```

## Performance Optimization

### Build Cache

```bash
# Use BuildKit for faster builds
DOCKER_BUILDKIT=1 docker compose build

# Multi-stage caching
docker compose build --build-arg BUILDKIT_INLINE_CACHE=1
```

### Resource Limits

Monitor and adjust:

```bash
# Real-time stats
docker stats polymarket-mcp

# Detailed info
docker inspect polymarket-mcp
```

### Volume Performance

For better I/O on macOS:

```yaml
volumes:
  - ./logs:/app/logs:delegated  # Faster writes
```

## Security Best Practices

1. **Never commit `.env`** - Add to `.gitignore`
2. **Use secrets** for production:
   ```bash
   echo "my_secret" | docker secret create polygon_key -
   ```
3. **Run as non-root** (already configured in Dockerfile)
4. **Scan images**:
   ```bash
   docker scan polymarket-mcp:latest
   ```
5. **Update base images**:
   ```bash
   docker pull python:3.12-slim
   docker compose build --no-cache
   ```

## Monitoring

### Health Checks

```bash
# Check health status
docker inspect --format='{{.State.Health.Status}}' polymarket-mcp

# View health check logs
docker inspect --format='{{range .State.Health.Log}}{{.Output}}{{end}}' polymarket-mcp
```

### Metrics

```bash
# Resource usage
docker stats --no-stream polymarket-mcp

# Detailed metrics (JSON)
docker stats --no-stream --format "table {{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}" polymarket-mcp
```

## Cleanup

### Remove Everything

```bash
# Stop and remove containers
docker compose down

# Remove volumes too
docker compose down -v

# Remove images
docker rmi polymarket-mcp:latest

# Clean system
docker system prune -a
```

## Getting Help

- **Docker Issues**: https://docs.docker.com/
- **Polymarket MCP Issues**: https://github.com/your-repo/issues
- **MCP Protocol**: https://modelcontextprotocol.io/

## Next Steps

- Read [README.md](README.md) for API documentation
- See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup
- Check [examples/](examples/) for usage examples
- Deploy to Kubernetes with [k8s/](k8s/) manifests

---

**Ready to trade?** Your Polymarket MCP Server is now running in Docker!
