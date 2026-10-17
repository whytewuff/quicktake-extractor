"""QTK -> viewable image: decode, colour-render, and encode to a file.

Self-contained and pure-Python -- no dcraw, no ImageMagick, no libraw. The raw
QuickTake codecs are decoded in lib/codec.py; this module turns a decoded frame
into an 8-bit RGB image under a chosen rendering preset and writes it out
(PNG/JPEG/TIFF via Pillow, or a dependency-free PPM), tagging the file with EXIF
derived from the .qtk header plus the camera's fixed lens specs.

Public API:
    render_qtk(qtk_path, preset, wb=False[, on_warn])  -> HxWx3 uint8 ndarray
    out_path(qtk_path, fmt, preset, wb=False)          -> destination path
    convert_qtk(qtk_path, fmt, preset, wb=False[, on_warn]) -> written path
"""

import os

from . import codec
from .qtk import qtk_meta

_PIL_FORMAT = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "tiff": "TIFF"}


def _load_pillow():
    try:
        from PIL import Image
        return Image
    except ImportError:
        raise RuntimeError("Pillow not installed (pip install Pillow), "
                           "or use --format ppm")


# Fixed hardware specs of the QuickTake lens/sensor. Both the 100 and 150 use
# the same optics (Chinon-built, Kodak-designed): a fixed-focus 8 mm f/2.8-16
# lens (~50 mm equivalent) over a 640x480 CCD rated at ISO 85. None of this is
# stored per-frame, so it's the same for every image the camera produces.
_LENS = {
    "focal_mm": 8,          # actual focal length
    "focal_35mm": 50,       # 35mm-equivalent field of view
    "max_aperture": 2.8,    # widest the lens opens (range is f/2.8-f/16)
    "iso": 85,              # fixed film-speed equivalent
    "lens_model": "QuickTake 8mm f/2.8",
}


def _build_exif(Image, meta):
    """Assemble a Pillow Exif block from parsed .qtk metadata plus the camera's
    fixed lens specs. IFD0 tags (Make/Model/DateTime/...) survive in every
    Pillow-encoded format; the Exif sub-IFD (capture time, lens, ISO) is written
    for JPEG and PNG but dropped by Pillow's TIFF encoder."""
    import math
    from PIL.ExifTags import Base, IFD
    from PIL.TiffImagePlugin import IFDRational
    exif = Image.Exif()
    exif[Base.Make.value] = "Apple"
    exif[Base.Model.value] = meta.get("model", "QuickTake")
    exif[Base.Software.value] = "quicktake.py"
    desc = meta.get("model", "QuickTake")
    quality = meta.get("quality")
    if quality and quality != "Unknown":
        desc += f", {quality} quality"
    exif[Base.ImageDescription.value] = desc

    sub = exif.get_ifd(IFD.Exif)                        # Exif sub-IFD
    dt = meta.get("datetime")
    if dt:
        exif[Base.DateTime.value] = dt                 # IFD0 file datetime
        sub[Base.DateTimeOriginal.value] = dt          # capture time
        sub[Base.DateTimeDigitized.value] = dt
    # Fixed lens / sensor characteristics (see _LENS).
    sub[Base.FocalLength.value] = IFDRational(_LENS["focal_mm"], 1)
    sub[Base.FocalLengthIn35mmFilm.value] = _LENS["focal_35mm"]
    sub[Base.MaxApertureValue.value] = IFDRational(          # APEX: 2*log2(N)
        round(2 * math.log2(_LENS["max_aperture"]), 4))
    sub[Base.ISOSpeedRatings.value] = _LENS["iso"]
    sub[Base.LensMake.value] = "Apple"
    sub[Base.LensModel.value] = _LENS["lens_model"]
    return exif


# --------------------------------------------------------------------------- #
# "photoflash" preset -- reproduce Apple PhotoFlash's rendering
# --------------------------------------------------------------------------- #
# The none preset neutralises the green cast but can't reproduce the look of
# Apple's original QuickTake software (PhotoFlash), which had its own demosaic,
# a real colour matrix, and a contrasty tone curve. For QuickTake 150 photos
# render_qtk uses the byte-exact codec port instead; this fitted approximation
# is only the fallback for QuickTake 100 files (a different, unported codec):
#
#   CFA -> smooth-hue demosaic -> - black -> colour matrix -> per-channel tone
#       -> sRGB -> luminance unsharp
#
# The smooth-hue demosaic (bilinear green + colour-difference chroma, the period-
# Kodak approach) reproduces PhotoFlash's smooth gradients and neutral edges far
# better than a modern edge-directed demosaic. The colour/tone constants were fit
# against PhotoFlash's own TIFF exports of seven frames (see photoflash_tune/); a
# single fixed transform matches them all -- PhotoFlash applied no per-image
# auto-exposure or auto-WB.
_PHOTOFLASH = {
    "black": (8.011353e-05, 1.464933e-03, 0.0),          # per-channel sensor floor
    "matrix": ((1.38633468, -0.25751357, -0.34453641),   # camera-linear -> linear sRGB
               (-0.41885657, 1.46615660, -0.25534432),
               (-0.80715569, -0.58992137, 2.17402922)),
    "gamma": (1.44656395, 1.40539571, 1.45452156),       # per-channel tone (in linear)
    "gain": 1.62397763,
    "unsharp": dict(amount=1.0, radius=1.3),
}


def _conv3(a, k):
    """3x3 correlation of a 2-D array with reflect padding (k is symmetric)."""
    import numpy as np
    p = np.pad(a, 1, mode="reflect")
    out = np.zeros_like(a, dtype=np.float64)
    for dy in range(3):
        for dx in range(3):
            if k[dy][dx]:
                out += k[dy][dx] * p[dy:dy + a.shape[0], dx:dx + a.shape[1]]
    return out


def _demosaic_hue(cfa):
    """Smooth-hue-transition demosaic of a GRBG CFA: bilinear green, then
    bilinearly interpolated R-G / B-G colour differences (smooth chroma) --
    the period-Kodak method. Used only for QuickTake 100 files and the 'none'
    preset; QuickTake 150 photoflash rendering uses the byte-exact codec."""
    import numpy as np
    h, w = cfa.shape
    yy, xx = np.mgrid[0:h, 0:w]
    R = (yy % 2 == 0) & (xx % 2 == 1)       # GRBG: R at even row / odd col
    B = (yy % 2 == 1) & (xx % 2 == 0)       #       B at odd row / even col
    G = ~R & ~B
    kbil = ((.25, .5, .25), (.5, 1, .5), (.25, .5, .25))
    kg = ((0, .25, 0), (.25, 1, .25), (0, .25, 0))
    Gf = np.where(G, cfa, _conv3(np.where(G, cfa, 0.0), kg))
    dr = np.where(R, cfa - Gf, 0.0)
    db = np.where(B, cfa - Gf, 0.0)
    covR = _conv3(R.astype(float), kbil) + 1e-9
    covB = _conv3(B.astype(float), kbil) + 1e-9
    Rf = Gf + _conv3(dr, kbil) / covR
    Bf = Gf + _conv3(db, kbil) / covB
    return np.stack([Rf, Gf, Bf], -1)


def _unsharp(img, amount, radius):
    """Luminance-only unsharp mask of a uint8 HxWx3 image: sharpen Y with a
    separable Gaussian and add that detail equally to all channels, leaving the
    colour (chroma) untouched. Sharpening RGB directly would amplify the sensor's
    chroma noise into edge fringing that PhotoFlash does not have."""
    import numpy as np
    f = img.astype(np.float32)
    Y = f @ np.array([0.299, 0.587, 0.114], np.float32)
    rad = max(1, int(round(radius * 3)))
    x = np.arange(-rad, rad + 1)
    k = np.exp(-(x * x) / (2 * radius * radius))
    k /= k.sum()
    p = np.pad(Y, ((0, 0), (rad, rad)), mode="reflect")
    tmp = sum(k[i] * p[:, i:i + Y.shape[1]] for i in range(len(k)))
    p2 = np.pad(tmp, ((rad, rad), (0, 0)), mode="reflect")
    blur = sum(k[i] * p2[i:i + Y.shape[0], :] for i in range(len(k)))
    detail = amount * (Y - blur)
    return np.clip(f + detail[..., None], 0, 255).astype(np.uint8)


def _srgb_encode(lin):
    import numpy as np
    lin = np.clip(lin, 0, 1)
    return np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)


def _auto_wb(img, gamma):
    """Gray-world white balance on an 8-bit RGB image, applied in linear light:
    decode the given gamma, scale each channel so its mean equals the overall
    mean, re-encode. Neutralises a colour cast without touching brightness."""
    import numpy as np
    lin = (img.astype(np.float64) / 255.0) ** gamma
    m = lin.reshape(-1, 3).mean(0)
    lin *= m.mean() / np.maximum(m, 1e-9)
    return (np.clip(lin, 0, 1) ** (1.0 / gamma) * 255 + 0.5).astype(np.uint8)


def render_qtk(qtk_path, preset, wb=False, on_warn=None):
    """Decode a .qtk and return an 8-bit RGB ndarray (H x W x 3).

    preset 'photoflash' (default): for QuickTake 150 files this runs the
      original codec, ported byte-exactly -- the output is pixel-identical to
      Apple PhotoFlash. For QuickTake 100 files (different codec, never part of
      the port) it falls back to the fitted approximation of that look.
    preset 'none': raw sensor colours (green cast), auto-brightened + sRGB.
    wb=True additionally applies automatic gray-world white balance.
    on_warn: optional callable(str) for a human-readable fallback notice.
    """
    import numpy as np
    is_150 = codec._read_bytes(qtk_path)[0:4] == b"qktn"
    if preset == "photoflash" and is_150:
        try:
            img = codec.decode_codec_rgb(qtk_path)
            # the codec's output uses the classic-Mac ~1.8 gamma
            return _auto_wb(img, gamma=1.8) if wb else img
        except RuntimeError as e:
            # e.g. Standard-quality 320x240 (unported codec mode): fall back
            # to the fitted approximation below
            if on_warn:
                on_warn(f"byte-exact decode unavailable ({e}); using approximation")
    cfa, sat = codec.decode_cfa(qtk_path)
    rgb = _demosaic_hue(cfa.astype(np.float64) / sat)
    if preset == "photoflash":                     # QuickTake 100 approximation
        P = _PHOTOFLASH
        black, matrix, gamma = np.array(P["black"]), np.array(P["matrix"]), np.array(P["gamma"])
        corr = np.clip((np.clip(rgb.reshape(-1, 3) - black, 0, None) @ matrix.T)
                       * P["gain"], 0, 1) ** gamma
        img = (np.clip(_srgb_encode(corr), 0, 1) * 255 + 0.5).reshape(rgb.shape).astype(np.uint8)
        img = _unsharp(img, **P["unsharp"])
        return _auto_wb(img, gamma=2.2) if wb else img
    lin = rgb.reshape(-1, 3).copy()                # preset 'none': linear render
    if wb:                                         # gray-world in sensor space
        m = lin.mean(0)
        lin *= m.mean() / np.maximum(m, 1e-9)
    lin *= 0.94 / max(np.percentile(lin, 99.5), 1e-9)     # auto-brighten
    return (np.clip(_srgb_encode(lin), 0, 1) * 255 + 0.5).reshape(rgb.shape).astype(np.uint8)


def out_path(qtk_path, fmt, preset, wb=False):
    """Where convert_qtk writes: the .qtk stem, the preset (plus '-wb' when
    white balance is applied), then the format extension -- e.g.
    IMAGE001-photoflash.png. The suffix lets the same .qtk be converted under
    several settings without clobbering."""
    base = qtk_path[:-4] if qtk_path.lower().endswith(".qtk") else qtk_path
    ext = "jpg" if fmt == "jpeg" else fmt
    suffix = preset + ("-wb" if wb else "")
    return f"{base}-{suffix}.{ext}"


def convert_qtk(qtk_path, fmt, preset, wb=False, on_warn=None):
    """Decode a .qtk into <fmt> next to it and return the output path.

    We decode + demosaic the raw ourselves (no dcraw) and apply the chosen
    colour-correction preset (see render_qtk); Pillow re-encodes to png/jpg/tiff,
    tagging each with EXIF (camera model and capture date/time) from the .qtk
    header. 'ppm' output carries no metadata.
    """
    import numpy as np
    out = out_path(qtk_path, fmt, preset, wb)
    img = render_qtk(qtk_path, preset, wb, on_warn=on_warn)

    if fmt == "ppm":
        h, w, _ = img.shape
        with open(out, "wb") as f:
            f.write(b"P6\n%d %d\n255\n" % (w, h) + np.ascontiguousarray(img).tobytes())
    else:
        Image = _load_pillow()
        with open(qtk_path, "rb") as fh:
            meta = qtk_meta(fh.read(738))   # 738: enough to tell the two
                                            # container flavours apart
        if meta and not meta.get("datetime") and on_warn:
            on_warn(f"{os.path.basename(qtk_path)}: no capture date in the .qtk; "
                    f"the image will carry no EXIF date")
        save_kw = {"exif": _build_exif(Image, meta)} if meta else {}
        Image.fromarray(img).save(out, _PIL_FORMAT[fmt], **save_kw)

    if not os.path.exists(out):
        raise RuntimeError("decoder produced no output file")
    return out
