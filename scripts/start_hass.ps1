# Arranca Home Assistant Core en WSL (sin Docker) y lo deja en 127.0.0.1:8123
$ErrorActionPreference = "Stop"
$config = "/mnt/c/Users/gacz/Documents/jarvis/aether/data/homeassistant"
wsl -d Ubuntu -- bash -lc "export HOME=/root; mkdir -p $config; exec /root/hass-venv/bin/hass -c $config"
