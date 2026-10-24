#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MODE="${1:---text}"
if [ "$MODE" != "--audio" ] && [ "$MODE" != "--text" ]; then
  echo "uso: $0 [--text|--audio]" >&2
  exit 2
fi

SVXLINK="${SVXLINK_BIN:-/usr/local/bin/svxlink}"
PYTHON="${PYTHON_BIN:-/usr/bin/python3}"
BASE_CONFIG="$ROOT/config/ModuleRnsPartyline.conf"
EVENTS_TCL="${SVXLINK_EVENTS_TCL:-/usr/local/share/svxlink/events.tcl}"
EVENTS_DIR="${SVXLINK_EVENTS_DIR:-/usr/local/share/svxlink/events.d}"
SOUNDS_DIR="${SVXLINK_SOUNDS_DIR:-/usr/local/share/svxlink/sounds}"

if [ ! -x "$SVXLINK" ]; then
  echo "error: no encuentro $SVXLINK" >&2
  exit 1
fi

if [ ! -f "$ROOT/build/ModuleRnsPartyline.so" ]; then
  echo "error: falta build/ModuleRnsPartyline.so; ejecuta scripts/build.sh" >&2
  exit 1
fi

if [ "$MODE" = "--audio" ]; then
  TEMPLATE="$ROOT/test/svxlink-test-audio.conf"
  TEST_CFG_DIR="$ROOT/build/test-config-audio"
  TEXT_ONLY=0
  AUTO_ACTIVATE=1
else
  TEMPLATE="$ROOT/test/svxlink-test.conf"
  TEST_CFG_DIR="$ROOT/build/test-config-text"
  TEXT_ONLY=1
  AUTO_ACTIVATE=0
fi

mkdir -p "$ROOT/build" "$TEST_CFG_DIR"
# Copia de prueba: el INI de produccion sigue siendo config/ModuleRnsPartyline.conf.
sed -e "s/^TEXT_ONLY=.*/TEXT_ONLY=$TEXT_ONLY/" \
    -e "s/^AUTO_ACTIVATE=.*/AUTO_ACTIVATE=$AUTO_ACTIVATE/" \
    "$BASE_CONFIG" > "$TEST_CFG_DIR/ModuleRnsPartyline.conf"
if [ -n "${ROOM_OVERRIDE:-}" ]; then
  sed -i "s|^ROOM=.*|ROOM=$ROOM_OVERRIDE|" "$TEST_CFG_DIR/ModuleRnsPartyline.conf"
fi

SOCKET="$(awk -F= '/^SOCKET_PATH=/{print $2; exit}' "$TEST_CFG_DIR/ModuleRnsPartyline.conf")"
AUDIO_SOCKET="$(awk -F= '/^AUDIO_SOCKET_PATH=/{print $2; exit}' "$TEST_CFG_DIR/ModuleRnsPartyline.conf")"

# Arbol Tcl local sin tocar /usr/local: enlaces a los eventos del sistema y
# nuestro RnsPartyline.tcl.
mkdir -p "$ROOT/test/events.d"
if [ ! -e "$ROOT/test/events.tcl" ]; then
  ln -s "$EVENTS_TCL" "$ROOT/test/events.tcl"
fi
if [ ! -e "$ROOT/test/sounds" ]; then
  ln -s "$SOUNDS_DIR" "$ROOT/test/sounds"
fi
for event_file in "$EVENTS_DIR"/*.tcl; do
  base="$(basename "$event_file")"
  if [ ! -e "$ROOT/test/events.d/$base" ]; then
    ln -s "$event_file" "$ROOT/test/events.d/$base"
  fi
done

if [ ! -f "$ROOT/test/events.d/RnsPartyline.tcl" ]; then
  echo "error: falta test/events.d/RnsPartyline.tcl" >&2
  exit 1
fi

sed -e "s|__PROJECT_ROOT__|$ROOT|g" \
    -e "s|__CFG_DIR__|$TEST_CFG_DIR|g" \
    "$TEMPLATE" > "$ROOT/build/svxlink-test.conf"

LOG="$ROOT/build/sidecar-${MODE#--}.log"
: > "$LOG"

"$PYTHON" "$ROOT/sidecar/rns_partyline_sidecar.py" \
  --config "$TEST_CFG_DIR/ModuleRnsPartyline.conf" >"$LOG" 2>&1 &
SIDECAR_PID=$!

cleanup() {
  if kill -0 "$SIDECAR_PID" 2>/dev/null; then
    kill "$SIDECAR_PID" 2>/dev/null || true
    wait "$SIDECAR_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

wait_for_socket() {
  local path="$1"
  local label="$2"
  for _ in $(seq 1 80); do
    if [ -S "$path" ]; then
      return 0
    fi
    if ! kill -0 "$SIDECAR_PID" 2>/dev/null; then
      echo "error: el sidecar termino antes de crear $label ($path)" >&2
      tail -60 "$LOG" >&2 || true
      exit 1
    fi
    sleep 0.25
  done
  echo "error: el sidecar no creo $label ($path) a tiempo" >&2
  tail -60 "$LOG" >&2 || true
  exit 1
}

wait_for_log_text() {
  local pattern="$1"
  local timeout="${2:-10}"
  local attempts=$((timeout * 4))
  for _ in $(seq 1 "$attempts"); do
    if grep -qF -- "$pattern" "$LOG"; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

wait_for_socket "$SOCKET" "el socket de control"
if [ "$MODE" = "--audio" ]; then
  wait_for_socket "$AUDIO_SOCKET" "el socket de audio"
  sleep 0.5
  if grep -qF -- "conectando a " "$LOG"; then
    echo "error: el sidecar conectó antes de la activación (--audio)" >&2
    tail -40 "$LOG" >&2 || true
    exit 1
  fi
  if ! wait_for_log_text "modo audio: en espera de la activación" 5; then
    echo "error: el sidecar no arrancó en espera de activación (--audio)" >&2
    tail -40 "$LOG" >&2 || true
    exit 1
  fi
  echo "check:      audio en idle hasta la activación"
else
  if ! wait_for_log_text "conectando a " 10; then
    echo "error: el sidecar no conectó al arrancar en modo texto (--text)" >&2
    tail -40 "$LOG" >&2 || true
    exit 1
  fi
  echo "check:      texto conecta al arrancar"
fi

echo "modo:        ${MODE#--}"
echo "sidecar log: $LOG"
echo "config:      $TEST_CFG_DIR/ModuleRnsPartyline.conf"
echo "socket:      $SOCKET"
if [ "$MODE" = "--audio" ]; then
  echo "audio:       $AUDIO_SOCKET"
  echo "udp rx:      127.0.0.1:52010"
  echo "udp tx:      127.0.0.1:52011"
  echo "tono:        $PYTHON $ROOT/scripts/audio-tone.py --both"
fi
echo "svxlink:     $SVXLINK --config=$ROOT/build/svxlink-test.conf"
echo

"$SVXLINK" --config="$ROOT/build/svxlink-test.conf"
