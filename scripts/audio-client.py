#!/usr/bin/python3
"""Cliente sintético para el socket de audio del sidecar (M2).

Conecta al socket de control, envía PCM s16le mono 48 kHz por el socket de
audio, conmuta PTT y opcionalmente captura el audio remoto. No usa SvxLink ni
PortAudio.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import struct
import sys
import threading
import time
from typing import Optional

AUDIO_TYPE_PCM = 0x01
AUDIO_HEADER = struct.Struct("<BI")
SAMPLE_RATE = 48000


def log(message: str) -> None:
    print(time.strftime("[%H:%M:%S] ") + message, flush=True)


class Client:
    def __init__(self, control: str, audio: str):
        self.control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.control.connect(control)
        self.audio = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.audio.connect(audio)
        self.control_lock = threading.Lock()
        self.audio_lock = threading.Lock()
        self.stop = threading.Event()
        self.rx_pcm = bytearray()
        self.rx_frames = 0
        self.tx_frames = 0
        self.reader: Optional[threading.Thread] = None

    def send_control(self, obj: dict) -> None:
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        with self.control_lock:
            self.control.sendall(data)

    def hello(self) -> None:
        self.send_control({
            "cmd": "hello", "client": "audio-client", "ver": "m2",
            "audio_rate": SAMPLE_RATE, "format": "s16le", "channels": 1,
        })
        self.control.settimeout(2.0)
        buffer = b""
        try:
            while b"\n" not in buffer:
                chunk = self.control.recv(4096)
                if not chunk:
                    break
                buffer += chunk
        except socket.timeout:
            pass
        for line in buffer.split(b"\n"):
            if line.strip():
                log("control: " + line.decode("utf-8", "replace"))

    def ptt(self, on: bool) -> None:
        self.send_control({"cmd": "ptt", "on": bool(on)})
        log(f"PTT {'on' if on else 'off'}")

    def start_reader(self) -> None:
        self.audio.settimeout(0.2)
        self.reader = threading.Thread(target=self._read_audio, daemon=True)
        self.reader.start()

    def _read_audio(self) -> None:
        pending = bytearray()
        while not self.stop.is_set():
            try:
                data = self.audio.recv(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            pending.extend(data)
            while len(pending) >= AUDIO_HEADER.size:
                frame_type, length = AUDIO_HEADER.unpack_from(pending, 0)
                if length > 65536:
                    log(f"frame inválido: {length}")
                    self.stop.set()
                    return
                if len(pending) < AUDIO_HEADER.size + length:
                    break
                payload = bytes(pending[AUDIO_HEADER.size:AUDIO_HEADER.size + length])
                del pending[:AUDIO_HEADER.size + length]
                if frame_type == AUDIO_TYPE_PCM and length % 2 == 0:
                    with self.audio_lock:
                        self.rx_pcm.extend(payload)
                        self.rx_frames += 1

    def send_tone(self, frequency: float, seconds: float,
                  amplitude: float = 0.3, chunk_ms: int = 20) -> None:
        samples_per_chunk = int(SAMPLE_RATE * chunk_ms / 1000)
        phase = 0.0
        next_at = time.monotonic()
        chunks = max(1, int(seconds * 1000 / chunk_ms))
        for _ in range(chunks):
            values = []
            for index in range(samples_per_chunk):
                sample = amplitude * math.sin(
                    phase + 2 * math.pi * frequency * index / SAMPLE_RATE
                )
                values.append(int(max(-1.0, min(1.0, sample)) * 32767))
            phase = (
                phase
                + 2 * math.pi * frequency * samples_per_chunk / SAMPLE_RATE
            ) % (2 * math.pi)
            payload = struct.pack(f"<{len(values)}h", *values)
            frame = AUDIO_HEADER.pack(AUDIO_TYPE_PCM, len(payload)) + payload
            self.audio.sendall(frame)
            self.tx_frames += 1
            next_at += chunk_ms / 1000
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    def wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.stop.is_set():
            time.sleep(0.05)

    def stats(self) -> None:
        with self.audio_lock:
            pcm = bytes(self.rx_pcm)
            frames = self.rx_frames
        if pcm:
            samples = struct.unpack(f"<{len(pcm)//2}h", pcm)
            if samples:
                peak = max(abs(sample) for sample in samples) / 32768.0
                rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768.0
                log(f"RX: {frames} frames, {len(samples)} muestras, "
                    f"peak={peak:.3f}, rms={rms:.4f}")
                return
        log(f"RX: {frames} frames, sin muestras")

    def save(self, path: str) -> None:
        with self.audio_lock:
            pcm = bytes(self.rx_pcm)
        with open(path, "wb") as handle:
            handle.write(pcm)
        log(f"guardado {len(pcm)} B en {path}")

    def close(self) -> None:
        self.stop.set()
        if self.reader is not None:
            self.reader.join(timeout=1.0)
        for sock in (self.audio, self.control):
            try:
                sock.close()
            except OSError:
                pass


def parse_args() -> argparse.Namespace:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-socket", default="/tmp/rns-partyline-svx.sock")
    parser.add_argument("--audio-socket", default="/tmp/rns-partyline-svx.sock.audio")
    parser.add_argument("--tone", type=float, default=None,
                        help="frecuencia del tono a enviar (Hz)")
    parser.add_argument("--seconds", type=float, default=3.0,
                        help="duración del tono (s)")
    parser.add_argument("--amplitude", type=float, default=0.3)
    parser.add_argument("--listen", type=float, default=0.0,
                        help="segundos de escucha después de enviar")
    parser.add_argument("--save", default=None, help="archivo PCM de salida")
    parser.add_argument("--no-ptt", action="store_true",
                        help="no enviar PTT (útil con ptt_mode=vox/open)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        client = Client(args.control_socket, args.audio_socket)
    except OSError as exc:
        log(f"ERROR: no se pudo conectar a los sockets: {exc}")
        return 2
    try:
        client.hello()
        client.start_reader()
        if args.tone is not None:
            if not args.no_ptt:
                client.ptt(True)
                client.wait(0.1)
            client.send_tone(args.tone, args.seconds, args.amplitude)
            if not args.no_ptt:
                client.ptt(False)
        if args.listen > 0:
            client.wait(args.listen)
        client.stats()
        if args.save:
            client.save(args.save)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
