# rns-partyline-svx

Control and audio bridge between **SvxLink** and a **Partyline** server
(Reticulum + LXST). M1 provides the control channel; M2 adds real audio:

- SvxLink loads `ModuleRnsPartyline.so` in a `SimplexLogic`.
- A Python sidecar connects to Partyline, joins the configured room and
  validates the Opus/Codec2 profiles.
- In audio mode the sidecar starts in `idle` and only connects after the module
  is activated (`3#`). Deactivating it (`#`) leaves the room and returns to
  `idle`; text mode and `CONNECT_ON_ACTIVATE=0`/`--connect-now` keep connecting
  at startup.
- Control state travels as NDJSON over one Unix socket.
- PCM audio travels as binary frames over a second Unix socket.
- The receiver squelch is translated into Partyline PTT, with local
  half-duplex priority.
- Remote audio is flushed out of SvxLink's TX chain after 200 ms without RX
  audio, so the local transmitter unkeys even when a remote talker stops
  without an explicit end-of-stream signal.

```
SvxLink (SimplexLogic)                 Python sidecar
┌───────────────────────┐  control   ┌───────────────────────┐  RNS/LXST  ┌──────────────────┐
│ ModuleRnsPartyline.so │◄── NDJSON ─►│ rns_partyline_sidecar │◄─────────►│ partyline-server │
│  C++: no codecs       │  audio     │  SvxSource/SvxSink    │            │  rooms/codecs    │
│  PCM s16le 48 kHz     │◄── frames ─►│  Playout/Pipeline     │            │                  │
└───────────────────────┘            └───────────────────────┘            └──────────────────┘
```

The C++ side never sees RNS/LXST or codecs. The wire format is `s16le`, mono,
48 kHz; SvxLink's internal pipeline runs at `INTERNAL_SAMPLE_RATE` (16 kHz in
the installed build), so the module resamples 16↔48 kHz at the ring boundary.
The CMake build reads `INTERNAL_SAMPLE_RATE` from the SvxLink build.

## Requirements

- SvxLink **1.10.1** built from source, with the source tree at
  `~/svx/svxlink-maint` and the build at `~/svx/svxlink-maint/build`
  (`Module.h`/`Logic.h` are not installed; they are used from the source tree).
- `/usr/bin/python3` 3.14 with `partyline`, `rns`, `lxst`, `numpy` and
  `pycodec2`.
- A Partyline server reachable over RNS. Set its destination hash in
  `SERVER_HASH`; the current development setup targets a server named `Test`
  and the room `Opus High`.
- CMake >= 3.16, `sigc++-3.0`, `pthread`, g++ with C++17.
- The normal tests require no sudo and do not touch systemd. The optional
  hardware/walkie test requires temporarily stopping `svxlink.service`; never
  edit `/usr/local/etc/svxlink` or the systemd unit for testing. The separate
  production installer is the only sanctioned root change.

Check the Python dependencies:

```bash
/usr/bin/python3 -m pip list | grep -iE 'partyline|rns|lxst|pycodec2|numpy'
```

## Build

```bash
./scripts/build.sh
# equivalent to:
# cmake -S . -B build -DSVXLINK_SRC="$HOME/svx/svxlink-maint"
# cmake --build build
```

If the SvxLink source or build lives elsewhere:

```bash
SVXLINK_SRC=/path/to/source SVXLINK_BUILD=/path/to/build ./scripts/build.sh
```

The build prints `SvxLink version: 1.10.1` and
`SvxLink internal sample rate: 16000`. The output is
`build/ModuleRnsPartyline.so`. The module returns exactly the
`SVXLINK_APP_VERSION` from `build/include/version/SVXLINK.h`; rebuild after a
SvxLink upgrade.

## Configuration

A single INI file: `config/ModuleRnsPartyline.conf`.

| Section | Key | Read by | Notes |
|---|---|---|---|
| `[ModuleRnsPartyline]` | `NAME` | SvxLink | Must be `RnsPartyline` to load `ModuleRnsPartyline.so` |
| | `ID` | SvxLink | Module DTMF ID; currently `3`, so dial `3#` or `*3#` |
| | `AUTO_CONNECT` | module | `1` connects at startup, without DTMF |
| | `AUTO_ACTIVATE` | module | `1` activates the module automatically in audio mode |
| `[bridge]` | `SOCKET_PATH` | both | Absolute Unix control socket path |
| | `AUDIO_SOCKET_PATH` | both | Absolute audio socket; must differ from `SOCKET_PATH`; default `SOCKET_PATH + ".audio"` |
| | `RECONNECT_DELAY_MS` | both | Retry interval for both connections |
| | `CONNECT_ON_ACTIVATE` | sidecar | `1` (default) waits for `3#` in audio; `0` connects at startup |
| | `AUDIO_PUSH_MS` | both | Module TX block size in ms (default 20) |
| `[partyline]` | `SERVER_HASH` | sidecar | Server hash, 32 hex characters |
| | `ROOM` | sidecar | Number (`1`) or name (`Opus High`) |
| | `USER_NAME` | sidecar | Visible name (max 48) |
| | `SERVER_PASSWORD` | sidecar | Empty = not used |
| | `ROOM_PASSWORD` | sidecar | Empty = not used |
| | `RNS_CONFIG_DIR` | sidecar | RNS directory; must exist and use `share_instance = No` |
| | `IDENTITY_FILE` | sidecar | Created with mode `0600` if missing |
| | `TEXT_ONLY` | both | `0` = audio + PTT; `1` = control only (M1) |
| | `SAMPLE_RATE` | both | Audio wire rate; this change requires `48000` |
| | `ALLOWED_CODECS` | sidecar | `opus,c2`; empty = all |
| | `PTT_MODE` | sidecar | `ptt` (squelch), `vox` or `open` |
| | `HALF_DUPLEX` | both | `1` mutes remote audio while the local PTT is active |

Rooms on the development server (IDs are 1..N in the server's room order):

```
1 Opus High   2 Opus Med   3 Opus Low
4 Codec2 3200 5 Codec2 2400 6 Codec2 1200 7 Codec2 700
8 Music Low   9 Music Med  10 Music High
```

`ROOM=1` is the same as `ROOM=Opus High`. A numeric value is sent as the room
ID; a non-numeric value is matched by name, case-insensitively.

The current test server is:

```
SERVER_HASH=<server destination hash>
server name: Test
room: Opus High (id 1, opus-high, 20 ms)
```

The hash is stable only while the server identity is stable. If the server is
rebuilt or its identity changes, update `SERVER_HASH`. Value redacted here on
purpose; use the one in your local `config/ModuleRnsPartyline.conf`.

With `TEXT_ONLY=0` and `CONNECT_ON_ACTIVATE=1` (the default), the sidecar starts
in `idle`: it does not connect to Partyline or appear in the room until the
module is activated with `3#`. A bare `#` deactivates the module; the last active
client leaves the sidecar disconnected and publishes `state: idle`. To restore
the classic behavior without editing code, either set `CONNECT_ON_ACTIVATE=0`
and restart the sidecar, or start it with:

```bash
/usr/bin/python3 sidecar/rns_partyline_sidecar.py \
  --config config/ModuleRnsPartyline.conf --connect-now
```

`TEXT_ONLY=1` always connects at startup regardless of `CONNECT_ON_ACTIVATE`,
because the module is never activated in text mode. Reconnecting the module
while it is still active sends `active: 1` in its `hello`, so the sidecar
reconnects without another `3#`.

The module ID is currently `3`. Activate it with `3#` from an idle logic, or
`*3#` to force the core command. While it is active, a bare `#` deactivates it
(SvxLink delivers this as an empty DTMF command). In the production
`RepeaterLogic`, ID `3` is `ModuleTclVoiceMail`; do not load both modules with
ID 3 in the same logic. The test/hardware `SimplexLogic` only loads
`ModuleRnsPartyline`.

`TEXT_ONLY=1` keeps the M1 behavior: no audio socket, no activation and no
PTT. The module and sidecar always share the same INI.

### Passwords

They are accepted in plain text in the INI. The sidecar masks them in logs and
in the startup summary, and warns when the file is readable by other users:

```bash
chmod 600 config/ModuleRnsPartyline.conf
```

For production, `SERVER_PASSWORD_FILE`/`ROOM_PASSWORD_FILE` can be added later.

## Test run without radio

`scripts/run-test.sh` starts the sidecar, waits for the socket and runs a
**separate** SvxLink instance (it does not touch systemd,
`/usr/local/etc/svxlink` or the real sound card):

```bash
./scripts/run-test.sh --text    # M1 regression: alsa:null, no audio
./scripts/run-test.sh --audio   # M2: UDP audio on 52010/52011
```

The script also checks the activation gate before launching SvxLink:
`--audio` must start with `state: idle` and no `conectando a ...` line, while
`--text` must connect at startup. For a sidecar already running in audio mode,
`scripts/activation-check.py` drives the control socket and verifies the
`activate`/`deactivate` lifecycle:

```bash
/usr/bin/python3 scripts/activation-check.py --scenario lifecycle
/usr/bin/python3 scripts/activation-check.py --scenario reconnect
```

The script creates `build/test-config-{text,audio}/ModuleRnsPartyline.conf`
from the production INI, so the production file is never overwritten. It also
generates `build/svxlink-test.conf` from `test/svxlink-test*.conf` and creates
the local Tcl tree.

For the audio mode, `Rx1` listens for PCM on `udp:127.0.0.1:52010` and `Tx1`
sends to `udp:127.0.0.1:52011`. The UDP device uses raw 16-bit samples; the
test template uses 2 interleaved channels. Send a tone and capture the return:

```bash
# terminal 1
./scripts/run-test.sh --audio

# terminal 2: open VOX with a tone and capture TX (waits 6 s)
/usr/bin/python3 scripts/audio-tone.py --both --seconds 3 --frequency 1000 \
  --channels 2 --tail-silence 1 --save /tmp/tx.raw
```

`--both` prints RX peak/RMS and the dominant frequency. To validate the
Partyline direction, run an official client in another terminal:

```bash
partyline-client <server destination hash> \
  --configdir ~/.config/partyline-dev/client-rns \
  --identity ~/.config/partyline-dev/client_identity \
  --name Listener --room "Opus High" --listen --null-audio --duration 10
```

The synthetic client can drive the sidecar without SvxLink (M2a):

```bash
/usr/bin/python3 scripts/audio-client.py --tone 1000 --seconds 3 --listen 2
```

The sidecar writes to `build/sidecar-text.log` or `build/sidecar-audio.log`;
the SvxLink instance writes to the terminal. `Ctrl-C` stops both.

To run them separately:

```bash
/usr/bin/python3 sidecar/rns_partyline_sidecar.py \
  --config config/ModuleRnsPartyline.conf

/usr/local/bin/svxlink --config=build/svxlink-test.conf
```

(the second command requires `run-test.sh` to have generated
`build/svxlink-test.conf`, or use a template after replacing
`__PROJECT_ROOT__` and `__CFG_DIR__`).

## Test with a real radio (walkie)

The production `svxlink` uses the USB card `Fred` and `PTT_TYPE=Hidraw`. After
`deploy/install-production.sh`, its `[SimplexLogic]` also loads
`ModuleRnsPartyline`. Before installing production, or when you need an isolated
test, use `test/svxlink-test-radio.conf`: it keeps the real card, the CM108B HID
PTT (`/dev/cm108gpio`, GPIO3) and the module in the same `SimplexLogic`. Adjust
`CARD` and the HID device/pin in that template for different hardware.

The hardware instance needs exclusive access to the card and HID device. Stop
the production service for the duration of the test and restore it afterwards.
Do not edit `/usr/local/etc/svxlink` or the systemd unit.

```bash
# 1. Stop production. This is the only step that affects the live service.
systemctl stop svxlink

# 2. Use the current shared INI (server Test, room Opus High).
mkdir -p build/test-config-radio
cp config/ModuleRnsPartyline.conf build/test-config-radio/

# 3. Generate the hardware test config from the tracked template.
sed -e "s|__PROJECT_ROOT__|$PWD|g" \
    -e "s|__CFG_DIR__|$PWD/build/test-config-radio|g" \
    test/svxlink-test-radio.conf > build/svxlink-test-radio.conf

# 4a. Terminal 1: sidecar with the hardware config.
/usr/bin/python3 sidecar/rns_partyline_sidecar.py \
  --config build/test-config-radio/ModuleRnsPartyline.conf

# 4b. Terminal 2: SvxLink with the module and real hardware.
/usr/local/bin/svxlink --config=build/svxlink-test-radio.conf
```

Expected on startup:

```text
--- Hidraw sound chip is CM108B
Module RnsPartyline: conectado al sidecar /tmp/rns-partyline-svx.sock
Module RnsPartyline: audio conectado a /tmp/rns-partyline-svx.sock.audio
[...] {"ev":"room","id":1,"name":"Opus High", ...}
```

Now press the **walkie PTT**, speak and release. Expected behavior:

```text
Rx1: The squelch is OPEN (100.0...)
Rx1: The squelch is CLOSED (...)
audio: ptt on (prioridad local) [in=0 ...]
audio: ptt off [in=... ...]
```

The sidecar is the authority for Partyline PTT. The walkie PTT itself is RF;
SvxLink detects it through `Rx1` (`SQL_DET=CTCSS`, `CTCSS_FQ=100.0`). If the
squelch never opens, the walkie is not sending the expected CTCSS tone; for a
quick test change `SQL_DET=VOX` in the generated config, but keep CTCSS for a
real installation.

Stop with `Ctrl-C` in both terminals, then restore production:

```bash
systemctl start svxlink
```

If a test process is left behind, stop it by PID (`kill <pid>`). Avoid
`pkill` in this environment; it can hang. If SvxLink crashed while the HID PTT
was on, release GPIO3 explicitly:

```bash
/usr/bin/python3 - <<'PY'
import os
fd = os.open('/dev/cm108gpio', os.O_WRONLY)
os.write(fd, bytes([0, 0, 0, 0x04, 0]))
os.close(fd)
PY
```

### PTT lifecycle and the stuck-PTT fix

There are two independent PTT directions:

- **Local walkie -> Partyline:** squelch opens -> module sends
  `{"cmd":"ptt","on":true}` -> sidecar calls Partyline PTT. Releasing the
  walkie sends `on:false`. The sidecar log must show `ptt on` and `ptt off`.
- **Partyline -> local transmitter:** remote PCM is written to SvxLink's TX
  chain and keys `Tx1`. SvxLink only unkeys when its PTT control receives an
  end-of-audio flush, so the module flushes the RX sink after 200 ms without
  audio (`RX_IDLE_FLUSH_MS`) and when the local squelch opens. The module also
  overrides `allSamplesFlushed` because it is an audio source without a chained
  handler; without the override SvxLink aborts on the base-class assertion.

If `Tx1` stays ON after a remote talker stops, check that the built module
contains the idle flush and that the log shows `Tx1: Turning the transmitter
OFF`. The 200 ms timeout is intentionally short enough to unkey promptly but
long enough to survive normal `Playout` block gaps. Do not remove the flush or
the `allSamplesFlushed` override.

## Production installation (optional)

`deploy/install-production.sh` installs the real integration. It requires root
and makes a timestamped backup of `svxlink.conf` first:

- installs `build/ModuleRnsPartyline.so` in `/usr/local/lib/svxlink`;
- installs `config/ModuleRnsPartyline.conf` in
  `/usr/local/etc/svxlink/svxlink.d/`;
- installs `test/events.d/RnsPartyline.tcl` in
  `/usr/local/share/svxlink/events.d/` (required by SvxLink);
- adds `ModuleRnsPartyline` to `[SimplexLogic] MODULES` (preserving the other
  modules);
- sets `AUTO_ACTIVATE=0` so the module is activated manually with `3#`;
- installs and enables `rns-partyline-sidecar.service` (runs as `areche`);
- restarts the sidecar and `svxlink.service`.

```bash
sudo ./deploy/install-production.sh
systemctl status rns-partyline-sidecar.service svxlink.service
journalctl -u rns-partyline-sidecar.service -n 50 --no-pager
```

Then dial `3#` (or `*3#`) to activate `ModuleRnsPartyline`, and a bare `#` to
deactivate it. With the installed `AUTO_ACTIVATE=0`, the module connects its
control socket at startup but only opens the audio socket after `3#`. The
sidecar service stays alive in `idle` and connects to Partyline only after that
activation; on `#` it leaves the room and returns to `idle`. The module
reconnects with `RECONNECT_DELAY_MS` if either process restarts, and an active
module announces `active: 1` so the sidecar restores the connection without a
new `3#`.

The installed production INI is a copy of the project INI. If you change
`SERVER_HASH`, `ROOM`, credentials or audio settings afterwards, update
`/usr/local/etc/svxlink/svxlink.d/ModuleRnsPartyline.conf` and restart both
services. Do not point the service at the mutable test copy under `build/`.

## Expected output

Sidecar:

```
servidor: <server destination hash>
sala: Opus High
socket control: /tmp/rns-partyline-svx.sock
socket audio: /tmp/rns-partyline-svx.sock.audio
connect on activate: si
text only: no
ptt mode: ptt
half duplex: si
modo audio: en espera de la activación del módulo
# tras 3#:
conectando a <server destination hash> (sala Opus High) ...
conectado a 'Test' (esperando sincronización)
codec preflight: opus-high OK (Opus 16 kbps, 20 ms frames; ...)
codec preflight: c2-3200 OK (Codec2 3200 bps, 200 ms frames; ...)
en sala 'Opus High': perfil opus-high, Opus 16 kbps, 20 ms frames
audio: módulo conectado
audio: 48000 Hz s16le OK
audio: ptt on (prioridad local) [in=... out=...]
audio: ptt off [...]
```

SvxLink:

```
SimplexLogic: Loading module "ModuleRnsPartyline"
    Found build/ModuleRnsPartyline.so
    Module RnsPartyline v1.10.1 starting (socket /tmp/rns-partyline-svx.sock,
      audio /tmp/rns-partyline-svx.sock.audio, mode audio)
Module RnsPartyline: conectado al sidecar /tmp/rns-partyline-svx.sock
Module RnsPartyline: {"ev":"snapshot","state":"idle",...}
# tras 3#:
Module RnsPartyline: {"ev":"snapshot","state":"connecting",...}
Module RnsPartyline: audio conectado a /tmp/rns-partyline-svx.sock.audio
Module RnsPartyline: {"ev":"room","id":1,"name":"Opus High",...}
Tx1: Turning the transmitter ON
Tx1: Turning the transmitter OFF
```

`Tx1` turning ON and OFF is the remote-audio direction. The sidecar's
`ptt on`/`ptt off` lines are the local-walkie direction. With the production
installer's `AUTO_ACTIVATE=0`, `audio conectado` appears only after `3#`.

If you kill the sidecar, the module logs
`sidecar desconectado; reintentando` and reconnects when it starts again. The
sidecar also reconnects to the server.

## Troubleshooting

- **`ModuleModuleRnsPartyline.so: cannot open`**: missing `NAME=RnsPartyline`
  in `[ModuleRnsPartyline]`. SvxLink looks for `Module<NAME>.so`.
- **`Could not source TCL file 'RnsPartyline.tcl'`**: missing
  `test/events.d/RnsPartyline.tcl` or `EVENT_HANDLER` does not point to the
  local tree. `scripts/run-test.sh` creates the links and generates
  `build/svxlink-test.conf`.
- **`Failed to load module ... version`**: rebuild against the installed
  SvxLink (`SVXLINK_SRC`/`SVXLINK_BUILD`).
- **`no path to server`**: the server is not announced or RNS has no route.
  Check `RNS_CONFIG_DIR`, the hash and the network (TTP/AutoInterface).
- **`denied ... wrong password`**: check `SERVER_PASSWORD`/`ROOM_PASSWORD`.
- **Sidecar stays in `idle` with an error**: an activation failed permanently
  (wrong server/room password, nonexistent room, unavailable profile or codec,
  `ALLOWED_CODECS`). The process intentionally stays alive; fix the config and
  send `#` followed by `3#` to retry. Reconnecting an active module also retries.
- **Nothing connects in audio mode**: the sidecar now waits for `3#`
  (`CONNECT_ON_ACTIVATE=1`). Activate the module, or use `CONNECT_ON_ACTIVATE=0`
  or `--connect-now` for the classic startup connection.
- **`ALLOWED_CODECS`**: if the room uses a codec outside the list, the sidecar
  does not join. Empty = accept `opus` and `c2`.
- **No audio**: check `TEXT_ONLY=0`, both socket paths, and that
  `PTT_MODE=ptt` matches the test client. Without a local PTT the sidecar does
  not encode microphone PCM. The production installer sets `AUTO_ACTIVATE=0`;
  dial `3#` to open the audio socket.
- **Audio sounds fast/slow**: `SAMPLE_RATE` must match the module's wire rate
  (48000). The module converts SvxLink's internal `INTERNAL_SAMPLE_RATE` to the
  wire rate; do not set `SAMPLE_RATE` to `CARD_SAMPLE_RATE` if they differ.
- **UDP test has no tone**: use `--channels 2` with the test template and make
  sure `Rx1`/`Tx1` use different ports (52010/52011).
- **`Tx1` stays ON after remote audio**: the module must flush its RX sink
  after `RX_IDLE_FLUSH_MS` (200 ms) and must override `allSamplesFlushed`.
  Rebuild with `./scripts/build.sh`; if SvxLink aborts with
  `Assertion 'm_handler != 0' failed`, the override is missing.
- **Walkie PTT does nothing**: verify `MODULES` includes
  `ModuleRnsPartyline` after the installer, that `3#` activates it, and that
  `rns-partyline-sidecar.service` is active. Use the hardware test section for
  an isolated test.
- **Walkie PTT does not open squelch**: verify the walkie CTCSS is 100.0 Hz
  (`CTCSS_FQ=100.0`) or temporarily set `SQL_DET=VOX` in the test config.
- **HID PTT stuck after a crash**: send the GPIO3-off report with the snippet in
  the hardware test section. Stop test processes by PID; avoid `pkill` here.
- **Production is down**: `systemctl status svxlink`; restore with
  `systemctl start svxlink` after the hardware test.
- **Socket**: it must be absolute; if SvxLink runs as another user, the sidecar
  applies `chmod 0666`. Default: `/tmp/rns-partyline-svx.sock` and
  `/tmp/rns-partyline-svx.sock.audio`.

## Layout

```
CMakeLists.txt                     external module build
src/module/ModuleRnsPartyline.*    C++ plugin: control, audio, PTT, activation
sidecar/rns_partyline_sidecar.py   Partyline client + control/audio sockets
config/ModuleRnsPartyline.conf     single shared configuration
test/svxlink-test.conf             text-only test SvxLink instance
test/svxlink-test-audio.conf       UDP audio test instance
test/svxlink-test-radio.conf       real card + HID PTT test instance
test/events.d/RnsPartyline.tcl     module Tcl handler
scripts/build.sh                   build
scripts/run-test.sh                sidecar + test SvxLink (--text/--audio)
scripts/audio-client.py            synthetic sidecar audio client
scripts/audio-tone.py              UDP tone injector/capturer
scripts/activation-check.py        control-socket activation lifecycle checks
deploy/install-production.sh       root installer for module + sidecar service
deploy/rns-partyline-sidecar.service
                                   systemd sidecar unit
openspec/changes/archive/2026-10-25-add-audio-bridge/
                                   archived M2 change
openspec/specs/                    synced main specs
```

## Remaining work (M3)

- Audio level/CTCSS tuning and long-run testing with the physical transceiver.
  The CM108B HID PTT path is now exercised by `test/svxlink-test-radio.conf`.
- Harden the production service (dedicated sidecar user, secret handling, log
  rotation) and finish long-run tuning.
- Optionally move the control/audio socket I/O to `Async::FdWatch` to remove
  the socket thread.
