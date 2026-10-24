#!/usr/bin/python3
"""Puente de control y audio entre SvxLink y Partyline (RNS/LXST).

Conecta, entra en una sala, publica estado por NDJSON y, en modo audio,
intercambia PCM por un segundo socket Unix. No abre PortAudio ni /dev/snd.
"""

from __future__ import annotations

import argparse
import collections
import configparser
import json
import os
import re
import signal
import socket
import socketserver
import stat
import struct
import sys
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Optional

try:
    import numpy as np
    import RNS
    import partyline.client as partyline_client
    from partyline.client import Client, Config
    from partyline.common import (
        describe,
        frame_ms as profile_frame_ms,
        load_identity,
        make_codec,
        parse_hash,
    )
except ImportError as exc:  # pragma: no cover - depende del entorno
    sys.stderr.write(
        "error: no se pudo importar Partyline/RNS/LXST con {}\n"
        "       {}\n"
        "       Usa /usr/bin/python3 y comprueba:\n"
        "       /usr/bin/python3 -m pip list | grep -iE 'partyline|rns|lxst|pycodec2|numpy'\n".format(
            sys.executable, exc
        )
    )
    raise SystemExit(2)

try:
    from LXST.Sinks import Sink as LxstSink
    from LXST.Sources import Source as LxstSource
except ImportError as exc:  # pragma: no cover - depende del entorno
    sys.stderr.write(f"error: no se pudo importar LXST: {exc}\n")
    raise SystemExit(2)


DEFAULT_SOCKET = "/tmp/rns-partyline-svx.sock"
DEFAULT_RECONNECT_MS = 5000
DEFAULT_CONNECT_ON_ACTIVATE = "1"
DEFAULT_SAMPLE_RATE = 48000
DEFAULT_ALLOWED_CODECS = "opus,c2"
DEFAULT_AUDIO_PUSH_MS = 20
MAX_USER_NAME = 48
AUDIO_SAMPLE_RATE = 48000
AUDIO_TYPE_PCM = 0x01
AUDIO_HEADER = struct.Struct("<BI")
MAX_AUDIO_FRAME = 65536
MAX_MIC_SAMPLES = AUDIO_SAMPLE_RATE * 2
MAX_OUT_FRAMES = 64
AUDIO_QUEUE_TARGET = 24


class ConfigError(Exception):
    """Configuración inválida o incompleta."""


def log(message: str) -> None:
    print(time.strftime("[%Y-%m-%d %H:%M:%S] ") + message, flush=True)


def parse_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "si", "sí"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{field} debe ser 1/0, true/false, yes/no (recibido {value!r})")


def codec_family(profile: str) -> str:
    if profile.startswith(("opus-", "music-")):
        return "opus"
    if profile.startswith("c2-"):
        return "c2"
    return "unknown"


@dataclass
class SidecarConfig:
    config_path: str
    server_hash_text: str
    server_hash: bytes
    room_text: str
    room: Any
    user_name: str
    server_password: str
    room_password: str
    socket_path: str
    audio_socket_path: str
    reconnect_delay_ms: int
    connect_on_activate: bool
    sample_rate: int
    audio_push_ms: int
    ptt_mode: str
    half_duplex: bool
    auto_activate: bool
    allowed_codecs: set[str]
    text_only: bool
    rns_config_dir: str
    identity_file: str
    partyline_section: str
    bridge_section: str
    module_section: str


def _get(parser: configparser.ConfigParser, section: str, key: str, default: Optional[str] = None,
         required: bool = False) -> Optional[str]:
    if parser.has_option(section, key):
        value = parser.get(section, key).strip()
        if value:
            return value
    if required:
        raise ConfigError(f"falta {section}/{key} en la configuración")
    return default


def load_config(path: str, args: argparse.Namespace) -> SidecarConfig:
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise ConfigError(f"no existe el archivo de configuración {path}")

    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, encoding="utf-8") as handle:
            parser.read_file(handle)
    except OSError as exc:
        raise ConfigError(f"no se pudo leer {path}: {exc}") from exc

    partyline_section = args.section
    bridge_section = args.bridge_section
    module_section = args.module_section

    if not parser.has_section(partyline_section):
        raise ConfigError(f"falta la sección [{partyline_section}] en {path}")

    server_hash_text = _get(parser, partyline_section, "SERVER_HASH", required=True)
    assert server_hash_text is not None
    if not re.fullmatch(r"[0-9a-fA-F]{32}", server_hash_text):
        raise ConfigError(
            "SERVER_HASH debe ser exactamente 32 caracteres hexadecimales "
            f"(recibido {server_hash_text!r})"
        )
    try:
        server_hash = parse_hash(server_hash_text)
    except ValueError as exc:
        raise ConfigError(f"SERVER_HASH inválido: {exc}") from exc

    room_text = _get(parser, partyline_section, "ROOM", required=True)
    assert room_text is not None
    if room_text.isdigit():
        room: Any = int(room_text)
        if room <= 0:
            raise ConfigError("ROOM debe ser un número de sala positivo")
    else:
        room = room_text

    user_name = _get(parser, partyline_section, "USER_NAME", required=True)
    assert user_name is not None
    if len(user_name) > MAX_USER_NAME:
        log(f"aviso: USER_NAME supera {MAX_USER_NAME} caracteres; se recorta")
        user_name = user_name[:MAX_USER_NAME]

    socket_path = (
        args.socket
        or _get(parser, bridge_section, "SOCKET_PATH")
        or _get(parser, module_section, "SOCKET_PATH")
        or DEFAULT_SOCKET
    )
    assert socket_path is not None
    if not os.path.isabs(socket_path):
        raise ConfigError(f"SOCKET_PATH debe ser una ruta absoluta (recibido {socket_path!r})")

    reconnect_text = _get(parser, bridge_section, "RECONNECT_DELAY_MS",
                          str(DEFAULT_RECONNECT_MS))
    try:
        reconnect_delay_ms = int(reconnect_text or DEFAULT_RECONNECT_MS)
    except ValueError as exc:
        raise ConfigError("RECONNECT_DELAY_MS debe ser un número entero") from exc
    if reconnect_delay_ms < 100:
        raise ConfigError("RECONNECT_DELAY_MS debe ser >= 100")

    connect_on_activate = parse_bool(
        _get(parser, bridge_section, "CONNECT_ON_ACTIVATE", DEFAULT_CONNECT_ON_ACTIVATE),
        "CONNECT_ON_ACTIVATE",
    )

    sample_rate_text = _get(parser, partyline_section, "SAMPLE_RATE", str(DEFAULT_SAMPLE_RATE))
    try:
        sample_rate = int(sample_rate_text or DEFAULT_SAMPLE_RATE)
    except ValueError as exc:
        raise ConfigError("SAMPLE_RATE debe ser un número entero") from exc
    if sample_rate <= 0:
        raise ConfigError("SAMPLE_RATE debe ser positivo")

    allowed_text = _get(parser, partyline_section, "ALLOWED_CODECS", DEFAULT_ALLOWED_CODECS)
    allowed_codecs: set[str] = set()
    for item in (allowed_text or DEFAULT_ALLOWED_CODECS).replace(";", ",").split(","):
        item = item.strip().lower()
        if not item:
            continue
        if item not in ("opus", "c2"):
            raise ConfigError(f"ALLOWED_CODECS solo admite opus y c2 (recibido {item!r})")
        allowed_codecs.add(item)
    if not allowed_codecs:
        allowed_codecs = {"opus", "c2"}

    text_only = parse_bool(_get(parser, partyline_section, "TEXT_ONLY", "1"), "TEXT_ONLY")

    audio_socket_path = (
        args.audio_socket
        or _get(parser, bridge_section, "AUDIO_SOCKET_PATH")
        or f"{socket_path}.audio"
    )
    assert audio_socket_path is not None
    if not os.path.isabs(audio_socket_path):
        raise ConfigError(
            f"AUDIO_SOCKET_PATH debe ser una ruta absoluta (recibido {audio_socket_path!r})"
        )
    if os.path.abspath(audio_socket_path) == os.path.abspath(socket_path):
        raise ConfigError("AUDIO_SOCKET_PATH debe ser distinto de SOCKET_PATH")

    audio_push_text = _get(parser, bridge_section, "AUDIO_PUSH_MS",
                           str(DEFAULT_AUDIO_PUSH_MS))
    try:
        audio_push_ms = int(audio_push_text or DEFAULT_AUDIO_PUSH_MS)
    except ValueError as exc:
        raise ConfigError("AUDIO_PUSH_MS debe ser un número entero") from exc
    if audio_push_ms < 1 or audio_push_ms > 200:
        raise ConfigError("AUDIO_PUSH_MS debe estar entre 1 y 200")

    ptt_mode = (_get(parser, partyline_section, "PTT_MODE", "ptt") or "ptt").strip().lower()
    if ptt_mode not in ("ptt", "vox", "open"):
        raise ConfigError(f"PTT_MODE debe ser ptt, vox u open (recibido {ptt_mode!r})")

    half_duplex = parse_bool(
        _get(parser, partyline_section, "HALF_DUPLEX", "1"), "HALF_DUPLEX"
    )
    auto_activate = parse_bool(
        _get(parser, module_section, "AUTO_ACTIVATE", "1"), "AUTO_ACTIVATE"
    )
    if not text_only and sample_rate != AUDIO_SAMPLE_RATE:
        log(
            f"aviso: SAMPLE_RATE={sample_rate} no está soportado en audio; "
            f"se exige {AUDIO_SAMPLE_RATE}"
        )

    server_password = _get(parser, partyline_section, "SERVER_PASSWORD", "") or ""
    room_password = _get(parser, partyline_section, "ROOM_PASSWORD", "") or ""

    rns_config_dir = os.path.abspath(os.path.expanduser(
        _get(parser, partyline_section, "RNS_CONFIG_DIR", "~/.reticulum")
        or "~/.reticulum"
    ))
    if not os.path.isdir(rns_config_dir):
        raise ConfigError(f"RNS_CONFIG_DIR no existe o no es un directorio: {rns_config_dir}")

    identity_file = os.path.abspath(os.path.expanduser(
        _get(parser, partyline_section, "IDENTITY_FILE",
             "~/.config/rns-partyline-svx/identity")
        or "~/.config/rns-partyline-svx/identity"
    ))

    return SidecarConfig(
        config_path=path,
        server_hash_text=server_hash_text.lower(),
        server_hash=server_hash,
        room_text=room_text,
        room=room,
        user_name=user_name,
        server_password=server_password,
        room_password=room_password,
        socket_path=socket_path,
        audio_socket_path=audio_socket_path,
        reconnect_delay_ms=reconnect_delay_ms,
        connect_on_activate=connect_on_activate,
        sample_rate=sample_rate,
        audio_push_ms=audio_push_ms,
        ptt_mode=ptt_mode,
        half_duplex=half_duplex,
        auto_activate=auto_activate,
        allowed_codecs=allowed_codecs,
        text_only=text_only,
        rns_config_dir=rns_config_dir,
        identity_file=identity_file,
        partyline_section=partyline_section,
        bridge_section=bridge_section,
        module_section=module_section,
    )


def log_summary(cfg: SidecarConfig) -> None:
    log(f"config: {cfg.config_path}")
    log(f"servidor: {cfg.server_hash_text}")
    log(f"sala: {cfg.room_text}")
    log(f"usuario: {cfg.user_name}")
    log(f"password servidor: {'***' if cfg.server_password else '(no configurada)'}")
    log(f"password sala: {'***' if cfg.room_password else '(no configurada)'}")
    log(f"socket control: {cfg.socket_path}")
    log(f"socket audio: {cfg.audio_socket_path if not cfg.text_only else '(solo texto)'}")
    log(f"reconnect: {cfg.reconnect_delay_ms} ms")
    log(f"connect on activate: {'si' if cfg.connect_on_activate else 'no'}")
    log(f"audio push: {cfg.audio_push_ms} ms")
    log(f"ptt mode: {cfg.ptt_mode}")
    log(f"half duplex: {'si' if cfg.half_duplex else 'no'}")
    log(f"auto activate: {'si' if cfg.auto_activate else 'no'}")
    log(f"rns config: {cfg.rns_config_dir}")
    log(f"identidad: {cfg.identity_file}")
    log(f"text only: {'si' if cfg.text_only else 'no'}")
    log(f"sample rate: {cfg.sample_rate}")
    log(f"codecs permitidos: {','.join(sorted(cfg.allowed_codecs))}")

    if cfg.server_password or cfg.room_password:
        try:
            mode = stat.S_IMODE(os.stat(cfg.config_path).st_mode)
        except OSError:
            return
        if mode & 0o044:
            log(
                f"AVISO: {cfg.config_path} contiene contraseñas y es legible por "
                f"otros usuarios (modo {oct(mode)}); usa chmod 600"
            )


def preflight_profile(profile: str, frame_ms_value: int, sample_rate: int) -> dict:
    try:
        codec = make_codec(profile)
        codec.source = SimpleNamespace(samplerate=sample_rate, channels=1, bitdepth=32)
        codec.sink = SimpleNamespace(samplerate=sample_rate, channels=1)
        samples = max(1, int(sample_rate * frame_ms_value / 1000))
        pcm = np.zeros((samples, 1), dtype="float32")
        encoded = codec.encode(pcm)
        decoded = codec.decode(encoded)
        return {
            "ok": True,
            "codec": describe(profile),
            "profile": profile,
            "frame_ms": frame_ms_value,
            "encoded_bytes": len(encoded),
            "decoded_samples": int(np.asarray(decoded).shape[0]),
        }
    except Exception as exc:  # noqa: BLE001 - el objetivo es diagnosticar
        return {
            "ok": False,
            "profile": profile,
            "frame_ms": frame_ms_value,
            "error": f"{type(exc).__name__}: {exc}",
        }


class AudioLink:
    """Canal PCM local entre el módulo SvxLink y el cliente Partyline."""

    def __init__(self, cfg: SidecarConfig, logger: Callable[[str], None]):
        self.cfg = cfg
        self.logger = logger
        self.enabled = not cfg.text_only
        self.audio_ok = True
        self.ptt_mode = cfg.ptt_mode
        self.half_duplex = cfg.half_duplex
        self.sample_rate = AUDIO_SAMPLE_RATE

        self.mic_lock = threading.Lock()
        self.mic_cond = threading.Condition(self.mic_lock)
        self.mic: list[np.ndarray] = []
        self.mic_count = 0
        self.mic_dropped = 0

        self.out_lock = threading.Lock()
        self.out_cond = threading.Condition(self.out_lock)
        self.out: collections.deque[bytes] = collections.deque()
        self.out_dropped = 0

        self.conn_lock = threading.Lock()
        self.connection: Any = None
        self.connected = threading.Event()

        self.ptt_lock = threading.Lock()
        self.ptt_down = False
        self.mute_rx = False

        self.stop = threading.Event()
        self.server: Optional[AudioUnixServer] = None
        self.writer_thread: Optional[threading.Thread] = None
        self.pending = bytearray()
        self.frames_in = 0
        self.frames_out = 0
        self.encode_dropped = 0
        self.last_error: Optional[str] = None

    def start(self) -> None:
        if not self.enabled:
            return
        self.server = AudioUnixServer(self.cfg.audio_socket_path, self, self.logger)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.writer_thread = threading.Thread(target=self._writer_job, daemon=True)
        self.writer_thread.start()
        self.logger(f"audio: escuchando en {self.cfg.audio_socket_path}")

    def attach(self, connection: Any) -> bool:
        with self.conn_lock:
            if self.connection is not None:
                return False
            self.connection = connection
        self.connected.set()
        self.clear_queues()
        self.logger("audio: módulo conectado")
        return True

    def detach(self, connection: Any) -> None:
        with self.conn_lock:
            if self.connection is not connection:
                return
            self.connection = None
        self.connected.clear()
        self.clear_queues()
        self.logger("audio: módulo desconectado")

    def clear_queues(self) -> None:
        self.clear_mic()
        self.clear_out()

    def clear_mic(self) -> None:
        with self.mic_cond:
            self.mic.clear()
            self.mic_count = 0
            self.mic_cond.notify_all()

    def clear_out(self) -> None:
        with self.out_cond:
            self.out.clear()
            self.out_cond.notify_all()

    def out_pending(self) -> int:
        with self.out_lock:
            return len(self.out)

    def notify_mic(self) -> None:
        with self.mic_cond:
            self.mic_cond.notify_all()

    def push_mic(self, payload: bytes) -> None:
        if not payload:
            return
        dump_path = os.environ.get("SVX_AUDIO_MIC_DUMP")
        if dump_path:
            try:
                with open(dump_path, "ab") as dump:
                    dump.write(payload)
            except OSError:
                pass
        samples = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
        samples = samples.reshape(-1, 1)
        with self.mic_cond:
            self.mic.append(samples)
            self.mic_count += len(samples)
            while self.mic_count > MAX_MIC_SAMPLES and self.mic:
                old = self.mic.pop(0)
                self.mic_count -= len(old)
                self.mic_dropped += len(old)
            self.mic_cond.notify_all()

    def pop_mic(self, count: int, timeout: float = 0.2) -> Optional[np.ndarray]:
        deadline = time.monotonic() + timeout
        with self.mic_cond:
            while self.mic_count < count and not self.stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.mic_cond.wait(remaining)
            if self.stop.is_set() or self.mic_count < count:
                return None
            parts: list[np.ndarray] = []
            need = count
            while need > 0:
                chunk = self.mic[0]
                if len(chunk) <= need:
                    parts.append(chunk)
                    self.mic.pop(0)
                    need -= len(chunk)
                else:
                    parts.append(chunk[:need])
                    self.mic[0] = chunk[need:]
                    need = 0
            self.mic_count -= count
            return np.concatenate(parts, axis=0) if len(parts) > 1 else parts[0]

    def send_pcm(self, block: Any) -> None:
        if not self.enabled or not self.audio_ok:
            return
        if not self.connected.is_set():
            return
        with self.ptt_lock:
            if self.mute_rx:
                return
        samples = np.asarray(block, dtype=np.float32)
        if samples.ndim == 2 and samples.shape[1] > 1:
            samples = samples.mean(axis=1)
        elif samples.ndim == 2:
            samples = samples[:, 0]
        if samples.size == 0:
            return
        samples = np.clip(samples, -1.0, 1.0)
        pcm = (samples * 32767.0).astype("<i2").tobytes()
        frame = AUDIO_HEADER.pack(AUDIO_TYPE_PCM, len(pcm)) + pcm
        with self.out_cond:
            if len(self.out) >= MAX_OUT_FRAMES:
                self.out.popleft()
                self.out_dropped += 1
            self.out.append(frame)
            self.out_cond.notify()
        self.frames_out += 1

    def handle_bytes(self, data: bytes) -> bool:
        self.pending.extend(data)
        while len(self.pending) >= AUDIO_HEADER.size:
            frame_type, length = AUDIO_HEADER.unpack_from(self.pending, 0)
            if length > MAX_AUDIO_FRAME:
                self.last_error = f"frame de audio demasiado grande: {length}"
                self.logger(f"ERROR audio: {self.last_error}")
                return False
            if len(self.pending) < AUDIO_HEADER.size + length:
                break
            payload = bytes(self.pending[AUDIO_HEADER.size:AUDIO_HEADER.size + length])
            del self.pending[:AUDIO_HEADER.size + length]
            if frame_type != AUDIO_TYPE_PCM:
                self.logger(f"audio: frame desconocido 0x{frame_type:02x} ({length} B)")
                continue
            if length % 2:
                self.last_error = "frame PCM con longitud impar"
                self.logger(f"ERROR audio: {self.last_error}")
                return False
            self.push_mic(payload)
            self.frames_in += 1
        return True

    def _writer_job(self) -> None:
        while not self.stop.is_set():
            with self.out_cond:
                while not self.out and not self.stop.is_set():
                    self.out_cond.wait(0.1)
                if self.stop.is_set():
                    return
                frame = self.out.popleft()
            with self.conn_lock:
                connection = self.connection
            if connection is None:
                continue
            try:
                connection.sendall(frame)
            except OSError as exc:
                self.last_error = f"envío de audio: {exc}"
                self.detach(connection)

    def set_supported(self, supported: bool, reason: Optional[str] = None) -> None:
        self.audio_ok = bool(supported)
        if not supported:
            self.last_error = reason
            self.clear_queues()

    def set_ptt(self, down: bool) -> None:
        if not self.enabled:
            return
        with self.ptt_lock:
            self.ptt_down = bool(down)
            self.mute_rx = bool(down) and self.half_duplex
        if down:
            self.clear_mic()
            self.clear_out()
        self.logger(
            f"audio: ptt {'on' if down else 'off'}"
            + (" (prioridad local)" if down and self.half_duplex else "")
            + f" [in={self.frames_in} out={self.frames_out} "
              f"mic_drop={self.mic_dropped} out_drop={self.out_dropped} "
              f"enc_drop={self.encode_dropped}]"
        )

    @property
    def encode_enabled(self) -> bool:
        if not self.enabled or not self.audio_ok:
            return False
        with self.ptt_lock:
            if self.ptt_mode == "ptt":
                return self.ptt_down
            return True

    def on_client_disconnect(self) -> None:
        self.set_ptt(False)
        self.clear_queues()

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "ok": self.audio_ok,
            "connected": self.connected.is_set(),
            "ptt_down": self.ptt_down,
            "mute_rx": self.mute_rx,
            "frames_in": self.frames_in,
            "frames_out": self.frames_out,
            "mic_dropped": self.mic_dropped,
            "out_dropped": self.out_dropped,
            "encode_dropped": self.encode_dropped,
            "error": self.last_error,
        }

    def close(self) -> None:
        self.stop.set()
        self.notify_mic()
        self.clear_out()
        if self.server is not None:
            try:
                self.server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.server.server_close()
            except Exception:  # noqa: BLE001
                pass
            try:
                os.unlink(self.cfg.audio_socket_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                self.logger(f"aviso: no se pudo borrar {self.cfg.audio_socket_path}: {exc}")
            self.server = None
        with self.conn_lock:
            connection = self.connection
            self.connection = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        if self.writer_thread is not None:
            self.writer_thread.join(timeout=1.0)
            self.writer_thread = None


class SvxSink(LxstSink):
    """Sumidero de Partyline que entrega PCM al módulo por el socket de audio."""

    def __init__(self, link: AudioLink, preferred_device: Optional[str] = None,
                 low_latency: bool = False):
        self.link = link
        self.preferred_device = preferred_device
        self.low_latency = low_latency
        self.samplerate = AUDIO_SAMPLE_RATE
        self.channels = 1
        self.should_run = False
        self.next_at = 0.0

    def can_receive(self, from_source: Any = None) -> bool:
        return self.link.out_pending() < AUDIO_QUEUE_TARGET

    def handle_frame(self, frame: Any, source: Any = None) -> None:
        samples = np.asarray(frame)
        count = samples.shape[0] if samples.ndim else 0
        now = time.monotonic()
        if self.next_at < now:
            self.next_at = now
        self.next_at += count / AUDIO_SAMPLE_RATE
        delay = self.next_at - now
        if delay > 0:
            time.sleep(min(delay, 0.5))
        self.link.send_pcm(frame)

    def start(self) -> None:
        self.should_run = True
        self.next_at = time.monotonic()

    def stop(self) -> None:
        self.should_run = False
        self.next_at = 0.0


class SvxSource(LxstSource):
    """Fuente de Partyline que lee PCM del módulo y lo codifica por sala."""

    def __init__(self, link: AudioLink, preferred_device: Optional[str] = None,
                 target_frame_ms: int = 80):
        self.link = link
        self.preferred_device = preferred_device
        self.target_frame_ms = max(1, int(target_frame_ms))
        self.samplerate = AUDIO_SAMPLE_RATE
        self.channels = 1
        self.bitdepth = 32
        self.should_run = False
        self.codec: Any = None
        self.sink: Any = None
        self.pipeline: Any = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.should_run:
            return
        self.should_run = True
        self._thread = threading.Thread(target=self._job, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.should_run = False
        self.link.notify_mic()

    def _job(self) -> None:
        frame_samples = max(1, int(self.samplerate * self.target_frame_ms / 1000))
        while self.should_run and not self.link.stop.is_set():
            if not self.link.encode_enabled:
                time.sleep(0.01)
                continue
            frame = self.link.pop_mic(frame_samples, timeout=0.1)
            if frame is None:
                continue
            if not self.should_run or self.codec is None or self.sink is None:
                continue
            try:
                if hasattr(self.sink, "can_receive") and not self.sink.can_receive(from_source=self):
                    self.link.encode_dropped += 1
                    continue
                self.sink.handle_frame(self.codec.encode(frame), self)
            except Exception as exc:  # noqa: BLE001 - no tumbar la fuente
                self.link.encode_dropped += 1
                self.link.logger(f"audio: error codificando: {type(exc).__name__}: {exc}")


def install_audio_hooks(link: AudioLink) -> None:
    """Sustituye las líneas de audio de Partyline por el socket local."""
    if not hasattr(partyline_client, "LineSink") or not hasattr(partyline_client, "LineSource"):
        raise RuntimeError(
            "partyline.client no expone LineSink/LineSource; "
            "revisa la versión de Partyline"
        )
    partyline_client.LineSink = (
        lambda preferred_device=None, autodigest=True, low_latency=False, **kwargs:
        SvxSink(link, preferred_device, low_latency)
    )
    partyline_client.LineSource = (
        lambda preferred_device=None, target_frame_ms=80, **kwargs:
        SvxSource(link, preferred_device, target_frame_ms)
    )


class _AudioHandler(socketserver.StreamRequestHandler):
    connection: Any = None

    def setup(self) -> None:
        super().setup()
        if not self.server.link.attach(self.request):
            self.server.logger("audio: segundo cliente rechazado")
            self.request.close()
            return
        self.connection = self.request
        try:
            self.request.settimeout(1.0)
        except OSError:
            pass

    def handle(self) -> None:
        if self.connection is None:
            return
        while not self.server.link.stop.is_set():
            try:
                data = self.request.recv(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            if not self.server.link.handle_bytes(data):
                break

    def finish(self) -> None:
        if self.connection is not None:
            self.server.link.detach(self.request)
        super().finish()


class AudioUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: str, link: AudioLink, logger: Callable[[str], None]):
        self.socket_path = path
        self.link = link
        self.logger = logger
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.exists(path):
            info = os.stat(path)
            if stat.S_ISSOCK(info.st_mode):
                os.unlink(path)
            else:
                raise SystemExit(f"error: {path} existe y no es un socket")
        super().__init__(path, _AudioHandler)
        os.chmod(path, 0o666)


class Sidecar:
    def __init__(self, cfg: SidecarConfig, identity: Any,
                 server: Optional["UnixEventServer"] = None,
                 audio_link: Optional[AudioLink] = None,
                 connect_now: bool = False):
        self.cfg = cfg
        self.identity = identity
        self.server = server
        self.audio_link = audio_link
        self.connect_now = connect_now
        self.lock = threading.Lock()

        self.state = "idle"
        self.server_name: Optional[str] = None
        self.room: Optional[str] = None
        self.profile: Optional[str] = None
        self.codec: Optional[str] = None
        self.frame_ms: Optional[int] = None
        self.error: Optional[str] = None
        self.channels: dict[int, Any] = {}
        self.preflight: dict[str, dict] = {}

        self.client: Optional[Client] = None
        self.next_connect = 0.0
        self.preflight_done = False
        self.move_sent = False
        self.target: Any = None
        self.active_clients: set[Any] = set()
        self.classic_client = False
        self.permanent_failure = False
        self.permanent_reason: Optional[str] = None
        self.activation_dirty = False

    # --- estado y publicación -------------------------------------------------

    def channel_dict(self, channel: Any) -> dict:
        result = self.preflight.get(channel.profile, {})
        return {
            "id": channel.id,
            "name": channel.name,
            "profile": channel.profile,
            "codec": codec_family(channel.profile),
            "frame_ms": channel.frame_ms,
            "preflight": bool(result.get("ok")),
        }

    def connection_mode(self) -> str:
        if self.cfg.text_only:
            return "text"
        if not self.cfg.connect_on_activate or self.connect_now:
            return "classic"
        return "activation"

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "ev": "snapshot",
                "state": self.state,
                "server": self.server_name,
                "room": self.room,
                "profile": self.profile,
                "codec": self.codec,
                "frame_ms": self.frame_ms,
                "error": self.error,
                "channels": [self.channel_dict(ch) for ch in self.channels.values()],
                "preflight": self.preflight,
                "users": len(self.client.users) if self.client is not None else 0,
                "mode": "text" if self.cfg.text_only else "audio",
                "audio": self.audio_link.stats() if self.audio_link is not None else None,
                "configured_room": self.cfg.room_text,
                "activation": bool(self.active_clients),
                "connection_mode": self.connection_mode(),
            }

    def publish(self, event: dict) -> None:
        if self.server is not None:
            self.server.broadcast(event)

    def publish_state(self) -> None:
        self.publish(self.snapshot())

    # --- conexión -------------------------------------------------------------

    def _should_connect_locked(self) -> bool:
        if self.permanent_failure:
            return False
        return (
            self.cfg.text_only
            or not self.cfg.connect_on_activate
            or self.connect_now
            or self.classic_client
            or bool(self.active_clients)
        )

    def _clear_connection_locked(self) -> None:
        self.server_name = None
        self.room = None
        self.profile = None
        self.codec = None
        self.frame_ms = None
        self.channels = {}
        self.preflight = {}
        self.target = None
        self.preflight_done = False
        self.move_sent = False

    def _disconnect_client_locked(self) -> None:
        client = self.client
        self.client = None
        if client is not None:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if self.audio_link is not None:
            self.audio_link.on_client_disconnect()
        self.next_connect = 0.0

    def go_idle(self) -> None:
        with self.lock:
            self._disconnect_client_locked()
            self.state = "idle"
            self._clear_connection_locked()
        self.publish_state()

    def fail_permanent(self, reason: str, publish_error: bool = True) -> None:
        with self.lock:
            self.error = reason
            self.permanent_failure = True
            self.permanent_reason = None
            self.active_clients.clear()
            self._disconnect_client_locked()
            self.state = "idle"
            self._clear_connection_locked()
        if publish_error:
            self.publish({"ev": "error", "reason": reason})
        self.publish_state()
        log(f"activación fallida: {reason}; en espera de una nueva activación")

    def reconcile_connection(self) -> None:
        with self.lock:
            self.activation_dirty = False
            want = self._should_connect_locked()
            client = self.client
            state = self.state
            has_data = bool(
                self.server_name or self.room or self.channels
                or self.preflight or self.target is not None
            )
        if not want:
            if client is not None or state != "idle" or has_data:
                self.go_idle()
            return
        if client is None and time.monotonic() >= self.next_connect:
            self.start_client()

    def client_activate(self, connection: Any) -> None:
        with self.lock:
            if connection in self.active_clients:
                return
            self.active_clients.add(connection)
            self.permanent_failure = False
            self.next_connect = 0.0
            self.activation_dirty = True

    def enable_classic_client(self, connection: Any) -> None:
        with self.lock:
            self.classic_client = True
            self.permanent_failure = False
            self.next_connect = 0.0
            self.active_clients.add(connection)
            self.activation_dirty = True

    def client_deactivate(self, connection: Any) -> None:
        with self.lock:
            if connection not in self.active_clients:
                return
            self.active_clients.discard(connection)
            self.activation_dirty = True

    def client_closed(self, connection: Any) -> None:
        with self.lock:
            if connection not in self.active_clients:
                return
            self.active_clients.discard(connection)
            self.activation_dirty = True

    def start_client(self) -> None:
        client_config = Config(text_only=self.cfg.text_only, mode=self.cfg.ptt_mode)
        client = Client(client_config, self.identity, self.cfg.user_name)
        self.client = client
        if self.audio_link is not None:
            self.audio_link.clear_queues()
            self.audio_link.set_ptt(False)
            self.audio_link.set_supported(True)
        with self.lock:
            self.state = "connecting"
            self.error = None
            self.permanent_failure = False
            self.permanent_reason = None
            self._clear_connection_locked()
        self.publish_state()
        log(f"conectando a {self.cfg.server_hash_text} (sala {self.cfg.room_text}) ...")
        client.connect(
            self.cfg.server_hash,
            room=None,
            password=None,
            server_password=self.cfg.server_password or None,
        )

    def pump(self) -> None:
        self.reconcile_connection()

        client = self.client
        if client is None:
            return

        while client.events:
            try:
                self.handle_event(client.events.popleft())
            except Exception as exc:  # noqa: BLE001 - no tumbar el sidecar
                log(f"error procesando evento: {type(exc).__name__}: {exc}")

        if self.permanent_reason is not None:
            reason = self.permanent_reason
            self.permanent_reason = None
            self.fail_permanent(reason, publish_error=False)
            return

        client = self.client
        if client is None:
            return

        if client.state == "closed":
            self.on_disconnect()
        elif client.state == "idle" and client.error:
            if client.kicked:
                self.fail_permanent(client.error)
            else:
                self.publish({"ev": "error", "reason": client.error})
                self.on_disconnect()
        elif client.synced and not self.preflight_done:
            try:
                self.on_synced(client)
            except Exception as exc:  # noqa: BLE001
                self.fail_permanent(
                    f"error preparando la sala: {type(exc).__name__}: {exc}"
                )

    def handle_event(self, event: tuple) -> None:
        kind = event[0]

        if kind == "connected":
            client = self.client
            with self.lock:
                self.state = "connected"
                self.server_name = client.server_name if client else None
            log(f"conectado a {self.server_name!r} (esperando sincronización)")
            self.publish_state()
        elif kind == "version":
            self.publish({"ev": "version", "text": event[1]})
        elif kind == "motd":
            self.publish({"ev": "motd", "text": event[1]})
        elif kind == "notice":
            self.publish({"ev": "notice", "text": event[1]})
        elif kind == "info":
            self.publish({"ev": "info", "text": event[1]})
        elif kind == "synced":
            # on_synced se dispara desde pump() para no hacerlo dos veces
            pass
        elif kind == "room":
            room_id = event[1]
            if self.move_sent and self.target is not None and room_id == self.target.id:
                self.set_final_room(self.target)
            elif room_id is None:
                self.publish({"ev": "room", "id": None, "name": None})
        elif kind == "denied":
            room_id, reason = event[1], event[2]
            log(f"denegado (sala {room_id}): {reason}")
            self.publish({"ev": "denied", "room_id": room_id, "reason": reason})
            with self.lock:
                self.error = reason
                self.permanent_reason = reason
        elif kind == "error":
            reason = event[1]
            log(f"error del cliente: {reason}")
            self.publish({"ev": "error", "reason": reason})
            with self.lock:
                self.error = reason
                if self.client is not None and self.client.kicked:
                    self.permanent_reason = reason
        elif kind == "closed":
            log(f"enlace cerrado: {event[1]}")
            self.publish({"ev": "closed", "reason": event[1]})
        elif kind == "channel":
            self.channels[event[1].id] = event[1]
        elif kind in ("user_joined", "user_left", "user_moved", "user_state",
                      "self_state", "channel_gone"):
            self.publish({"ev": "users", "count": len(self.client.users) if self.client else 0})

    def on_synced(self, client: Client) -> None:
        self.channels = dict(client.channels)
        profiles: dict[str, int] = {}
        for channel in self.channels.values():
            profiles.setdefault(channel.profile, channel.frame_ms)

        results: dict[str, dict] = {}
        for profile, frame_ms_value in sorted(profiles.items()):
            result = preflight_profile(profile, frame_ms_value, self.cfg.sample_rate)
            results[profile] = result
            if result["ok"]:
                log(
                    "codec preflight: {} OK ({}; {} ms; {} B; {} muestras)".format(
                        profile, result["codec"], result["frame_ms"],
                        result["encoded_bytes"], result["decoded_samples"]
                    )
                )
            else:
                log(f"codec preflight: {profile} FAIL: {result.get('error')}")

        with self.lock:
            self.preflight = results

        self.publish({
            "ev": "synced",
            "server": client.server_name,
            "channels": [self.channel_dict(ch) for ch in self.channels.values()],
            "preflight": results,
        })

        target = self.resolve_target(client)
        if target is None:
            self.fail_permanent(
                f"no existe la sala {self.cfg.room_text!r} en el servidor"
            )
            return

        family = codec_family(target.profile)
        if family not in self.cfg.allowed_codecs:
            self.fail_permanent(
                f"la sala {target.name!r} usa {family}, no permitido por "
                f"ALLOWED_CODECS={'/'.join(sorted(self.cfg.allowed_codecs))}"
            )
            return

        result = results.get(target.profile, {})
        if not result.get("ok"):
            self.fail_permanent(
                f"la sala {target.name!r} usa el perfil {target.profile}, "
                "cuyo códec no está disponible"
            )
            return

        self.target = target
        with self.lock:
            self.room = target.name
            self.profile = target.profile
            self.codec = describe(target.profile)
            self.frame_ms = target.frame_ms
            self.error = None
        self.preflight_done = True

        if client.my_room == target.id:
            self.set_final_room(target)
        else:
            log(f"entrando en la sala {target.name!r} (id {target.id}) ...")
            self.move_sent = True
            client.move(target.id, self.cfg.room_password or None)
            self.publish_state()

    def resolve_target(self, client: Client) -> Any:
        if isinstance(self.cfg.room, int):
            return client.channels.get(self.cfg.room)
        wanted = str(self.cfg.room).strip().casefold()
        for channel in client.channels.values():
            if channel.name.casefold() == wanted:
                return channel
        return None

    def set_final_room(self, target: Any) -> None:
        with self.lock:
            self.state = "connected"
            self.room = target.name
            self.profile = target.profile
            self.codec = describe(target.profile)
            self.frame_ms = target.frame_ms
            self.error = None
        self.move_sent = False
        self.publish({
            "ev": "room",
            "id": target.id,
            "name": target.name,
            "profile": target.profile,
            "codec": describe(target.profile),
            "frame_ms": target.frame_ms,
        })
        self.publish_state()
        log(
            f"en sala {target.name!r}: perfil {target.profile}, "
            f"{describe(target.profile)}"
        )

    def on_disconnect(self) -> None:
        with self.lock:
            self._disconnect_client_locked()
            want = self._should_connect_locked()
            error = self.error
            if want:
                self.state = "reconnecting"
                self.next_connect = (
                    time.monotonic() + self.cfg.reconnect_delay_ms / 1000.0
                )
            else:
                self.state = "idle"
                self._clear_connection_locked()
                self.next_connect = 0.0
        if want:
            self.publish({"ev": "state", "state": "reconnecting", "error": error})
            self.publish_state()
            log(f"reconectando en {self.cfg.reconnect_delay_ms} ms")
        else:
            self.publish_state()
            log("sin clientes activos; en espera de activación")

    def set_ptt(self, on: bool) -> None:
        if on and self.client is not None:
            self.client.clear_speakers()
        if self.audio_link is not None:
            self.audio_link.set_ptt(on)
        if self.client is not None and self.cfg.ptt_mode == "ptt":
            self.client.set_transmit(on)

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        return str(value).strip().lower() in ("1", "true", "yes", "on", "si", "sí")

    def command(self, connection: Any, obj: dict) -> None:
        cmd = obj.get("cmd")
        if cmd == "hello":
            audio_rate = obj.get("audio_rate")
            audio_format = str(obj.get("format", "s16le")).lower()
            activation_gate = "activation_gate" in obj
            active = self._truthy(obj.get("active")) if activation_gate else True
            log(
                "módulo conectado: client={} ver={} audio_rate={} format={} "
                "channels={} active={} activation_gate={}".format(
                    obj.get("client"), obj.get("ver"), audio_rate,
                    audio_format, obj.get("channels"),
                    int(active), int(activation_gate)
                )
            )
            if not activation_gate:
                self.enable_classic_client(connection)
            elif active:
                self.client_activate(connection)
            else:
                self.client_deactivate(connection)
            if self.audio_link is not None and not self.cfg.text_only:
                if audio_rate != AUDIO_SAMPLE_RATE or audio_format != "s16le":
                    reason = (
                        f"audio no soportado: rate={audio_rate}, format={audio_format}; "
                        f"se exige {AUDIO_SAMPLE_RATE} Hz y s16le"
                    )
                    self.audio_link.set_supported(False, reason)
                    log(f"ERROR: {reason}")
                    self.publish({"ev": "error", "reason": reason})
                else:
                    self.audio_link.set_supported(True)
                    log("audio: 48000 Hz s16le OK")
            if self.server is not None:
                self.server.broadcast(self.snapshot())
        elif cmd == "activate":
            self.client_activate(connection)
        elif cmd == "deactivate":
            self.client_deactivate(connection)
        elif cmd == "ptt":
            self.set_ptt(bool(obj.get("on")))
        elif cmd == "say":
            text = str(obj.get("text", ""))
            if (self.client is not None and self.client.connected
                    and self.client.my_room is not None and text.strip()):
                self.client.send_text(text)
                log(f"texto enviado: {text[:80]}")
            else:
                log("texto ignorado: no hay conexión o sala")
        elif cmd == "ping":
            if self.server is not None:
                self.server.broadcast({"ev": "pong"})

    def shutdown(self) -> None:
        if self.audio_link is not None:
            self.audio_link.on_client_disconnect()
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self.client = None


class _Handler(socketserver.StreamRequestHandler):
    def setup(self) -> None:
        super().setup()
        # Enviar la instantánea antes de registrar el cliente evita que un
        # broadcast se cuele entre la instantánea y los eventos siguientes.
        self.server.send(self.request, self.server.snapshot_provider())
        self.server.register_client(self.request)
        self.server.logger("módulo conectado al socket")

    def handle(self) -> None:
        while True:
            try:
                line = self.rfile.readline()
            except OSError:
                break
            if not line:
                break
            try:
                obj = json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                try:
                    self.server.command_handler(self.request, obj)
                except Exception as exc:  # noqa: BLE001
                    self.server.logger(f"error procesando comando: {exc}")

    def finish(self) -> None:
        self.server.client_closed_handler(self.request)
        self.server.unregister_client(self.request)
        self.server.logger("módulo desconectado del socket")
        super().finish()


class UnixEventServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, snapshot_provider: Callable[[], dict],
                 command_handler: Callable[[Any, dict], None],
                 client_closed_handler: Callable[[Any], None],
                 logger: Callable[[str], None]):
        self.socket_path = path
        self.snapshot_provider = snapshot_provider
        self.command_handler = command_handler
        self.client_closed_handler = client_closed_handler
        self.logger = logger
        self.clients: set[Any] = set()
        self.clients_lock = threading.Lock()

        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.exists(path):
            info = os.stat(path)
            if stat.S_ISSOCK(info.st_mode):
                os.unlink(path)
            else:
                raise SystemExit(f"error: {path} existe y no es un socket")

        super().__init__(path, _Handler)
        os.chmod(path, 0o666)

    def register_client(self, connection: Any) -> None:
        with self.clients_lock:
            self.clients.add(connection)

    def unregister_client(self, connection: Any) -> None:
        with self.clients_lock:
            self.clients.discard(connection)

    def send(self, connection: Any, event: dict) -> bool:
        data = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            connection.sendall(data)
            return True
        except OSError:
            return False

    def broadcast(self, event: dict) -> None:
        with self.clients_lock:
            clients = list(self.clients)
        for connection in clients:
            if not self.send(connection, event):
                self.unregister_client(connection)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(
        description="Puente de control SvxLink <-> Partyline (M1, sin audio)"
    )
    parser.add_argument(
        "--config",
        default=os.path.join(root, "config", "ModuleRnsPartyline.conf"),
        help="archivo INI compartido (por defecto config/ModuleRnsPartyline.conf)",
    )
    parser.add_argument("--section", default="partyline",
                        help="sección con los datos de Partyline (por defecto partyline)")
    parser.add_argument("--bridge-section", default="bridge",
                        help="sección con SOCKET_PATH/RECONNECT_DELAY_MS (por defecto bridge)")
    parser.add_argument("--module-section", default="ModuleRnsPartyline",
                        help="sección del módulo SvxLink (por defecto ModuleRnsPartyline)")
    parser.add_argument("--socket", default=None,
                        help="sobrescribe SOCKET_PATH")
    parser.add_argument("--audio-socket", default=None,
                        help="sobrescribe AUDIO_SOCKET_PATH")
    parser.add_argument("--connect-now", action="store_true",
                        help="conecta al arrancar (comportamiento clásico)")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    try:
        cfg = load_config(args.config, args)
    except ConfigError as exc:
        log(f"ERROR de configuración: {exc}")
        return 2

    log_summary(cfg)

    try:
        identity = load_identity(cfg.identity_file)
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        log(f"ERROR: no se pudo cargar o crear la identidad {cfg.identity_file}: {exc}")
        return 2

    try:
        RNS.Reticulum(configdir=cfg.rns_config_dir)
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        log(f"ERROR: no se pudo iniciar Reticulum con {cfg.rns_config_dir}: {exc}")
        return 2

    audio_link = AudioLink(cfg, log)
    if not cfg.text_only:
        try:
            install_audio_hooks(audio_link)
        except RuntimeError as exc:
            log(f"ERROR: {exc}")
            return 2
        try:
            audio_link.start()
        except (OSError, SystemExit) as exc:
            log(f"ERROR: no se pudo abrir el socket de audio {cfg.audio_socket_path}: {exc}")
            return 2

    sidecar = Sidecar(
        cfg, identity, audio_link=audio_link, connect_now=args.connect_now
    )
    try:
        server = UnixEventServer(
            cfg.socket_path,
            snapshot_provider=sidecar.snapshot,
            command_handler=sidecar.command,
            client_closed_handler=sidecar.client_closed,
            logger=log,
        )
    except (OSError, SystemExit) as exc:
        log(f"ERROR: no se pudo abrir el socket {cfg.socket_path}: {exc}")
        return 2
    sidecar.server = server

    stop = threading.Event()

    def on_signal(signum: int, _frame: Any) -> None:
        log(f"señal {signum}; cerrando")
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    log(f"escuchando módulos en {cfg.socket_path}")
    if (not cfg.text_only and cfg.connect_on_activate
            and not args.connect_now):
        log("modo audio: en espera de la activación del módulo")
    else:
        log("modo clásico: conexión al arrancar")

    try:
        while not stop.is_set():
            sidecar.pump()
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        sidecar.shutdown()
        audio_link.close()
        server.shutdown()
        server.server_close()
        try:
            os.unlink(cfg.socket_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log(f"aviso: no se pudo borrar el socket {cfg.socket_path}: {exc}")
        log("sidecar detenido")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
