# SPDX-License-Identifier: MIT
"""unity_pack: Sprites and textures: PNG decoding, sprite sheets, texture GUIDs and sorting.

Moved out of tools/unity_pack.py unchanged; unity_pack re-exports every name
here, so `unity_pack.<name>` keeps working."""

from __future__ import annotations
import hashlib
import json
import os
import pickle
import re
import sys
import math
import copy
import tools.cs2cpp as cs2cpp  # noqa: E402

import tools.cs2cpp as cs2cpp  # noqa: E402
from tools.unity_pack_common import *  # noqa: E402,F401,F403
from tools.unity_pack_physics import *  # noqa: E402,F401,F403

__all__ = [
    '_SPRITE_SHEET_CACHE',
    '_apply_sprite_sorting',
    '_attach_sprite_textures',
    '_camera_script_view_pixels',
    '_collect_textures',
    '_crop_rgba',
    '_ensure_texture_guids',
    '_gos_with_sprite',
    '_layout_parent_pixel_size',
    '_load_png_rgba',
    '_load_sprite_rgba',
    '_paeth',
    '_parse_sprite_sheet',
    '_pixels_per_unit',
    '_prefab_sprite_object_refs',
    '_sprite_border_from_meta',
]


def _camera_script_view_pixels(screen_w, screen_h, view_w, view_h):
    """Pixel size of ``Camera.rect`` after CameraScript.HandleViewSize.

    Letterboxes / pillarboxes so the camera aspect (viewSize) fits inside the
    player screen. Matches Unity's normalized viewport when clamped to [0,1].
    """
    sw = max(1, int(screen_w))
    sh = max(1, int(screen_h))
    vw = float(view_w)
    vh = float(view_h)
    if vw < 1e-6 or vh < 1e-6:
        return sw, sh
    cam_aspect = vw / vh
    screen_aspect = float(sw) / float(sh)
    # CameraScript: size = (cam/screen, min(1, screen/cam)); then clamp.
    rw = min(1.0, cam_aspect / screen_aspect)
    rh = min(1.0, screen_aspect / cam_aspect)
    return (max(1, int(round(sw * rw))), max(1, int(round(sh * rh))))


def _apply_sprite_sorting(objects, sorting_layers):
    """Resolve SpriteRenderer sorting_layer index from TagManager uniqueIDs."""
    id_to_idx = {}
    for i, L in enumerate(sorting_layers or []):
        id_to_idx[int(L["unique_id"])] = i
    for o in objects:
        sp = o.get("sprite")
        if not sp:
            continue
        lid = int(sp.get("sorting_layer_id") or 0)
        if lid in id_to_idx:
            sp["sorting_layer"] = id_to_idx[lid]
        else:
            # Fall back to authored m_SortingLayer index (clamped).
            idx = int(sp.get("sorting_layer_yaml") or 0)
            n = len(sorting_layers) if sorting_layers else 1
            if idx < 0:
                idx = 0
            if idx >= n:
                idx = n - 1
            sp["sorting_layer"] = idx
        sp["sorting_order"] = int(sp.get("sorting_order") or 0)


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _load_png_rgba(path):
    """Decode an 8-bit non-interlaced PNG to (w, h, rgba_bytes).

    Supports color types 2 (RGB) and 6 (RGBA). Used so editing a referenced
    sprite asset changes packed visuals — no invented placeholder colors.
    """
    import struct
    import zlib

    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise PackError("not a PNG: %s" % path)
    pos = 8
    w = h = None
    color_type = None
    idat = []
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos = pos + 12 + length
        if tag == b"IHDR":
            w, h, bit_depth, color_type, comp, filt, inter = struct.unpack(
                ">IIBBBBB", chunk)
            if bit_depth != 8 or inter != 0 or comp != 0 or filt != 0:
                raise PackError(
                    "unsupported PNG (need 8-bit non-interlaced): %s" % path)
            if color_type not in (2, 6):
                raise PackError(
                    "unsupported PNG color type %d (need RGB/RGBA): %s"
                    % (color_type, path))
        elif tag == b"IDAT":
            idat.append(chunk)
        elif tag == b"IEND":
            break
    if w is None or not idat:
        raise PackError("incomplete PNG: %s" % path)
    bpp = 4 if color_type == 6 else 3
    raw = zlib.decompress(b"".join(idat))
    stride = w * bpp
    expect = (stride + 1) * h
    if len(raw) < expect:
        raise PackError("PNG IDAT too short: %s" % path)
    rows = []
    prev = bytearray(stride)
    off = 0
    for _y in range(h):
        ftype = raw[off]
        off += 1
        row = bytearray(raw[off:off + stride])
        off += stride
        if ftype == 0:
            pass
        elif ftype == 1:  # Sub
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + left) & 255
        elif ftype == 2:  # Up
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 255
        elif ftype == 3:  # Average
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) // 2)) & 255
        elif ftype == 4:  # Paeth
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                up = prev[i]
                ul = prev[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + _paeth(left, up, ul)) & 255
        else:
            raise PackError("bad PNG filter %d in %s" % (ftype, path))
        rows.append(bytes(row))
        prev = row
    # PNG stores top row first; OpenGL / Unity sprite UVs treat the first
    # texel row as the bottom. Flip so authored art is not Y-mirrored.
    rows.reverse()
    if color_type == 6:
        rgba = b"".join(rows)
    else:
        out = bytearray(w * h * 4)
        i = 0
        for row in rows:
            for x in range(w):
                o = x * 3
                out[i] = row[o]
                out[i + 1] = row[o + 1]
                out[i + 2] = row[o + 2]
                out[i + 3] = 255
                i += 4
        rgba = bytes(out)
    return w, h, rgba


def _pixels_per_unit(asset_path):
    """Unity TextureImporter `spritePixelsToUnits` (Sprite.pixelsPerUnit).

    Default 100 matches Unity when the .meta omits the field.
    """
    meta = asset_path + ".meta"
    try:
        text = _read(meta)
    except IOError:
        return 100.0
    m = re.search(r"(?m)^\s*spritePixelsToUnits:\s*([0-9.]+)\s*$", text)
    if not m:
        return 100.0
    v = float(m.group(1))
    return v if v > 0.0 else 100.0


_SPRITE_SHEET_CACHE = {}


def _parse_sprite_sheet(asset_path):
    """TextureImporter.spriteSheet → {internalID: rect dict}.

    ``spriteMode: 2`` (Multiple) packs several sprites in one PNG; Image /
    SpriteRenderer ``m_Sprite: {fileID, guid}`` names a sheet entry by
    ``internalID``. Rect ``y`` is from the texture bottom (Unity).
    """
    meta = asset_path + ".meta"
    abspath = os.path.abspath(meta)
    if abspath in _SPRITE_SHEET_CACHE:
        return _SPRITE_SHEET_CACHE[abspath]
    out = {}
    try:
        text = _read(meta)
    except IOError:
        _SPRITE_SHEET_CACHE[abspath] = out
        return out
    mode_m = re.search(r"(?m)^\s*spriteMode:\s*(\d+)\s*$", text)
    mode = int(mode_m.group(1)) if mode_m else 1
    # Always index sheet entries when present — Single-mode metas may still
    # list one sprite; Multiple requires them. fileID lookup is opt-in.
    sheet = re.search(r"(?m)^\s*spriteSheet:\s*$", text)
    if not sheet:
        _SPRITE_SHEET_CACHE[abspath] = out
        return out
    body = text[sheet.end():]
    # Stop before mipmapLimit / userData / next top-level key at column 0–2.
    stop = re.search(r"(?m)^(mipmapLimitGroupName|userData|assetBundleName):",
                     body)
    if stop:
        body = body[:stop.start()]
    for m in re.finditer(
            r"(?ms)^\s{4}-\s+serializedVersion:\s*\d+\s*\n"
            r"\s+name:\s*(.*?)\n"
            r"\s+rect:\s*\n"
            r"\s+serializedVersion:\s*\d+\s*\n"
            r"\s+x:\s*([^\n]+)\s*\n"
            r"\s+y:\s*([^\n]+)\s*\n"
            r"\s+width:\s*([^\n]+)\s*\n"
            r"\s+height:\s*([^\n]+)\s*\n"
            r".*?"
            r"\s+border:\s*\{x:\s*([^,}]+),\s*y:\s*([^,}]+),"
            r"\s*z:\s*([^,}]+),\s*w:\s*([^}]+)\}"
            r".*?"
            r"\s+internalID:\s*(-?\d+)",
            body):
        iid = int(m.group(10))
        out[iid] = {
            "name": m.group(1).strip(),
            "x": float(m.group(2)),
            "y": float(m.group(3)),
            "w": float(m.group(4)),
            "h": float(m.group(5)),
            "border": (float(m.group(6)), float(m.group(7)),
                       float(m.group(8)), float(m.group(9))),
            "sprite_mode": mode,
        }
    _SPRITE_SHEET_CACHE[abspath] = out
    return out


def _crop_rgba(rgba, tw, th, x, y, cw, ch):
    """Crop RGBA bytes; Unity sprite rect ``y`` is from the texture bottom.

    ``rgba`` from ``_load_png_rgba`` is already bottom-up (row 0 = texture
    bottom, same as ``_sample_rgba``). Unity's rect ``y`` is that same origin,
    so the crop starts at row ``y`` — do **not** convert as if the buffer were
    PNG top-down (that pulls the wrong half when the slice is shorter than the
    atlas, e.g. Sound Toggle icons padded above the sprite rect).
    """
    tw, th = int(tw), int(th)
    x0 = max(0, min(tw, int(round(x))))
    y0 = max(0, int(round(y)))
    ch_i = max(0, int(round(ch)))
    cw_i = max(0, int(round(cw)))
    if x0 + cw_i > tw:
        cw_i = tw - x0
    if y0 + ch_i > th:
        ch_i = th - y0
    if cw_i < 1 or ch_i < 1:
        return 0, 0, b""
    rows = []
    for row in range(ch_i):
        o = ((y0 + row) * tw + x0) * 4
        rows.append(rgba[o:o + cw_i * 4])
    return cw_i, ch_i, b"".join(rows)


def _load_sprite_rgba(path, file_id=None):
    """Load PNG and crop to the spriteSheet entry for ``file_id`` when set.

    ``spriteMode: Multiple`` textures share one guid; each Image/SpriteRenderer
    names a sub-rect via ``m_Sprite`` fileID (= sheet ``internalID``). Without
    the crop, every reference draws the whole atlas (e.g. Settings Menu Full
    appearing inside every small button that used a sheet slice).
    """
    w, h, rgba = _load_png_rgba(path)
    fid = int(file_id or 0)
    if fid == 0 or fid == 21300000:
        return w, h, rgba, _sprite_border_from_meta(path)
    sheet = _parse_sprite_sheet(path)
    entry = sheet.get(fid)
    if not entry:
        return w, h, rgba, _sprite_border_from_meta(path)
    cw, ch, cropped = _crop_rgba(
        rgba, w, h, entry["x"], entry["y"], entry["w"], entry["h"])
    if cw < 1 or ch < 1:
        return w, h, rgba, entry.get("border") or (0.0, 0.0, 0.0, 0.0)
    return cw, ch, cropped, entry.get("border") or (0.0, 0.0, 0.0, 0.0)


def _attach_sprite_textures(objects, asset_guids):
    """Load PNG pixels for each SpriteRenderer that references a project sprite.

    World half-extents follow Unity: (pixels / pixelsPerUnit) * scale / 2.
    Multiple-mode sheet slices are cropped by ``sprite_file_id`` (internalID).
    """
    todo = [o for o in objects if o.get("sprite")]
    n = len(todo)
    cache = {}  # (path, file_id) -> (w, h, rgba, ppu, border) or None
    if n:
        _progress("loading sprites for %d SpriteRenderer(s)" % n)
    for i, o in enumerate(todo):
        if n >= 8 and ((i + 1) % 100 == 0 or i + 1 == n):
            _progress("  sprites %d/%d (%d unique PNG(s))" % (
                i + 1, n, len(cache)))
        sp = o.get("sprite")
        if not sp:
            continue
        if sp.get("builtin") or "tex_rgba" in sp:
            if "a" not in sp:
                sp["a"] = 1.0
            continue
        path = asset_guids.get(sp.get("sprite_guid") or "")
        if not path or not path.lower().endswith(".png"):
            o["sprite"] = None
            continue
        fid = int(sp.get("sprite_file_id") or 0)
        key = (path, fid)
        if key not in cache:
            try:
                w, h, rgba, border = _load_sprite_rgba(path, fid)
                cache[key] = (w, h, rgba, _pixels_per_unit(path), border)
            except (PackError, IOError):
                cache[key] = None
        hit = cache[key]
        if hit is None:
            o["sprite"] = None
            continue
        w, h, rgba, ppu, border = hit
        sx = abs(float(sp.get("scale_x", 1.0)))
        sy = abs(float(sp.get("scale_y", 1.0)))
        sp["tex_path"] = path
        sp["tex_w"] = w
        sp["tex_h"] = h
        sp["tex_rgba"] = rgba
        sp["pixels_per_unit"] = ppu
        sp["border"] = border
        if sp.get("source") not in ("ui", "ui_tmp"):
            sp["half_w"] = (float(w) / ppu) * sx * 0.5
            sp["half_h"] = (float(h) / ppu) * sy * 0.5
        if "a" not in sp:
            sp["a"] = 1.0


def _collect_textures(objects):
    """Deduplicate sprite PNGs → plan texture table; set tex_id on sprites.

    Key is (guid, sprite_file_id) so Multiple-mode sheet slices stay distinct.
    """
    textures = []
    by_key = {}
    for o in objects:
        sp = o.get("sprite")
        if not sp or "tex_rgba" not in sp:
            continue
        g = sp["sprite_guid"]
        fid = int(sp.get("sprite_file_id") or 0)
        key = (g, fid)
        if key not in by_key:
            by_key[key] = len(textures)
            textures.append({
                "guid": g,
                "file_id": fid,
                "path": sp["tex_path"],
                "w": sp["tex_w"],
                "h": sp["tex_h"],
                "rgba": sp["tex_rgba"],
                "ppu": float(sp.get("pixels_per_unit") or 100.0),
            })
        sp["tex_id"] = by_key[key]
    return textures


def _ensure_texture_guids(textures, guids, asset_guids):
    """Load PNGs for animation-only sprite guids into the texture table.

    Idle.anim swaps to Eyes Closed which may not be any SpriteRenderer's
    initial m_Sprite — still must pack those texels.
    """
    by_key = {(t["guid"], int(t.get("file_id") or 0)): i
              for i, t in enumerate(textures)}
    # Also index plain guid → first tex for anim lookups that omit fileID.
    by_guid = {}
    for i, t in enumerate(textures):
        by_guid.setdefault(t["guid"], i)
    cache = {}
    for raw in guids or []:
        g = (raw or "").lower()
        if not g or g in by_guid:
            continue
        path = (asset_guids or {}).get(g)
        if not path or not path.lower().endswith(".png"):
            continue
        if path not in cache:
            try:
                w, h, rgba, _border = _load_sprite_rgba(path, 0)
                cache[path] = (w, h, rgba, _pixels_per_unit(path))
            except (PackError, IOError):
                cache[path] = None
        hit = cache[path]
        if hit is None:
            continue
        w, h, rgba, ppu = hit
        idx = len(textures)
        by_key[(g, 0)] = idx
        by_guid[g] = idx
        textures.append({
            "guid": g,
            "file_id": 0,
            "path": path,
            "w": w,
            "h": h,
            "rgba": rgba,
            "ppu": float(ppu),
        })
    return by_guid


def _layout_parent_pixel_size(obj, by_xf, screen_w, screen_h):
    """Parent RectTransform.rect size (pre-localScale) for layout fitters."""
    fid = obj.get("father_id")
    parent = by_xf.get(str(fid)) if fid else None
    if parent is None:
        return (float(screen_w), float(screen_h))
    cache = {}
    pw, ph = _ui_local_rect_wh(
        parent, by_xf, screen_w, screen_h, cache)
    return (abs(pw), abs(ph))


def _sprite_border_from_meta(path):
    """PNG .meta spriteBorder {x,y,z,w} → (left, bottom, right, top)."""
    meta = path + ".meta"
    if not os.path.isfile(meta):
        return (0.0, 0.0, 0.0, 0.0)
    text = _read(meta)
    m = re.search(
        r"spriteBorder:\s*\{x:\s*([^,}]+),\s*y:\s*([^,}]+),"
        r"\s*z:\s*([^,}]+),\s*w:\s*([^}]+)\}",
        text)
    if not m:
        return (0.0, 0.0, 0.0, 0.0)
    return (float(m.group(1)), float(m.group(2)),
            float(m.group(3)), float(m.group(4)))


def _prefab_sprite_object_refs(inst_raw):
    """(target_mb_file_id, sprite_file_id, sprite_guid) for m_Sprite mods."""
    out = []
    if not inst_raw:
        return out
    for m in re.finditer(
            r"target:\s*\{fileID:\s*(-?\d+),[^}]*\}\s*\n"
            r"\s*propertyPath:\s*m_Sprite\s*\n"
            r"\s*value:\s*[^\n]*\s*\n"
            r"\s*objectReference:\s*\{fileID:\s*(-?\d+)"
            r"(?:,\s*guid:\s*([0-9a-fA-F]+))?",
            inst_raw):
        spr_fid = int(m.group(2))
        if spr_fid == 0:
            continue
        sg = m.group(3).lower() if m.group(3) else None
        out.append((m.group(1), spr_fid, sg))
    return out


def _gos_with_sprite(plan):
    """Authored GO indices that already have a SpriteRenderer.

    Call after ``_build_go_tables`` so ``go_index`` is stamped. Indices (not
    display names) so duplicate UI names do not share sprite presence.
    """
    idxs = set()
    for cl in (plan.get("classes") or {}).values():
        for o in cl.get("instances") or []:
            if not o.get("sprite"):
                continue
            gi = o.get("go_index")
            if gi is None:
                continue
            idxs.add(int(gi))
    return idxs
