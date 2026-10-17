"""Serial protocol driver for the Apple QuickTake 100/150 camera.

Extracted from quicktake.py to separate concerns: this module speaks the wire
protocol only (wake, speed negotiation, metadata, image transfer); all console
styling stays with the CLI, injected via the on_* callbacks.

Protocol provenance: JQuickTake (Kevin Godin) and Colin Leroy-Mira's
reverse engineering, as ported in quicktake.py.
"""

import time

import serial


# --------------------------------------------------------------------------- #

class QuickTakeError(Exception):
    pass


class Camera:
    # --- command byte strings (verbatim from JQuickTake Camera.java) --------
    OPEN_CMD = bytearray([0x5A, 0xA5, 0x55, 0x05, 0x00, 0x00,
                          0x25, 0x80, 0x00, 0x80, 0x02, 0x00, 0x80])
    PORT_SPEED_CMD = bytearray([0x16, 0x2A, 0x00, 0x03, 0x00, 0x00, 0x00, 0x00,
                                0x00, 0x05, 0x00, 0x03, 0x03, 0x08, 0x04, 0x00])
    PING_CMD = bytes([0x16, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
    ACK = bytes([0x06])
    METADATA_CMD = bytes([0x16, 0x28, 0x00, 0x30, 0x00, 0x00,
                          0x00, 0x00, 0x00, 0x80, 0x00])
    GET_IMAGE_HEADER_CMD = bytearray([0x16, 0x28, 0x00, 0x21, 0x00, 0x00,
                                      0x00, 0x00, 0x00, 0x40, 0x00])
    GET_IMAGE_DATA_CMD = bytearray([0x16, 0x28, 0x00, 0x10, 0x00, 0x00,
                                    0x00, 0x00, 0x00, 0x00, 0x00])
    DELETE_IMAGES_CMD = bytes([0x16, 0x29, 0x00, 0x00, 0x00,
                               0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
    TAKE_PICTURE_CMD = bytes([0x16, 0x1B, 0x00, 0x00, 0x00, 0x00, 0x00])

    # bytes 6,7 (open cmd) and byte 12 (checksum) per requested speed, plus
    # byte 13 of the port-speed cmd.
    SPEED_TABLE = {
        9600:  dict(open6=0x25, open7=0x80, open12=0x80, spd13=0x08),
        19200: dict(open6=0x4B, open7=0x00, open12=0x26, spd13=0x10),
        38400: dict(open6=0x96, open7=0x00, open12=0x71, spd13=0x20),
        57600: dict(open6=0xE1, open7=0x00, open12=0xBC, spd13=0x30),
    }

    def __init__(self, port, speed=57600, debug=False,
                 on_step=None, on_ok=None, on_warn=None, on_kv=None, on_log=None):
        # UI callbacks let the caller style output; defaults print plainly.
        self._step = on_step or (lambda m: print(f"-> {m}"))
        self._ok = on_ok or (lambda m: print(f"ok {m}"))
        self._warn = on_warn or (lambda m: print(f"!  {m}"))
        self._kv = on_kv or (lambda k, v: print(f"  {k:<14}{v}"))
        self._log = on_log or print
        if speed not in self.SPEED_TABLE:
            raise QuickTakeError(f"Unsupported speed {speed}")
        self.port_name = port
        self.speed = speed
        self.debug = debug
        self.ser = None
        self.model = None
        # metadata
        self.battery = None
        self.taken = 0
        self.remaining = 0
        self.name = ""
        self.flash = ""
        self.quality = ""
        self.date = ""
        self.time = ""

    # --- low level ---------------------------------------------------------
    def _dbg(self, label, data):
        if self.debug:
            hexs = " ".join(f"{b:02X}" for b in data)
            self._log(f"    [{label}] {hexs}")

    def _set_dtr(self, value):
        # Real USB-serial adapters honor DTR; virtual/PTY ports raise OSError.
        try:
            self.ser.dtr = value
        except (OSError, IOError) as e:
            self._warn(f"port does not support DTR control ({e}); "
                       "camera may not wake unless the adapter drops DTR itself")

    def _write(self, data):
        self._dbg("W", data)
        self.ser.write(bytes(data))

    def _read_exact(self, n, timeout=15.0):
        """Accumulate exactly n bytes or raise on timeout."""
        buf = bytearray()
        deadline = time.time() + timeout
        while len(buf) < n:
            chunk = self.ser.read(n - len(buf))
            if chunk:
                buf.extend(chunk)
                deadline = time.time() + timeout  # progress resets the clock
            elif time.time() > deadline:
                raise QuickTakeError(
                    f"Timed out reading from camera (got {len(buf)}/{n} bytes)")
        self._dbg("R", buf)
        return bytes(buf)

    def _drain(self, n, timeout=10.0):
        """Read up to n throwaway bytes; tolerate the camera sending fewer."""
        buf = bytearray()
        deadline = time.time() + timeout
        while len(buf) < n and time.time() < deadline:
            chunk = self.ser.read(n - len(buf))
            if chunk:
                buf.extend(chunk)
            else:
                break
        self._dbg("R(drain)", buf)
        return bytes(buf)

    # --- session lifecycle -------------------------------------------------
    def open(self):
        self._step(f"Opening {self.port_name} at 9600 8N1 ...")
        self.ser = serial.Serial(
            port=self.port_name,
            baudrate=9600,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.5,      # per-read timeout used by _read_exact loop
            rtscts=False,
            dsrdtr=False,
        )

        # Wake the camera by dropping DTR (high -> low edge).
        self._step("Waking camera (dropping DTR) ...")
        self._set_dtr(True)
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        self._set_dtr(False)

        # Poll up to 10s for the 7-byte wake burst.
        waited = 0.0
        while self.ser.in_waiting < 1 and waited < 10.0:
            time.sleep(0.1)
            waited += 0.1
        if self.ser.in_waiting < 1:
            self.close()
            raise QuickTakeError(
                "Camera did not wake. Check it is powered on, the cable "
                "wiring, and that you picked the right --port.")

        wake = self._read_exact(7)
        # byte[3] == 0xC8 -> QT150, else QT100
        self.model = "QuickTake 150" if wake[3] == 0xC8 else "QuickTake 100"
        self._ok(f"Camera awake ({self.model}) after {waited:.1f}s")

        # Reply with the OPEN command, patched for the requested speed.
        t = self.SPEED_TABLE[self.speed]
        self.OPEN_CMD[6] = t["open6"]
        self.OPEN_CMD[7] = t["open7"]
        self.OPEN_CMD[12] = t["open12"]
        self._write(self.OPEN_CMD)

        self._drain(10)  # 10 throwaway bytes echoing the open cmd

        # Switch to even parity, settle, then negotiate the faster baud rate.
        self.ser.parity = serial.PARITY_EVEN
        time.sleep(1.0)

        self._step(f"Negotiating {self.speed} baud ...")
        self._set_port_speed()

        self._acquire_metadata()
        self._ok("Session open")

    def _set_port_speed(self):
        self.PORT_SPEED_CMD[13] = self.SPEED_TABLE[self.speed]["spd13"]

        if not self._ping():
            raise QuickTakeError("Camera did not answer ping before speed change")

        self._write(self.PORT_SPEED_CMD)
        self._read_exact(1)            # camera ACK
        self._write(self.ACK)          # our ACK
        time.sleep(0.1)

        # Both ends switch to the new baud rate now.
        self.ser.baudrate = self.speed

        self._drain(1024)              # ~1024 bytes of 0xAA filler, discarded
        self._write(self.ACK)
        self._read_exact(1)            # final ACK
        time.sleep(0.1)

    def _ping(self):
        self._write(self.PING_CMD)
        resp = self._read_exact(1)
        return resp[0] == 0x00

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    # --- metadata ----------------------------------------------------------
    def _acquire_metadata(self):
        self._write(self.METADATA_CMD)
        self._read_exact(1)            # ACK
        self._write(self.ACK)
        m = self._read_exact(128)

        self.battery = m[2]
        self.taken = m[4]
        self.remaining = m[6]
        self.date = f"{m[16]:02d}/{m[17]:02d}/{m[18]:02d}"      # mm/dd/yy
        self.time = f"{m[19]:02d}:{m[20]:02d}:{m[21]:02d}"
        self.flash = {0: "Auto", 1: "Disabled", 2: "Forced"}.get(m[22], "Unknown")
        self.quality = {32: "Standard", 16: "High"}.get(m[27], "Unknown")
        self.name = m[47:79].decode("latin-1", "replace").rstrip(" \x00")

    def print_status(self):
        kv = self._kv
        kv("Model:", self.model)
        kv("Name:", self.name or "(unnamed)")
        kv("Battery:", f"{self.battery}%")
        kv("Pictures:", f"{self.taken} taken, {self.remaining} remaining")
        kv("Quality:", self.quality)
        kv("Flash:", self.flash)
        kv("Clock:", f"{self.date} {self.time}")

    # --- image transfer ----------------------------------------------------
    def read_image_header(self, image_num):
        self.GET_IMAGE_HEADER_CMD[6] = image_num & 0xFF
        self._write(self.GET_IMAGE_HEADER_CMD)
        self._read_exact(1)            # ACK
        self._write(self.ACK)
        return self._read_exact(64)

    def read_image_data(self, image_num, header, on_progress=None):
        size = int.from_bytes(b"\x00" + header[5:8], "big")  # 3-byte size

        cmd = bytearray(self.GET_IMAGE_DATA_CMD)
        cmd[6] = image_num & 0xFF
        cmd[7], cmd[8], cmd[9] = header[5], header[6], header[7]
        self._write(cmd)
        self._read_exact(1)            # ACK
        self._write(self.ACK)

        data = bytearray()
        to_read = 512
        while len(data) < size:
            chunk = self._read_exact(to_read)
            data.extend(chunk)
            if on_progress:
                on_progress(len(data), size)
            if len(data) >= size:
                break
            self._write(self.ACK)
            if size - len(data) < 512:
                to_read = size - len(data)
        return bytes(data)

    def take_picture(self):
        self._write(self.TAKE_PICTURE_CMD)
        self._read_exact(1)            # ACK
        time.sleep(2.0)                # let the camera expose and store the frame
        self._acquire_metadata()       # refresh taken/remaining counts

    def delete_all(self):
        self._write(self.DELETE_IMAGES_CMD)
        self._read_exact(1)            # ACK
        self._acquire_metadata()       # refresh counts (should be 0 taken)
