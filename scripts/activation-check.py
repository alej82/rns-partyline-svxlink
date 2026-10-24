#!/usr/bin/python3
"""Comprueba el gate de activación del sidecar por el socket de control.

No abre dispositivos de audio: envía hello/activate/deactivate y observa las
instantáneas NDJSON. Pensado para ejecutarse contra un sidecar real en modo
audio (TEXT_ONLY=0).
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from typing import Any, Callable, Optional


class ControlClient:
    def __init__(self, path: str):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.buffer = b""

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, obj: dict) -> None:
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        self.sock.sendall(data)

    def read_event(self, timeout: float = 2.0) -> Optional[dict]:
        deadline = time.monotonic() + timeout
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                line = self.buffer[:newline]
                self.buffer = self.buffer[newline + 1:]
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    return obj
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                return None
            self.buffer += chunk

    def wait_for(self, predicate: Callable[[dict], bool],
                 timeout: float = 20.0) -> Optional[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self.read_event(max(0.1, deadline - time.monotonic()))
            if event is None:
                continue
            if predicate(event):
                return event
        return None

    def latest_snapshot(self, timeout: float = 2.0) -> Optional[dict]:
        snapshot = self.read_event(timeout)
        if snapshot is None or snapshot.get("ev") != "snapshot":
            snapshot = self.wait_for(lambda event: event.get("ev") == "snapshot",
                                     timeout)
        return snapshot


def log(message: str) -> None:
    print(message, flush=True)


def hello_object(active: bool, activation_gate: bool = True) -> dict:
    obj: dict[str, Any] = {
        "cmd": "hello",
        "client": "activation-check",
        "ver": "test",
        "audio_rate": 48000,
        "format": "s16le",
        "channels": 1,
        "text_only": 0,
    }
    if activation_gate:
        obj["active"] = 1 if active else 0
        obj["activation_gate"] = 1
    return obj


def expect_idle(snapshot: dict, label: str) -> bool:
    ok = (
        snapshot.get("state") == "idle"
        and snapshot.get("server") is None
        and snapshot.get("room") is None
        and snapshot.get("profile") is None
        and snapshot.get("codec") is None
        and snapshot.get("frame_ms") is None
        and not snapshot.get("channels")
        and not snapshot.get("preflight")
        and snapshot.get("users") == 0
    )
    if not ok:
        log(f"FALLO {label}: instantánea no idle: {json.dumps(snapshot)}")
    return ok


def not_idle(event: dict) -> bool:
    state = event.get("state")
    return state in ("connecting", "connected", "reconnecting", "error")


def scenario_idle(path: str) -> bool:
    client = ControlClient(path)
    try:
        snapshot = client.latest_snapshot()
        if snapshot is None or snapshot.get("state") != "idle":
            log(f"FALLO idle: estado inicial {snapshot and snapshot.get('state')}")
            return False
        client.send(hello_object(active=False))
        time.sleep(0.5)
        snapshot = client.latest_snapshot()
        if snapshot is None or not expect_idle(snapshot, "tras hello inactivo"):
            return False
        log("OK idle: no conecta sin activación")
        return True
    finally:
        client.close()


def scenario_lifecycle(path: str) -> bool:
    client = ControlClient(path)
    try:
        client.latest_snapshot()
        client.send(hello_object(active=False))
        time.sleep(0.2)
        client.send({"cmd": "activate"})
        event = client.wait_for(not_idle, timeout=30.0)
        if event is None:
            log("FALLO ciclo: la activación no inició la conexión")
            return False
        log(f"OK activación: estado {event.get('state')}")

        client.send({"cmd": "deactivate"})
        event = client.wait_for(
            lambda item: item.get("ev") == "snapshot"
            and item.get("state") == "idle",
            timeout=10.0,
        )
        if event is None or not expect_idle(event, "tras deactivate"):
            return False
        log("OK desactivación: vuelve a idle y limpia el estado")

        client.send({"cmd": "ping"})
        if client.wait_for(lambda item: item.get("ev") == "pong", 2.0) is None:
            log("FALLO ciclo: el sidecar no responde tras desactivar")
            return False
        return True
    finally:
        client.close()


def scenario_reconnect(path: str) -> bool:
    client = ControlClient(path)
    client.latest_snapshot()
    client.send(hello_object(active=True))
    event = client.wait_for(not_idle, timeout=30.0)
    if event is None:
        log("FALLO reconexión: no conectó con hello active=1")
        client.close()
        return False
    client.close()

    replacement = ControlClient(path)
    try:
        replacement.latest_snapshot()
        replacement.send(hello_object(active=True))
        event = replacement.wait_for(not_idle, timeout=10.0)
        if event is None:
            log("FALLO reconexión: no recuperó la conexión tras reconectar")
            return False
        log("OK reconexión: hello active=1 reconstruyó la conexión")
        return True
    finally:
        replacement.close()


def scenario_classic(path: str) -> bool:
    client = ControlClient(path)
    try:
        client.latest_snapshot()
        client.send(hello_object(active=False, activation_gate=False))
        event = client.wait_for(not_idle, timeout=10.0)
        if event is None:
            log("FALLO clásico: un hello sin gate no forzó la conexión")
            return False
        log(f"OK clásico: estado {event.get('state')}")
        return True
    finally:
        client.close()


def scenario_connect_now(path: str) -> bool:
    client = ControlClient(path)
    try:
        snapshot = client.latest_snapshot()
        if snapshot is None or not not_idle(snapshot):
            log("FALLO --connect-now: el sidecar no conectó al arrancar")
            return False
        log(f"OK --connect-now: estado inicial {snapshot.get('state')}")
        return True
    finally:
        client.close()


def scenario_permanent(path: str) -> bool:
    client = ControlClient(path)
    try:
        client.latest_snapshot()
        client.send(hello_object(active=True))
        event = client.wait_for(
            lambda item: item.get("ev") == "snapshot"
            and item.get("state") == "idle"
            and bool(item.get("error")),
            timeout=30.0,
        )
        if event is None or not expect_idle(event, "tras fallo permanente"):
            return False
        client.send({"cmd": "ping"})
        if client.wait_for(lambda item: item.get("ev") == "pong", 2.0) is None:
            log("FALLO permanente: el sidecar no sigue vivo en idle")
            return False
        log(f"OK permanente: idle con error ({event.get('error')}) y proceso vivo")
        return True
    finally:
        client.close()


SCENARIOS: dict[str, Callable[[str], bool]] = {
    "idle": scenario_idle,
    "lifecycle": scenario_lifecycle,
    "reconnect": scenario_reconnect,
    "classic": scenario_classic,
    "connect-now": scenario_connect_now,
    "permanent": scenario_permanent,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-socket", default="/tmp/rns-partyline-svx.sock")
    parser.add_argument(
        "--scenario",
        choices=sorted(SCENARIOS),
        default="lifecycle",
        help="comprobación a ejecutar (por defecto lifecycle)",
    )
    args = parser.parse_args()
    try:
        ok = SCENARIOS[args.scenario](args.control_socket)
    except OSError as exc:
        log(f"ERROR: no se pudo conectar a {args.control_socket}: {exc}")
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
