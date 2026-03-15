# Deployment Guide — PolyKalshi Arb Engine

Two options: **Mac background process** (launchd) or **DigitalOcean cloud** (systemd).

---

## Option 1: Mac Background Process (launchd)

The bot runs silently on login, restarts if it crashes, and writes all output to `logs/scan.log`.

### Setup

```bash
# 1. Create the logs directory (already exists if you've run --once)
mkdir -p /Users/maxbranfield/polymarket-arb-bot/logs

# 2. Install the plist into LaunchAgents
cp /Users/maxbranfield/polymarket-arb-bot/com.polymarket.arb.plist \
   ~/Library/LaunchAgents/com.polymarket.arb.plist

# 3. Load and start it now
launchctl load ~/Library/LaunchAgents/com.polymarket.arb.plist
```

### Commands

```bash
# Start
launchctl start com.polymarket.arb

# Stop
launchctl stop com.polymarket.arb

# Disable autostart on login
launchctl unload ~/Library/LaunchAgents/com.polymarket.arb.plist

# Re-enable autostart
launchctl load ~/Library/LaunchAgents/com.polymarket.arb.plist

# Check if it's running
launchctl list | grep polymarket

# View live logs (Ctrl+C to stop)
tail -f /Users/maxbranfield/polymarket-arb-bot/logs/scan.log

# View last 100 lines
tail -100 /Users/maxbranfield/polymarket-arb-bot/logs/scan.log
```

### After a code change

```bash
launchctl stop com.polymarket.arb
launchctl start com.polymarket.arb
```

---

## Option 2: DigitalOcean Cloud (24/7, ~$4-6/month)

### Droplet specs

- **Image**: Ubuntu 24.04 LTS (x64)
- **Size**: Basic, 1 vCPU, 1 GB RAM — **$6/month** (512MB RAM works too at $4/month)
- **Region**: New York or San Francisco (lowest latency to Polymarket/Kalshi APIs)
- **Authentication**: SSH key (add yours during creation)
- **No backups needed** — the DB is local, all state is reconstructable

### 1. First login

```bash
ssh root@YOUR_DROPLET_IP
```

### 2. System setup

```bash
# Update and install dependencies
apt update && apt upgrade -y
apt install -y python3 python3-pip git

# Install Python packages
pip3 install aiohttp aiosqlite rapidfuzz rich python-dotenv python-dateutil \
             aiohttp-retry cryptography orjson psutil
```

### 3. Upload the bot

From your **local Mac**, run:

```bash
# Copy the project (excluding .env, DB, logs)
rsync -av --exclude='.env' --exclude='database/*.db*' --exclude='logs/' \
  /Users/maxbranfield/polymarket-arb-bot/ root@YOUR_DROPLET_IP:/opt/arb-bot/

# Securely transfer ONLY the .env (never commit this)
scp /Users/maxbranfield/polymarket-arb-bot/.env root@YOUR_DROPLET_IP:/opt/arb-bot/.env
```

### 4. First test run on the server

```bash
ssh root@YOUR_DROPLET_IP
cd /opt/arb-bot
mkdir -p logs
python3 main.py --once
```

### 5. Set up systemd service

```bash
cat > /etc/systemd/system/arb-bot.service << 'EOF'
[Unit]
Description=PolyKalshi Arbitrage Scanner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/arb-bot
ExecStart=/usr/bin/python3 /opt/arb-bot/main.py
Restart=always
RestartSec=60
StandardOutput=append:/opt/arb-bot/logs/scan.log
StandardError=append:/opt/arb-bot/logs/scan.log

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
systemctl daemon-reload
systemctl enable arb-bot
systemctl start arb-bot
```

### 6. Managing the bot remotely

```bash
# Check status
systemctl status arb-bot

# View live logs (from anywhere with SSH)
ssh root@YOUR_DROPLET_IP "tail -f /opt/arb-bot/logs/scan.log"

# Stop
systemctl stop arb-bot

# Restart after a code update
systemctl restart arb-bot

# View last 200 lines
journalctl -u arb-bot -n 200
```

### 7. Deploying code updates

From your **local Mac**:

```bash
rsync -av --exclude='.env' --exclude='database/*.db*' --exclude='logs/' \
  /Users/maxbranfield/polymarket-arb-bot/ root@YOUR_DROPLET_IP:/opt/arb-bot/

ssh root@YOUR_DROPLET_IP "systemctl restart arb-bot"
```

### 8. Check logs from your phone

Install the **SSH Files** or **Blink Shell** app, connect to `root@YOUR_DROPLET_IP`, and run:

```bash
tail -f /opt/arb-bot/logs/scan.log
```

Or use the **DigitalOcean mobile app** → Droplet Console (no SSH client needed).

### Security hardening (recommended)

```bash
# Disable root password login (SSH key only)
sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart sshd

# Basic firewall (SSH only)
ufw allow OpenSSH
ufw enable
```

---

## Log rotation (both Mac and cloud)

Prevent `scan.log` from growing indefinitely:

```bash
# On Mac — add to crontab (crontab -e)
0 0 * * * find /Users/maxbranfield/polymarket-arb-bot/logs -name "*.log" -size +50M -exec truncate -s 10M {} \;

# On DigitalOcean — logrotate config
cat > /etc/logrotate.d/arb-bot << 'EOF'
/opt/arb-bot/logs/scan.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
EOF
```
