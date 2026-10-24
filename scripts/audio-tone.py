#!/usr/bin/python3
"""Inyecta y captura PCM por los dispositivos UDP de SvxLink (M2).

Rx1 escucha en 127.0.0.1:52010 y Tx1 envia a 127.0.0.1:52011. El dispositivo
UDP de SvxLink usa enteros de 16 bits; --channels permite ajustar el numero de
canales intercalados (1 o 2).
"""

from __future__ import annotations

import argparse
import math
import socket
import struct
import threading
import time
from typing import Optional

RATE = 48000


def log(message: str) -> None:
    print(time.strftime("[%H:%M:%S] ") + message, flush=True)


def make_pcm(frequency: float, samples: int, amplitude: float,
             channels: int) -> bytes:
    values = []
    for index in range(samples):
        sample = amplitude * math.sin(2 * math.pi * frequency * index / RATE)
        value = int(max(-1.0, min(1.0, sample)) * 32767)
        values.extend([value] * channels)
    return struct.pack(f"<{len(values)}h", *values)


def send_tone(host: str, port: int, seconds: float, frequency: float,
              amplitude: float, channels: int, chunk_ms: int,
              tail_silence: float = 0.0) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    samples = int(RATE * chunk_ms / 1000)
    chunks = max(1, int(seconds * 1000 / chunk_ms))
    next_at = time.monotonic()
    for _ in range(chunks):
        payload = make_pcm(frequency, samples, amplitude, channels)
        sock.sendto(payload, (host, port))
        next_at += chunk_ms / 1000
        delay = next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
    silence_chunks = max(0, int(tail_silence * 1000 / chunk_ms))
    for _ in range(silence_chunks):
        payload = make_pcm(0.0, samples, 0.0, channels)
        sock.sendto(payload, (host, port))
        next_at += chunk_ms / 1000
        delay = next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
    sock.close()
    log(f"TX {chunks} bloques de {chunk_ms} ms a {host}:{port} "
        f"({channels} canal(es), {frequency:.0f} Hz)"
        + (f" + {tail_silence:.1f}s de silencio" if tail_silence else ""))


def capture(host: str, port: int, seconds: float, save: Optional[str],
            channels: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.settimeout(0.5)
    deadline = time.monotonic() + seconds
    chunks = 0
    samples = []
    while time.monotonic() < deadline:
        try:
            data, _ = sock.recvfrom(8192)
        except socket.timeout:
            continue
        chunks += 1
        if len(data) % 2 == 0:
            samples.extend(struct.unpack(f"<{len(data)//2}h", data))
    sock.close()
    if not samples:
        log(f"RX: 0 bloques en {host}:{port}")
        return
    if channels > 1:
        samples = samples[::channels]
    peak = max(abs(value) for value in samples) / 32768.0
    rms = math.sqrt(sum(value * value for value in samples) / len(samples)) / 32768.0
    log(f"RX {chunks} bloques, {len(samples)} muestras, "
        f"peak={peak:.3f}, rms={rms:.4f}")
    try:
        import numpy as np
        signal = np.asarray(samples, dtype="float32") / 32768.0
        spectrum = np.abs(np.fft.rfft(signal * np.hanning(len(signal))))
        frequencies = np.fft.rfftfreq(len(signal), 1 / RATE)
        if len(spectrum):
            log(f"RX pico dominante: {frequencies[int(np.argmax(spectrum))]:.0f} Hz")
    except Exception:
        pass
    if save:
        with open(save, "wb") as handle:
            handle.write(struct.pack(f"<{len(samples)}h", *samples))
        log(f"guardado {save}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--rx-port", type=int, default=52010)
    parser.add_argument("--tx-port", type=int, default=52011)
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--listen", type=float, default=6.0)
    parser.add_argument("--frequency", type=float, default=1000.0)
    parser.add_argument("--amplitude", type=float, default=0.3)
    parser.add_argument("--channels", type=int, default=1, choices=(1, 2))
    parser.add_argument("--chunk-ms", type=int, default=20)
    parser.add_argument("--tail-silence", type=float, default=0.0,
                        help="silencio tras el tono para cerrar VOX")
    parser.add_argument("--save", default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--send", action="store_true")
    mode.add_argument("--capture", action="store_true")
    mode.add_argument("--both", action="store_true")
    args = parser.parse_args()

    if args.both:
        thread = threading.Thread(
            target=capture,
            args=(args.host, args.tx_port, args.listen, args.save, args.channels),
            daemon=True,
        )
        thread.start()
        time.sleep(0.7)
        send_tone(args.host, args.rx_port, args.seconds, args.frequency,
                  args.amplitude, args.channels, args.chunk_ms,
                  args.tail_silence)
        thread.join()
    elif args.capture:
        capture(args.host, args.tx_port, args.listen, args.save, args.channels)
    else:
        send_tone(args.host, args.rx_port, args.seconds, args.frequency,
                  args.amplitude, args.channels, args.chunk_ms,
                  args.tail_silence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
