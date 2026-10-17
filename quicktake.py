#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyserial>=3.5", "Pillow>=10", "numpy>=1.24"]
# ///
"""
quicktake.py - Talk to an Apple QuickTake 100/150 camera over a serial port.

A CLI port of the serial protocol implemented by JQuickTake
(https://github.com/Crazylegstoo/JQuickTake) by Kevin Godin, which in turn
builds on Colin Leroy-Mira's reverse engineering of the QuickTake 100/150
protocol (https://www.colino.net/).

Verbs:
    info                 Connect and print camera status.
    list                 Connect and list the photos on the camera.
    download             Download every photo (.qtk) and convert to an image.
    shoot                Take a picture (see --count).
    erase                Erase the camera memory.
    diagnose             Probe the raw serial link (wiring / adapter check).
    convert              Decode .qtk files to images (no camera needed).
    reencode             Rewrite .qtk files into the container the original
                         QuickTake Mac software can open (no camera needed).

Flags:
    --port PORT          Serial device, e.g. /dev/cu.usbserial-XXXX  (required)
    --speed {9600,19200,38400,57600}   Session baud rate (default 57600).
    --output DIR         Where to write files (default: ./quicktake_out).
    --prefix STR         Optional filename prefix: PREFIX-IMAGE001.qtk.
    --overwrite          Reuse IMAGE001.. names (default: keep numbering up).
    --format FMT         Converted format: png/jpg/tiff/ppm, or qtk for raw only.
    --preset PRESET      Rendering: photoflash (default; byte-exact Apple
                         codec for QT150) or none (raw colours).
    --wb                 Apply automatic (gray-world) white balance.
    --no-convert         Save raw .qtk only (skip decoding).
    --no-keep-raw        Delete the .qtk after a successful conversion.
    --count N            Number of pictures for 'shoot' (default 1).
    --delete             After a successful download, erase the camera memory.
    --erase              Erase the camera memory (also a standalone verb).
    --yes                Skip the confirmation prompt for erase operations.
    --debug              Print raw hex of every command / response.

Fully self-contained and pure Python: the QuickTake raw codecs (100 gradient /
150 Kodak RADC) are decoded and rendered in-process (lib/codec.py,
lib/render.py), so NO external tools are needed -- no dcraw, ImageMagick, or
libraw. The only requirements are three pip packages (pyserial, Pillow, numpy),
declared inline above so `uv run` installs them automatically; PPM output needs
Pillow-free numpy only.

The QuickTake uses NO RTS/CTS flow control; it is woken by dropping DTR and
handshakes with even parity after a speed negotiation. Only plain RS-232
USB adapters (PL-2303, FTDI) work -- CH340 adapters misbehave.

Vibed by WhyteWolf in 2026 using Claude
"""

import argparse
import os
import re
import sys
import time

import serial


# --------------------------------------------------------------------------- #
# Pretty console output
# --------------------------------------------------------------------------- #

class C:
    """ANSI helpers; disabled automatically when stdout is not a TTY."""
    _on = sys.stdout.isatty()
    RESET = "\033[0m" if _on else ""
    BOLD = "\033[1m" if _on else ""
    DIM = "\033[2m" if _on else ""
    BLUE = "\033[34m" if _on else ""
    GREEN = "\033[32m" if _on else ""
    YELLOW = "\033[33m" if _on else ""
    RED = "\033[31m" if _on else ""
    CYAN = "\033[36m" if _on else ""


def step(msg):
    print(f"{C.BLUE}→{C.RESET} {msg}")


def ok(msg):
    print(f"{C.GREEN}✓{C.RESET} {msg}")


def warn(msg):
    print(f"{C.YELLOW}!{C.RESET} {msg}")


def fail(msg):
    print(f"{C.RED}✗{C.RESET} {msg}")


def kv(key, value):
    print(f"  {C.DIM}{key:<14}{C.RESET}{value}")


def progress_bar(done, total, width=32, label=""):
    frac = 0.0 if total == 0 else done / total
    filled = int(frac * width)
    bar = "█" * filled + "░" * (width - filled)
    pct = int(frac * 100)
    sys.stdout.write(f"\r  {label} {C.CYAN}{bar}{C.RESET} {pct:3d}%")
    sys.stdout.flush()
    if done >= total:
        sys.stdout.write("\n")
        sys.stdout.flush()


# --------------------------------------------------------------------------- #
# QTK -> viewable image conversion
# --------------------------------------------------------------------------- #
# Decoding + colour rendering + file encoding all live in lib/render.py, which
# is fully self-contained (pure Python -- no dcraw, ImageMagick, or libraw).
# QuickTake 150 photos are decoded by the byte-exact codec port (identical to
# Apple PhotoFlash); QuickTake 100 photos and the 'none' preset use the
# dcraw-model decoder plus a conventional demosaic. See lib/render.py.

from lib import render
from lib.camera import Camera, QuickTakeError
from lib.qtk import build_qtk, extract_qtk, _full_ok
from lib.render import convert_qtk, out_path


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def confirm(prompt, assume_yes):
    if assume_yes:
        return True
    try:
        return input(f"{C.YELLOW}{prompt} [y/N] {C.RESET}").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def next_seq(output, prefix):
    """Next image number so existing files in OUTPUT are never overwritten.
    Scans for names like PREFIX-IMAGE07.qtk / IMAGE07.png (any extension) and
    returns one past the highest number found -- 1 if the dir is empty/absent."""
    stem = f"{prefix}-IMAGE" if prefix else "IMAGE"
    pat = re.compile(re.escape(stem) + r"(\d+)")
    highest = 0
    try:
        names = os.listdir(output)
    except FileNotFoundError:
        return 1
    for n in names:
        m = pat.match(n)
        if m:
            highest = max(highest, int(m.group(1)))
    return highest + 1


def do_download(cam, args):
    if cam.taken == 0:
        warn("No pictures on the camera.")
        return []

    os.makedirs(args.output, exist_ok=True)

    convert = args.convert and args.format != "qtk"
    saved = []
    print()
    dest = f" as {C.BOLD}{args.format}{C.RESET}" if convert else ""
    step(f"Downloading {C.BOLD}{cam.taken}{C.RESET} picture(s){dest} to "
         f"{C.BOLD}{args.output}{C.RESET}")

    # By default continue numbering past whatever is already in the output dir
    # so nothing gets clobbered; --overwrite restores the old 1-based naming.
    seq = 1 if args.overwrite else next_seq(args.output, args.prefix)

    cw = len(str(cam.taken))                            # counter width, e.g. 2 for 15
    for i in range(1, cam.taken + 1):
        label = f"IMAGE{seq:03d}"                       # output filename stem
        # progress bar shows the name plus an N-of-total counter, e.g.
        # "IMAGE001 ( 1/15)"; the counter is right-padded to cam.taken's width
        # so the "N/total" field is constant width and every bar starts aligned
        bar_label = f"{label} ({i:>{cw}}/{cam.taken})"
        header = cam.read_image_header(i)
        data = cam.read_image_data(
            i, header,
            on_progress=lambda done, total, l=bar_label: progress_bar(done, total, label=l))

        qtk = build_qtk(header, data, cam.model)
        name = f"{args.prefix}-{label}.qtk" if args.prefix else f"{label}.qtk"
        path = os.path.join(args.output, name)
        seq += 1
        with open(path, "wb") as fh:
            fh.write(qtk)

        entry = {"qtk": path, "image": None}
        if convert:
            try:
                entry["image"] = convert_qtk(path, args.format, args.preset,
                                             args.wb, on_warn=warn)
            except RuntimeError as e:
                warn(f"{label}: conversion failed ({e}); kept raw .qtk")
        if convert and not args.keep_raw and entry["image"]:
            os.remove(path)
            entry["qtk"] = None
        saved.append(entry)

    ok(f"Downloaded {len(saved)} picture(s)")
    for e in saved:
        shown = e["image"] or e["qtk"]
        kv("saved", shown)
    return saved


def do_reencode(args):
    """Rewrite existing .qtk files into the full container the original
    QuickTake software can open. PATH may be a .qtk file or a folder. By default
    a corrected copy is written as <name>-full.qtk; --overwrite replaces the
    original in place."""
    path = args.path or os.getcwd()
    if os.path.isdir(path):
        qtks = sorted(os.path.join(path, f) for f in os.listdir(path)
                      if f.lower().endswith(".qtk")
                      and not f.lower().endswith("-full.qtk"))
    elif os.path.isfile(path):
        qtks = [path]
    else:
        fail(f"not a file or directory: {path}")
        return 2

    if not qtks:
        warn(f"No .qtk files found in {path}")
        return 0

    where = "in place" if args.overwrite else "as <name>-full.qtk"
    step(f"Re-encoding {C.BOLD}{len(qtks)}{C.RESET} file(s) to the full "
         f"QT150 container ({where})")
    n = len(qtks)
    cw = len(str(n))                                   # counter width for alignment
    done = skipped = failed = 0
    for i, src in enumerate(qtks, 1):
        name = os.path.basename(src)
        tag = f"[{i:>{cw}}/{n}]"                        # e.g. [ 3/15]
        try:
            b = open(src, "rb").read()
            if b[0:3] != b"qkt" or len(b) < 74:
                print(f"  {C.DIM}{tag} skip  {name} (not a QuickTake file){C.RESET}")
                skipped += 1
                continue
            image, header, model, is_full = extract_qtk(b)
            if is_full and not args.force:
                print(f"  {C.DIM}{tag} skip  {name} (already full container){C.RESET}")
                skipped += 1
                continue
            if not _full_ok(header, image, model):
                print(f"  {C.DIM}{tag} skip  {name} (needs a High-quality 640x480 "
                      f"QuickTake 150 frame){C.RESET}")
                skipped += 1
                continue
            out = build_qtk(header, image, model)
            base = src[:-4] if src.lower().endswith(".qtk") else src
            dest = src if args.overwrite else f"{base}-full.qtk"
            with open(dest, "wb") as fh:
                fh.write(out)
            ok(f"{tag} {name} -> {os.path.basename(dest)}")
            done += 1
        except OSError as e:
            fail(f"{tag} {name}: {e}")
            failed += 1
    print()
    ok(f"{done} corrected, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


def do_convert(args):
    """Batch-convert existing .qtk files to images -- no camera needed.
    PATH may be a .qtk file or a folder."""
    fmt = "png" if args.format == "qtk" else args.format

    path = args.path or os.getcwd()
    if os.path.isdir(path):
        qtks = sorted(os.path.join(path, f) for f in os.listdir(path)
                      if f.lower().endswith(".qtk"))
    elif os.path.isfile(path):
        qtks = [path]
    else:
        fail(f"not a file or directory: {path}")
        return 2

    if not qtks:
        warn(f"No .qtk files found in {path}")
        return 0

    step(f"Converting {C.BOLD}{len(qtks)}{C.RESET} file(s) to "
         f"{C.BOLD}{fmt}{C.RESET} (preset: {args.preset})")
    n = len(qtks)
    cw = len(str(n))                                   # counter width for alignment
    done = skipped = failed = 0
    for i, src in enumerate(qtks, 1):
        tag = f"[{i:>{cw}}/{n}]"                        # e.g. [ 3/15]
        out = out_path(src, fmt, args.preset, args.wb)
        if os.path.exists(out) and not args.force:
            print(f"  {C.DIM}{tag} skip  {os.path.basename(src)} "
                  f"({os.path.basename(out)} exists){C.RESET}")
            skipped += 1
            continue
        try:
            convert_qtk(src, fmt, args.preset, args.wb, on_warn=warn)
            ok(f"{tag} {os.path.basename(src)} -> {os.path.basename(out)}")
            done += 1
        except RuntimeError as e:
            fail(f"{tag} {os.path.basename(src)}: {e}")
            failed += 1
    print()
    ok(f"{done} converted, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


def do_diagnose(port):
    """Probe the physical link: can we toggle DTR/RTS, does the camera drive
    any line or send its wake burst? Helps distinguish a wiring fault from an
    adapter or camera fault without touching the higher-level protocol."""
    step(f"Opening {C.BOLD}{port}{C.RESET} @ 9600 8N1 for line diagnostics ...")
    s = serial.Serial(port, 9600, bytesize=serial.EIGHTBITS,
                       parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                       timeout=0.3, rtscts=False, dsrdtr=False)
    try:

        def lines():
            try:
                return f"CTS={int(s.cts)} DSR={int(s.dsr)} CD={int(s.cd)} RI={int(s.ri)}"
            except Exception as e:
                return f"(line read error: {e})"

        def can_set(attr, val):
            try:
                setattr(s, attr, val); return True
            except Exception as e:
                fail(f"{attr} control not supported: {e}"); return False

        dtr_ok = can_set("dtr", True)
        rts_ok = can_set("rts", True)
        kv("DTR control", "ok" if dtr_ok else "FAILED")
        kv("RTS control", "ok" if rts_ok else "FAILED")
        kv("lines @ start", lines())

        saw_bytes = False
        saw_line = False
        for label, dtr in [("baseline DTR high", True),
                           ("WAKE: drop DTR", False),
                           ("raise DTR again", True),
                           ("drop DTR again", False)]:
            can_set("dtr", dtr)
            s.reset_input_buffer()
            t0 = time.time(); got = b""
            while time.time() - t0 < 2.5:
                c = s.read(64)
                if c:
                    got += c
            ln = lines()
            if any(int(x) for x in (s.cts, s.dsr, s.cd, s.ri)):
                saw_line = True
            if got:
                saw_bytes = True
                hexs = " ".join(f"{b:02X}" for b in got[:16])
                ok(f"{label}: {len(got)} bytes  [{hexs}{' ...' if len(got) > 16 else ''}]  {ln}")
            else:
                print(f"  {C.DIM}{label}: no bytes  {ln}{C.RESET}")
    finally:
        s.close()

    print()
    if saw_bytes:
        ok("Camera is talking — the link works. Run:  info / list / download")
    elif saw_line:
        warn("Camera drives a handshake line but sent no data. Likely TX/RX "
             "swapped or a parity/level issue — try swapping DIN8 pin2<->pin3.")
    else:
        fail("No data and no handshake activity on any input line.")
        print("  Most likely a wiring fault. Verify continuity (multimeter) for:")
        print("    DIN8 4 -> DB9 5 (GND)   DIN8 3 -> DB9 2 (camera TX)")
        print("    DIN8 2 -> DB9 3 (RX)    DIN8 5 -> DB9 4 (DTR / wake)")
        print("  If GND/TX are missing or pins are swapped you get exactly this.")
    return 0 if saw_bytes else 3


def do_erase(cam, args):
    step("Erasing all pictures from the camera ...")
    cam.delete_all()
    ok(f"Camera memory cleared ({cam.taken} pictures remain, "
       f"{cam.remaining} shots free)")


PRESET_HELP = """\
rendering presets (--preset):
  photoflash  (default) the original codec's own rendering. For QuickTake 150
              files this is a byte-exact port of Apple's codec -- output is
              pixel-identical to Apple PhotoFlash. (QuickTake 100 files use a
              fitted approximation of the same look.)
  none        raw sensor colours (green cast), auto-brightened.

Add --wb to either preset for automatic gray-world white balance
(the camera had no white balance; indoor shots often need it).
"""


def main():
    ap = argparse.ArgumentParser(
        description="Extract photos from an Apple QuickTake 100/150 over serial.",
        epilog=PRESET_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("verb",
                    choices=["info", "list", "download", "shoot", "erase",
                             "diagnose", "convert", "reencode"],
                    help="info/list/download/shoot (take a picture)/erase/"
                         "diagnose (probe the link)/convert (qtk->image, no "
                         "camera)/reencode (rewrite .qtk into the full "
                         "originally-openable container, no camera)")
    ap.add_argument("path", nargs="?", default=None,
                    help="for 'convert'/'reencode': a .qtk file or folder "
                         "(default: cwd)")
    ap.add_argument("--port", default=None,
                    help="serial device, e.g. /dev/cu.usbserial-XXXX "
                         "(required except for 'convert')")
    ap.add_argument("--count", type=int, default=1,
                    help="number of pictures to take with 'shoot' (default 1)")
    ap.add_argument("--speed", type=int, default=57600,
                    choices=[9600, 19200, 38400, 57600],
                    help="session baud rate (default 57600)")
    ap.add_argument("--output", default="quicktake_out",
                    help="output directory for downloaded files")
    ap.add_argument("--prefix", default="",
                    help="optional filename prefix")
    ap.add_argument("--format", default="png",
                    choices=["png", "jpg", "jpeg", "tiff", "ppm", "qtk"],
                    help="converted image format (default png; 'qtk' = raw only)")
    ap.add_argument("--convert", action=argparse.BooleanOptionalAction, default=True,
                    help="decode each .qtk to --format (default on)")
    ap.add_argument("--keep-raw", action=argparse.BooleanOptionalAction, default=True,
                    help="keep the raw .qtk alongside the converted image (default on)")
    ap.add_argument("--preset", dest="preset", default="photoflash",
                    choices=["none", "photoflash"],
                    metavar="{photoflash,none}",
                    help="rendering preset (default photoflash); "
                         "see the presets section below")
    ap.add_argument("--wb", action="store_true",
                    help="apply automatic (gray-world) white balance to the "
                         "converted image")
    ap.add_argument("--overwrite", action="store_true",
                    help="reuse IMAGE001.. names, overwriting existing output "
                         "(default: continue numbering past existing files)")
    ap.add_argument("--force", action="store_true",
                    help="for 'convert': re-encode even if the output exists")
    ap.add_argument("--delete", action="store_true",
                    help="erase the camera after a successful download")
    ap.add_argument("--erase", action="store_true",
                    help="erase the camera memory (works with any verb)")
    ap.add_argument("--yes", action="store_true",
                    help="skip erase confirmation prompts")
    ap.add_argument("--debug", action="store_true",
                    help="print raw command/response hex")
    args = ap.parse_args()

    print(f"{C.BOLD}QuickTake CLI{C.RESET}  {C.DIM}(JQuickTake protocol port){C.RESET}\n")

    # convert and reencode need no camera at all.
    if args.verb == "convert":
        return do_convert(args)
    if args.verb == "reencode":
        return do_reencode(args)

    if not args.port:
        fail("--port is required for this command "
             "(find it with: ls /dev/cu.*)")
        return 2

    # diagnose works at the raw serial level and never wakes/opens a session.
    if args.verb == "diagnose":
        try:
            return do_diagnose(args.port)
        except serial.SerialException as e:
            fail(f"Serial error: {e}")
            return 2

    cam = Camera(args.port, speed=args.speed, debug=args.debug,
                 on_step=step, on_ok=ok, on_warn=warn, on_kv=kv,
                 on_log=lambda m: print(f"{C.DIM}{m}{C.RESET}"))
    downloaded = []
    try:
        cam.open()
        print()
        cam.print_status()

        if args.verb in ("info", "list"):
            if args.verb == "list" and cam.taken:
                print()
                step(f"Reading headers for {cam.taken} picture(s) ...")
                for i in range(1, cam.taken + 1):
                    h = cam.read_image_header(i)
                    size = int.from_bytes(b"\x00" + h[5:8], "big")
                    w = int.from_bytes(h[8:10], "big")
                    ht = int.from_bytes(h[10:12], "big")
                    date = f"{h[13]:02d}/{h[14]:02d}/{h[15]:02d}"
                    tm = f"{h[16]:02d}:{h[17]:02d}:{h[18]:02d}"
                    q = {32: "Std", 16: "High"}.get(h[24], "?")
                    kv(f"#{i:02d}", f"{w}x{ht}  {size:>7} bytes  {q:>4}  {date} {tm}")

        elif args.verb == "download":
            downloaded = do_download(cam, args)

        elif args.verb == "shoot":
            n = max(1, args.count)
            print()
            for k in range(1, n + 1):
                step(f"Taking picture {k}/{n} ...")
                cam.take_picture()
            ok(f"Done shooting — camera now holds {cam.taken} picture(s), "
               f"{cam.remaining} shot(s) free")

        # Erasing: the 'erase' verb, the --erase flag, or --delete after download.
        want_erase = (args.verb == "erase" or args.erase
                      or (args.delete and args.verb == "download"))
        if want_erase:
            if args.delete and args.verb == "download" and not downloaded:
                warn("Nothing was downloaded; skipping erase.")
            elif confirm("Erase ALL pictures on the camera? This cannot be undone.",
                         args.yes):
                print()
                do_erase(cam, args)
            else:
                warn("Erase cancelled; camera left untouched.")

        print()
        ok("Done.")
    except QuickTakeError as e:
        print()
        fail(str(e))
        return 2
    except serial.SerialException as e:
        print()
        fail(f"Serial error: {e}")
        return 2
    except KeyboardInterrupt:
        # Ctrl-C during a camera session: drop out cleanly (the finally below
        # ends the session and releases the port); anything already written to
        # --output stays on disk.
        print()
        warn("Interrupted -- any files already downloaded are kept.")
        return 130
    finally:
        cam.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Ctrl-C outside a camera session (convert / reencode / diagnose,
        # argument parsing, or a prompt) -- exit quietly instead of dumping a
        # traceback. 130 = the conventional "terminated by SIGINT" status.
        print()
        warn("Interrupted.")
        sys.exit(130)
