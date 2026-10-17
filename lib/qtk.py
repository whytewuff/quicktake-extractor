"""The .qtk container format: building, parsing, and capture metadata.

The camera's serial protocol delivers only a ~60-byte image header plus the
compressed pixel stream; this module wraps that into a .qtk file (and back).
Two container flavours exist:

  minimal  -- stub 736-byte header + raw stream. Our decoder and dcraw open
              it; the original Apple software does not.
  full     -- the complete 738-byte QuickTake 150 container (inner 'qktn'
              records, PICT placeholder, thumbnail block) that the original
              Mac software (PhotoFlash / QuickTake app) opens. Produced for
              High-quality 640x480 QT150 frames.

Public API: build_qtk(), extract_qtk(), qtk_meta().
Ported from JQuickTake's Image.java plus our own diffing of real QT150 files.
"""

import base64


_QUALITY = {32: "Standard", 16: "High"}

# Where the capture date (mo, da, yr, hh, mi, ss) sits in each container. The
# minimal stub copies the camera header to [14:74], so image-header byte i is at
# qtk[i+10] and the date (header[13:19]) lands at 23. The full QT150 container
# lays the same six fields out one byte earlier -- see qtk_meta.
_DATE_OFF = {False: 23, True: 22}



# --------------------------------------------------------------------------- #
# QuickTake 150 container (openable by the original Mac software)
# --------------------------------------------------------------------------- #
#
# The camera's serial protocol only hands over a ~60-byte image header plus the
# compressed pixel stream. JQuickTake (and _build_minimal below) wrap that
# in a stub 736-byte header that dcraw accepts but the original Apple software
# (PhotoFlash / the QuickTake app) rejects -- a limitation the format's own
# reverse-engineer notes in his code. Diffing real QuickTake 150 files
# reveals the full layout the original software wants:
#
#   [0 .. 737]  738-byte header (type=30 @552, inner 'qktn' markers @644/@726)
#   [738 .. )   compressed image, 0xFF-padded so its length S % 256 == 0xA0
#   [ ... ]     132-byte QuickTime/PICT placeholder block  (static)
#   [ ... EOF]  2412-byte thumbnail block: 12-byte header + 80x60 4bpp (2400 B)
#
# Six big-endian pointer fields in the header are all derived from S = the
# padded compressed length: S+870 (=thumbnail offset) at [9:12], then S at
# [16], S+358 at [512], S+166 at [570], S+12 at [686], and S at [736]. Getting
# any of them wrong makes the decoder seek to a bad offset and crash.
#
# The template header and the static PICT block are captured verbatim from a
# real QuickTake 150 file (Colin Leroy-Mira's public QT150 test corpus); the
# offset fields are zeroed here and recomputed per image. Only High-quality
# 640x480 QT150 frames are covered -- Standard quality and other sizes have no
# sample to model, so they fall back to the minimal header.

_QT150_HDR_TEMPLATE = base64.b64decode(
    "cWt0bgAAAAgAAAAAABAAAAAAAoAB4AobGBIfNwAAABsAHBAEAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA8AFAABEC/wwA//4AAACQAAAAkAAA"
    "AAAAAAHgAoAAAAAAAB4AAQAKgAGAAX//f/+CAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAQAA"
    "AAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAABAAAAAAAHgAoAAAAMAAAAAAAAAAFZxa3Ru"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIAAoAB4ABIAAAASAAAAAAAAAAAClF1aWNrVGFrZaoA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAGP//cWt0bgACAAgAAAAA"
)
# 132-byte QuickTime/PICT placeholder block ("QuickTime and a QuickTake
# decompressor are needed to see this picture"); byte-identical in every
# QuickTake 150 file, so it carries no per-file offsets and is embedded verbatim.
_QT150_PICT_BLOCK = base64.b64decode(
    "AAcArgB8AAcAAQABAAMAAwAEAAAADQAMABACgAKAAoACgAAoAOQBCBBRdWlja1RpbWWqIGFu"
    "ZCBhAAAoAPAA7hdRdWlja1Rha2WqIGRlY29tcHJlc3NvcgAoAPwA4SENIGFyZSBuZWVkZWQg"
    "dG8gc2VlIHRoaXMgcGljdHVyZQ0AAAD/"
)
# Thumbnail block = 12-byte header + 2400 bytes of uncompressed 80x60 4bpp
# pixels. We can't yet re-encode a real thumbnail, so we ship a mid-gray
# placeholder (0x88 = level 8/15 in both nibbles); it is structurally valid, so
# the file opens -- browsers just show a flat gray preview.
_QT150_THUMB_HDR = bytes.fromhex("716b746e0002010000000960")
_QT150_THUMB_FILL = 0x88


def _build_full(image_data, header):
    """Wrap a padded QT150 compressed stream in the full container.

    Every byte outside the six size pointers comes from the template, which was
    captured from one real QT150 file -- so the per-image capture date has to be
    stamped in explicitly, or every file inherits the template's date."""
    pad = (0xA0 - (len(image_data) & 0xFF)) & 0xFF   # pad to length % 256 == 0xA0
    image_data = image_data + b"\xFF" * pad
    s = len(image_data)

    hdr = bytearray(_QT150_HDR_TEMPLATE)
    hdr[9:12]   = (s + 870).to_bytes(3, "big")   # thumbnail offset
    hdr[16:18]  = s.to_bytes(2, "big")
    hdr[512:514] = (s + 358).to_bytes(2, "big")
    hdr[570:572] = (s + 166).to_bytes(2, "big")
    hdr[686:688] = (s + 12).to_bytes(2, "big")
    hdr[736:738] = s.to_bytes(2, "big")          # inner 'qktn' size
    d = _DATE_OFF[True]
    hdr[d:d + 6] = header[13:19]                 # capture mo, da, yr, hh, mi, ss

    thumb = _QT150_THUMB_HDR + bytes([_QT150_THUMB_FILL]) * 2400
    return bytes(hdr) + image_data + _QT150_PICT_BLOCK + thumb


def _full_ok(header, image_data, model):
    """True when we can produce the full (originally-openable) QT150 container:
    a High-quality 640x480 QuickTake 150 frame whose size fits the 16-bit
    pointer fields."""
    if model == "QuickTake 100":
        return False                                    # no QT100 sample to model
    if header[24] != 16:                                # 0x10 High / 0x20 Standard
        return False
    width = int.from_bytes(header[8:10], "big")
    height = int.from_bytes(header[10:12], "big")
    if (width, height) != (640, 480):
        return False
    padded = len(image_data) + ((0xA0 - (len(image_data) & 0xFF)) & 0xFF)
    return padded + 870 <= 0xFFFF                       # pointers stay in range


def build_qtk(header, image_data, model):
    """Return the full .qtk file bytes for a downloaded image.

    High-quality 640x480 QuickTake 150 frames get the full container the
    original Apple software can open; everything else falls back to the minimal
    header our own decoder reads.
    """
    if _full_ok(header, image_data, model):
        return _build_full(image_data, header)
    return _build_minimal(header, image_data, model)


def _build_minimal(header, image_data, model):
    """Stub 736-byte header + raw image data. Our decoder (and dcraw) opens it;
    the original QuickTake software does not (see _build_full)."""
    fh = bytearray(736)

    # base magic block
    fh[0:14] = bytes([0x71, 0x6B, 0x74, 0x6E, 0x00, 0x00, 0x00, 0x08,
                      0x00, 0x00, 0x00, 0x00, 0x00, 0x01])

    # Quality preamble byte: 0x08 for High, 0x04 for Standard. The flag lives
    # in header[24] (0x10 = High, 0x20 = Standard); camera files pair 0x08
    # with High and 0x04 with Standard for *both* models -- previously the 150
    # branch hardcoded 0x04, contradicting the High-quality flag it also wrote.
    fh[7] = 0x08 if header[24] == 16 else 0x04
    if model == "QuickTake 100":
        fh[0:4] = bytes([0x71, 0x6B, 0x74, 0x6B])       # 'qktk'
    else:  # QuickTake 150 (and treat unknown as 150)
        fh[0:4] = bytes([0x71, 0x6B, 0x74, 0x6E])       # 'qktn'

    fh[9], fh[10], fh[11] = header[5], header[6], header[7]   # image size
    fh[544], fh[545] = header[10], header[11]                 # height
    fh[546], fh[547] = header[8], header[9]                   # width
    fh[13] = header[19]                                       # flash
    fh[14:74] = header[4:64]                                  # 60 header bytes

    return bytes(fh) + image_data


def _is_full(b):
    """True for the full QT150 container (data at 738), False for the stub."""
    return (len(b) >= 738
            and int.from_bytes(b[552:554], "big") == 30
            and b[726:730] == b"qktn")


def qtk_meta(qtk):
    """Pull capture metadata out of a .qtk header for EXIF tagging.

    Magic in [0:4] ('qktk'=100, 'qktn'=150). The capture date lives at a
    different offset in each container flavour (see _DATE_OFF), so pass the
    whole file (or at least its first 738 bytes) -- with less than that the
    full container can't be recognised and its date is missed.

    Full-container files written before the date was stamped in carry the
    template's date in those bytes; that exact value is treated as "no date"
    rather than reported as a bogus 2024 capture.
    """
    if len(qtk) < 74 or qtk[0:3] != b"qkt":
        return {}
    model = "QuickTake 100" if qtk[3] == 0x6B else "QuickTake 150"
    meta = {"model": model, "quality": _QUALITY.get(qtk[34], "Unknown")}
    d = _DATE_OFF[_is_full(qtk)]
    date = qtk[d:d + 6]
    if date != _QT150_HDR_TEMPLATE[d:d + 6]:       # template date = capture lost
        mo, da, yr, hh, mi, ss = date
        if 1 <= mo <= 12 and 1 <= da <= 31 and hh < 24 and mi < 60 and ss < 60:
            year = 2000 + yr if yr < 70 else 1900 + yr  # 2-digit clock, 90s pivot
            meta["datetime"] = "%04d:%02d:%02d %02d:%02d:%02d" % (
                year, mo, da, hh, mi, ss)
    return meta

def extract_qtk(b):
    """Pull (image_data, reconstructed_header, model, is_full) out of an
    existing .qtk so it can be fed back through build_qtk. Handles both the
    minimal stub format (data at 736) and the full container (data at 738,
    bounded by the size field at 736)."""
    model = "QuickTake 100" if b[0:4] == b"qktk" else "QuickTake 150"
    is_full = _is_full(b)
    if is_full:
        size = int.from_bytes(b[736:738], "big")
        image = b[738:738 + size]
    else:
        image = b[736:]
    # Rebuild the ~60-byte camera header build_qtk reads. Both formats keep the
    # camera header bytes at [14:74] (minimal) or template equivalents there;
    # dims live at 546/544 and the quality flag at 34, all inside that window.
    header = bytearray(64)
    header[4:64] = b[14:74]
    header[5:8] = len(image).to_bytes(3, "big")
    if is_full:                       # date sits a byte earlier in this flavour
        d = _DATE_OFF[True]
        header[13:19] = b[d:d + 6]
    return image, header, model, is_full
