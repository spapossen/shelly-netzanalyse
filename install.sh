#!/usr/bin/env bash
# Installiert oder aktualisiert den Shelly-Monitor in einem Debian/Ubuntu-LXC.
# Aufruf im Container als root:  bash install.sh <IP-des-Shelly> [Sicherung in A]
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Bitte als root ausführen"; exit 1; }
HIER="$(cd "$(dirname "$0")" && pwd)"
HOST="${1:-}"
SICHERUNG="${2:-63}"
KONFIG=/etc/shelly-monitor/config.ini

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-requests tzdata sqlite3 >/dev/null

id shelly >/dev/null 2>&1 || useradd --system --home /var/lib/shelly-monitor --shell /usr/sbin/nologin shelly
install -d -o shelly -g shelly -m 750 /var/lib/shelly-monitor
install -d -m 755 /opt/shelly-monitor /etc/shelly-monitor
install -m 755 "$HIER/shelly_monitor.py" /opt/shelly-monitor/shelly_monitor.py

if [ ! -f "$KONFIG" ]; then
  [ -n "$HOST" ] || { echo "Erstinstallation: bash install.sh <IP-des-Shelly> [Sicherung in A]"; exit 1; }
  sed -e "s/^host = .*/host = $HOST/" -e "s/^sicherung = .*/sicherung = $SICHERUNG/" \
    "$HIER/config.example.ini" > "$KONFIG"
  chown root:shelly "$KONFIG"
  chmod 640 "$KONFIG"
  echo "Konfiguration angelegt: $KONFIG"
else
  echo "Konfiguration bleibt erhalten: $KONFIG"
fi

ZIEL="$(sed -n 's/^host *= *//p' "$KONFIG" | head -1)"
if python3 -c "import requests, sys; requests.get(f'http://{sys.argv[1]}/rpc/Shelly.GetDeviceInfo', timeout=5)" "$ZIEL" 2>/dev/null; then
  echo "Shelly unter $ZIEL erreichbar"
else
  echo "WARNUNG: Shelly unter $ZIEL nicht erreichbar – IP und Netzwerk prüfen (der Dienst versucht es weiter)"
fi

install -m 644 "$HIER/shelly-monitor.service" /etc/systemd/system/shelly-monitor.service
systemctl daemon-reload
systemctl enable shelly-monitor >/dev/null
systemctl restart shelly-monitor
sleep 2
systemctl --no-pager --lines=5 status shelly-monitor || true

PORT="$(sed -n 's/^port *= *//p' "$KONFIG" | head -1)"
echo
echo "Bericht: http://$(hostname -I | awk '{print $1}'):${PORT:-8080}/"
