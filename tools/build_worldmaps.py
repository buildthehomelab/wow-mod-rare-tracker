#!/usr/bin/env python3
"""
Build the map images for the rare map page from the WoW 3.3.5a client's own world map textures.

For every zone and continent it stitches the 12 base tiles together, paints every explored-area
overlay on top (so the map looks fully explored), crops to the 1002x668 area the game uses and
saves a JPEG. It also writes maps.json, which tells the page which image belongs to which zone and
where each continent's edges are, so it can place rares by their world coordinates.

Output (copy the whole folder to the portal's realm-public folder as "worldmap"):
    zone-<areaId>.jpg        one per zone, named by the zone id mod-rare-tracker reports
    continent-<mapId>.jpg    Eastern Kingdoms (0), Kalimdor (1), Outland (530), Northrend (571)
    maps.json

Input, either:
    --mpq-dir DIR     the client's Data folder. The MPQs are read directly, in the order the game
                      loads them, so patches win. Works on Windows, macOS and Linux.
    --extracted DIR   a folder holding Interface/WorldMap/... and DBFilesClient/... already
                      extracted from the client's MPQs

DBC files used: WorldMapArea.dbc, WorldMapOverlay.dbc and WorldMapTransforms.dbc. The client's
copies are best; --dbc DIR reads them from another folder, such as the server's data/dbc.

Needs only Pillow:  python3 -m pip install pillow   (on Windows: py -m pip install pillow)

Example (Windows):
    py build_worldmaps.py --mpq-dir "C:\\World of Warcraft\\Data" --out worldmap
"""

import argparse
import bz2
import io
import json
import math
import os
import struct
import sys
import zlib

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is missing: python3 -m pip install pillow")

TILE = 256
MAP_W, MAP_H = 1002, 668  # the visible part of the 1024x768 tile grid
CONTINENTS = {0: "Eastern Kingdoms", 1: "Kalimdor", 530: "Outland", 571: "Northrend"}


# --- Reading files: an extracted folder or the MPQs themselves --------------------------------

class ExtractedSource:
    """Files extracted from the MPQs into a folder. MPQ names are case-insensitive, so are we."""

    def __init__(self, root):
        self.files = {}
        for folder, _, names in os.walk(root):
            for name in names:
                full = os.path.join(folder, name)
                key = os.path.relpath(full, root).replace(os.sep, "\\").lower()
                self.files[key] = full

    def read(self, name):
        path = self.files.get(name.lower())
        if not path:
            return None
        with open(path, "rb") as f:
            return f.read()


# --- MPQ reading, in plain Python ------------------------------------------------------------
# Enough of the MPQ format for the 3.3.5a client's archives: format versions 1 and 2, encrypted
# tables and files, single-unit and sectored files, zlib, bzip2 and PKWARE implode.

def _crypt_table():
    table = [0] * 0x500
    seed = 0x00100001
    for i in range(0x100):
        for j in range(5):
            seed = (seed * 125 + 3) % 0x2AAAAB
            high = (seed & 0xFFFF) << 16
            seed = (seed * 125 + 3) % 0x2AAAAB
            table[i + j * 0x100] = high | (seed & 0xFFFF)
    return table


CRYPT = _crypt_table()
HASH_OFFSET, HASH_A, HASH_B, HASH_KEY = 0, 1, 2, 3


def mpq_hash(name, kind):
    seed1, seed2 = 0x7FED7FED, 0xEEEEEEEE
    for ch in name.upper().replace("/", "\\").encode("latin-1"):
        seed1 = (CRYPT[(kind << 8) + ch] ^ (seed1 + seed2)) & 0xFFFFFFFF
        seed2 = (ch + seed1 + seed2 + (seed2 << 5) + 3) & 0xFFFFFFFF
    return seed1


def mpq_decrypt(data, key):
    words = struct.unpack(f"<{len(data) // 4}I", data[:len(data) // 4 * 4])
    out = []
    seed = 0xEEEEEEEE
    for word in words:
        seed = (seed + CRYPT[0x400 + (key & 0xFF)]) & 0xFFFFFFFF
        plain = (word ^ (key + seed)) & 0xFFFFFFFF
        out.append(plain)
        key = ((~key << 0x15) + 0x11111111 | key >> 0x0B) & 0xFFFFFFFF
        seed = (plain + seed + (seed << 5) + 3) & 0xFFFFFFFF
    return struct.pack(f"<{len(out)}I", *out) + data[len(data) // 4 * 4:]


def _huffman(rep):
    """A PKWARE DCL code table from its compact form (as in zlib's contrib/blast)."""
    lengths = []
    for byte in rep:
        lengths += [byte & 15] * ((byte >> 4) + 1)
    count = [0] * 14
    for length in lengths:
        count[length] += 1
    offs = [0] * 14
    for length in range(1, 13):
        offs[length + 1] = offs[length] + count[length]
    symbol = [0] * len(lengths)
    for sym, length in enumerate(lengths):
        if length:
            symbol[offs[length]] = sym
            offs[length] += 1
    return count, symbol


_LITERALS = _huffman(bytes([
    11, 124, 8, 7, 28, 7, 188, 13, 76, 4, 10, 8, 12, 10, 12, 10, 8, 23, 8, 9, 7, 6, 7, 8, 7, 6,
    55, 8, 23, 24, 12, 11, 7, 9, 11, 12, 6, 7, 22, 5, 7, 24, 6, 11, 9, 6, 7, 22, 7, 11, 38, 7,
    9, 8, 25, 11, 8, 11, 9, 12, 8, 12, 5, 38, 5, 38, 5, 11, 7, 5, 6, 21, 6, 10, 53, 8, 7, 24,
    10, 27, 44, 253, 253, 253, 252, 252, 252, 13, 12, 45, 12, 45, 12, 61, 12, 45, 44, 173]))
_LENGTHS = _huffman(bytes([2, 35, 36, 53, 38, 23]))
_DISTANCES = _huffman(bytes([2, 20, 53, 230, 247, 151, 248]))
_LENGTH_BASE = [3, 2, 4, 5, 6, 7, 8, 9, 10, 12, 16, 24, 40, 72, 136, 264]
_LENGTH_EXTRA = [0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8]


def explode(data):
    """PKWARE DCL "implode" decompression, a port of zlib's contrib/blast/blast.c."""
    pos = 0
    bitbuf = 0
    bitcnt = 0

    def bits(need):
        nonlocal pos, bitbuf, bitcnt
        while bitcnt < need:
            if pos >= len(data):
                raise ValueError("implode: out of input")
            bitbuf |= data[pos] << bitcnt
            pos += 1
            bitcnt += 8
        value = bitbuf & ((1 << need) - 1)
        bitbuf >>= need
        bitcnt -= need
        return value

    def decode(table):
        count, symbol = table
        code = first = index = 0
        for length in range(1, 14):
            code |= bits(1) ^ 1  # PKWARE stores its codes bit-inverted
            if code < first + count[length]:
                return symbol[index + code - first]
            index += count[length]
            first = (first + count[length]) << 1
            code <<= 1
        raise ValueError("implode: bad code")

    coded = bits(8)
    dict_bits = bits(8)
    if coded > 1 or not 4 <= dict_bits <= 6:
        raise ValueError("implode: bad header")
    out = bytearray()
    while True:
        if bits(1):
            sym = decode(_LENGTHS)
            length = _LENGTH_BASE[sym] + bits(_LENGTH_EXTRA[sym])
            if length == 519:
                return bytes(out)
            shift = 2 if length == 2 else dict_bits
            dist = (decode(_DISTANCES) << shift) + bits(shift) + 1
            if dist > len(out):
                raise ValueError("implode: distance too far back")
            for _ in range(length):
                out.append(out[-dist])
        else:
            out.append(decode(_LITERALS) if coded else bits(8))


def decompress(data, expected):
    """One compressed MPQ sector (or single-unit file): a mask byte, then the data."""
    mask, body = data[0], data[1:]
    if mask & 0x10:
        body = bz2.decompress(body)
    if mask & 0x08:
        body = explode(body)
    if mask & 0x02:
        body = zlib.decompress(body)
    if mask & ~0x1A:
        raise ValueError(f"unsupported MPQ compression 0x{mask:02x}")
    if len(body) != expected:
        raise ValueError("MPQ sector has the wrong size")
    return body


class MpqArchive:
    FLAG_IMPLODE = 0x00000100
    FLAG_COMPRESS = 0x00000200
    FLAG_ENCRYPTED = 0x00010000
    FLAG_FIX_KEY = 0x00020000
    FLAG_SINGLE_UNIT = 0x01000000
    FLAG_DELETED = 0x02000000
    FLAG_SECTOR_CRC = 0x04000000
    FLAG_EXISTS = 0x80000000

    def __init__(self, path):
        self.file = open(path, "rb")
        self.base = self._find_header()
        f = self.file
        f.seek(self.base)
        (_, header_size, _, version, sector_shift, hash_pos, block_pos,
         hash_count, block_count) = struct.unpack("<4sIIHHIIII", f.read(32))
        hi_block_pos = hash_hi = block_hi = 0
        if version >= 1 and header_size >= 44:
            hi_block_pos, hash_hi, block_hi = struct.unpack("<QHH", f.read(12))
        self.sector_size = 512 << sector_shift

        f.seek(self.base + (hash_hi << 32 | hash_pos))
        table = mpq_decrypt(f.read(hash_count * 16), mpq_hash("(hash table)", HASH_KEY))
        self.hashes = [struct.unpack_from("<IIHHI", table, i * 16) for i in range(hash_count)]

        f.seek(self.base + (block_hi << 32 | block_pos))
        table = mpq_decrypt(f.read(block_count * 16), mpq_hash("(block table)", HASH_KEY))
        self.blocks = [list(struct.unpack_from("<IIII", table, i * 16)) for i in range(block_count)]
        if hi_block_pos:
            f.seek(self.base + hi_block_pos)
            highs = struct.unpack(f"<{block_count}H", f.read(block_count * 2))
            for block, high in zip(self.blocks, highs):
                block[0] |= high << 32

    def _find_header(self):
        offset = 0
        while True:
            self.file.seek(offset)
            magic = self.file.read(4)
            if not magic:
                raise ValueError("not an MPQ archive")
            if magic == b"MPQ\x1a":
                return offset
            offset += 512

    def _block(self, name):
        count = len(self.hashes)
        start = mpq_hash(name, HASH_OFFSET) % count
        a, b = mpq_hash(name, HASH_A), mpq_hash(name, HASH_B)
        found = None
        for i in range(count):
            hash_a, hash_b, locale, _, block = self.hashes[(start + i) % count]
            if block == 0xFFFFFFFF:
                break
            if hash_a == a and hash_b == b and block < len(self.blocks):
                if locale == 0:
                    return self.blocks[block]
                found = found or self.blocks[block]
        return found

    def read(self, name):
        block = self._block(name)
        if not block:
            return None
        offset, packed_size, size, flags = block
        if not flags & self.FLAG_EXISTS or flags & self.FLAG_DELETED:
            return None
        if size == 0:
            return b""

        key = 0
        if flags & self.FLAG_ENCRYPTED:
            key = mpq_hash(name.replace("/", "\\").rsplit("\\", 1)[-1], HASH_KEY)
            if flags & self.FLAG_FIX_KEY:
                key = ((key + offset) ^ size) & 0xFFFFFFFF

        self.file.seek(self.base + offset)
        raw = self.file.read(packed_size)
        packed = flags & (self.FLAG_COMPRESS | self.FLAG_IMPLODE)

        if flags & self.FLAG_SINGLE_UNIT:
            if key:
                raw = mpq_decrypt(raw, key)
            if packed and packed_size < size:
                return decompress(raw, size) if flags & self.FLAG_COMPRESS else explode(raw)
            return raw[:size]

        sectors = (size + self.sector_size - 1) // self.sector_size
        if packed:
            table_size = (sectors + 1) * 4
            table = raw[:table_size]
            if key:
                table = mpq_decrypt(table, (key - 1) & 0xFFFFFFFF)
            offsets = struct.unpack(f"<{sectors + 1}I", table)
        else:
            offsets = [min(i * self.sector_size, packed_size) for i in range(sectors + 1)]

        out = bytearray()
        for i in range(sectors):
            chunk = raw[offsets[i]:offsets[i + 1]]
            if key:
                chunk = mpq_decrypt(chunk, (key + i) & 0xFFFFFFFF)
            expected = min(self.sector_size, size - i * self.sector_size)
            if packed and len(chunk) < expected:
                chunk = decompress(chunk, expected) if flags & self.FLAG_COMPRESS else explode(chunk)
            out += chunk[:expected]
        return bytes(out)


class MpqSource:
    """The client's MPQs, newest patch first."""

    def __init__(self, data_dir):
        self.archives = []
        for path in mpq_load_order(data_dir):
            try:
                self.archives.append(MpqArchive(path))
            except (OSError, ValueError, struct.error) as e:
                print(f"warning: skipping {path}: {e}", file=sys.stderr)
        self.archives.reverse()  # later archives override earlier ones
        if not self.archives:
            sys.exit(f"No MPQs found in {data_dir}")
        print(f"Reading {len(self.archives)} MPQs from {data_dir}")

    def read(self, name):
        for archive in self.archives:
            try:
                data = archive.read(name)
            except (ValueError, zlib.error, OSError) as e:
                print(f"warning: can't read {name}: {e}", file=sys.stderr)
                continue
            if data is not None:
                return data
        return None


def mpq_load_order(data_dir):
    """The order the 3.3.5a client loads its MPQs in: base archives, locale archives, then patches."""
    base = ["common.mpq", "common-2.mpq", "expansion.mpq", "lichking.mpq"]
    found = {}
    locales = []
    for folder, _, names in os.walk(data_dir):
        for name in names:
            if name.lower().endswith(".mpq"):
                found[os.path.relpath(os.path.join(folder, name), data_dir).lower()] = os.path.join(folder, name)
        if folder != data_dir:
            locales.append(os.path.relpath(folder, data_dir).lower())

    def patch_key(name, loc=""):
        # patch.mpq, patch-2.mpq, patch-3.mpq, then lettered custom patches (patch-c.mpq, ...)
        stem = os.path.basename(name)[:-4]
        if loc:
            stem = stem.replace("-" + loc, "")
        suffix = stem.split("-", 1)[1] if "-" in stem else ""
        return (not suffix.isdigit() and suffix != "", suffix.rjust(3, "0"))

    order = [found[n] for n in base if n in found]
    for loc in sorted(locales):
        order += [p for n, p in sorted(found.items()) if n.startswith(loc + os.sep) and "patch" not in os.path.basename(n)]
    root_patches = [n for n in found if os.sep not in n and n.startswith("patch")]
    order += [found[n] for n in sorted(root_patches, key=patch_key)]
    for loc in sorted(locales):
        loc_patches = [n for n in found if n.startswith(loc + os.sep) and os.path.basename(n).startswith("patch")]
        order += [found[n] for n in sorted(loc_patches, key=lambda n: patch_key(n, loc))]
    return order


# --- DBC ----------------------------------------------------------------------------------

def read_dbc(data):
    """Rows of 32-bit fields (raw bytes per field) plus the string block."""
    magic, count, fields, size, string_size = struct.unpack_from("<4s4I", data)
    if magic != b"WDBC":
        raise ValueError("not a DBC file")
    rows = []
    for i in range(count):
        offset = 20 + i * size
        rows.append([data[offset + j * 4: offset + j * 4 + 4] for j in range(fields)])
    strings = data[20 + count * size: 20 + count * size + string_size]
    return rows, strings


def u32(field):
    return struct.unpack("<I", field)[0]


def i32(field):
    return struct.unpack("<i", field)[0]


def f32(field):
    return struct.unpack("<f", field)[0]


def text(strings, field):
    start = u32(field)
    return strings[start: strings.index(b"\0", start)].decode("utf-8", "replace")


def load_dbc(source, dbc_dir, name):
    if dbc_dir:
        path = os.path.join(dbc_dir, name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                return read_dbc(f.read())
    data = source.read("DBFilesClient\\" + name) if source else None
    if data is None:
        sys.exit(f"Can't find {name}: extract DBFilesClient\\{name} or pass --dbc")
    return read_dbc(data)


# --- Images -------------------------------------------------------------------------------

def open_blp(source, name):
    data = source.read(name)
    if data is None:
        return None
    return Image.open(io.BytesIO(data)).convert("RGBA")


def paste_tiles(source, canvas, folder, texture, width, height, left, top):
    """Paint a texture split into 256px tiles named <texture>1.blp, <texture>2.blp, ..."""
    cols = math.ceil(width / TILE)
    rows = math.ceil(height / TILE)
    missing = 0
    for r in range(rows):
        for c in range(cols):
            tile = open_blp(source, f"Interface\\WorldMap\\{folder}\\{texture}{r * cols + c + 1}.blp")
            if tile is None:
                missing += 1
                continue
            canvas.alpha_composite(tile, (left + c * TILE, top + r * TILE))
    return missing < cols * rows


def render(source, folder, overlays):
    canvas = Image.new("RGBA", (4 * TILE, 3 * TILE), (0, 0, 0, 255))
    if not paste_tiles(source, canvas, folder, folder, 4 * TILE, 3 * TILE, 0, 0):
        return None
    for o in overlays:
        paste_tiles(source, canvas, folder, o["texture"], o["width"], o["height"], o["left"], o["top"])
    return canvas.crop((0, 0, MAP_W, MAP_H)).convert("RGB")


# --- Main ---------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--extracted", help="folder with Interface/WorldMap (and DBFilesClient) extracted from the MPQs")
    group.add_argument("--mpq-dir", help="the client's Data folder")
    parser.add_argument("--dbc", help="folder with WorldMapArea/WorldMapOverlay/WorldMapTransforms.dbc")
    parser.add_argument("--out", default="worldmap", help="output folder (default: worldmap)")
    parser.add_argument("--quality", type=int, default=85, help="JPEG quality (default: 85)")
    args = parser.parse_args()

    source = ExtractedSource(args.extracted) if args.extracted else MpqSource(args.mpq_dir)

    areas, area_strings = load_dbc(source, args.dbc, "WorldMapArea.dbc")
    overlays, overlay_strings = load_dbc(source, args.dbc, "WorldMapOverlay.dbc")
    transforms, _ = load_dbc(source, args.dbc, "WorldMapTransforms.dbc")

    # WorldMapOverlay: ID, MapAreaID, AreaID[4], MapPointX, MapPointY, TextureName, TextureWidth,
    # TextureHeight, OffsetX, OffsetY, HitRect[4]
    overlays_by_area = {}
    for row in overlays:
        texture = text(overlay_strings, row[8])
        if not texture:
            continue
        overlays_by_area.setdefault(u32(row[1]), []).append({
            "texture": texture, "width": u32(row[9]), "height": u32(row[10]),
            "left": u32(row[11]), "top": u32(row[12]),
        })

    os.makedirs(args.out, exist_ok=True)
    index = {"width": MAP_W, "height": MAP_H, "continents": {}, "zones": {}, "transforms": []}

    # WorldMapArea: ID, MapID, AreaID, AreaName, LocLeft (y1), LocRight (y2), LocTop (x1),
    # LocBottom (x2), DisplayMapID, DefaultDungeonFloor, ParentWorldMapID
    for row in areas:
        map_id, area_id, folder = u32(row[1]), u32(row[2]), text(area_strings, row[3])
        y1, y2, x1, x2 = f32(row[4]), f32(row[5]), f32(row[6]), f32(row[7])
        display_map = i32(row[8])
        bounds = {"y1": round(y1, 3), "y2": round(y2, 3), "x1": round(x1, 3), "x2": round(x2, 3)}

        if area_id == 0:
            if map_id not in CONTINENTS:
                continue
            name, file, key, target = CONTINENTS[map_id], f"continent-{map_id}.jpg", str(map_id), "continents"
            entry = {"name": name, "file": file, **bounds}
        else:
            file, key, target = f"zone-{area_id}.jpg", str(area_id), "zones"
            entry = {"folder": folder, "file": file, "map": map_id,
                     "continent": display_map if display_map >= 0 else map_id, **bounds}

        image = render(source, folder, overlays_by_area.get(u32(row[0]), []))
        if image is None:
            continue  # no art for it in this client (old test zones and the like)
        image.save(os.path.join(args.out, file), "JPEG", quality=args.quality, optimize=True)
        index[target][key] = entry
        print(f"{file:22} {folder}")

    # WorldMapTransforms: ID, MapID, RegionMin (x, y), RegionMax (x, y), NewMapID, RegionOffset (x, y)
    # moves the Burning Crusade starting zones from map 530 onto the old continents' maps.
    for row in transforms:
        index["transforms"].append({
            "map": u32(row[1]), "minX": f32(row[2]), "minY": f32(row[3]), "maxX": f32(row[4]), "maxY": f32(row[5]),
            "newMap": u32(row[6]), "offsetX": f32(row[7]), "offsetY": f32(row[8]),
        })

    with open(os.path.join(args.out, "maps.json"), "w") as f:
        json.dump(index, f, separators=(",", ":"))

    print(f"\n{len(index['continents'])} continents and {len(index['zones'])} zones written to {args.out}")


if __name__ == "__main__":
    main()
