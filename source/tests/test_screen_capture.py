"""Real-pixel capture and immutable-artifact regression tests (no display needed)."""
import base64
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
import zlib

from framebridge_expanded.real_screen import screen_capture as screen


def geometry(**changes):
    base = screen.Geometry(width=2, height=1, virtual_width=2, virtual_height=1,
                           xoffset=0, yoffset=0, stride=8, bpp=32,
                           red=(16, 8), green=(8, 8), blue=(0, 8),
                           memory_bytes=8, driver="unit-test", byteorder="little")
    return replace(base, **changes)


def png_pixels(data):
    """Parse PNG framing/CRC and decode exact RGB or packed palette pixel rows."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    rows = b""
    size = None
    palette = None
    depth = color = None
    seen_end = False
    while offset < len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        body = data[offset + 8:offset + 8 + length]
        crc = struct.unpack(">I", data[offset + 8 + length:offset + 12 + length])[0]
        assert crc == zlib.crc32(kind + body) & 0xffffffff
        if kind == b"IHDR":
            w, h, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", body)
            assert (compression, filtering, interlace) == (0, 0, 0)
            assert (color == 2 and depth == 8) or (color == 3 and depth in (1, 2, 4, 8))
            size = (w, h)
        elif kind == b"PLTE":
            assert len(body) > 0 and len(body) % 3 == 0
            palette = [body[i:i + 3] for i in range(0, len(body), 3)]
        elif kind == b"IDAT":
            rows += body
        elif kind == b"IEND":
            assert body == b""
            seen_end = True
        offset += length + 12
    assert offset == len(data) and size and seen_end
    scan = zlib.decompress(rows)
    width, height = size
    row_bytes = width * 3 if color == 2 else (width * depth + 7) // 8
    assert len(scan) == height * (1 + row_bytes)
    pixels = bytearray()
    for y in range(height):
        start = y * (1 + row_bytes)
        assert scan[start] == 0
        row = scan[start + 1:start + 1 + row_bytes]
        if color == 2:
            pixels.extend(row)
        else:
            assert palette is not None and len(palette) <= 1 << depth
            for x in range(width):
                shift = 8 - depth * (1 + x % (8 // depth))
                index = (row[x // (8 // depth)] >> shift) & ((1 << depth) - 1)
                assert index < len(palette)
                pixels.extend(palette[index])
    return size, bytes(pixels)


class CaptureTests(unittest.TestCase):
    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(screen.CaptureError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_real_bgra_viewport_ignores_padding_and_hidden_pixels(self):
        g = geometry(width=2, height=2, virtual_width=4, virtual_height=4,
                     xoffset=1, yoffset=1, stride=20, memory_bytes=80)
        raw = bytearray(b"\xa5" * g.memory_bytes)
        expected = bytearray()
        for y in range(4):
            for x in range(4):
                r, green, b = 10 * y + x, 20 * y + x, 30 * y + x
                start = y * g.stride + x * 4
                raw[start:start + 4] = bytes((b, green, r, 255))
                if 1 <= y <= 2 and 1 <= x <= 2:
                    expected.extend((r, green, b))
        self.assertEqual(screen.decode_framebuffer(bytes(raw), g), bytes(expected))

    def test_real_big_endian_rgb24_with_row_padding(self):
        g = geometry(width=2, height=2, virtual_width=2, virtual_height=2,
                     bpp=24, stride=8, memory_bytes=16, byteorder="big")
        raw = bytes((1, 2, 3, 4, 5, 6, 0xa5, 0xa5, 7, 8, 9, 10, 11, 12, 0xa5, 0xa5))
        self.assertEqual(screen.decode_framebuffer(raw, g), bytes(range(1, 13)))

    def test_rgb565_channel_scaling(self):
        g = geometry(width=4, virtual_width=4, bpp=16, stride=8, memory_bytes=8,
                     red=(11, 5), green=(5, 6), blue=(0, 5))
        raw = struct.pack("<4H", 0xf800, 0x07e0, 0x001f, 0xffff)
        self.assertEqual(screen.decode_framebuffer(raw, g),
                         bytes((255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255)))

    def test_partial_framebuffer_is_rejected(self):
        self.assert_code("FRAMEBUFFER_READ_SHORT", screen.decode_framebuffer, b"\0" * 7, geometry())

    def test_overlapping_rgb_fields_are_rejected(self):
        self.assert_code("COLOR_LAYOUT_OVERLAP", screen.validate_geometry,
                         geometry(green=(16, 8)))

    def test_ten_bit_channels_are_rejected_instead_of_silently_quantized(self):
        self.assert_code("COLOR_LAYOUT_UNSUPPORTED", screen.decode_framebuffer, b"\xff" * 8,
                         geometry(red=(20, 10), green=(10, 10), blue=(0, 10)))

    def test_viewport_outside_virtual_screen_is_rejected(self):
        self.assert_code("VIEWPORT_INVALID", screen.validate_geometry,
                         geometry(xoffset=1))

    def test_stride_cannot_hide_missing_pixels(self):
        self.assert_code("FRAMEBUFFER_LAYOUT_INVALID", screen.validate_geometry,
                         geometry(stride=4))

    def test_png_preserves_each_pixel_without_overlays(self):
        pixels = bytes((1, 2, 3, 255, 0, 0, 0, 255, 0, 0, 0, 255))
        self.assertEqual(png_pixels(screen.encode_png(pixels, 2, 2)), ((2, 2), pixels))

    def test_png_supports_more_than_256_distinct_colors(self):
        pixels = b"".join(bytes((i % 256, i // 256, i % 113)) for i in range(1024))
        self.assertGreater(len({pixels[i:i + 3] for i in range(0, len(pixels), 3)}), 256)
        self.assertEqual(png_pixels(screen.encode_png(pixels, 32, 32)), ((32, 32), pixels))

    def assert_indexed_pixels(self, color_count, expected_depth):
        # Odd row widths exercise unused packed bits and independent row starts.
        width = color_count + (2 if color_count % 2 else 1)
        height = 2
        colors = [bytes((i, 255 - i, i * 37 % 256)) for i in range(color_count)]
        pixels = b"".join(colors[i % color_count] for i in range(width * height))
        png = screen.encode_png(pixels, width, height, prefer_indexed=True)
        self.assertEqual((png[24], png[25]), (expected_depth, 3))
        self.assertEqual(png_pixels(png), ((width, height), pixels))

    def test_palette_one_bit_storage_preserves_one_and_two_colors(self):
        for count in (1, 2):
            with self.subTest(count=count):
                self.assert_indexed_pixels(count, 1)

    def test_palette_two_bit_storage_preserves_three_and_four_colors(self):
        for count in (3, 4):
            with self.subTest(count=count):
                self.assert_indexed_pixels(count, 2)

    def test_palette_four_bit_storage_preserves_five_and_sixteen_colors(self):
        for count in (5, 16):
            with self.subTest(count=count):
                self.assert_indexed_pixels(count, 4)

    def test_palette_eight_bit_storage_preserves_seventeen_and_256_colors(self):
        for count in (17, 256):
            with self.subTest(count=count):
                self.assert_indexed_pixels(count, 8)

    def test_palette_attempt_at_257_colors_falls_back_without_pixel_loss(self):
        pixels = b"".join(bytes((i % 256, i // 256, i * 37 % 256)) for i in range(257))
        self.assertEqual(len({pixels[i:i + 3] for i in range(0, len(pixels), 3)}), 257)
        png = screen.encode_png(pixels, 257, 1, prefer_indexed=True)
        self.assertEqual((png[24], png[25]), (8, 2))
        self.assertEqual(png_pixels(png), ((257, 1), pixels))

    def test_default_rgb_format_stays_truecolor_even_for_palette_sized_input(self):
        pixels = bytes((1, 2, 3, 4, 5, 6))
        png = screen.encode_png(pixels, 2, 1)
        self.assertEqual((png[24], png[25]), (8, 2))
        self.assertEqual(png_pixels(png), ((2, 1), pixels))

    def tty_session(self, **changes):
        s = {"Id": "2", "User": "1000", "Leader": "1234", "Active": "yes", "Remote": "no",
             "Type": "tty", "Seat": "seat0", "VTNr": "1", "TTY": "tty1"}
        s.update(changes)
        return s

    def test_foreground_identifies_actual_active_vt(self):
        s = self.tty_session()
        selected = screen.choose_foreground("tty1", [s, self.tty_session(Id="3", Active="no")], "seat0")
        self.assertEqual((selected.active_vt, selected.session_id, selected.leader), ("tty1", "2", 1234))

    def test_active_graphical_session_never_uses_framebuffer_fallback(self):
        for mode in ("x11", "wayland"):
            with self.subTest(mode=mode):
                self.assert_code("GRAPHICAL_CAPTURE_REQUIRED", screen.choose_foreground,
                                 "tty1", [self.tty_session(Type=mode)], "seat0")

    def test_unmatched_vt_is_rejected(self):
        self.assert_code("FOREGROUND_SESSION_UNVERIFIED", screen.choose_foreground,
                         "tty2", [self.tty_session()], "seat0")

    def test_remote_session_does_not_bind_local_display(self):
        self.assert_code("FOREGROUND_SESSION_UNVERIFIED", screen.choose_foreground,
                         "tty1", [self.tty_session(Remote="yes")], "seat0")

    def test_foreground_switch_during_capture_is_rejected(self):
        before = screen.choose_foreground("tty1", [self.tty_session()], "seat0")
        screen.require_unchanged(before, before)
        self.assert_code("FOREGROUND_CHANGED_DURING_CAPTURE", screen.require_unchanged,
                         before, replace(before, session_id="4"))

    def x11_session(self, **changes):
        s = self.tty_session(Type="x11", User=str(os.getuid()), TTY="tty7", VTNr="7", Display=":0")
        s.update(changes)
        return s

    def test_display_selector_accepts_same_user_active_local_x11_vt(self):
        f = screen.choose_display_foreground("tty7", [self.x11_session()], "seat0")
        self.assertEqual((f.session_type, f.uid, f.display, f.vtnr), ("x11", os.getuid(), ":0", 7))

    def test_lightdm_empty_tty_keeps_verified_numeric_vt(self):
        f = screen.choose_display_foreground("tty7", [self.x11_session(TTY="")], "seat0")
        self.assertEqual((f.active_vt, f.tty, f.vtnr, f.display), ("tty7", "", 7, ":0"))

    def test_lightdm_empty_tty_never_accepts_wrong_vt_user_or_remote_session(self):
        cases = [({"VTNr": "8"}, "GRAPHICAL_VT_UNVERIFIED"),
                 ({"User": str(os.getuid() + 1)}, "X11_SESSION_IDENTITY_MISMATCH"),
                 ({"Remote": "yes"}, "FOREGROUND_SESSION_UNVERIFIED")]
        for changes, code in cases:
            with self.subTest(changes=changes):
                s = self.x11_session(TTY="", **changes)
                self.assert_code(code, screen.choose_display_foreground, "tty7", [s], "seat0")

    def test_lightdm_explicit_contradictory_tty_is_rejected(self):
        self.assert_code("GRAPHICAL_VT_UNVERIFIED", screen.choose_display_foreground,
                         "tty7", [self.x11_session(TTY="tty8")], "seat0")

    def test_lightdm_missing_tty_metadata_is_not_treated_as_empty(self):
        s = self.x11_session()
        del s["TTY"]
        self.assert_code("GRAPHICAL_VT_UNVERIFIED", screen.choose_display_foreground,
                         "tty7", [s], "seat0")

    def test_lightdm_capture_identity_rejects_vt_switch_or_session_replacement(self):
        before = screen.choose_display_foreground("tty7", [self.x11_session(TTY="")], "seat0")
        for after in [replace(before, active_vt="tty8", vtnr=8),
                      replace(before, session_id="replacement"),
                      replace(before, display=":1"), replace(before, uid=before.uid + 1)]:
            with self.subTest(after=after):
                self.assert_code("FOREGROUND_CHANGED_DURING_CAPTURE",
                                 screen.require_unchanged, before, after)

    def test_display_selector_keeps_text_vt_identity(self):
        f = screen.choose_display_foreground("tty1", [self.tty_session()], "seat0")
        self.assertEqual((f.session_type, f.display, f.active_vt), ("tty", "", "tty1"))

    def test_display_selector_refuses_wayland_instead_of_stale_fbdev(self):
        self.assert_code("WAYLAND_CAPTURE_UNSUPPORTED", screen.choose_display_foreground,
                         "tty7", [self.x11_session(Type="wayland")], "seat0")

    def test_display_selector_refuses_network_and_invalid_display_names(self):
        for display in ("host:0", "localhost:0", "tcp/localhost:0", ":0;anything", ""):
            with self.subTest(display=display):
                self.assert_code("X11_DISPLAY_UNVERIFIED", screen.choose_display_foreground,
                                 "tty7", [self.x11_session(Display=display)], "seat0")

    def test_display_selector_requires_matching_vt_and_identity(self):
        self.assert_code("GRAPHICAL_VT_UNVERIFIED", screen.choose_display_foreground,
                         "tty8", [self.x11_session()], "seat0")
        self.assert_code("X11_SESSION_IDENTITY_MISMATCH", screen.choose_display_foreground,
                         "tty7", [self.x11_session(User=str(os.getuid() + 1))], "seat0")

    def test_display_selector_requires_one_active_seat_session(self):
        self.assert_code("FOREGROUND_SESSION_UNVERIFIED", screen.choose_display_foreground,
                         "tty7", [self.x11_session(), self.x11_session(Id="3")], "seat0")

    def test_x11_color_masks_accept_contiguous_fields_only(self):
        self.assertEqual(screen._mask_field(0xff0000), (16, 8))
        self.assertEqual(screen._mask_field(0x07e0), (5, 6))
        self.assertEqual(screen._mask_field(1), (0, 1))
        for mask in (0, -1, 0b101, 0xff00ff):
            with self.subTest(mask=mask):
                self.assert_code("X11_COLOR_MASK_INVALID", screen._mask_field, mask)

    def test_root_display_manager_environment_is_never_read(self):
        f = screen.Foreground("tty7", "19", 1000, 3668, "x11", "", "seat0", 7, ":0")
        leader = MagicMock()
        leader.stat.return_value = SimpleNamespace(st_uid=0)
        with patch.object(screen, "Path") as path:
            path.return_value.__truediv__.return_value = leader
            self.assertEqual(screen._x11_leader_environment(f, ":0"), {})
        leader.__truediv__.assert_not_called()

    def test_foreign_user_session_leader_is_rejected(self):
        f = screen.Foreground("tty7", "19", 1000, 3668, "x11", "", "seat0", 7, ":0")
        leader = MagicMock()
        leader.stat.return_value = SimpleNamespace(st_uid=1001)
        with patch.object(screen, "Path") as path:
            path.return_value.__truediv__.return_value = leader
            self.assert_code("X11_LEADER_IDENTITY_MISMATCH",
                             screen._x11_leader_environment, f, ":0")
        leader.__truediv__.assert_not_called()

    def test_user_leader_context_rejects_a_different_display(self):
        f = screen.Foreground("tty7", "19", os.getuid(), 3668, "x11", "", "seat0", 7, ":0")
        leader = MagicMock()
        leader.stat.return_value = SimpleNamespace(st_uid=f.uid)
        leader.__truediv__ = Mock(return_value=SimpleNamespace(read_bytes=lambda: b"DISPLAY=:1\0"))
        with patch.object(screen, "Path") as path:
            path.return_value.__truediv__.return_value = leader
            self.assert_code("X11_DISPLAY_IDENTITY_MISMATCH",
                             screen._x11_leader_environment, f, ":0")

    def private_home_context(self, home, **changes):
        f = screen.choose_display_foreground("tty7", [self.x11_session(TTY="")], "seat0")
        f = replace(f, **changes)
        account = SimpleNamespace(pw_uid=f.uid, pw_dir=str(home))
        return f, account

    def test_lightdm_root_leader_falls_back_to_validated_same_user_authority(self):
        with tempfile.TemporaryDirectory() as home:
            authority = Path(home) / ".Xauthority"
            authority.touch(mode=0o600)
            f, account = self.private_home_context(home)
            with patch.dict(os.environ, {}, clear=True), \
                    patch.object(screen, "_x11_leader_environment", return_value={}), \
                    patch.object(screen.pwd, "getpwuid", return_value=account):
                self.assertEqual(screen._x11_context(f, None, None), (":0", str(authority)))

    def test_canonical_home_fallback_ignores_callers_home(self):
        with tempfile.TemporaryDirectory() as home:
            authority = Path(home) / ".Xauthority"
            authority.touch(mode=0o600)
            f, account = self.private_home_context(home)
            with patch.dict(os.environ, {"HOME": "/unrelated-user"}, clear=True), \
                    patch.object(screen, "_x11_leader_environment", return_value={}), \
                    patch.object(screen.pwd, "getpwuid", return_value=account):
                self.assertEqual(screen._x11_context(f, None, None)[1], str(authority))

    def test_authority_fallback_rejects_missing_world_readable_or_symlink_file(self):
        with tempfile.TemporaryDirectory() as home:
            authority = Path(home) / ".Xauthority"
            target = Path(home) / "target"
            f, account = self.private_home_context(home)
            with patch.dict(os.environ, {}, clear=True), \
                    patch.object(screen, "_x11_leader_environment", return_value={}), \
                    patch.object(screen.pwd, "getpwuid", return_value=account):
                self.assert_code("X11_AUTHORITY_UNAVAILABLE", screen._x11_context, f, None, None)
                authority.touch(mode=0o644)
                # Ensure the negative fixture stays world-readable under a private umask.
                authority.chmod(0o644)
                self.assert_code("X11_AUTHORITY_UNTRUSTED", screen._x11_context, f, None, None)
                authority.unlink()
                target.touch(mode=0o600)
                authority.symlink_to(target)
                self.assert_code("X11_AUTHORITY_UNTRUSTED", screen._x11_context, f, None, None)

    def test_authority_fallback_rejects_foreign_file_owner(self):
        with tempfile.TemporaryDirectory() as home:
            authority = Path(home) / ".Xauthority"
            authority.touch(mode=0o600)
            f, account = self.private_home_context(home, uid=os.getuid() + 1)
            with patch.dict(os.environ, {}, clear=True), \
                    patch.object(screen.os, "getuid", return_value=f.uid), \
                    patch.object(screen, "_x11_leader_environment", return_value={}), \
                    patch.object(screen.pwd, "getpwuid", return_value=account):
                self.assert_code("X11_AUTHORITY_UNTRUSTED", screen._x11_context, f, None, None)

    def test_authority_fallback_rejects_missing_account_or_relative_home(self):
        f, account = self.private_home_context("relative-home")
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(screen, "_x11_leader_environment", return_value={}):
            with patch.object(screen.pwd, "getpwuid", side_effect=KeyError):
                self.assert_code("X11_AUTHORITY_UNAVAILABLE", screen._x11_context, f, None, None)
            with patch.object(screen.pwd, "getpwuid", return_value=account):
                self.assert_code("X11_AUTHORITY_PATH_INVALID", screen._x11_context, f, None, None)

    def test_context_rejects_explicit_display_or_user_mismatch(self):
        f, _ = self.private_home_context("/unused")
        self.assert_code("X11_DISPLAY_IDENTITY_MISMATCH", screen._x11_context, f, ":1", None)
        self.assert_code("X11_SESSION_IDENTITY_MISMATCH", screen._x11_context,
                         replace(f, uid=f.uid + 1), None, None)

    def artifact(self, directory, data=b"0123456789"):
        cid = "1" * 32
        digest = hashlib.sha256(data).hexdigest()
        manifest = {"capture_id": cid, "sha256": digest, "bytes": len(data),
                    "synthetic": False, "pixel_source_verified": True}
        screen._write_new(Path(directory) / (cid + ".png"), data)
        screen._write_new(Path(directory) / (cid + ".json"), json.dumps(manifest).encode())
        return cid, digest, manifest

    def test_artifact_write_cannot_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "one.png"
            screen._write_new(path, b"first")
            self.assert_code("CAPTURE_ARTIFACT_CREATE_FAILED", screen._write_new, path, b"second")
            self.assertEqual(path.read_bytes(), b"first")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_chunks_reassemble_exact_immutable_bytes_and_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            cid, digest, _ = self.artifact(directory)
            results = [screen.read_chunk(directory, cid, i, chunk_bytes=4, expected_sha256=digest)
                       for i in range(3)]
            self.assertEqual(b"".join(base64.b64decode(p["data"], validate=True) for p in results),
                             b"0123456789")
            self.assertEqual([p["chunk_bytes"] for p in results], [4, 4, 2])
            self.assertEqual([p["more"] for p in results], [True, True, False])
            self.assertTrue(all(p["sha256"] == digest and p["total_chunks"] == 3 for p in results))

    def test_chunk_index_and_boolean_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            cid, _, _ = self.artifact(directory)
            self.assert_code("CHUNK_INDEX_OUT_OF_RANGE", screen.read_chunk, directory, cid, 1)
            self.assert_code("CHUNK_BOUNDS_INVALID", screen.read_chunk, directory, cid, True)
            self.assert_code("CHUNK_BOUNDS_INVALID", screen.read_chunk, directory, cid, 0,
                             chunk_bytes=screen.MAX_CHUNK_BYTES + 1)

    def test_capture_id_cannot_escape_artifact_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assert_code("CAPTURE_ID_INVALID", screen.read_chunk, directory, "../target", 0)

    def test_mutated_image_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            cid, _, _ = self.artifact(directory)
            (Path(directory) / (cid + ".png")).write_bytes(b"changed")
            self.assert_code("CAPTURE_SHA256_MISMATCH", screen.read_chunk, directory, cid, 0)

    def test_expected_digest_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            cid, _, _ = self.artifact(directory)
            self.assert_code("EXPECTED_SHA256_MISMATCH", screen.read_chunk, directory, cid, 0,
                             expected_sha256="0" * 64)

    def test_image_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            cid, _, _ = self.artifact(directory)
            p = Path(directory) / (cid + ".png")
            target = Path(directory) / "target.png"
            p.rename(target)
            p.symlink_to(target)
            self.assert_code("CAPTURE_ARTIFACT_UNAVAILABLE", screen.read_chunk, directory, cid, 0)

    def test_hard_linked_image_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            cid, _, _ = self.artifact(directory)
            os.link(Path(directory) / (cid + ".png"), Path(directory) / "alias.png")
            self.assert_code("CAPTURE_ARTIFACT_UNTRUSTED", screen.read_chunk, directory, cid, 0)

    def test_artifact_directory_cannot_be_a_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "real"
            target.mkdir(mode=0o700)
            link = Path(directory) / "linked"
            link.symlink_to(target, target_is_directory=True)
            self.assert_code("CAPTURE_DIRECTORY_UNTRUSTED", screen._directory, link)

    def test_synthetic_manifest_is_not_accepted_as_real_image(self):
        with tempfile.TemporaryDirectory() as directory:
            cid, _, manifest = self.artifact(directory)
            manifest["synthetic"] = True
            (Path(directory) / (cid + ".json")).write_text(json.dumps(manifest))
            self.assert_code("CAPTURE_MANIFEST_INVALID", screen.read_chunk, directory, cid, 0)

    def test_manifest_requires_json_object_not_null_or_array(self):
        for invalid in (None, [], ["capture_id"], "text", 3):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                cid, _, _ = self.artifact(directory)
                (Path(directory) / (cid + ".json")).write_text(json.dumps(invalid))
                self.assert_code("CAPTURE_MANIFEST_INVALID", screen.read_chunk, directory, cid, 0)


if __name__ == "__main__":
    unittest.main()
