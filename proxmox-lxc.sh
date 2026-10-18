#!/usr/bin/env bash
# Die Abfragewerte (CTID, NETZ, …) setzt frage() indirekt per printf -v.
# shellcheck disable=SC2153
# Legt auf einem Proxmox-VE-Host einen Debian-LXC an und installiert darin den Shelly-Monitor.
#
# Auf dem Proxmox-Host als root:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/spapossen/shelly-netzanalyse/main/proxmox-lxc.sh)"
#
# Aktualisieren eines bestehenden Containers (Daten und Konfiguration bleiben erhalten):
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/spapossen/shelly-netzanalyse/main/proxmox-lxc.sh)" -- --update 120
#
# Alle Fragen lassen sich über Umgebungsvariablen vorbelegen, z. B. für eine Installation ohne Rückfragen:
#   SHELLY_IP=192.168.1.50 SICHERUNG=35 CTID=120 NETZ=dhcp bash proxmox-lxc.sh
# Variablen: SHELLY_IP SICHERUNG CTID CT_NAME STORAGE TEMPLATE_STORAGE DISK_GB RAM_MB CORES BRIDGE NETZ GATEWAY VLAN
set -euo pipefail

REPO="${REPO:-spapossen/shelly-netzanalyse}"
BRANCH="${BRANCH:-main}"
DEBIAN_VERSION="${DEBIAN_VERSION:-12}"

rot() { printf '\033[31m%s\033[0m\n' "$*" >&2; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }
fehler() { rot "Fehler: $*"; exit 1; }

# frage VARIABLE "Text" "Vorgabe": nutzt eine gesetzte Umgebungsvariable, sonst Eingabe, sonst Vorgabe
frage() {
  local var="$1" text="$2" vorgabe="${3:-}" antwort=""
  if [ -n "${!var:-}" ]; then
    return
  fi
  if [ -r /dev/tty ]; then
    read -r -p "$text${vorgabe:+ [$vorgabe]}: " antwort </dev/tty || true
  fi
  printf -v "$var" '%s' "${antwort:-$vorgabe}"
}

[ "$(id -u)" -eq 0 ] || fehler "Bitte als root auf dem Proxmox-Host ausführen."
if ! command -v pct >/dev/null || ! command -v pveam >/dev/null; then
  fehler "pct/pveam nicht gefunden – ist das ein Proxmox-VE-Host?"
fi

UPDATE_CTID=""
while [ $# -gt 0 ]; do
  case "$1" in
    --update) UPDATE_CTID="${2:-}"; shift 2 ;;
    --) shift ;;
    -h|--help) echo "Aufruf: proxmox-lxc.sh [--update CTID]  (siehe https://github.com/$REPO)"; exit 0 ;;
    *) fehler "Unbekannte Option: $1" ;;
  esac
done

ARBEIT="$(mktemp -d)"
trap 'rm -rf "$ARBEIT"' EXIT

# Programmdateien: aus dem Verzeichnis dieses Skripts, falls es aus einem Klon läuft, sonst von GitHub
paket_bauen() {
  local hier
  hier=""
  if cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null; then
    hier="$(pwd)"
    cd - >/dev/null
  fi
  mkdir -p "$ARBEIT/shelly-monitor"
  if [ -n "$hier" ] && [ -f "$hier/shelly_monitor.py" ] && [ -f "$hier/install.sh" ]; then
    info "Verwende die Dateien aus $hier"
    cp "$hier"/{shelly_monitor.py,install.sh,shelly-monitor.service,config.example.ini} "$ARBEIT/shelly-monitor/"
  else
    info "Lade $REPO ($BRANCH) von GitHub"
    curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" -o "$ARBEIT/quelle.tar.gz" \
      || fehler "Download von GitHub fehlgeschlagen"
    tar xzf "$ARBEIT/quelle.tar.gz" -C "$ARBEIT/shelly-monitor" --strip-components=1
  fi
  tar czf "$ARBEIT/shelly-monitor.tar.gz" -C "$ARBEIT" shelly-monitor
}

# Befehl im Container ausführen. Die Locale des Hosts (z. B. en_US.UTF-8) ist im Container nicht
# erzeugt, daher C.UTF-8 setzen, sonst warnen apt und perl bei jedem Aufruf.
im_ct() {
  local ctid="$1"
  shift
  pct exec "$ctid" -- env LANG=C.UTF-8 LC_ALL=C.UTF-8 LANGUAGE= "$@"
}

dateien_installieren() {
  local ctid="$1"
  shift
  pct push "$ctid" "$ARBEIT/shelly-monitor.tar.gz" /root/shelly-monitor.tar.gz
  im_ct "$ctid" bash -c 'rm -rf /root/shelly-monitor && tar xzf /root/shelly-monitor.tar.gz -C /root && rm /root/shelly-monitor.tar.gz'
  im_ct "$ctid" OHNE_ADRESSE=1 bash /root/shelly-monitor/install.sh "$@"
}

adresse_anzeigen() {
  local ctid="$1" ip port
  ip="$(im_ct "$ctid" hostname -I 2>/dev/null | awk '{print $1}')"
  port="$(im_ct "$ctid" sed -n 's/^port *= *//p' /etc/shelly-monitor/config.ini 2>/dev/null | head -1)"
  echo
  info "Fertig. Container $ctid, Bericht: http://${ip:-<container-ip>}:${port:-8080}/"
  echo "    Einstellungen: pct exec $ctid -- nano /etc/shelly-monitor/config.ini"
  echo "                   danach pct exec $ctid -- systemctl restart shelly-monitor"
  echo "    Log:           pct exec $ctid -- journalctl -u shelly-monitor -f"
}

# ------------------------------------------------------------------ Aktualisieren
if [ -n "$UPDATE_CTID" ]; then
  pct status "$UPDATE_CTID" >/dev/null 2>&1 || fehler "Container $UPDATE_CTID existiert nicht."
  pct status "$UPDATE_CTID" | grep -q running || pct start "$UPDATE_CTID"
  paket_bauen
  dateien_installieren "$UPDATE_CTID"
  adresse_anzeigen "$UPDATE_CTID"
  exit 0
fi

# ------------------------------------------------------------------ Neuinstallation
echo "Shelly-Monitor: neuen LXC auf $(hostname) anlegen"
echo

frage SHELLY_IP "IP-Adresse des Shelly Pro 3EM"
[ -n "$SHELLY_IP" ] || fehler "Ohne Shelly-IP geht es nicht."
frage SICHERUNG "Nennstrom der Hausanschlusssicherung in A" "63"
frage CTID "Container-ID" "$(pvesh get /cluster/nextid)"
pct status "$CTID" >/dev/null 2>&1 && fehler "Container $CTID existiert bereits (zum Aktualisieren: --update $CTID)."
frage CT_NAME "Hostname" "shelly-monitor"
frage STORAGE "Speicher für das Root-Dateisystem" "$(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3=="active" {print $1; exit}')"
frage TEMPLATE_STORAGE "Speicher für Vorlagen" "$(pvesm status -content vztmpl 2>/dev/null | awk 'NR>1 && $3=="active" {print $1; exit}')"
frage DISK_GB "Größe Root-Dateisystem in GB" "16"
frage RAM_MB "Arbeitsspeicher in MB" "512"
frage CORES "CPU-Kerne" "1"
frage BRIDGE "Netzwerkbrücke" "vmbr0"
frage VLAN "VLAN-Tag (leer = keins)" ""
frage NETZ "IP des Containers: dhcp oder z. B. 192.168.1.60/24" "dhcp"
if [ "$NETZ" != "dhcp" ]; then
  frage GATEWAY "Gateway" ""
fi
if [ -z "$STORAGE" ] || [ -z "$TEMPLATE_STORAGE" ]; then
  fehler "Kein passender Speicher gefunden."
fi

info "Suche aktuelle Debian-$DEBIAN_VERSION-Vorlage"
pveam update >/dev/null || rot "pveam update fehlgeschlagen, nutze vorhandene Liste"
TEMPLATE="$(pveam available --section system | awk -v v="debian-$DEBIAN_VERSION-standard_" 'index($2, v) == 1 {print $2}' | sort -V | tail -1)"
[ -n "$TEMPLATE" ] || fehler "Keine Debian-$DEBIAN_VERSION-Vorlage gefunden."
if ! pveam list "$TEMPLATE_STORAGE" | grep -q "$TEMPLATE"; then
  info "Lade Vorlage $TEMPLATE"
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

NET0="name=eth0,bridge=$BRIDGE,ip=$NETZ"
[ -n "${GATEWAY:-}" ] && NET0="$NET0,gw=$GATEWAY"
[ -n "$VLAN" ] && NET0="$NET0,tag=$VLAN"

paket_bauen

info "Lege Container $CTID ($CT_NAME) an"
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
  --hostname "$CT_NAME" --cores "$CORES" --memory "$RAM_MB" --swap 256 \
  --rootfs "$STORAGE:$DISK_GB" --net0 "$NET0" \
  --unprivileged 1 --features nesting=1 --onboot 1 --timezone host \
  --description "Shelly-Monitor für Shelly Pro 3EM ($SHELLY_IP) – https://github.com/$REPO"
pct start "$CTID"

info "Warte auf Netzwerk im Container"
for _ in $(seq 1 60); do
  if im_ct "$CTID" getent hosts deb.debian.org >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
if ! im_ct "$CTID" getent hosts deb.debian.org >/dev/null 2>&1; then
  fehler "Container $CTID hat kein Netzwerk/DNS."
fi
# Dauerhafte Locale im Container, damit auch spätere Anmeldungen ohne Warnungen laufen
im_ct "$CTID" bash -c 'echo "LANG=C.UTF-8" > /etc/default/locale'

info "Aktualisiere Container und installiere den Dienst"
im_ct "$CTID" bash -c 'export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get -y -qq dist-upgrade >/dev/null'
dateien_installieren "$CTID" "$SHELLY_IP" "$SICHERUNG"
adresse_anzeigen "$CTID"
