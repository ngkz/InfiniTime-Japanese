#!/usr/bin/env python3
# InfiniTime BLEFS resource manager
# 2026 Kazutoshi Noguchi
# SPDX-License-Identifier: CC0-1.0
import argparse
import os
import struct
import sys
import time
import zipfile
from pathlib import Path

from bluepy.btle import Scanner, Peripheral, BTLEException


# ---------------------------------------------------------------------------
# BLEFS Protocol constants (BLEFS.md)
# ---------------------------------------------------------------------------

VERSION_CHAR_UUID = "adaf0100-4669-6c65-5472-616e73666572"
TRANSFER_CHAR_UUID = "adaf0200-4669-6c65-5472-616e73666572"

# Command opcodes
OP_READ_FILE_HEADER = 0x10
OP_READ_FILE_CONT   = 0x12
OP_WRITE_FILE_HEADER = 0x20
OP_WRITE_FILE_DATA  = 0x22
OP_DELETE_FILE      = 0x30
OP_MKDIR            = 0x40
OP_LIST_DIR         = 0x50
OP_MOVE             = 0x60

# Response opcodes
RESP_READ_FILE     = 0x11
RESP_WRITE_FILE    = 0x21
RESP_DELETE_FILE   = 0x31
RESP_MKDIR         = 0x41
RESP_LIST_DIR      = 0x51
RESP_MOVE          = 0x61

# Default scan timeout (seconds)
SCAN_TIMEOUT = 30


# ---------------------------------------------------------------------------
# BLEFSClient — bluepy-based BLE FS client
# ---------------------------------------------------------------------------

class BLEFSClient:
    """BLE File System client using bluepy's Peripheral."""

    def __init__(self, mac_address: str):
        self.mac = mac_address.upper()
        self._peripheral: Peripheral | None = None
        self._transfer_char = None
        self._version_char = None
        self._connected = False
        self._notifications = []
        self._notification_delegate = None
        self._mtu = 23

    # ---- connection -------------------------------------------------------

    def connect(self) -> None:
        """Scan for the device and establish a BLE connection."""
        print(f"Scanning for {self.mac} ...")
        scanner = Scanner()
        devices = scanner.scan(SCAN_TIMEOUT)

        if not devices:
            print("No BLE devices found during scan.")
            sys.exit(1)

        target = None
        for dev in devices:
            if dev.addr.upper() == self.mac:
                target = dev
                break

        if target is None:
            print(f"{self.mac} not found.  Nearby devices:")
            for dev in devices:
                rssi_str = f"RSSI {dev.rssi} dBm" if dev.rssi is not None else "no RSSI"
                name = ""
                for _, desc, value in dev.getScanData():
                    if desc in ("Complete Local Name", "Short Local Name"):
                        name = f" ({value})"
                        break
                print(f"  {dev.addr}{name}  [{rssi_str}]")
            sys.exit(1)

        print(f"Connecting to {self.mac} ...")
        try:
            self._peripheral = Peripheral()
            self._peripheral.connect(self.mac, target.addrType)
        except BTLEException as exc:
            print(f"Connection failed: {exc}")
            sys.exit(1)

        self._connected = True
        self._negotiate_mtu()
        print("Connected. Discovering BLEFS service ...")
        self._discover_services()
        self._setup_notifications()

    def disconnect(self) -> None:
        """Disconnect from the device."""
        if self._peripheral and self._connected:
            try:
                self._set_notifications(False)
            except Exception:
                pass
            try:
                self._peripheral.withDelegate(None)
            except Exception:
                pass
            try:
                self._peripheral.disconnect()
            except Exception:
                pass
            self._connected = False

    def _negotiate_mtu(self) -> None:
        """Request a larger ATT MTU when bluepy/the adapter support it."""
        try:
            mtu = self._peripheral.setMTU(247)
            if mtu is not None:
                self._mtu = mtu
            else:
                self._mtu = 23
        except Exception:
            self._mtu = 23
        print(f"ATT MTU: {self._mtu}")

    def _discover_services(self) -> None:
        """Find the BLEFS transfer characteristic."""
        for svc in self._peripheral.getServices():
            for ch in svc.getCharacteristics():
                if ch.uuid == TRANSFER_CHAR_UUID:
                    self._transfer_char = ch
                elif ch.uuid == VERSION_CHAR_UUID:
                    self._version_char = ch

        if self._transfer_char is None:
            print("BLEFS transfer characteristic not found.")
            sys.exit(1)

        # Read version if possible
        if self._version_char:
            try:
                raw_ver = self._version_char.read()
                if len(raw_ver) >= 4:
                    ver = struct.unpack("<I", raw_ver[:4])[0]
                elif len(raw_ver) >= 2:
                    ver = struct.unpack("<H", raw_ver[:2])[0]
                elif raw_ver:
                    ver = raw_ver[0]
                else:
                    ver = 0
                print(f"BLEFS version: {ver}")
            except Exception:
                pass

    # ---- internal helpers -------------------------------------------------

    def _setup_notifications(self) -> None:
        """Install a persistent notification delegate and enable transfer notifications."""
        client = self

        class _RespDelegate:
            def handleNotification(self, handle, ntf_data):
                if client._transfer_char and handle != client._transfer_char.getHandle():
                    return
                client._notifications.append(bytes(ntf_data))

        self._notification_delegate = _RespDelegate()
        self._peripheral.withDelegate(self._notification_delegate)
        self._set_notifications(True)

    def _set_notifications(self, enabled: bool) -> None:
        """Write the transfer characteristic CCCD."""
        value = b"\x01\x00" if enabled else b"\x00\x00"
        cccd_descs = self._transfer_char.getDescriptors(forUUID=0x2902)
        if not cccd_descs:
            raise RuntimeException("BLE CCCD descriptor not found")
        cccd_descs[0].write(value, withResponse=True)

    @staticmethod
    def _status(resp: bytes) -> int:
        """Return the signed BLEFS status byte."""
        return struct.unpack_from("<b", resp, 1)[0]

    def _response_min_len(self, expected_resp: int) -> int:
        return {
            RESP_READ_FILE: 16,
            RESP_WRITE_FILE: 20,
            RESP_DELETE_FILE: 2,
            RESP_MKDIR: 16,
            RESP_LIST_DIR: 28,
            RESP_MOVE: 2,
        }.get(expected_resp, 2)

    def _wait_for_response(self, expected_resp: int, timeout: float = 5.0, min_len: int | None = None) -> bytes:
        """Wait for one notification with the expected response opcode."""
        if min_len is None:
            min_len = self._response_min_len(expected_resp)

        deadline = time.time() + timeout
        while time.time() < deadline:
            for idx, resp in enumerate(self._notifications):
                if resp and resp[0] == expected_resp:
                    del self._notifications[idx]
                    if len(resp) < min_len:
                        raise RuntimeError(
                            f"Short response 0x{expected_resp:02x}: {len(resp)} bytes, expected at least {min_len}"
                        )
                    return resp

            try:
                self._peripheral.waitForNotifications(timeout=min(0.5, max(0.0, deadline - time.time())))
            except BTLEException as exc:
                raise RuntimeError(f"Notification wait failed: {exc}")

        raise TimeoutError(
            f"No response (0x{expected_resp:02x}) within {timeout}s. "
            "Check that file transfer/DFU mode is enabled on the device."
        )

    def _write_and_wait(self, data: bytes, expected_resp: int, timeout: float = 5.0, min_len: int | None = None) -> bytes:
        """Write a command to the transfer characteristic and wait for one response notification."""
        self._notifications.clear()
        try:
            self._peripheral.writeCharacteristic(
                self._transfer_char.getHandle(), data, withResponse=True
            )
        except BTLEException as exc:
            raise RuntimeError(f"Write failed: {exc}")

        return self._wait_for_response(expected_resp, timeout=timeout, min_len=min_len)

    def _max_read_chunk(self, requested: int) -> int:
        return max(1, min(requested, self._mtu - 3 - 16))

    def _max_write_chunk(self) -> int:
        return max(1, min(200, self._mtu - 3 - 12))

    # ---- BLEFS operations -------------------------------------------------

    def read_file(self, path: str, chunk_size: int = 200) -> bytes:
        """Read a file from the device. Returns file contents as bytes."""
        path_bytes = path.encode("utf-8")
        chunk_size = self._max_read_chunk(chunk_size)

        # --- send header (0x10) ---
        hdr = struct.pack("<BBHII", OP_READ_FILE_HEADER, 0x00,
                          len(path_bytes), 0, chunk_size) + path_bytes
        resp = self._write_and_wait(hdr, RESP_READ_FILE)

        status = self._status(resp)
        if status < 0:
            raise RuntimeError(f"Read header failed for {path}: status {status}")

        total_size = struct.unpack_from("<I", resp, 8)[0]
        chunk_len = struct.unpack_from("<I", resp, 12)[0]
        if len(resp) < 16 + chunk_len:
            raise RuntimeError(f"Short read response: {len(resp)} bytes, expected {16 + chunk_len}")
        data = bytearray(resp[16:16 + chunk_len])
        next_offset = struct.unpack_from("<I", resp, 4)[0] + chunk_len

        # --- read remaining chunks (0x12) ---
        while len(data) < total_size:
            cont = struct.pack("<BBHII", OP_READ_FILE_CONT, 0x01,
                               0, next_offset, chunk_size)
            resp = self._write_and_wait(cont, RESP_READ_FILE)

            status = self._status(resp)
            if status < 0:
                raise RuntimeError(f"Read chunk at offset {next_offset}: status {status}")

            r_offset = struct.unpack_from("<I", resp, 4)[0]
            r_len = struct.unpack_from("<I", resp, 12)[0]
            if len(resp) < 16 + r_len:
                raise RuntimeError(f"Short read response: {len(resp)} bytes, expected {16 + r_len}")
            if r_len == 0:
                raise RuntimeError(f"Read stalled at offset {next_offset}")
            data += resp[16:16 + r_len]
            next_offset = r_offset + r_len

        return bytes(data)

    def write_file(self, path: str, data: bytes, progress=None) -> None:
        """Write a file to the device."""
        path_bytes = path.encode("utf-8")
        file_size = len(data)

        # --- send header (0x20) ---
        hdr = struct.pack("<BBHIQI", OP_WRITE_FILE_HEADER, 0x00,
                          len(path_bytes), 0, int(time.time() * 1e9), file_size) + path_bytes
        resp = self._write_and_wait(hdr, RESP_WRITE_FILE)

        status = self._status(resp)
        if status < 0:
            raise RuntimeError(f"Write header failed for {path}: status {status}")

        # --- send data chunks (0x22) ---
        offset = 0
        chunk_size = self._max_write_chunk()
        while offset < file_size:
            chunk = data[offset:offset + chunk_size]
            chunk_len = len(chunk)
            pkt = struct.pack("<BBHII", OP_WRITE_FILE_DATA, 0x01,
                              0, offset, chunk_len) + chunk

            resp = self._write_and_wait(pkt, RESP_WRITE_FILE)

            status = self._status(resp)
            if status < 0:
                raise RuntimeError(
                    f"Write chunk at offset {offset}: status {status}")

            offset += chunk_len

            if progress:
                progress(offset, file_size)

    def delete_file(self, path: str) -> None:
        """Delete a file from the device."""
        path_bytes = path.encode("utf-8")
        pkt = struct.pack("<BBH", OP_DELETE_FILE, 0x00, len(path_bytes)) + path_bytes
        resp = self._write_and_wait(pkt, RESP_DELETE_FILE)

        status = self._status(resp)
        if status < 0:
            raise RuntimeError(f"Delete failed for {path}: status {status}")

    def mkdir(self, path: str) -> None:
        """Create a directory on the device. Ignores 'already exists' (-17)."""
        path_bytes = path.encode("utf-8")
        pkt = (struct.pack("<BBHIQ", OP_MKDIR, 0x00, len(path_bytes),
                            0, int(time.time() * 1e9)) + path_bytes)
        resp = self._write_and_wait(pkt, RESP_MKDIR)

        # Ignore "entry already exists" error (-17 / LFS_ERR_EXIST)
        status = self._status(resp)
        if status < 0 and status != -17:
            raise RuntimeError(f"Mkdir failed for {path}: status {status}")

    def list_dir(self, path: str) -> list[dict]:
        """List directory contents. Returns list of dicts with keys: name, is_dir, size."""
        path_bytes = path.encode("utf-8")
        pkt = struct.pack("<BBH", OP_LIST_DIR, 0x00, len(path_bytes)) + path_bytes

        entries = []
        resp = self._write_and_wait(pkt, RESP_LIST_DIR)

        while True:
            status = self._status(resp)
            if status < 0:
                raise RuntimeError(f"List failed for {path}: status {status}")

            name_len = struct.unpack_from("<H", resp, 2)[0]
            entry_num = struct.unpack_from("<I", resp, 4)[0]
            total_entries = struct.unpack_from("<I", resp, 8)[0]
            flags = struct.unpack_from("<I", resp, 12)[0]
            is_dir = bool(flags & 0x01)
            file_size = struct.unpack_from("<I", resp, 24)[0]

            if name_len:
                if len(resp) < 28 + name_len:
                    raise RuntimeError(f"Short list response: {len(resp)} bytes, expected {28 + name_len}")
                name = resp[28:28 + name_len].decode("utf-8", errors="replace")
                entries.append({
                    "name": name,
                    "is_dir": is_dir,
                    "size": file_size,
                })

            # Firmware sends a final zero-length entry with entry == totalentries.
            if entry_num >= total_entries:
                break

            resp = self._wait_for_response(RESP_LIST_DIR)

        return entries

    def move(self, old_path: str, new_path: str) -> None:
        """Move/rename a file or directory."""
        old_bytes = old_path.encode("utf-8")
        new_bytes = new_path.encode("utf-8")
        pkt = (struct.pack("<BBHH", OP_MOVE, 0x00, len(old_bytes), len(new_bytes))
               + old_bytes + b"\x00" + new_bytes)
        resp = self._write_and_wait(pkt, RESP_MOVE)

        status = self._status(resp)
        if status < 0:
            raise RuntimeError(f"Move failed from {old_path} to {new_path}: status {status}")




# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------

def _cmd_read(args):
    client = BLEFSClient(args.device)
    try:
        client.connect()
        data = client.read_file(args.path)
        sys.stdout.buffer.write(data)
    finally:
        client.disconnect()


def _cmd_write(args):
    client = BLEFSClient(args.device)
    if args.file:
        data = Path(args.file).read_bytes()
    else:
        data = sys.stdin.buffer.read()

    try:
        client.connect()
        client.write_file(args.path, data)
        print("Write complete.")
    finally:
        client.disconnect()


def _cmd_rm(args):
    client = BLEFSClient(args.device)
    try:
        client.connect()
        client.delete_file(args.path)
        print(f"Deleted {args.path}")
    finally:
        client.disconnect()


def _cmd_ls(args):
    client = BLEFSClient(args.device)
    try:
        client.connect()
        entries = client.list_dir(args.path)

        if not entries:
            print("(empty)")
            return

        # Determine column width for alignment
        name_w = max(len(e["name"]) for e in entries)
        name_w = max(name_w, 5)  # minimum "Name"
        TYPE_W = 4

        print(f"{'Name':<{name_w}} {'Type':<{TYPE_W}} {'Size'}")
        print("-" * (name_w + 1 + TYPE_W + 1 + 8))
        for e in entries:
            kind = "DIR" if e["is_dir"] else "FILE"
            size_str = str(e["size"]) if not e["is_dir"] else "-"
            print(f"{e['name']:<{name_w}} {kind:<{TYPE_W}} {size_str}")
    finally:
        client.disconnect()


def _cmd_mv(args):
    client = BLEFSClient(args.device)
    try:
        client.connect()
        client.move(args.old, args.new)
        print(f"Moved {args.old} -> {args.new}")
    finally:
        client.disconnect()


def _cmd_update(args):
    zip_path = Path(args.zipfile)
    if not zip_path.exists():
        print(f"Error: file not found: {zip_path}", file=sys.stderr)
        sys.exit(1)
    if not zipfile.is_zipfile(zip_path):
        print(f"Error: not a valid zip file: {zip_path}", file=sys.stderr)
        sys.exit(1)

    with zipfile.ZipFile(zip_path) as z:
        manifest = __import__("json").loads(z.read("resources.json"))

        client = BLEFSClient(args.device)
        try:
            client.connect()

            # Remove obsolete files
            for entry in manifest.get("obsolete_files", []):
                fpath = entry["path"]
                print(f"  Removing obsolete file {fpath} ...")
                try:
                    client.delete_file(fpath)
                except RuntimeError as exc:
                    print(f"    Error: {exc}")

            # Upload resources
            resources = manifest.get("resources", [])
            total = len(resources)
            for i, entry in enumerate(resources, 1):
                filename = entry["filename"]
                dest = entry["path"]

                with z.open(filename) as src:
                    data = src.read()

                print(f"  [{i}/{total}] Uploading {filename} -> {dest}")
                bar_w = 40
                last_pct = -1

                def _prog(sent, total_size, _pw=bar_w, _lp=[-1]):
                    pct = int(sent * 100 / total_size) if total_size else 100
                    if pct != _lp[0]:
                        _lp[0] = pct
                        filled = int(_pw * sent / total_size) if total_size else _pw
                        bar = "\u2588" * filled + "\u2591" * (_pw - filled)
                        sys.stdout.write(f"\r    [{bar}] {sent:>7}/{total_size:>7} ({pct:3d}%)")
                        sys.stdout.flush()

                try:
                    client.mkdir(Path(dest).parent.as_posix())
                    client.write_file(dest, data, progress=_prog)
                    print()  # newline after progress bar
                except RuntimeError as exc:
                    print(f"\n    Error: {exc}")

            print("\nDone! Resources updated successfully.")
        finally:
            client.disconnect()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="resource.py",
        description="InfiniTime BLEFS resource manager",
        epilog="Examples:\n"
               "  %(prog)s ls 01:23:45:67:89:AB /\n"
               "  %(prog)s read 01:23:45:67:89:AB /resources/logo.bin > logo.bin\n"
               "  %(prog)s update 01:23:45:67:89:AB infinitime-resources.zip\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="Command")

    # read
    p_read = sub.add_parser("read", help="Read a file from the device")
    p_read.add_argument("device", help="Device MAC address")
    p_read.add_argument("path", help="File path on device (e.g. /resources/logo.bin)")
    p_read.set_defaults(func=_cmd_read)

    # write
    p_write = sub.add_parser("write", help="Write a file to the device")
    p_write.add_argument("device", help="Device MAC address")
    p_write.add_argument("path", help="Destination path on device")
    p_write.add_argument("file", nargs="?", default=None,
                         help="Local file to upload (stdin if omitted)")
    p_write.set_defaults(func=_cmd_write)

    # rm
    p_rm = sub.add_parser("rm", help="Delete a file from the device")
    p_rm.add_argument("device", help="Device MAC address")
    p_rm.add_argument("path", help="File path on device")
    p_rm.set_defaults(func=_cmd_rm)

    # ls
    p_ls = sub.add_parser("ls", help="List directory contents")
    p_ls.add_argument("device", help="Device MAC address")
    p_ls.add_argument("path", help="Directory path on device (e.g. /)")
    p_ls.set_defaults(func=_cmd_ls)

    # mv
    p_mv = sub.add_parser("mv", help="Move/rename a file or directory")
    p_mv.add_argument("device", help="Device MAC address")
    p_mv.add_argument("old", help="Source path on device")
    p_mv.add_argument("new", help="Destination path on device")
    p_mv.set_defaults(func=_cmd_mv)

    # update
    p_upd = sub.add_parser("update", help="Upload resources zip")
    p_upd.add_argument("device", help="Device MAC address")
    p_upd.add_argument("zipfile", help="Path to resources zip file")
    p_upd.set_defaults(func=_cmd_update)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
