# AGENTS.md

## Project

`rns-partyline-svx` is a control and audio bridge between SvxLink 1.10.1 and a
Partyline server (Reticulum + LXST). M1 provides the control channel; M2 adds
real audio:

- `ModuleRnsPartyline.so` loads in a SvxLink `SimplexLogic`.
- A Python sidecar uses `partyline.Client` to connect, join a room and validate
  Opus/Codec2 profiles.
- In audio mode the sidecar starts in `idle` and waits for the module's
  activation (`3#`); deactivation returns it to `idle`. `TEXT_ONLY=1` and
  `CONNECT_ON_ACTIVATE=0`/`--connect-now` connect at startup.
- State is published to the module over a Unix socket as NDJSON.
- PCM audio is exchanged over a second Unix socket as binary frames.
- The receiver squelch drives Partyline PTT, with local half-duplex priority.
- Remote audio is flushed from SvxLink's TX chain after 200 ms without RX
  audio, so `Tx1` unkeys when a remote talker stops.

The C++ module must stay free of RNS/LXST and codec logic. The sidecar owns the
Partyline connection, configuration and secrets.

## Layout

```
CMakeLists.txt                     external module build
src/module/ModuleRnsPartyline.*    C++ plugin: control, audio, PTT, activation
sidecar/rns_partyline_sidecar.py   Partyline client + control/audio sockets
config/ModuleRnsPartyline.conf     single INI shared by both processes
test/svxlink-test.conf             text-only test SvxLink instance (template)
test/svxlink-test-audio.conf       UDP audio test instance (template)
test/svxlink-test-radio.conf       real card + HID PTT test instance (template)
test/events.d/RnsPartyline.tcl     module Tcl handler (required by SvxLink)
scripts/build.sh                   cmake configure + build
scripts/run-test.sh                sidecar + test SvxLink (--text/--audio)
scripts/audio-client.py            synthetic sidecar audio client
scripts/audio-tone.py              UDP tone injector/capturer
scripts/activation-check.py        control-socket activation lifecycle checks
deploy/install-production.sh       root installer for module + sidecar service
deploy/rns-partyline-sidecar.service
                                   systemd unit for the sidecar
openspec/changes/archive/2026-10-25-add-audio-bridge/
                                   archived M2 change
openspec/specs/                    synced main specs
```

## Quick commands

```bash
./scripts/build.sh                       # -> build/ModuleRnsPartyline.so
./scripts/run-test.sh --text             # M1 regression, no audio
./scripts/run-test.sh --audio            # M2, UDP audio
/usr/bin/python3 scripts/activation-check.py --scenario lifecycle
/usr/bin/python3 sidecar/rns_partyline_sidecar.py \
  --config config/ModuleRnsPartyline.conf          # sidecar alone
/usr/local/bin/svxlink --config=build/svxlink-test.conf
openspec validate --specs --strict       # main specs
sudo ./deploy/install-production.sh      # production module + sidecar unit
```

Hardware/walkie test (brief; full steps in README):

```bash
systemctl stop svxlink                   # temporary; restore afterwards
mkdir -p build/test-config-radio
cp config/ModuleRnsPartyline.conf build/test-config-radio/
sed -e "s|__PROJECT_ROOT__|$PWD|g" \
    -e "s|__CFG_DIR__|$PWD/build/test-config-radio|g" \
    test/svxlink-test-radio.conf > build/svxlink-test-radio.conf
/usr/bin/python3 sidecar/rns_partyline_sidecar.py \
  --config build/test-config-radio/ModuleRnsPartyline.conf &
/usr/local/bin/svxlink --config=build/svxlink-test-radio.conf
# Ctrl-C; stop the sidecar by PID (no pkill); then: systemctl start svxlink
```

`run-test.sh` copies `config/ModuleRnsPartyline.conf` into
`build/test-config-{text,audio}/` with the selected `TEXT_ONLY` and
`AUTO_ACTIVATE`, then generates `build/svxlink-test.conf` from the matching
template by replacing `__PROJECT_ROOT__` and `__CFG_DIR__`. It also creates
the local `test/events.tcl`, `test/events.d/*` and `test/sounds` symlinks.

## Architecture rules

- The module is a normal SvxLink plugin: `extern "C" module_init`, derives from
  `Module`, returns `SVXLINK_APP_VERSION` from
  `build/include/version/SVXLINK.h`. The core compares it with `strcmp`, so it
  must be exactly the installed version (`1.10.1` here).
- The module must not open audio devices, implement codecs, parse Partyline, or
  read passwords. It reads `SOCKET_PATH`, `AUDIO_SOCKET_PATH`, `AUTO_CONNECT`,
  `AUTO_ACTIVATE`, `RECONNECT_DELAY_MS`, `AUDIO_PUSH_MS`, `TEXT_ONLY` and
  `SAMPLE_RATE`; everything else belongs to the sidecar.
- SvxLink's internal audio rate is `INTERNAL_SAMPLE_RATE` (16 kHz in this
  build), **not** `CARD_SAMPLE_RATE`. The wire format is 48 kHz mono `s16le`;
  the module resamples 16↔48 at the ring boundary. CMake reads
  `INTERNAL_SAMPLE_RATE` from the SvxLink build and defines it for the module.
- Audio threading: `writeSamples` runs on the SvxLink audio thread and only
  appends to the TX ring. The audio socket thread parses RX frames into the RX
  ring and flushes the TX ring. An `Async::Timer` (20 ms) drains the RX ring and
  calls `sinkWriteSamples`. Never call the audio API from the socket thread.
- `AudioFifo` is not thread-safe across those threads. Use the bounded
  ring/deque + mutex design; drop the oldest samples on overflow.
- Auto-activation must use `Async::Timer` from the event loop, never
  `activateMe()` inside `initialize()`: `Logic::loadModule` registers the
  module's audio source/sink after `initialize()`.
- Activation signaling: maintain the `active_` mirror, send `activate` in
  `activateInit()`, send `deactivate` at the very start of
  `deactivateCleanup()` (before PTT/audio teardown), and include `active` plus
  `activation_gate: 1` in every `hello`. Use the mirror, not `isActive()`, so
  a reconnect restores the sidecar connection. Do not send `activate` in text
  mode.
- Sidecar connection gate: audio + `CONNECT_ON_ACTIVATE=1` stays `idle` until
  an `activate` or a gated `hello` with `active: 1`. Keep the per-connection
  active-client set; only the last deactivation/close leaves the room. A
  `hello` without `activation_gate` is a classic client and forces connection.
  `TEXT_ONLY=1` and `CONNECT_ON_ACTIVATE=0`/`--connect-now` bypass the gate.
- On the last deactivation, stop PTT, clear queues, cancel retries, disconnect
  Partyline, clear connection fields and publish `idle` (preserve `error`). A
  permanent failure (denied/server or room password, missing room, codec
  policy/unavailable) also returns to `idle` without exiting; retry only after
  a new activation or a reconnecting active `hello`.
- `dtmfCmdReceived` must handle the empty command: SvxLink turns a bare `#`
  into an empty DTMF command, and the module must call `deactivateMe()` or `#`
  does nothing. Keep this override when changing module DTMF handling.
- `Logic::squelchOpen` only calls the active module. `TEXT_ONLY=1` must stay
  inactive; `TEXT_ONLY=0` auto-activates (unless `AUTO_ACTIVATE=0`).
- The module emits PTT to the sidecar; the local transmitter is still driven by
  SvxLink `TX_AUTO` when remote audio is written. `flushSamples` must be
  overridden (the base `AudioSink` asserts if no handler is set).
- End-of-audio invariant: `PttCtrl` only unkeys `Tx1` when it receives
  `allSamplesFlushed`. `pushRxToSink` must call `sinkFlushSamples()` after
  `RX_IDLE_FLUSH_MS` (200 ms) with an empty RX ring, and immediately when the
  local squelch opens. Reset `rx_has_audio_` on activation/deactivation. Do not
  remove this flush: without it remote audio leaves `Tx1` keyed.
- `allSamplesFlushed` must be overridden as a no-op in the module. The module
  is an audio source without a chained handler; the base-class implementation
  asserts `m_handler != 0` and aborts SvxLink when the RX sink is flushed.
  Call `sinkFlushSamples()` only from the event-loop/timer path, never from the
  audio socket thread.
- The sidecar must use `/usr/bin/python3` (system Python with `partyline`,
  `rns`, `lxst`, `numpy`, `pycodec2`). Do not assume a project venv.
- The sidecar must not open PortAudio or `/dev/snd`. It injects
  `SvxSink`/`SvxSource` for `partyline.client.LineSink`/`LineSource` and reuses
  `Playout`, `Pipeline`, `VoiceGate` and `make_codec`. Guard the injection: if
  those names disappear, fail with a clear error instead of falling back to
  PortAudio.
- `SvxSink` must pace output at the wire rate; without pacing `Playout` floods
  the module socket and the RX ring drops audio.
- `SvxSource` must emit exactly one codec frame per room `frame_ms`
  (20/40/60/200/400 ms). Codec2 must not be fed 20 ms chunks.
- Do not log passwords. The startup summary shows `***` and warns when the INI
  is group/other-readable. Never add a log line that prints a password value.
- Codec selection is always driven by the room's `profile` from the server
  (`FIELD_CHANNEL`). Use LXST `make_codec`; do not hardcode Opus/Codec2 in the
  C++ side. `ALLOWED_CODECS` (`opus,c2`) is a policy filter, not a selector.
- The sidecar connects with `room=None` to obtain the channel list, runs the
  codec preflight, then moves to the configured room with `ROOM_PASSWORD`.
  Keep this flow: it is what allows validating a room before joining it.
- Room resolution: numeric `ROOM` is an ID; otherwise match the name
  case-insensitively. Publish the resolved name.
- The Unix sockets are local trust: `chmod 0666` is deliberate so the systemd
  `svxlink` user can connect. Do not expose them over TCP without adding
  authentication.

## Configuration contract

One INI, three sections:

- `[ModuleRnsPartyline]`: `NAME=RnsPartyline` (must match `Module<NAME>.so`),
  `ID` (currently `3`; activate with `3#` or `*3#`), `AUTO_CONNECT`,
  `AUTO_ACTIVATE`.
- `[bridge]`: `SOCKET_PATH` (absolute), `AUDIO_SOCKET_PATH` (absolute and
  different), `RECONNECT_DELAY_MS`, `CONNECT_ON_ACTIVATE` (0/1, default 1),
  `AUDIO_PUSH_MS`. `--connect-now` forces the classic startup connection.
- `[partyline]`: `SERVER_HASH` (32 hex), `ROOM`, `USER_NAME`,
  `SERVER_PASSWORD`, `ROOM_PASSWORD`, `RNS_CONFIG_DIR`, `IDENTITY_FILE`,
  `TEXT_ONLY`, `SAMPLE_RATE`, `ALLOWED_CODECS`, `PTT_MODE`, `HALF_DUPLEX`.

`TEXT_ONLY=1` is the control mode (M1). `TEXT_ONLY=0` enables audio and PTT.
`SAMPLE_RATE` is the wire rate; this change requires `48000`. Do not confuse it
with `GLOBAL/CARD_SAMPLE_RATE`. The module reads its sections through
`Async::Config`; the sidecar reads the same file with `configparser`. Do not
introduce a second production config file. Keep comments on their own lines.

## Control protocol (NDJSON)

```
module -> sidecar   {"cmd":"hello","client":"svxlink","ver":"1.10.1","audio_rate":48000,"format":"s16le","channels":1,"text_only":0,"active":0|1,"activation_gate":1}
module -> sidecar   {"cmd":"activate"} | {"cmd":"deactivate"}
module -> sidecar   {"cmd":"say","text":"..."}          # optional, text only
module -> sidecar   {"cmd":"ptt","on":true|false}
module -> sidecar   {"cmd":"ping"}
sidecar -> module   {"ev":"snapshot","state":...,"server":...,"room":...,"profile":...,"codec":...,"frame_ms":...,"channels":[...],"preflight":{...},"mode":"audio|text","audio":{...},"configured_room":...,"activation":true|false,"connection_mode":"activation|classic|text"}
sidecar -> module   {"ev":"state","state":"idle|connecting|connected|reconnecting|error"}
sidecar -> module   {"ev":"synced","server":...,"channels":[...],"preflight":{...}}
sidecar -> module   {"ev":"room","id":...,"name":...,"profile":...,"codec":...,"frame_ms":...}
sidecar -> module   {"ev":"denied","room_id":...,"reason":...}
sidecar -> module   {"ev":"error","reason":...} | {"ev":"closed","reason":...}
```

A new client receives a `snapshot` before any later broadcast. Keep this
ordering: `_Handler.setup` sends the snapshot, then registers the client.
`hello` must include `format:"s16le"`; a rate other than 48000 disables audio
but keeps control.

## Audio protocol

The audio socket is `AUDIO_SOCKET_PATH` (default `SOCKET_PATH + ".audio"`).
Each message is:

```
type:u8 (0x01 = PCM) | length:u32 little-endian | payload
```

Payload is mono `s16le` at 48 kHz. Maximum frame 65536 bytes; an invalid length
closes the audio connection. The module sends chunks of `AUDIO_PUSH_MS`
(default 20 ms); the sidecar sends `Playout` blocks (~60 ms). Both sides use
bounded queues and drop the oldest samples on overflow.

## Test harness rules

- `ReflectorLogic` inherits `LogicBase` and does not load modules. Use the
  separate `SimplexLogic` instance from `test/svxlink-test*.conf`.
- Production runs on `CARD=Fred` with `PTT_TYPE=Hidraw` and initially did not
  load `ModuleRnsPartyline`. Install it only through
  `deploy/install-production.sh`, which backs up `svxlink.conf`, installs the
  module/config/Tcl handler and enables `rns-partyline-sidecar.service`. Do not
  hand-edit `/usr/local/etc/svxlink` or the systemd unit.
- The hardware/walkie test uses `test/svxlink-test-radio.conf`: real `Fred`
  card, CM108B HID PTT (`/dev/cm108gpio`, GPIO3), `SQL_DET=CTCSS` with
  `CTCSS_FQ=100.0`, and `MODULES=ModuleRnsPartyline`. It needs exclusive card
  and HID access, so stop `svxlink.service` first and restore it afterwards.
- The walkie PTT is RF into `Rx1`; the HID PTT is the opposite direction
  (Partyline audio -> local transmitter). Do not confuse them.
- Stop test processes by PID. Do not use `pkill` in this environment; it can
  hang. If the HID PTT is stuck after a crash, write the GPIO3-off report:
  `bytes([0, 0, 0, 0x04, 0])` to `/dev/cm108gpio`.
- `--text` uses `alsa:null`, `SQL_DET=VOX`, `PTT_TYPE=NONE` and
  `AUTO_ACTIVATE=0`. `--audio` uses UDP: `Rx1=udp:127.0.0.1:52010`,
  `Tx1=udp:127.0.0.1:52011`, `CARD_CHANNELS=2`, `SQL_DET=VOX`,
  `PTT_TYPE=NONE`. The UDP device uses raw 16-bit samples; use
  `scripts/audio-tone.py --channels 2`.
- `EVENT_HANDLER` must point to the local Tcl tree. SvxLink requires
  `<NAME>.tcl` in `events.d`; `test/events.d/RnsPartyline.tcl` provides it.
  `run-test.sh` creates the symlinks and substitutes the placeholders.
- `MODULE_PATH=build` is relative to the working directory; the test script
  runs from the repository root. Do not change that assumption without
  updating the script.
- `ROOM_OVERRIDE=4 ./scripts/run-test.sh --audio` exercises a Codec2 room.
  For the official listener use `--room "Codec2 3200"`; `--room 4` is treated
  as a room name by `partyline-client`, not as an ID.
- `run-test.sh --audio` asserts the sidecar starts in `idle` and has no
  `conectando a` line before SvxLink starts; `--text` asserts the startup
  connection. `scripts/activation-check.py` drives the control socket for
  `idle`, `lifecycle`, `reconnect`, `classic` and `connect-now` scenarios.

## Known gotchas

- `MODULES=ModuleRnsPartyline` is the config section; the plugin filename comes
  from `NAME=RnsPartyline`. Missing `NAME` produces `ModuleModuleRnsPartyline.so`.
- The module ID is currently `3`. In production `RepeaterLogic`, ID `3` is
  `ModuleTclVoiceMail`; do not load both in the same logic. The test/hardware
  `SimplexLogic` only loads `ModuleRnsPartyline`. The installer sets
  `AUTO_ACTIVATE=0`; activate with `3#` or `*3#`.
- The sidecar does not connect at startup in audio mode by default. If no
  `3#` was sent, `state: idle` and the absence of a `conectando a` line are
  correct, not a failure. Use `CONNECT_ON_ACTIVATE=0` or `--connect-now` only
  for the classic behavior.
- A permanent activation error (denied/password/missing room/codec) must not
  exit or crash-loop the sidecar. It stays alive in `idle`; a new `3#` (`#` then
  `3#`) or an active `hello` retries.
- The production service runs the sidecar as `areche` and reads
  `/usr/local/etc/svxlink/svxlink.d/ModuleRnsPartyline.conf`. The service uses
  `HOME=/home/areche`, so `RNS_CONFIG_DIR`/`IDENTITY_FILE` keep expanding under
  that home. Do not point the service at the mutable `build/` test config.
- The module source and build headers are not installed by SvxLink; the CMake
  project depends on `SVXLINK_SRC`/`SVXLINK_BUILD`. Rebuild after any upgrade.
- `INTERNAL_SAMPLE_RATE` is a SvxLink build define (16 kHz here), not a config
  value. The module resamples to the 48 kHz wire rate; changing `SAMPLE_RATE`
  without changing the code breaks audio.
- `Tx1` staying keyed is the end-of-audio regression: keep the 200 ms RX idle
  flush in `pushRxToSink`, the local-squelch flush and the no-op
  `allSamplesFlushed` override. Without the override SvxLink aborts with
  `Assertion 'm_handler != 0' failed`.
- The hardware test is the only way to exercise the CM108B HID PTT. It needs
  `systemctl stop svxlink` first; the normal `--text`/`--audio` harnesses never
  touch the real card.
- The current config points to a server named `Test` and room `Opus High`.
  Its destination hash lives only in `config/ModuleRnsPartyline.conf`;
  do not copy it into documentation.
- `test/events.tcl` and `test/events.d/*` are symlinks to the installed
  SvxLink files; only `test/events.d/RnsPartyline.tcl` is project-owned.
- The sidecar's RNS config must have `share_instance = No`. The development
  config is `~/.config/partyline-dev/client-rns`.
- The sidecar creates its own identity at `~/.config/rns-partyline-svx/identity`
  with mode `0600`; do not reuse the development client identity.
- The `partyline-client` test identity and the sidecar should use different RNS
  config/identity; running several test clients against the same `client-rns`
  storage can produce RNS `persist_job` warnings.
- The server hash is stable only while the server identity is stable. Update
  `SERVER_HASH` if the server identity changes.
- The full server password / room password denial path is integration-tested
  against a temporary password-protected server; the normal dev server has no
  passwords.

## Verification checklist

- `./scripts/build.sh` produces `build/ModuleRnsPartyline.so`, logs
  `SvxLink version: 1.10.1` and `SvxLink internal sample rate: 16000`.
- Sidecar: in audio mode starts in `idle` with no Partyline connection; after
  `3#` it connects to `Test`, joins the requested room and prints `codec
  preflight` lines for all advertised profiles (all ten currently pass).
- Module: loads in `SimplexLogic`, logs `conectado al sidecar`, sends
  `snapshot`/`synced`/`room` after activation; in audio mode also logs
  `audio conectado a /tmp/rns-partyline-svx.sock.audio`.
- `--text`: no audio socket, no auto-activation, no PTT, but connects to
  Partyline at startup despite `CONNECT_ON_ACTIVATE=1`.
- Audio without activation: `--audio` starts idle, no `conectando a` line and
  no room presence. `3#` connects; `#` leaves the room and returns to `idle`.
- `--audio`: UDP tone on 52010 opens VOX, module sends `ptt on`, and a
  Partyline listener hears 1000 Hz. A Partyline sender is heard on UDP 52011 at
  1000 Hz (capture with `--channels 2`).
- Opus High (room 1) and Codec2 3200 (room 4) both carry audio. Codec2 has more
  latency and spectral distortion; that is expected.
- PTT off appears in the sidecar log after the squelch closes (send
  `--tail-silence` with the UDP tone).
- Remote audio must show `Tx1: Turning the transmitter ON` followed by
  `Tx1: Turning the transmitter OFF` after the talker stops. Capture UDP 52011
  with `scripts/audio-tone.py --capture --channels 2`.
- Hardware walkie test: pressing PTT opens `Rx1` squelch, sidecar logs
  `ptt on (prioridad local)`, releasing logs `ptt off` with `mic` frames, and
  `Tx1` must not remain ON.
- After the hardware test, `systemctl start svxlink` restores production and
  the HID PTT is released (GPIO3 off).
- Production install: `MODULES` in `SimplexLogic` includes
  `ModuleRnsPartyline`, `RnsPartyline.tcl` is in the installed events tree,
  `rns-partyline-sidecar.service` and `svxlink.service` are active, `3#`
  activates the module (audio socket appears only after activation), and a
  bare `#` deactivates it.
- Kill and restart the sidecar: the module logs the disconnect and reconnects;
  if the module is still active, its gated `hello` (`active: 1`) restores the
  Partyline connection without a new `3#`.
- `ROOM=1` and `ROOM=Opus High` resolve to the same room.
- Nonexistent room and wrong server/room password produce `denied`/`error`,
  leave the sidecar alive in `idle`, and only retry after a new activation.
- `CONNECT_ON_ACTIVATE=0` and `--connect-now` connect at startup and do not
  leave the room on module deactivation; `TEXT_ONLY=1` connects at startup.
- Logs contain no password values; a group/other-readable INI with passwords
  triggers the permissions warning.
- No `/dev/snd` file descriptors are opened by the sidecar or the test
  SvxLink instance.

## Future work (M3)

- Audio level/CTCSS tuning and long-run testing with the physical transceiver.
  The CM108B HID PTT path is exercised by `test/svxlink-test-radio.conf`.
- Harden the production service (dedicated sidecar user, secret handling, log
  rotation) and finish long-run tuning.
- Optionally migrate the module socket I/O to `Async::FdWatch`.
