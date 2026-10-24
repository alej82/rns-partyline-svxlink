#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SVX_ETC="${SVX_ETC:-/usr/local/etc/svxlink}"
SVX_LIB="${SVX_LIB:-/usr/local/lib/svxlink}"
SVX_CONF="${SVX_ETC}/svxlink.conf"
MODULE_CONF="${SVX_ETC}/svxlink.d/ModuleRnsPartyline.conf"
EVENTS_DIR="${SVX_EVENTS_DIR:-/usr/local/share/svxlink/events.d}"
EVENT_TCL="${EVENTS_DIR}/RnsPartyline.tcl"
UNIT=/etc/systemd/system/rns-partyline-sidecar.service

if [ "$(id -u)" -ne 0 ]; then
  echo "error: ejecuta este script con sudo: sudo $0" >&2
  exit 1
fi

if [ ! -f "$ROOT/build/ModuleRnsPartyline.so" ]; then
  echo "error: falta $ROOT/build/ModuleRnsPartyline.so; ejecuta scripts/build.sh" >&2
  exit 1
fi
if [ ! -f "$ROOT/config/ModuleRnsPartyline.conf" ]; then
  echo "error: falta $ROOT/config/ModuleRnsPartyline.conf" >&2
  echo "       copia config/ModuleRnsPartyline.conf.example y pon tu SERVER_HASH" >&2
  exit 1
fi
if [ ! -f "$SVX_CONF" ]; then
  echo "error: no encuentro $SVX_CONF" >&2
  exit 1
fi

stamp="$(date +%Y%m%d-%H%M%S)"
backup="${SVX_CONF}.bak.${stamp}"
cp -a "$SVX_CONF" "$backup"
echo "backup: $backup"

install -m 0755 "$ROOT/build/ModuleRnsPartyline.so" "$SVX_LIB/ModuleRnsPartyline.so"
install -d -m 0755 "$SVX_ETC/svxlink.d"
install -d -m 0755 "$EVENTS_DIR"
if [ -f "$EVENT_TCL" ]; then
  cp -a "$EVENT_TCL" "${EVENT_TCL}.bak.${stamp}"
fi
install -m 0644 "$ROOT/test/events.d/RnsPartyline.tcl" "$EVENT_TCL"
if [ -f "$MODULE_CONF" ]; then
  cp -a "$MODULE_CONF" "${MODULE_CONF}.bak.${stamp}"
fi
install -m 0644 "$ROOT/config/ModuleRnsPartyline.conf" "$MODULE_CONF"
sed -i 's/^AUTO_ACTIVATE=.*/AUTO_ACTIVATE=0/' "$MODULE_CONF"
sed -i 's/^TEXT_ONLY=.*/TEXT_ONLY=0/' "$MODULE_CONF"

/usr/bin/python3 - "$SVX_CONF" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text().splitlines()
section = None
changed = False
for i, line in enumerate(lines):
    stripped = line.strip()
    if stripped.startswith('[') and stripped.endswith(']'):
        section = stripped[1:-1]
        continue
    if section == 'SimplexLogic' and stripped.startswith('MODULES='):
        modules = [m.strip() for m in stripped.split('=', 1)[1].split(',') if m.strip()]
        if 'ModuleRnsPartyline' not in modules:
            modules.append('ModuleRnsPartyline')
            lines[i] = 'MODULES=' + ','.join(modules)
            changed = True
        break
else:
    raise SystemExit('error: no se encontró MODULES= en [SimplexLogic]')
if not changed and 'ModuleRnsPartyline' not in '\n'.join(lines):
    raise SystemExit('error: no se pudo añadir ModuleRnsPartyline')
path.write_text('\n'.join(lines) + '\n')
PY

install -m 0644 "$ROOT/deploy/rns-partyline-sidecar.service" "$UNIT"
systemctl daemon-reload
systemctl enable rns-partyline-sidecar.service
systemctl restart rns-partyline-sidecar.service
sleep 1
if ! systemctl is-active --quiet rns-partyline-sidecar.service; then
  echo "error: el servicio del sidecar no arrancó" >&2
  systemctl status rns-partyline-sidecar.service --no-pager >&2 || true
  exit 1
fi

systemctl restart svxlink.service
sleep 2

echo
echo "módulo instalado en $SVX_LIB/ModuleRnsPartyline.so"
echo "configuración instalada en $MODULE_CONF"
echo "Tcl instalado en $EVENT_TCL"
echo "servicio: rns-partyline-sidecar.service"
echo "AUTO_ACTIVATE=0: activa con 3# (o *3#) en el SimplexLogic"
echo
systemctl --no-pager --full status rns-partyline-sidecar.service svxlink.service || true
