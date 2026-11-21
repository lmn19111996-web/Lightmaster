# Implementation plan

Priorities: reliability > looks > features. Every phase ends with something visible on the strip.
No hard deadline. New ideas go into `IDEAS.md`, not into the code.

## Hardware (verified 2026-11-21)
- WLED 0.15.4, ESP32, 192.168.68.50, WS281x on GPIO 16, color order RGB
- 56 addressable pixels (12 V blocks), straight line, 10 A PSU (worst case ~3.4 A at full white)
- WLED: realtime timeout 2.5 s, gamma off for realtime, ABL 8 A
- **Output uses WLED UDP realtime (DRGB, port 21324), not DDP.** Measured 2026-11-21: DDP queues
  inside WLED 0.15.4 (pings ~300 ms with heavy loss even at 30 fps, about 1 s visible lag), while
  UDP realtime at 60 fps keeps pings at ~8 ms. DDP stays available via `wled.protocol: ddp`.
- The PC must be on the home network (TP-Link Deco), not the phone hotspot: that path added ~2 s.
- ESP Wi-Fi RSSI about -70 dBm on 2.4 GHz: OK, keep an eye on it during the soak test
- Bluetooth playback happens, so the lights get a per-device delay

## Architecture
```
loopback audio ─> analyzer ─> delay line (latency) ─┐
                                                     ├─> color layer ─┐
web UI ─┐                                            ├─> white layer ─┼─> compositor ─> DDP ─> WLED
hotkeys ┼─> commands ─> queue ─> Controller (State) ─┘                │
HA (later)┘                         │                                 └─> terminal preview (optional)
           <── state + live telemetry over WebSocket ──┘
```
**Control core rules**
- One `State`, written only by the render loop. Every input sends the same validated commands
  (`set`, `nudge`, `toggle`, `cycle`), from any thread, via a queue.
- Other threads read immutable snapshots plus a version number. No locks shared with the render loop.
- Each parameter carries a schema (kind, range, step, choices, group). Patterns describe their own
  parameters, and the UI builds its controls from the schema, so a new pattern needs no UI code.
- The UI runs in its own thread. If it crashes, the lights keep running and the hotkeys still work.

## Phase 1: Output path ✅ mostly done
- [x] `lightsync/output_ddp.py`: non-blocking DDP sender
- [x] `tools/test_wled.py`: info, ends, chase, order, ramp, burst, fps, failsafe
- [ ] Run the visual steps (ends, chase, order, ramp, burst, failsafe) and confirm by eye
- [ ] WLED: turn on "Force max brightness" (Sync Interfaces → Realtime)

## Phase 2: Control core, audio through to pixels
- `control.py`: `Param` schema, `Controller` (command queue, state, snapshots, schema export). Unit tests.
- `compositor.py`: `out = C·(1 − duck·w) + w·W_tint` → overflow (clip | normalize) → `L^(1/2.2)`
  → temporal dithering → uint8. Unit tests.
- `audio.py`: pyaudiowpatch default WASAPI loopback → mono ring buffer. Reopens on stream errors and
  after a few seconds of silence, which also picks up a changed default device (e.g. Bluetooth).
- `analyzer.py` (temporary): 40–150 Hz level, auto-gain, attack/release envelope. `DelayLine` for latency.
- `main.py`: fixed-timestep loop, per-frame error isolation, stats log, `--preview` terminal sink.
- **Done when:** the strip pulses with the bass of Spotify/YouTube, on speakers and on Bluetooth.

## Phase 3: Web UI v1
- `web.py`: aiohttp in its own thread with its own event loop. Serves one static `web/index.html`
  (no build step), `GET /api/schema`, `GET /api/state`, WebSocket `/ws` for commands and pushes
  (state on change, telemetry at ~15 Hz: energy state, bass level, beats, fps).
- Binds to `127.0.0.1` by default and opens the browser on start. Setting `web.lan: true` allows
  phones on the network, protected by `web.pin`.
- v1 controls: mode, color swatches + picker, intensity, color level, blackout, latency (shows the
  active audio device), live meters.
- `state.json`: save the state on change (debounced) and restore on start, so a restart keeps the look.
- **Done when:** everything in v1 can be controlled from the browser, and killing the web thread does
  not affect the lights.

## Phase 4: Musical behavior
- `analyzer.py`: Hann + rfft, log bands, auto-gain (slow 90th-percentile reference), bass envelope,
  spectral-flux onsets with an adaptive threshold + 250 ms refractory period, "strong hit" flag,
  energy EMA (~8 s) → `chill | medium | peak` with hysteresis and an ~8 s minimum dwell.
- `layers/white.py`: masks (spots from `width_px`/`gap_px`, odd/even, full, chase), envelopes
  (flash, breathe, hold), triggers (beat, strong hit, state change), capped at `spot.max`.
- `layers/color.py`: base color × state behavior (breathe / bass swells / hue drift), capped.
- All new settings are exposed as `Param`s, so they show up in the UI automatically.
- **Done when:** spots hit on beats, the states clearly differ between a quiet song and a club track.

## Phase 5: Hotkeys, presets, hardening
- Hotkeys (pynput) as a thin client of the same commands: color next/prev, intensity ±, blackout,
  strobe (hold only), mode toggle, latency ±, quit (sends black ×3).
- Presets: save/load named State snapshots from the UI.
- Logging: rotating file + a stats line every 10 s. `run.bat` restarts the script if it exits.
- Fault tests: switch/unplug audio devices, reboot WLED, kill the script (WLED falls back after 2.5 s).
- Soak test: 4+ h with a mixed playlist. Target CPU < 10 %, flat memory, no stalls in the log.
- Backup: a WLED sound-reactive preset, one click away.

## Phase 6: DJ table
- Pattern registry: each pattern is a class with a `params` list and a `render()`; the UI lists them.
- Style/pattern picker, color picker, preset grid, built on the schema from the control core.
