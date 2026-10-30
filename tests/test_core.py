"""Tests of the Tk-free core: tool registry, built-in tools, history, load/save, plug-ins.

    python -m unittest discover -s tests -v
"""
import importlib.machinery
import importlib.util
import os
import tempfile
import unittest

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("imageforge_core", os.path.join(HERE, "..", "ImageForge.pyw"))
spec = importlib.util.spec_from_loader("imageforge_core", loader)
il = importlib.util.module_from_spec(spec)
loader.exec_module(il)


def gradient(h=40, w=60):
    x = np.linspace(0, 255, w, dtype=np.uint8)
    img = np.zeros((h, w, 3), np.uint8)
    img[:, :, 0] = x
    img[:, :, 1] = np.linspace(0, 255, h, dtype=np.uint8)[:, None]
    img[:, :, 2] = 90
    return img


class ToolTests(unittest.TestCase):
    def test_every_tool_returns_rgb_uint8_at_defaults_and_extremes(self):
        img = gradient()
        for name, t in il.TOOLS.items():
            for pick in ('default', 'lo', 'hi'):
                params = {}
                for p in t.params:
                    if p.kind in ('int', 'float') and pick != 'default':
                        params[p.key] = p.lo if pick == 'lo' else min(p.hi, 200)
                out = il.apply_tool(name, img, params)
                self.assertEqual(out.dtype, np.uint8, name)
                self.assertEqual(out.ndim, 3, name)
                self.assertEqual(out.shape[2], 3, name)

    def test_tools_do_not_modify_their_input(self):
        img = gradient()
        ref = img.copy()
        for name in il.TOOLS:
            il.apply_tool(name, img, {})
            il.apply_tool(name, img, {p.key: min(p.hi, 50) for p in il.TOOLS[name].params if p.kind in ('int', 'float')})
        self.assertTrue(np.array_equal(img, ref))

    def test_param_coerce(self):
        p = il.Param('a', 'A', 'int', 5, 0, 10)
        self.assertEqual(p.coerce(99), 10)
        self.assertEqual(p.coerce(-4.6), 0)
        self.assertEqual(p.coerce('7.4'), 7)
        self.assertEqual(p.coerce('x'), 5)
        c = il.Param('c', 'C', 'choice', 'u', choices=('u', 'v'))
        self.assertEqual(c.coerce('v'), 'v')
        self.assertEqual(c.coerce('zzz'), 'u')

    def test_brightness_contrast(self):
        img = gradient()
        self.assertIs(il.apply_tool("Brightness / contrast", img, {}), img)
        up = il.apply_tool("Brightness / contrast", img, {'brightness': 30})
        self.assertGreater(int(up.mean()), int(img.mean()))
        flat = il.apply_tool("Brightness / contrast", img, {'contrast': -100})
        self.assertLess(int(flat.max()) - int(flat.min()), 3)
        hard = il.apply_tool("Brightness / contrast", img, {'contrast': 80})
        self.assertGreater(hard.std(), img.std())

    def test_grayscale(self):
        out = il.apply_tool("Grayscale", gradient(), {})
        self.assertTrue(np.array_equal(out[:, :, 0], out[:, :, 1]))
        self.assertTrue(np.array_equal(out[:, :, 1], out[:, :, 2]))
        img = gradient()
        self.assertIs(il.apply_tool("Grayscale", img, {'strength': 0}), img)

    def test_invert_twice_is_identity(self):
        img = gradient()
        self.assertTrue(np.array_equal(il.apply_tool("Invert", il.apply_tool("Invert", img)), img))

    def test_sharpen_increases_edge_contrast_and_leaves_flat_areas(self):
        img = np.full((30, 30, 3), 100, np.uint8)
        img[:, 15:] = 160
        out = il.apply_tool("Sharpen (unsharp mask)", img, {'amount': 300, 'radius': 1.5})
        self.assertGreater(int(out[15, 15, 0]), 160 - 1)
        self.assertLess(int(out[15, 14, 0]), 100 + 1)
        self.assertEqual(int(out[15, 2, 0]), 100)
        self.assertEqual(int(out[15, 27, 0]), 160)
        self.assertIs(il.apply_tool("Sharpen (unsharp mask)", img, {'amount': 0}), img)

    def test_rotate_quarter_and_back(self):
        img = gradient()
        cw = il.apply_tool("Rotate 90 / 180", img, {'turn': '90 clockwise'})
        self.assertEqual(cw.shape[:2], (60, 40))
        back = il.apply_tool("Rotate 90 / 180", cw, {'turn': '90 counter-clockwise'})
        self.assertTrue(np.array_equal(back, img))
        self.assertTrue(cw.flags['C_CONTIGUOUS'])

    def test_rotate_free(self):
        img = gradient(40, 60)
        self.assertIs(il.apply_tool("Rotate (any angle)", img, {'angle': 0}), img)
        r90 = il.apply_tool("Rotate (any angle)", img, {'angle': 90})
        self.assertEqual(r90.shape[:2], (60, 40))
        r45 = il.apply_tool("Rotate (any angle)", img, {'angle': 45})
        self.assertGreater(r45.shape[0], 40)
        self.assertEqual(tuple(r45[0, 0]), (255, 255, 255))
        same = il.apply_tool("Rotate (any angle)", img, {'angle': 10, 'expand': False})
        self.assertEqual(same.shape, img.shape)

    def test_flip(self):
        img = gradient()
        self.assertTrue(np.array_equal(il.apply_tool("Flip", img, {'axis': 'horizontal'}), img[:, ::-1]))
        self.assertTrue(np.array_equal(il.apply_tool("Flip", img, {'axis': 'vertical'}), img[::-1]))

    def test_resize(self):
        img = gradient(40, 60)
        self.assertEqual(il.apply_tool("Resize", img, {'percent': 50})[:, :, 0].shape, (20, 30))
        self.assertEqual(il.apply_tool("Resize", img, {'width': 120}).shape[:2], (80, 120))
        self.assertEqual(il.apply_tool("Resize", img, {'height': 20}).shape[:2], (20, 30))
        self.assertEqual(il.apply_tool("Resize", img, {'width': 10, 'height': 10}).shape[:2], (10, 10))
        self.assertIs(il.apply_tool("Resize", img, {}), img)

    def test_region_limits_a_local_tool_but_not_a_geometry_tool(self):
        img = gradient()
        out = il.apply_tool("Invert", img, region=(10, 5, 20, 15))
        self.assertTrue(np.array_equal(out[5:15, 10:20], 255 - img[5:15, 10:20]))
        mask = np.ones(img.shape[:2], bool)
        mask[5:15, 10:20] = False
        self.assertTrue(np.array_equal(out[mask], img[mask]))
        rot = il.apply_tool("Rotate 90 / 180", img, {'turn': '180'}, region=(10, 5, 20, 15))
        self.assertTrue(np.array_equal(rot, img[::-1, ::-1]))

    def test_crop(self):
        img = gradient()
        c = il.crop(img, (10, 5, 30, 25))
        self.assertEqual(c.shape[:2], (20, 20))
        self.assertTrue(np.array_equal(c, img[5:25, 10:30]))
        c[0, 0] = 1
        self.assertNotEqual(int(img[5, 10, 0]), 1)               # an independent copy


class CleanTests(unittest.TestCase):
    @staticmethod
    def sheet():
        img = np.full((60, 80, 3), 255, np.uint8)
        img[20:24, 5:75] = (200, 0, 0)               # a red line
        img[40:44, 5:75] = (0, 0, 200)               # a blue line
        img[5, 5] = (0, 0, 0)                        # a one-pixel speck
        img[50:52, 60:62] = (0, 0, 0)                # a 4-pixel speck
        return img

    def test_parse_colors(self):
        self.assertEqual(il.parse_colors("#ff0000 00ff00, #12345 zz #0000FF"), [(255, 0, 0), (0, 255, 0), (0, 0, 255)])
        self.assertEqual(il.parse_colors(""), [])

    def test_erase_colour_leaves_the_other_ink(self):
        img = self.sheet()
        out = il.apply_tool("Erase / keep colours", img, {'colors': '#c80000', 'tolerance': 30})
        self.assertTrue((out[20:24, 5:75] == 255).all())
        self.assertTrue(np.array_equal(out[40:44, 5:75], img[40:44, 5:75]))
        self.assertIs(il.apply_tool("Erase / keep colours", img, {'colors': ''}), img)

    def test_keep_only_colour(self):
        img = self.sheet()
        out = il.apply_tool("Erase / keep colours", img, {'colors': '#0000c8', 'mode': 'keep only', 'tolerance': 30})
        self.assertTrue(np.array_equal(out[40:44, 5:75], img[40:44, 5:75]))
        self.assertTrue((out[20:24, 5:75] == 255).all())
        self.assertTrue((out[5, 5] == 255).all())

    def test_despeckle_removes_specks_not_lines(self):
        img = self.sheet()
        out = il.apply_tool("Despeckle", img, {'min_size': 6})
        self.assertTrue((out[5, 5] == 255).all())
        self.assertTrue((out[50:52, 60:62] == 255).all())
        self.assertTrue(np.array_equal(out[20:24, 5:75], img[20:24, 5:75]))
        self.assertTrue(np.array_equal(il.apply_tool("Despeckle", img, {'min_size': 1}), img))

    def test_flatten_makes_all_ink_black(self):
        out = il.apply_tool("Flatten ink to one colour", self.sheet(), {})
        self.assertTrue((out[20:24, 5:75] == 0).all())
        self.assertTrue((out[40:44, 5:75] == 0).all())
        self.assertTrue((out[0, 0] == 255).all())

    def test_paint_segment_round_stroke_and_region(self):
        img = np.full((40, 40, 3), 200, np.uint8)
        il.paint_segment(img, (5, 20), (30, 20), 6, (0, 0, 0))
        self.assertTrue((img[20, 5:31] == 0).all())
        self.assertTrue((img[17:24, 15] == 0).all())
        self.assertEqual(int(img[10, 15, 0]), 200)
        img2 = np.full((40, 40, 3), 200, np.uint8)
        il.paint_segment(img2, (5, 20), (30, 20), 6, (0, 0, 0), region=(10, 0, 20, 40))
        self.assertTrue((img2[20, 10:20] == 0).all())
        self.assertTrue((img2[20, :10] == 200).all() and (img2[20, 20:] == 200).all())
        il.paint_segment(img2, (-50, -50), (-40, -40), 6, (0, 0, 0))                  # outside: no error

    def test_straighten_angle_levels_a_tilted_line(self):
        self.assertAlmostEqual(il.straighten_angle((0, 0), (100, 0)), 0.0)
        for tilt, p1, p2 in ((7.0, (20, 120), (220, 120 - 200 * np.tan(np.radians(7.0)))),
                             (-4.0, (20, 50), (220, 50 + 200 * np.tan(np.radians(4.0))))):
            img = np.full((300, 300, 3), 255, np.uint8)
            import cv2
            cv2.line(img, (round(p1[0]), round(p1[1])), (round(p2[0]), round(p2[1])), (0, 0, 0), 1)
            ang = il.straighten_angle(p1, p2)
            self.assertAlmostEqual(ang, -tilt, places=3)
            rot = il.apply_tool("Rotate (any angle)", img, {'angle': ang, 'expand': False})
            ys = np.nonzero((rot < 100).all(axis=2))
            xs_left, xs_right = ys[1] < 80, ys[1] > 160
            self.assertAlmostEqual(ys[0][xs_left].mean(), ys[0][xs_right].mean(), delta=1.0)
        v = il.straighten_angle((10, 10), (11, 110))                                  # closer to vertical
        self.assertLess(abs(v), 2)
        self.assertAlmostEqual(il.straighten_angle((0, 0), (100, 0)), il.straighten_angle((100, 0), (0, 0)))


class SelectionTests(unittest.TestCase):
    B = (100, 80)

    def test_move_keeps_size_and_stops_at_the_border(self):
        self.assertEqual(il.drag_rect((10, 10, 30, 20), 'move', 5, -3, self.B), (15, 7, 35, 17))
        self.assertEqual(il.drag_rect((10, 10, 30, 20), 'move', -50, -50, self.B), (0, 0, 20, 10))
        self.assertEqual(il.drag_rect((10, 10, 30, 20), 'move', 500, 500, self.B), (80, 70, 100, 80))

    def test_resize_edges_and_corners(self):
        r = (10, 10, 30, 20)
        self.assertEqual(il.drag_rect(r, 'e', 5, 99, self.B), (10, 10, 35, 20))
        self.assertEqual(il.drag_rect(r, 'n', 99, -4, self.B), (10, 6, 30, 20))
        self.assertEqual(il.drag_rect(r, 'se', 4, 4, self.B), (10, 10, 34, 24))
        self.assertEqual(il.drag_rect(r, 'nw', -4, -4, self.B), (6, 6, 30, 20))

    def test_resize_cannot_flip_or_leave_the_image(self):
        r = (10, 10, 30, 20)
        self.assertEqual(il.drag_rect(r, 'w', 500, 0, self.B), (29, 10, 30, 20))
        self.assertEqual(il.drag_rect(r, 's', 0, -500, self.B), (10, 10, 30, 11))
        self.assertEqual(il.drag_rect(r, 'se', 500, 500, self.B), (10, 10, 100, 80))
        self.assertEqual(il.drag_rect(r, 'nw', -500, -500, self.B), (0, 0, 30, 20))

    def test_hit_handle_priority_and_zoom(self):
        r = (10, 10, 30, 20)
        z, ox, oy = 2.0, 5.0, 7.0
        sx = lambda x: x * z + ox
        sy = lambda y: y * z + oy
        self.assertEqual(il.hit_handle(r, z, ox, oy, sx(10), sy(10)), 'nw')
        self.assertEqual(il.hit_handle(r, z, ox, oy, sx(30) + 3, sy(20) - 2), 'se')
        self.assertEqual(il.hit_handle(r, z, ox, oy, sx(20), sy(10)), 'n')
        self.assertEqual(il.hit_handle(r, z, ox, oy, sx(20), sy(15)), 'move')
        self.assertIsNone(il.hit_handle(r, z, ox, oy, sx(50), sy(50)))


class HistoryTests(unittest.TestCase):
    def test_undo_redo_and_truncation(self):
        a, b, c = (np.full((2, 2, 3), v, np.uint8) for v in (1, 2, 3))
        h = il.History(a)
        self.assertFalse(h.can_undo())
        h.push("B", b)
        h.push("C", c)
        self.assertEqual(h.undo_label(), "C")
        self.assertIs(h.undo(), b)
        self.assertEqual(h.redo_label(), "C")
        self.assertIs(h.undo(), a)
        self.assertIs(h.redo(), b)
        h.push("D", a)                                        # a new edit drops the redo branch
        self.assertFalse(h.can_redo())
        self.assertIs(h.current, a)

    def test_memory_limit_drops_oldest_but_keeps_two(self):
        arr = [np.zeros((100, 100, 3), np.uint8) for _ in range(6)]
        h = il.History(arr[0], limit_bytes=arr[0].nbytes * 3)
        for i in range(1, 6):
            h.push("s%d" % i, arr[i])
        self.assertLessEqual(len(h.states), 3)
        self.assertIs(h.current, arr[5])
        tiny = il.History(arr[0], limit_bytes=1)
        tiny.push("x", arr[1])
        self.assertEqual(len(tiny.states), 2)
        self.assertTrue(tiny.can_undo())


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def p(self, name):
        return os.path.join(self.tmp.name, name)

    def test_png_roundtrip_is_lossless_and_keeps_dpi(self):
        img = gradient()
        il.save_image(img, self.p("a.png"), dpi=(300.0, 300.0))
        out, dpi = il.load_image(self.p("a.png"))
        self.assertTrue(np.array_equal(out, img))
        self.assertAlmostEqual(dpi[0], 300.0, places=0)

    def test_other_formats_write_and_read(self):
        img = gradient()
        for ext in ('.jpg', '.tif', '.bmp', '.webp'):
            il.save_image(img, self.p("a" + ext))
            out, _ = il.load_image(self.p("a" + ext))
            self.assertEqual(out.shape, img.shape, ext)
        tif, _ = il.load_image(self.p("a.tif"))
        self.assertTrue(np.array_equal(tif, img))

    def test_transparency_is_flattened_on_white_and_grey_becomes_rgb(self):
        rgba = np.zeros((4, 4, 4), np.uint8)
        rgba[:2, :, :3] = (10, 20, 30)
        rgba[:2, :, 3] = 255                                  # top half opaque, bottom half fully transparent
        Image.fromarray(rgba, 'RGBA').save(self.p("t.png"))
        out, _ = il.load_image(self.p("t.png"))
        self.assertEqual(tuple(out[0, 0]), (10, 20, 30))
        self.assertEqual(tuple(out[3, 0]), (255, 255, 255))
        Image.fromarray(np.arange(16, dtype=np.uint8).reshape(4, 4) * 16, 'L').save(self.p("g.png"))
        g, _ = il.load_image(self.p("g.png"))
        self.assertEqual(g.shape, (4, 4, 3))

    def test_16_bit_grey_is_scaled_to_8_bit(self):
        a = (np.arange(16, dtype=np.uint16).reshape(4, 4) * 4000)
        Image.fromarray(a).save(self.p("s.tif"))
        out, _ = il.load_image(self.p("s.tif"))
        self.assertEqual(out.dtype, np.uint8)
        self.assertAlmostEqual(int(out.max()), round(60000 / 65535 * 255), delta=1)      # full scale is 65535


class PluginTests(unittest.TestCase):
    def test_example_plugin_registers_a_working_tool(self):
        report = il.load_plugins()
        self.assertIn(("example_threshold.py", None), report)
        out = il.apply_tool("Threshold (black & white)", gradient(), {'level': 128})
        self.assertEqual(set(np.unique(out)), {0, 255})

    def test_broken_plugin_is_reported_and_leaves_no_tool_behind(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "bad.py"), "w") as f:
                f.write("@tool('Half registered', 'Test')\ndef t(img):\n    return img\nraise RuntimeError('boom')\n")
            with open(os.path.join(d, "good.py"), "w") as f:
                f.write("@tool('Good one', 'Test')\ndef t(img):\n    return img\n")
            with open(os.path.join(d, "_skipped.py"), "w") as f:
                f.write("raise RuntimeError('never run')\n")
            report = dict(il.load_plugins(d))
        self.assertIn("boom", report["bad.py"])
        self.assertIsNone(report["good.py"])
        self.assertNotIn("_skipped.py", report)
        self.assertNotIn("Half registered", il.TOOLS)
        self.assertIn("Good one", il.TOOLS)
        del il.TOOLS["Good one"]


if __name__ == '__main__':
    unittest.main()
