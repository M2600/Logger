# Configuration Guide

This guide explains how to configure the Core-Stream Logger for both local and remote deployment.

## Quick Start

### Local Deployment (No Authentication)

```bash
# Start daemon (no auth needed)
python daemon.py

# Send events (no config needed)
python log.py "My thought here"
```

### Remote Deployment (With Authentication)

```bash
# Start daemon with API key
python daemon.py --api-key "your-secret-key"

# Send events with authentication
python log.py --api-key "your-secret-key" "My thought here"
```

---

## Configuration Methods

### 1. Command-Line Arguments (Highest Priority)

#### Daemon
```bash
python daemon.py --api-key "my-secret-key"
```

#### Client
```bash
python log.py --api-key "my-secret-key" "event body"
python log.py status --api-key "my-secret-key"
python log.py report --api-key "my-secret-key"
python log.py backfill --api-key "my-secret-key"
```

### 2. Configuration Files

#### Daemon Config: `~/.logger/daemon.json`

Create the directory and config file:

```bash
mkdir -p ~/.logger
cat > ~/.logger/daemon.json << 'EOF'
{
  "api_key": "your-secret-key-here",
  "port": 8765,
  "ai_enabled": true
}
EOF
```

Then start daemon with:

```bash
python daemon.py --config-file ~/.logger/daemon.json
```

#### Client Config: `~/.logger/config.json`

```bash
mkdir -p ~/.logger
cat > ~/.logger/config.json << 'EOF'
{
  "api_key": "your-secret-key-here",
  "daemon_url": "http://remote-server:8765"
}
EOF
```

Then use client with:

```bash
python log.py --config-file ~/.logger/config.json "My thought"
```

### 3. Environment Variables (Lowest Priority)

Set the API key once:

```bash
export LOGGER_API_KEY="your-secret-key-here"
```

Then use client normally (no flag needed):

```bash
python log.py "My thought"
python log.py status
python log.py report --period today --format md
```

---

## Priority Order

For API key resolution, the client checks in this order:

1. **CLI Argument** (`--api-key "key"`)
   - Highest priority
   - Useful for one-off commands

2. **Config File** (`--config-file path/to/config.json`)
   - Middle priority
   - JSON file with `api_key` field

3. **Environment Variable** (`LOGGER_API_KEY`)
   - Lowest priority
   - Set once, used for all invocations

**Example:**
```bash
# This would use "cli-key" (highest priority)
export LOGGER_API_KEY="env-key"
python log.py --api-key "cli-key" "message"

# This would use "env-key" (no CLI arg)
python log.py "message"
```

---

## Example Scenarios

### Scenario 1: Personal Computer (Local, No Auth)

No configuration needed!

```bash
# Start daemon
python daemon.py

# Use client
python log.py "Quick thought"
python log.py "Another thought"
```

### Scenario 2: Work Environment (Remote Server, With Auth)

**Server Setup:**
```bash
# Create config
mkdir -p ~/.logger
echo '{"api_key":"team-secret-2026"}' > ~/.logger/daemon.json

# Start daemon
python daemon.py --config-file ~/.logger/daemon.json
```

**Client Setup (on different machine):**
```bash
# Create config
mkdir -p ~/.logger
cat > ~/.logger/config.json << 'EOF'
{
  "api_key": "team-secret-2026",
  "daemon_url": "http://work-server.com:8765"
}
EOF

# Use client
export LOGGER_API_KEY="team-secret-2026"
python log.py "Working on feature X"
python log.py report --period week --format md
```

### Scenario 3: Development (Multiple Keys)

```bash
# Development daemon (weak key)
python daemon.py --api-key "dev-key-123"

# Production daemon (strong key)
ssh prod-server
python daemon.py --api-key "prod-key-super-secret"

# Local client uses environment for quick testing
export LOGGER_API_KEY="dev-key-123"
python log.py "Testing locally"

# Production logging with explicit key
python log.py --api-key "prod-key-super-secret" "Production issue"
```

---

## Config File Format

### Daemon Config (`daemon.json`)

```json
{
  "api_key": "your-secret-key-here",
  "port": 8765,
  "ai_enabled": true
}
```

**Fields:**
- `api_key` (string): Bearer token for authentication
- `port` (number, optional): Port to bind daemon
- `ai_enabled` (boolean, optional): Enable/disable AI worker

### Client Config (`config.json`)

```json
{
  "api_key": "your-secret-key-here",
  "daemon_url": "http://remote-server:8765"
}
```

**Fields:**
- `api_key` (string): Bearer token for authentication
- `daemon_url` (string, optional): Daemon server address

---

## Security Best Practices

1. **Never commit secrets to git**
   ```bash
   # Add to .gitignore
   echo "~/.logger/" >> .gitignore
   ```

2. **Use strong keys**
   ```bash
   # Generate a random key
   python3 -c "import uuid; print(uuid.uuid4().hex)"
   ```

3. **Rotate keys periodically**
   - Update daemon with new `--api-key`
   - Update all clients with new key

4. **Use HTTPS in production**
   - Protect keys in transit over network
   - Consider reverse proxy (nginx) with TLS

5. **Environment-specific keys**
   - Different key for development, staging, production
   - Easy to track which environment each log came from

---

## Troubleshooting

### Error: "Missing or invalid Authorization header"

**Cause:** Client trying to connect to authenticated daemon without providing key.

**Solution:**
```bash
# Add API key flag
python log.py --api-key "your-key" "message"

# OR set environment variable
export LOGGER_API_KEY="your-key"
python log.py "message"
```

### Error: "Invalid API key"

**Cause:** API key doesn't match daemon's key.

**Solution:**
```bash
# Verify daemon key
python daemon.py --api-key "correct-key"

# Verify client key matches
python log.py --api-key "correct-key" "test"
```

### Daemon not responding

**Cause:** Daemon might be listening on different address/port.

**Solution:**
```bash
# Check daemon logs
tail -f /tmp/daemon.log

# Verify daemon is running
ps aux | grep daemon.py

# Check connection
curl http://localhost:8765/health
```

---

## Examples Included in Repository

- `daemon.config.example.json` - Daemon configuration template
- `client.config.example.json` - Client configuration template

Copy these files to `~/.logger/` and edit as needed:

```bash
cp daemon.config.example.json ~/.logger/daemon.json
cp client.config.example.json ~/.logger/config.json
# Edit the files with your secret keys
```
