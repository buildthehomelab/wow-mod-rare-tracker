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
    --extracted DIR   a folder holding Interface/WorldMap/... and DBFilesClient/... as extracted
                      from the client's MPQs (e.g. with Ladik's MPQ Editor: open the client's Data
                      folder, extract Interface\\WorldMap and the three DBC files below)
    --mpq-dir DIR     the client's Data folder; the MPQs are read with StormLib (set STORMLIB if
                      libstorm isn't /usr/local/lib/libstorm.dylib)

DBC files used: WorldMapArea.dbc, WorldMapOverlay.dbc and WorldMapTransforms.dbc. The client's
copies are best; --dbc DIR reads them from another folder, such as the server's data/dbc.

Needs Pillow:  python3 -m pip install pillow
"""

import argparse
import ctypes
import io
import json
import math
import os
import struct
import sys

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


class MpqSource:
    """The client's MPQs, newest patch first, read through StormLib."""

    def __init__(self, data_dir):
        lib = ctypes.CDLL(os.environ.get("STORMLIB", "/usr/local/lib/libstorm.dylib"))
        handle = ctypes.c_void_p
        lib.SFileOpenArchive.argtypes = [ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(handle)]
        lib.SFileOpenFileEx.argtypes = [handle, ctypes.c_char_p, ctypes.c_uint, ctypes.POINTER(handle)]
        lib.SFileGetFileSize.argtypes = [handle, ctypes.POINTER(ctypes.c_uint)]
        lib.SFileGetFileSize.restype = ctypes.c_uint
        lib.SFileReadFile.argtypes = [handle, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p]
        lib.SFileCloseFile.argtypes = [handle]
        self.lib = lib
        self.archives = []
        for path in mpq_load_order(data_dir):
            h = handle()
            if lib.SFileOpenArchive(path.encode(), 0, 0x100, ctypes.byref(h)):  # read-only
                self.archives.append(h)
            else:
                print(f"warning: can't open {path}", file=sys.stderr)
        self.archives.reverse()  # later archives override earlier ones
        if not self.archives:
            sys.exit(f"No MPQs found in {data_dir}")

    def read(self, name):
        for archive in self.archives:
            f = ctypes.c_void_p()
            if not self.lib.SFileOpenFileEx(archive, name.encode(), 0, ctypes.byref(f)):
                continue
            size = self.lib.SFileGetFileSize(f, None)
            buf = ctypes.create_string_buffer(size)
            read = ctypes.c_uint()
            ok = self.lib.SFileReadFile(f, buf, size, ctypes.byref(read), None)
            self.lib.SFileCloseFile(f)
            if ok or read.value == size:
                return buf.raw[:read.value]
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
