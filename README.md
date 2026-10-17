# quicktake-extractor

> This project is completely vibecoded. I take no credit and no responsibility, but I must admit it came out hella useful. Doing this reverse engineering task from 68k code would have been impossible for me and possibly anyone else without deep knowledge in the field.
> No need to spin up an emulator to convert pictures, no Java for downloading them. This is a super clean and self contained program designed to get any QuickTake 150 back to life, and with the original quality. No compromises. I tested this minimally, feel free to open issues.

A Python CLI for the **Apple QuickTake 100 / 150** camera on a modern machine:
read status, list photos, **download and convert them to PNG/JPEG**, take
pictures, and erase the camera — over a plain USB-to-serial adapter.

Conversion is fully self-contained (no dcraw, no ImageMagick), and for
QuickTake 150 photos it is **pixel-perfect**: the original Apple codec was
reverse-engineered from its 68k disassembly and ported instruction-faithfully,
so the default output is **byte-identical to Apple PhotoFlash**
(validated on every reference frame — see `codec_re/`).

Demo:
```
QuickTake CLI  (JQuickTake protocol port)

→ Opening /dev/cu.usbserial-110 at 9600 8N1 ...
→ Waking camera (dropping DTR) ...
✓ Camera awake (QuickTake 150) after 0.1s
→ Negotiating 57600 baud ...
✓ Session open

  Model:        QuickTake 150
  Name:         WhyteWolf's Camera
  Battery:      85%
  Pictures:     11 taken, 5 remaining
  Quality:      High
  Flash:        Auto
  Clock:        08/25/26 19:26:48

→ Downloading 11 picture(s) as png to /Users/whytewolf/Desktop/Photos
  IMAGE051 ( 1/11) ████████████████████████████████ 100%
  IMAGE052 ( 2/11) ████████████████████████████████ 100%
  IMAGE053 ( 3/11) ████████████████████████████████ 100%
  IMAGE054 ( 4/11) ████████████████████████████████ 100%
  IMAGE055 ( 5/11) ████████████████████████████████ 100%
  IMAGE056 ( 6/11) ████████████████████████████████ 100%
```

## Why this decoder is different (and so far, the only modern correct decoder in the world for QTK)

Every other QuickTake tool (including dcraw and everything derived from it)
decodes the raw stream with **dcraw's reverse-engineered approximation**: it
treats the data as a Bayer mosaic, uses slightly wrong arithmetic (its integer
division rounds toward zero where the codec's shifts round toward -inf), and
substitutes a generic demosaic and colour pipeline for the codec's own. The
results are close, but visibly softer and colour-shifted, and never match the
original software.

This project instead ported **Apple's own codec** instruction-by-instruction
from the disassembly of the QuickTime "QuickTake Image" decompressor, and
validated every stage against an m68k-interpreter oracle: the true pipeline is
planar luma + chroma with an edge-directed, gamma-space reconstruction and the
codec's own colour tables. The output is **byte-identical to what Apple
PhotoFlash produced** — not similar, identical.

It also writes downloads as **full QuickTake 150 containers that the original
Mac software opens** (header pointer fields, PICT preview block, thumbnail),
where other tools write a stub header only their own decoder understands —
and `reencode` upgrades such stub files in place.

The serial protocol is a from-scratch port of
[JQuickTake](https://github.com/Crazylegstoo/JQuickTake) (Kevin Godin), which
builds on [Colin Leroy-Mira's reverse engineering](https://www.colino.net/).
Verified end-to-end against a real QuickTake 150.

## Layout

| Path | What |
|---|---|
| `quicktake.py` | the CLI (all verbs) |
| `lib/codec.py` | the decoders: byte-exact QuickTake 150 codec port + dcraw-model fallback |
| `lib/render.py` | decode → colour render (presets, white balance) → image file (PNG/JPEG/TIFF/PPM + EXIF) |
| `lib/camera.py` | the serial wire protocol |
| `lib/qtk.py` | the `.qtk` container format (build / parse / metadata) |
| `codec_re/` | the codec reverse-engineering: disassembly, m68k interpreter, oracle harnesses, notes (`DECODE_PIPELINE.md`, `PLAN.md`), reference frames |
| `caps/` | hardware docs (recapping guide, protocol notes, part lists) |

## Requirements

**No external tools.** Decoding, colour rendering and image encoding are all
pure Python and happen in-process — there is **no dependency on dcraw,
ImageMagick, libraw, or any other binary**. The only requirements are three
pip packages, and `uv` installs them for you:

- [`uv`](https://docs.astral.sh/uv/) — runs the script and installs its
  dependencies (`pyserial`, `Pillow`, `numpy`) automatically on first run in an
  isolated environment. No venv, no `pip install`. Python 3.9+.

```bash
brew install uv     # or see https://docs.astral.sh/uv/ for other methods
```

The script declares its dependencies inline ([PEP 723](https://peps.python.org/pep-0723/));
the first `uv run` downloads and caches them, later runs start instantly.

## Hardware

- A **plain RS-232 USB-serial adapter**: PL-2303 or FTDI work
- A cable wired to the QuickTake schematic. The camera is woken by **dropping
  DTR** and uses **even parity** after a speed handshake — there is **no
  RTS/CTS flow control**. A generic Mac serial cable will *not* work; verify
  the wiring with a multimeter:

  | Mini-DIN8 (camera) | DB9 (adapter) | Signal |
  |---|---|---|
  | 1 | 8 | CTS |
  | 2 | 3 | RX  |
  | 3 | 2 | TX  |
  | 4 | 5 | GND |
  | 5 | 4 | DTR (wake) |
  | 6 | 6 | DSR |
  | 7 | 1 | DCD |
  | 8 | 7 | RTS |

I believe this works with flow control because they had to make it compatible with shitty PCs.

Find your adapter's device path with `ls /dev/cu.*` (macOS) — e.g.
`/dev/cu.usbserial-8310`.

## Usage

```bash
PORT=$(ls /dev/cu.usbserial-* | head -n 1)

# Camera status (safe, read-only) — good first connection test
uv run quicktake.py info --port $PORT

# List photos with size/dimensions/timestamp
uv run quicktake.py list --port $PORT

# Download everything to ~/Desktop/Photos as PNG
# (default rendering = the original codec, identical to Apple PhotoFlash)
uv run quicktake.py download --port $PORT --output ~/Desktop/Photos

# Same, with automatic white balance (the camera had none; indoor shots
# often come out warm/green)
uv run quicktake.py download --port $PORT --wb

# Download as JPEG, then erase the camera (asks to confirm; --yes to skip)
uv run quicktake.py download --port $PORT --format jpg --delete

# Take 3 pictures
uv run quicktake.py shoot --port $PORT --count 3

# Probe the serial link when the camera won't connect (wiring/adapter check)
uv run quicktake.py diagnose --port $PORT

# Re-convert existing .qtk files later (no camera needed).
# Takes a file or a folder (default: current dir).
uv run quicktake.py convert ~/Desktop/photos --format tiff --wb --force

# Rewrite .qtk files into the full container the ORIGINAL Mac software
# (PhotoFlash / the QuickTake app) can open
uv run quicktake.py reencode ~/Desktop/photos
```

Because the script starts with `#!/usr/bin/env -S uv run --script`, you can
also run it directly: `./quicktake.py info --port $PORT`.

### Rendering

- `--preset photoflash` *(default)* — the original codec's own rendering.
  For QuickTake 150 photos this is the byte-exact port: output is
  pixel-identical to Apple PhotoFlash. QuickTake 100 photos (a different,
  unported codec) use a fitted approximation of the same look.
- `--preset none` — raw sensor colours (green cast), auto-brightened.
- `--wb` — optional automatic white balance (gray-world, applied in linear
  light) on top of either preset. Output files are suffixed accordingly:
  `IMAGE001-photoflash-wb.png`.

### Useful flags

- `--speed {9600,19200,38400,57600}` — session baud rate (default **57600**;
  drop to 9600 if a link is flaky).
- `--format {png,jpg,tiff,ppm,qtk}` — output format; `qtk` (or `--no-convert`)
  keeps only the raw camera file.
- `--keep-raw / --no-keep-raw` — keep or discard the `.qtk` after conversion
  (kept by default; the `.qtk` is the lossless original).
- `--prefix trip` — name files `trip-IMAGE001.qtk`.
- `--force` — (with `convert`) re-encode even if the output already exists.
- `--debug` — print raw command/response hex.

## Notes & limitations

- **Half-resolution (Standard-quality, 320×240) images are not currently
  handled** by the byte-exact QuickTake 150 decoder — Standard quality uses a
  different codec mode that has not been ported. High-quality 640×480 frames
  (the camera default) are fully supported.
- `.qtk` files are the lossless raw capture. Keeping them (`--keep-raw`, the
  default) means you can always re-convert with different settings.
- Downloaded `.qtk` files use the full QuickTake 150 container, so the
  original Apple software can open them too; `reencode` upgrades older
  minimal-header files in place.
- Converted images carry EXIF (camera model, capture date/time, lens data).
- The camera cannot delete individual photos; `erase` / `--delete` clears all.
- QuickTake **200** uses a different protocol and is **not** supported.
- For the curious, demo photos were taken at EF

Vibed by WhyteWolf in 2026