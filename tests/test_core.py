"""Tests of the Tk-free core: tool registry, built-in tools, history, load/save, plug-ins.

    python -m unittest discover -s tests -v
"""
import importlib.machinery
import importlib.util
import os
import tempfile
import unittest

import cv2
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
            if t.external:                                  # needs a program that may not be there and can take minutes: tested with a fake (AITests)
                continue
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
            if il.TOOLS[name].external:
                continue
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


class AutoCropTests(unittest.TestCase):
    @staticmethod
    def framed(bg=(255, 255, 255)):
        img = np.empty((100, 120, 3), np.uint8)
        img[:] = bg
        img[30:60, 40:90] = (10, 90, 200)
        return img

    def test_crops_to_the_content_on_white_black_and_coloured_borders(self):
        for bg in ((255, 255, 255), (0, 0, 0), (200, 180, 20)):
            out = il.apply_tool("Auto-crop borders", self.framed(bg))
            self.assertEqual(out.shape[:2], (30, 50), bg)
            self.assertTrue((out == (10, 90, 200)).all(), bg)

    def test_explicit_border_colour_and_margin(self):
        img = self.framed()
        out = il.apply_tool("Auto-crop borders", img, {'background': 'white', 'margin': 5})
        self.assertEqual(out.shape[:2], (40, 60))
        near = il.apply_tool("Auto-crop borders", img, {'margin': 500})
        self.assertIs(near, img)                                  # the margin is clamped to the whole image: no change

    def test_tolerance_ignores_faint_noise_and_all_border_is_left_alone(self):
        img = self.framed()
        img[2, 3] = (240, 240, 240)
        self.assertEqual(il.apply_tool("Auto-crop borders", img, {'tolerance': 30}).shape[:2], (30, 50))
        self.assertEqual(il.apply_tool("Auto-crop borders", img, {'tolerance': 5}).shape[:2], (58, 87))     # rows 2..59, columns 3..89
        blank = np.full((20, 20, 3), 255, np.uint8)
        self.assertIs(il.apply_tool("Auto-crop borders", blank), blank)


class JoinDashesTests(unittest.TestCase):
    @staticmethod
    def dashed(y_of, x_from=10, x_to=290, on=14, off=9, width=3, shape=(120, 300)):
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (shape[1], shape[0]), "white")
        d = ImageDraw.Draw(im)
        for a in np.arange(x_from, x_to - on, on + off):
            seg = np.arange(a, a + on, 1.0)
            d.line(list(zip(seg, y_of(seg))), fill=(20, 20, 20), width=width)
        return np.asarray(im)

    @staticmethod
    def columns_with_ink(img, lo, hi):
        ink = (img < 128).any(axis=2).any(axis=0)
        return ink[lo:hi]

    def test_straight_dashes_become_a_continuous_line(self):
        img = self.dashed(lambda x: np.full_like(x, 60.0))
        self.assertFalse(self.columns_with_ink(img, 10, 280).all())
        out, n = il.join_dashes(img, 'white', 60, 15, 50)
        self.assertGreater(n, 8)
        self.assertTrue(self.columns_with_ink(out, 10, 275).all())
        self.assertTrue(np.array_equal(il.apply_tool("Join dashed lines", img, {}), out))

    def test_curved_dashes_and_gap_limit(self):
        img = self.dashed(lambda x: 80 - 40 * np.exp(-((x - 150) / 40.0) ** 2))
        out, n = il.join_dashes(img, 'white', 60, 15, 50)
        self.assertTrue(self.columns_with_ink(out, 10, 275).all())
        same, n0 = il.join_dashes(img, 'white', 60, 3, 50)             # gaps are 9 px: nothing within 3 px
        self.assertEqual(n0, 0)
        self.assertIs(same, img)

    def test_continuous_line_and_parallel_dashed_lines_are_not_cross_joined(self):
        solid = np.full((60, 200, 3), 255, np.uint8)
        solid[30:33, 10:190] = 20
        self.assertEqual(il.join_dashes(solid, 'white', 60, 20, 50)[1], 0)
        two = np.minimum(self.dashed(lambda x: np.full_like(x, 30.0), shape=(120, 300)),
                         self.dashed(lambda x: np.full_like(x, 44.0), shape=(120, 300)))
        out, n = il.join_dashes(two, 'white', 60, 16, 50)
        rows = (out < 128).any(axis=2)
        self.assertFalse(rows[34:41].any())                            # nothing drawn between the two lines
        self.assertGreater(n, 8)

    def test_white_on_black_and_selection(self):
        img = 255 - self.dashed(lambda x: np.full_like(x, 60.0))
        out, n = il.join_dashes(img, 'black', 60, 15, 50)
        self.assertGreater(n, 8)
        ink = (out > 128).any(axis=2).any(axis=0)
        self.assertTrue(ink[10:275].all())
        part = il.apply_tool("Join dashed lines", img, {'background': 'black'}, region=(0, 0, 100, 120))
        self.assertTrue(np.array_equal(part[:, 100:], img[:, 100:]))
        self.assertFalse(np.array_equal(part[:, :100], img[:, :100]))


class TextAndMoveTests(unittest.TestCase):
    @staticmethod
    def white(h=100, w=200):
        return np.full((h, w, 3), 255, np.uint8)

    def test_text_is_drawn_where_asked_in_the_colour_asked(self):
        img = self.white()
        out = il.apply_tool("Add text", img, {'text': "Hi", 'x': 50, 'y': 30, 'size': 30, 'color': '#ff0000'})
        ys, xs = np.nonzero((out != img).any(axis=2))
        self.assertGreater(len(xs), 50)
        self.assertGreaterEqual(xs.min(), 49)
        self.assertGreaterEqual(ys.min(), 29)
        self.assertLess(xs.max(), 120)
        dark = out[(out != img).any(axis=2)]
        self.assertTrue((dark[:, 0] == 255).all())                # a red text over white keeps full red
        self.assertEqual(int(dark[:, 1].min()), 0)                # and reaches pure red in the middle of the strokes

    def test_text_options_and_edge_cases(self):
        img = self.white()
        self.assertIs(il.apply_tool("Add text", img, {'text': "   "}), img)
        self.assertIs(il.apply_tool("Add text", img, {'text': "A", 'opacity': 0}), img)
        self.assertIs(il.apply_tool("Add text", img, {'text': "A", 'x': 500, 'y': 500}), img)     # entirely outside
        part = il.apply_tool("Add text", img, {'text': "ABCD", 'x': -20, 'y': -10, 'size': 40})    # partly outside
        self.assertTrue((part != img).any())
        faint = il.apply_tool("Add text", img, {'text': "H", 'x': 10, 'y': 10, 'size': 40, 'opacity': 50})
        self.assertGreater(int(faint.min()), 100)                 # half-transparent black over white is grey
        one = il.apply_tool("Add text", img, {'text': "A", 'x': 5, 'y': 5, 'size': 20})
        two = il.apply_tool("Add text", img, {'text': "A\nA", 'x': 5, 'y': 5, 'size': 20})
        self.assertGreater(int((two != img).any(axis=2).sum()), int((one != img).any(axis=2).sum()) * 1.8)
        rot = il.apply_tool("Add text", img, {'text': "Hello", 'x': 20, 'y': 20, 'size': 20, 'angle': 90})
        r, c = np.nonzero((rot != img).any(axis=2))
        self.assertGreater(np.ptp(r), np.ptp(c))                  # turned: taller than wide

    def test_text_is_clipped_to_the_selection(self):
        img = self.white()
        out = il.apply_tool("Add text", img, {'text': "WWWW", 'x': 10, 'y': 10, 'size': 40}, region=(0, 0, 40, 100))
        self.assertTrue((out[:, 40:] == 255).all())
        self.assertTrue((out[:, :40] != 255).any())

    def test_move_region_moves_and_leaves_the_fill(self):
        img = self.white()
        img[10:20, 10:30] = (200, 0, 0)
        out, rect = il.move_region(img, (10, 10, 30, 20), 50, 30, (255, 255, 255))
        self.assertEqual(rect, (60, 40, 80, 50))
        self.assertTrue((out[40:50, 60:80] == (200, 0, 0)).all())
        self.assertTrue((out[10:20, 10:30] == 255).all())
        self.assertTrue((img[10:20, 10:30] == (200, 0, 0)).all())                 # the input is untouched

    def test_move_region_copy_overlap_clamp_and_no_move(self):
        img = self.white()
        img[10:20, 10:30] = (0, 0, 200)
        out, rect = il.move_region(img, (10, 10, 30, 20), 5, 0, (255, 255, 255), copy=True)
        self.assertEqual(rect, (15, 10, 35, 20))
        self.assertTrue((out[10:20, 10:35] == (0, 0, 200)).all())                 # the copy joins the original
        out, rect = il.move_region(img, (10, 10, 30, 20), 3, 0, (255, 255, 255))   # overlapping move keeps all pixels
        self.assertTrue((out[10:20, 13:33] == (0, 0, 200)).all())
        self.assertTrue((out[10:20, 10:13] == 255).all())
        out, rect = il.move_region(img, (10, 10, 30, 20), 999, 999, (0, 0, 0))
        self.assertEqual(rect, (180, 90, 200, 100))
        same, rect = il.move_region(img, (10, 10, 30, 20), 0, 0, (0, 0, 0))
        self.assertIs(same, img)


class LayerTests(unittest.TestCase):
    @staticmethod
    def canvas(h=40, w=60, v=255):
        return np.full((h, w, 3), v, np.uint8)

    @staticmethod
    def patch(h, w, rgb, alpha=255):
        a = np.empty((h, w, 4), np.uint8)
        a[..., :3] = rgb
        a[..., 3] = alpha
        return a

    def test_composite_without_visible_layers_returns_the_base_itself(self):
        base = self.canvas()
        self.assertIs(il.composite(base, ()), base)
        hidden = il.Layer('image', 'h', 0, 0, visible=False, rgba=self.patch(5, 5, (0, 0, 0)))
        zero = il.Layer('image', 'z', 0, 0, opacity=0, rgba=self.patch(5, 5, (0, 0, 0)))
        self.assertIs(il.composite(base, (hidden, zero)), base)

    def test_composite_places_blends_clips_and_leaves_the_base_alone(self):
        base = self.canvas()
        red = il.Layer('image', 'r', 10, 5, rgba=self.patch(8, 8, (255, 0, 0)))
        out = il.composite(base, (red,))
        self.assertTrue((out[5:13, 10:18] == (255, 0, 0)).all())
        self.assertTrue((base == 255).all())
        half = il.Layer('image', 'h', 0, 0, opacity=50, rgba=self.patch(4, 4, (0, 0, 0)))
        self.assertAlmostEqual(int(il.composite(base, (half,))[0, 0, 0]), 128, delta=1)
        soft = il.Layer('image', 's', 0, 0, rgba=self.patch(4, 4, (0, 0, 0), alpha=128))
        self.assertAlmostEqual(int(il.composite(base, (soft,))[0, 0, 0]), 127, delta=1)
        edge = il.Layer('image', 'e', -4, -4, rgba=self.patch(8, 8, (0, 0, 255)))
        out = il.composite(base, (edge,))
        self.assertTrue((out[:4, :4] == (0, 0, 255)).all() and (out[4:, :] == 255).all())
        far = il.Layer('image', 'f', 500, 500, rgba=self.patch(8, 8, (0, 0, 0)))
        self.assertTrue((il.composite(base, (far,)) == 255).all())

    def test_layer_order_is_bottom_to_top(self):
        base = self.canvas()
        a = il.Layer('image', 'a', 0, 0, rgba=self.patch(6, 6, (255, 0, 0)))
        b = il.Layer('image', 'b', 3, 3, rgba=self.patch(6, 6, (0, 255, 0)))
        self.assertEqual(tuple(il.composite(base, (a, b))[4, 4]), (0, 255, 0))
        self.assertEqual(tuple(il.composite(base, (b, a))[4, 4]), (255, 0, 0))

    def test_text_layer_stays_editable_and_rasterises_like_the_tool(self):
        params = dict(text="Hi", size=30, font='Arial', color='#ff0000', angle=0.0, opacity=100)
        L = il.Layer('text', 'Text: Hi', 20, 10, params=params)
        base = self.canvas(100, 200)
        out = il.composite(base, (L,))
        self.assertTrue(np.array_equal(out, il.apply_tool("Add text", base, dict(params, x=20, y=10))))
        bigger = L.replace(params=dict(params, size=60))
        self.assertGreater(int((il.composite(base, (bigger,)) != base).any(axis=2).sum()), int((out != base).any(axis=2).sum()))
        self.assertIsNone(il.Layer('text', 'e', 0, 0, params=dict(params, text="  ")).raster())
        self.assertIs(il.composite(base, (il.Layer('text', 'e', 0, 0, params=dict(params, text="")),)), base)
        with self.assertRaises(Exception):
            L.x = 5                                               # frozen: a change is a new object in the history

    def test_merge_layers_matches_compositing_both(self):
        base = self.canvas(40, 60, 200)
        a = il.Layer('image', 'a', 5, 5, rgba=self.patch(10, 10, (255, 0, 0)))
        b = il.Layer('image', 'b', 10, 8, opacity=60, rgba=self.patch(10, 10, (0, 0, 255), alpha=200))
        merged = il.merge_layers(a, b)
        self.assertEqual((merged.x, merged.y, merged.rgba.shape[:2]), (5, 5, (13, 15)))
        diff = np.abs(il.composite(base, (merged,)).astype(int) - il.composite(base, (a, b)).astype(int))
        self.assertLessEqual(int(diff.max()), 2)
        txt = il.Layer('text', 't', 0, 0, params=dict(text="A", size=20, font='Arial', color='#000000', angle=0.0, opacity=100))
        self.assertEqual(il.merge_layers(a, txt).kind, 'image')

    def test_shift_layers(self):
        a = il.Layer('image', 'a', 5, 6, rgba=self.patch(2, 2, (0, 0, 0)))
        (b,) = il.shift_layers((a,), -5, -6)
        self.assertEqual((b.x, b.y, a.x, a.y), (0, 0, 5, 6))

    def test_history_keeps_layers_per_state_and_counts_shared_arrays_once(self):
        base = self.canvas(100, 100)
        h = il.History(base)
        L = il.Layer('image', 'a', 0, 0, rgba=self.patch(10, 10, (0, 0, 0)))
        h.push("Add layer", base, (L,))
        self.assertEqual(h.layers, (L,))
        h.push("Other", self.canvas(100, 100, 7))                # layers None: kept
        self.assertEqual(h.layers, (L,))
        h.undo()
        h.undo()
        self.assertEqual(h.layers, ())
        h.redo()
        self.assertEqual(h.layers, (L,))
        small = il.History(base, limit_bytes=base.nbytes + 500)
        for i in range(6):
            small.push("l%d" % i, base, (L,))                    # same base and same layer pixels in every state
        self.assertEqual(len(small.states), 7)                    # shared arrays are counted once: nothing dropped

    def test_load_rgba_keeps_transparency(self):
        with tempfile.TemporaryDirectory() as d:
            a = self.patch(4, 4, (10, 20, 30))
            a[:2, :, 3] = 0
            Image.fromarray(a, 'RGBA').save(os.path.join(d, "t.png"))
            out = il.load_rgba(os.path.join(d, "t.png"))
        self.assertEqual(out.shape, (4, 4, 4))
        self.assertEqual((int(out[0, 0, 3]), int(out[3, 0, 3])), (0, 255))


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "p.ifl")
        rng = np.random.default_rng(1)
        self.base = rng.integers(0, 255, (50, 70, 3), dtype=np.uint8)
        px = rng.integers(0, 255, (12, 9, 4), dtype=np.uint8)
        self.layers = (
            il.Layer('image', "Pic", 7, -3, visible=False, opacity=40, rgba=px),
            il.Layer('text', "Text: Hi", 20, 10, opacity=80,
                     params=dict(text="Hi\nthere", size=24, font='Arial', color='#ff0000', angle=15.5, opacity=90)))

    def test_roundtrip_keeps_pixels_layers_text_and_dpi(self):
        il.save_project(self.path, self.base, self.layers, (300.0, 300.0))
        base, layers, dpi = il.load_project(self.path)
        self.assertTrue(np.array_equal(base, self.base))
        self.assertEqual(dpi, (300.0, 300.0))
        self.assertEqual(len(layers), 2)
        a, b = layers
        self.assertEqual((a.kind, a.name, a.x, a.y, a.visible, a.opacity), ('image', "Pic", 7, -3, False, 40))
        self.assertTrue(np.array_equal(a.rgba, self.layers[0].rgba))
        self.assertEqual((b.kind, b.name, b.x, b.y, b.visible, b.opacity), ('text', "Text: Hi", 20, 10, True, 80))
        self.assertEqual(b.params, self.layers[1].params)
        self.assertTrue(np.array_equal(il.composite(base, layers), il.composite(self.base, self.layers)))

    def test_no_layers_no_dpi_and_no_leftover_temp_file(self):
        il.save_project(self.path, self.base, ())
        base, layers, dpi = il.load_project(self.path)
        self.assertEqual((layers, dpi), ((), None))
        self.assertEqual(os.listdir(self.tmp.name), ["p.ifl"])

    def test_failed_save_keeps_the_previous_project(self):
        il.save_project(self.path, self.base, self.layers)
        bad = il.Layer('image', "x", 0, 0, rgba=None)
        with self.assertRaises(Exception):
            il.save_project(self.path, self.base, (bad,))
        self.assertEqual(os.listdir(self.tmp.name), ["p.ifl"])
        self.assertEqual(len(il.load_project(self.path)[1]), 2)

    def test_bad_files_give_a_plain_error(self):
        import zipfile, json
        junk = os.path.join(self.tmp.name, "junk.ifl")
        with open(junk, "wb") as f:
            f.write(b"not a zip")
        with self.assertRaises(ValueError):
            il.load_project(junk)
        il.save_project(self.path, self.base, self.layers)
        newer = os.path.join(self.tmp.name, "new.ifl")
        with zipfile.ZipFile(self.path) as zin, zipfile.ZipFile(newer, "w") as zout:
            for n in zin.namelist():
                data = zin.read(n)
                if n == "manifest.json":
                    m = json.loads(data)
                    m["version"] = 99
                    data = json.dumps(m)
                zout.writestr(n, data)
        with self.assertRaisesRegex(ValueError, "newer"):
            il.load_project(newer)
        broken = os.path.join(self.tmp.name, "broken.ifl")
        with zipfile.ZipFile(self.path) as zin, zipfile.ZipFile(broken, "w") as zout:
            for n in zin.namelist():
                if n != "layer_0.png":
                    zout.writestr(n, zin.read(n))
        with self.assertRaises(ValueError):
            il.load_project(broken)


class LayerToolTests(unittest.TestCase):
    @staticmethod
    def sprite():
        a = np.zeros((30, 40, 4), np.uint8)
        a[..., :3] = 255
        a[5:25, 5:35, 3] = 255                              # an opaque 20x30 patch inside a transparent frame
        a[10:20, 10:20, :3] = (200, 0, 0)                   # a red square in it
        return a

    def test_a_tool_changes_the_colours_and_keeps_the_alpha(self):
        a = self.sprite()
        out = il.apply_tool_to_layer("Invert", a)
        self.assertTrue(np.array_equal(out[..., 3], a[..., 3]))
        self.assertTrue(np.array_equal(out[..., :3], 255 - a[..., :3]))
        self.assertTrue((a[10:20, 10:20, :3] == (200, 0, 0)).all())              # input untouched
        self.assertIs(il.apply_tool_to_layer("Invert", a, {}, (0, 0, 0, 0)), a)    # empty region: nothing to do
        self.assertIs(il.apply_tool_to_layer("Brightness / contrast", a, {}), a)   # no change at defaults

    def test_region_is_in_layer_coordinates(self):
        L = il.Layer('image', 'l', 100, 50, rgba=self.sprite())
        self.assertEqual(il.layer_region((110, 60, 130, 80), L), (10, 10, 30, 30))
        self.assertEqual(il.layer_region((90, 40, 120, 70), L), (0, 0, 20, 20))        # clipped
        r = il.layer_region((500, 500, 600, 600), L)
        self.assertTrue(r[2] <= r[0])                                                  # no overlap: empty
        self.assertIsNone(il.layer_region(None, L))
        region = il.layer_region((110, 60, 130, 80), L)
        out = il.apply_tool_to_layer("Invert", L.rgba, {}, region)
        self.assertTrue(np.array_equal(out[10:30, 10:30, :3], 255 - L.rgba[10:30, 10:30, :3]))
        self.assertTrue(np.array_equal(out[:10], L.rgba[:10]))

    def test_erase_colour_to_transparent_makes_the_pixels_see_through(self):
        a = self.sprite()
        out = il.apply_tool_to_layer("Erase / keep colours", a, {'colors': '#c80000', 'tolerance': 30, 'fill': 'transparent'})
        self.assertTrue((out[10:20, 10:20, 3] == 0).all())
        self.assertTrue((out[5:10, 5:35, 3] == 255).all())                              # the rest keeps its alpha
        self.assertTrue((out[0, 0, 3] == 0))
        base = np.full((40, 50, 3), 90, np.uint8)
        seen = il.composite(base, (il.Layer('image', 'l', 3, 3, rgba=out),))
        self.assertTrue((seen[3 + 10:3 + 20, 3 + 10:3 + 20] == 90).all())               # the Background shows through
        white = il.apply_tool_to_layer("Erase / keep colours", a, {'colors': '#c80000', 'tolerance': 30})
        self.assertTrue((white[10:20, 10:20, 3] == 255).all() and (white[10:20, 10:20, :3] == 255).all())

    def test_transparent_on_the_background_is_white(self):
        img = np.full((20, 20, 3), 50, np.uint8)
        img[5:10, 5:10] = (200, 0, 0)
        out = il.apply_tool("Erase / keep colours", img, {'colors': '#c80000', 'tolerance': 30, 'fill': 'transparent'})
        self.assertTrue((out[5:10, 5:10] == 255).all())

    def test_paint_segment_on_four_channels_erases_alpha(self):
        a = self.sprite()
        il.paint_segment(a, (8, 15), (30, 15), 6, (0, 0, 0, 0))
        self.assertTrue((a[15, 8:31, 3] == 0).all())
        self.assertEqual(int(a[5, 20, 3]), 255)
        b = self.sprite()
        il.paint_segment(b, (8, 15), (30, 15), 6, (0, 0, 0, 0), region=(0, 0, 20, 30))
        self.assertTrue((b[15, 8:20, 3] == 0).all() and (b[15, 20:31, 3] == 255).all())


class TransformLayerTests(unittest.TestCase):
    @staticmethod
    def layer(h=30, w=50, x=100, y=60):
        a = np.zeros((h, w, 4), np.uint8)
        a[..., :3] = 255                                   # invisible pixels are WHITE: a naive resample would halo
        a[5:h - 5, 5:w - 5, :3] = (255, 0, 0)
        a[5:h - 5, 5:w - 5, 3] = 255
        return il.Layer('image', 'sprite', x, y, rgba=a)

    def test_quarter_turn_swaps_the_size_keeps_the_centre_and_four_turns_come_back_exactly(self):
        L = self.layer()
        t = il.transform_layer("Rotate 90 / 180", L, {'turn': '90 clockwise'})
        self.assertEqual(t.rgba.shape[:2], (50, 30))
        self.assertEqual((t.x + 15, t.y + 25), (L.x + 25, L.y + 15))               # same centre
        back = t
        for _ in range(3):
            back = il.transform_layer("Rotate 90 / 180", back, {'turn': '90 clockwise'})
        self.assertTrue(np.array_equal(back.rgba, L.rgba))
        self.assertEqual((back.x, back.y), (L.x, L.y))
        self.assertTrue(np.array_equal(il.transform_layer("Rotate 90 / 180", L, {'turn': '180'}).rgba, L.rgba[::-1, ::-1]))

    def test_free_rotation_has_transparent_corners_and_no_light_halo(self):
        L = self.layer()
        t = il.transform_layer("Rotate (any angle)", L, {'angle': 30.0, 'expand': True, 'fill': 'white'})
        h, w = t.rgba.shape[:2]
        self.assertGreater(h, 30)
        self.assertEqual(int(t.rgba[0, 0, 3]), 0)                                  # corners see through, 'white' ignored
        seen = t.rgba[..., 3] > 0
        self.assertLess(int(t.rgba[seen][:, 1].max()), 40)                          # green stays ~0 on every visible pixel
        self.assertGreater(int(t.rgba[seen][:, 0].min()), 215)                      # and red stays red, edges included
        self.assertAlmostEqual(t.x + w / 2.0, L.x + 25, delta=1)
        self.assertAlmostEqual(t.y + h / 2.0, L.y + 15, delta=1)
        self.assertIs(il.transform_layer("Rotate (any angle)", L, {'angle': 0.0}), L)

    def test_resize_and_flip_keep_alpha_and_the_centre(self):
        L = self.layer()
        half = il.transform_layer("Resize", L, {'percent': 50.0})
        self.assertEqual(half.rgba.shape[:2], (15, 25))
        self.assertEqual(int(half.rgba[7, 12, 3]), 255)
        self.assertEqual(tuple(half.rgba[7, 12, :3]), (255, 0, 0))
        self.assertEqual((half.x, half.y), (L.x + 12, L.y + 7))
        by_width = il.transform_layer("Resize", L, {'width': 100})
        self.assertEqual(by_width.rgba.shape[:2], (60, 100))
        self.assertIs(il.transform_layer("Resize", L, {}), L)
        f = il.transform_layer("Flip", L, {'axis': 'horizontal'})
        self.assertTrue(np.array_equal(f.rgba, L.rgba[:, ::-1]))
        self.assertTrue(np.array_equal(L.rgba, self.layer().rgba))                  # the input layer is untouched

    def test_enlarging_keeps_the_edge_soft_and_the_colour_pure(self):
        L = self.layer()
        big = il.transform_layer("Resize", L, {'percent': 300.0, 'method': 'cubic'})
        seen = big.rgba[..., 3] > 0
        self.assertLess(int(big.rgba[seen][:, 1].max()), 40)
        self.assertEqual(big.rgba.shape[:2], (90, 150))

    def test_only_the_four_geometry_tools_are_layer_capable(self):
        modes = {n: t.layer_geometry for n, t in il.TOOLS.items() if t.layer_geometry}
        self.assertEqual(modes, {"Rotate 90 / 180": 'exact', "Flip": 'exact',
                                 "Rotate (any angle)": 'resample', "Resize": 'resample'})
        self.assertTrue(all(il.TOOLS[n].geometry for n in modes))


class InpaintTests(unittest.TestCase):
    @staticmethod
    def ramp_with_line():
        img = np.zeros((40, 80, 3), np.uint8)
        img[:] = np.linspace(40, 220, 80).astype(np.uint8)[None, :, None]            # a grey ramp left to right
        clean = img.copy()
        img[18:22, :] = (200, 0, 0)                                                   # a red line across it
        return img, clean

    def test_a_line_over_a_gradient_fades_into_it(self):
        img, clean = self.ramp_with_line()
        out = il.apply_tool("Erase / keep colours", img,
                            {'colors': '#c80000', 'tolerance': 30, 'fill': 'inpaint', 'radius': 3})
        self.assertFalse((out == (200, 0, 0)).all(axis=2).any())
        err = np.abs(out[18:22].astype(int) - clean[18:22].astype(int))
        self.assertLess(float(err.mean()), 3.0)                                       # back to (almost) the original ramp
        self.assertTrue(np.array_equal(out[:18], img[:18]) and np.array_equal(out[22:], img[22:]))
        flat = il.apply_tool("Erase / keep colours", img, {'colors': '#c80000', 'tolerance': 30, 'fill': 'white'})
        self.assertTrue((flat[18:22] == 255).all())                                   # the flat fills are as they were

    def test_grow_widens_the_erased_area_and_catches_the_pale_edge(self):
        img, clean = self.ramp_with_line()
        img[17] = (120, 40, 60)                                                       # a pale anti-aliased fringe, not matching
        img[22] = (120, 40, 60)
        plain = il.apply_tool("Erase / keep colours", img, {'colors': '#c80000', 'tolerance': 30, 'fill': 'inpaint'})
        wide = il.apply_tool("Erase / keep colours", img,
                             {'colors': '#c80000', 'tolerance': 30, 'fill': 'inpaint', 'grow': 1})
        self.assertTrue((plain[17] != clean[17]).any())                               # the fringe is still there
        self.assertLess(int(np.abs(wide[17].astype(int) - clean[17].astype(int)).mean()), 4)
        self.assertLess(int(np.abs(wide[22].astype(int) - clean[22].astype(int)).mean()), 4)
        white = il.apply_tool("Erase / keep colours", img, {'colors': '#c80000', 'tolerance': 30, 'grow': 2})
        self.assertTrue((white[16:24] == 255).all())                                  # grow works with the flat fills too

    def test_inpaint_mask_edge_cases_and_the_input_is_not_modified(self):
        img, _ = self.ramp_with_line()
        ref = img.copy()
        none = np.zeros(img.shape[:2], bool)
        self.assertIs(il.inpaint_mask(img, none, 3), img)
        self.assertIs(il.inpaint_mask(img, np.ones(img.shape[:2], bool), 3), img)    # nothing to learn from
        hole = np.zeros(img.shape[:2], bool)
        hole[10:14, 20:30] = True
        out = il.inpaint_mask(img, hole, 3)
        self.assertTrue(np.array_equal(img, ref))
        self.assertTrue(np.array_equal(out[~hole], img[~hole]))

    def test_on_a_layer_the_invisible_pixels_do_not_leak_into_the_fill(self):
        a = np.zeros((30, 50, 4), np.uint8)
        a[..., :3] = (255, 255, 255)                                                  # invisible pixels carry white
        a[8:22, 8:42, :3] = (30, 60, 90)
        a[8:22, 8:42, 3] = 255
        a[10:14, 10:40, :3] = (200, 0, 0)                                             # a red bar touching nothing invisible
        a[14:16, 8:12, :3] = (200, 0, 0)
        out = il.apply_tool_to_layer("Erase / keep colours", a,
                                     {'colors': '#c80000', 'tolerance': 30, 'fill': 'inpaint', 'radius': 3})
        patch = out[8:22, 8:42, :3].astype(int)
        self.assertLess(int(np.abs(patch - np.array([30, 60, 90])).max()), 12)        # no white or red crept in
        self.assertTrue(np.array_equal(out[..., 3], a[..., 3]))                       # alpha untouched


class RectangleTests(unittest.TestCase):
    @staticmethod
    def pic():
        img = np.zeros((40, 80, 3), np.uint8)
        img[:] = np.linspace(40, 220, 80).astype(np.uint8)[None, :, None]
        return img

    def test_erase_rect_paints_only_the_rectangle_and_clips_to_the_image(self):
        img = self.pic()
        out = il.erase_rect(img, (10, 5, 30, 15), 'white')
        self.assertTrue((out[5:15, 10:30] == 255).all())
        self.assertTrue(np.array_equal(out[:5], img[:5]) and np.array_equal(out[:, 30:], img[:, 30:]))
        self.assertTrue(np.array_equal(img, self.pic()))                              # input untouched
        edge = il.erase_rect(img, (-10, -10, 5, 5), 'black')
        self.assertTrue((edge[:5, :5] == 0).all() and np.array_equal(edge[5:], img[5:]))
        self.assertIs(il.erase_rect(img, (200, 200, 300, 300), 'white'), img)
        self.assertIs(il.erase_rect(out, (10, 5, 30, 15), 'white'), out)              # already white: no change
        self.assertTrue((il.erase_rect(img, (0, 0, 4, 4), 'transparent')[:4, :4] == 255).all())   # no alpha: white

    def test_erase_rect_with_a_picked_colour(self):
        img = self.pic()
        out = il.erase_rect(img, (10, 5, 30, 15), 'picked colour', 3, '#336699')
        self.assertTrue((out[5:15, 10:30] == (0x33, 0x66, 0x99)).all())
        self.assertTrue(np.array_equal(out[:5], img[:5]))
        self.assertTrue((il.erase_rect(img, (0, 0, 4, 4), 'picked colour', 3, '')[:4, :4] == 255).all())      # no colour: white
        self.assertTrue((il.erase_rect(img, (0, 0, 4, 4), 'picked colour', 3, 'nonsense #102030 #405060')[:4, :4] == (16, 32, 48)).all())
        a = np.zeros((30, 50, 4), np.uint8)
        w = il.erase_rect_layer(a, (10, 10, 20, 20), 'picked colour', 3, '#ff8000')
        self.assertTrue((w[10:20, 10:20] == (255, 128, 0, 255)).all())                                   # opaque on a layer
        self.assertTrue((w[:5] == 0).all())

    def test_erase_rect_inpaint_rebuilds_from_the_surroundings(self):
        img = self.pic()
        out = il.erase_rect(img, (30, 10, 40, 20), 'inpaint', 3)
        err = np.abs(out[10:20, 30:40].astype(int) - img[10:20, 30:40].astype(int))
        self.assertLess(float(err.mean()), 4.0)                                        # a smooth ramp comes back
        self.assertTrue(np.array_equal(out[:10], img[:10]))

    def test_erase_rect_layer_transparent_opaque_and_inpaint(self):
        a = np.zeros((30, 50, 4), np.uint8)
        a[..., :3] = 255
        a[5:25, 5:45, :3] = (30, 60, 90)
        a[5:25, 5:45, 3] = 255
        t = il.erase_rect_layer(a, (10, 10, 20, 20), 'transparent')
        self.assertTrue((t[10:20, 10:20, 3] == 0).all() and (t[5:10, 5:45, 3] == 255).all())
        w = il.erase_rect_layer(a, (10, 10, 20, 20), 'white')
        self.assertTrue((w[10:20, 10:20] == 255).all())
        i = il.erase_rect_layer(a, (10, 10, 20, 20), 'inpaint', 3)
        self.assertTrue((i[10:20, 10:20, :3] == (30, 60, 90)).all() or int(np.abs(i[10:20, 10:20, :3].astype(int) - (30, 60, 90)).max()) < 12)
        self.assertTrue(np.array_equal(i[..., 3], a[..., 3]))
        self.assertIs(il.erase_rect_layer(a, (100, 100, 110, 110), 'transparent'), a)
        self.assertTrue(np.array_equal(a[10:20, 10:20, :3], np.full((10, 10, 3), (30, 60, 90))))   # input untouched

    def test_move_region_on_a_layer_leaves_transparency_and_stays_in_the_layer(self):
        a = np.zeros((30, 50, 4), np.uint8)
        a[10:20, 10:20] = (200, 0, 0, 255)
        out, rect = il.move_region(a, (10, 10, 20, 20), 15, 0, (0, 0, 0, 0))
        self.assertEqual(rect, (25, 10, 35, 20))
        self.assertTrue((out[10:20, 25:35] == (200, 0, 0, 255)).all())
        self.assertTrue((out[10:20, 10:20, 3] == 0).all())                            # the hole sees through
        far, rect = il.move_region(a, (10, 10, 20, 20), 500, 0, (0, 0, 0, 0))
        self.assertEqual(rect, (40, 10, 50, 20))                                       # stops at the layer border
        cp, _ = il.move_region(a, (10, 10, 20, 20), 15, 0, (0, 0, 0, 0), copy=True)
        self.assertTrue((cp[10:20, 10:20, 3] == 255).all() and (cp[10:20, 25:35, 3] == 255).all())


class RetouchTests(unittest.TestCase):
    @staticmethod
    def two_halves():
        img = np.zeros((60, 100, 3), np.uint8)
        img[:, :50] = (200, 30, 30)                           # left half red
        img[:, 50:] = (30, 30, 200)                           # right half blue
        return img

    def test_stamp_copies_the_source_patch_with_a_hard_edge(self):
        src = self.two_halves()
        dst = src.copy()
        ok = il.stamp_clone(dst, src, 75, 30, -50, 0, 20, hardness=100)               # copy from 50 px to the left
        self.assertTrue(ok)
        self.assertTrue((dst[30, 66:85] == (200, 30, 30)).all())                       # red in the middle of the disc
        self.assertTrue((dst[30, :50] == (200, 30, 30)).all() and (dst[30, 90:] == (30, 30, 200)).all())
        self.assertTrue((dst[5, 75] == (30, 30, 200)).all())                           # outside the disc: untouched
        self.assertTrue(np.array_equal(src, self.two_halves()))                        # the source array is not changed

    def test_soft_edge_and_opacity_blend(self):
        src = self.two_halves()
        hard = src.copy(); soft = src.copy(); half = src.copy()
        il.stamp_clone(hard, src, 75, 30, -50, 0, 30, hardness=100)
        il.stamp_clone(soft, src, 75, 30, -50, 0, 30, hardness=0)
        il.stamp_clone(half, src, 75, 30, -50, 0, 30, hardness=100, opacity=50)
        self.assertEqual(tuple(hard[30, 62]), (200, 30, 30))                           # inside the hard disc, near its edge
        self.assertNotEqual(tuple(soft[30, 62]), (200, 30, 30))                        # soft: already fading there
        self.assertEqual(tuple(soft[30, 75]), (200, 30, 30))                           # but full strength at the centre
        self.assertEqual(tuple(half[30, 75]), (115, 30, 115))                          # half red, half blue

    def test_stamp_is_clipped_to_the_image_the_source_and_the_region(self):
        src = self.two_halves()
        dst = src.copy()
        self.assertFalse(il.stamp_clone(dst, src, 75, 30, 500, 0, 20))                 # the source is far outside
        self.assertTrue(np.array_equal(dst, src))
        edge = src.copy()
        self.assertTrue(il.stamp_clone(edge, src, 2, 2, 50, 0, 20))                    # half the disc is off the image: no error
        part = src.copy()
        il.stamp_clone(part, src, 75, 30, -50, 0, 30, region=(70, 0, 100, 60))
        self.assertTrue((part[30, 62:70] == (30, 30, 200)).all() and (part[30, 70:85] == (200, 30, 30)).all())

    def test_stamp_on_four_channels_copies_alpha_too(self):
        src = np.zeros((20, 40, 4), np.uint8)
        src[:, :20] = (10, 20, 30, 255)
        dst = src.copy()
        il.stamp_clone(dst, src, 30, 10, -20, 0, 10)
        self.assertEqual(tuple(dst[10, 30]), (10, 20, 30, 255))
        self.assertEqual(int(dst[0, 39, 3]), 0)

    def test_the_clean_brush_idea_rebuilds_a_painted_mask(self):
        img = np.zeros((40, 80, 3), np.uint8)
        img[:] = np.linspace(40, 220, 80).astype(np.uint8)[None, :, None]
        clean = img.copy()
        img[15:25, 30:36] = (255, 60, 60)                                              # what the red trace would cover
        mask = np.zeros(img.shape[:2], np.uint8)
        il.paint_segment(mask, (33, 16), (33, 24), 8, (255,))
        out = il.inpaint_mask(img, mask > 0, 3)
        self.assertLess(float(np.abs(out[15:25, 30:36].astype(int) - clean[15:25, 30:36].astype(int)).mean()), 5.0)
        self.assertTrue(((mask > 0).sum()) > 30)


class RemoveMarksTests(unittest.TestCase):
    """'Remove marks': red stars over a shaded disc with a black outline and a divider line; what lay under them must come back."""

    RED = (237, 28, 36)

    def figure(self):
        import cv2
        h, w = 140, 200
        yy, xx = np.mgrid[0:h, 0:w]
        img = np.full((h, w, 3), 255, np.uint8)
        disc = (xx - 100) ** 2 + (yy - 70) ** 2 < 55 ** 2
        shade = np.clip(120 + (xx - 45) * 0.9, 0, 255).astype(np.uint8)          # horizontal gradient inside the disc
        img[disc] = np.stack([shade, shade, np.full_like(shade, 230)], -1)[disc]
        clean = img.copy()
        cv2.circle(clean, (100, 70), 55, (30, 30, 30), 3, cv2.LINE_AA)
        cv2.line(clean, (100, 15), (100, 125), (30, 30, 30), 3, cv2.LINE_AA)
        marked = clean.copy()
        for c in ((100, 40), (62, 100)):                                           # one on the divider, one on the outline
            cv2.circle(marked, c, 9, self.RED, -1, cv2.LINE_AA)
        return clean, marked

    def run_tool(self, img, **kw):
        p = dict(colors='#%02x%02x%02x' % self.RED, tolerance=35, grow=3, max_area=1500, method='colour + outlines')
        p.update(kw)
        return il.apply_tool("Remove marks", img, p, None)

    def test_marks_gone_and_lines_continue(self):
        clean, marked = self.figure()
        out = self.run_tool(marked)
        self.assertFalse((np.abs(out.astype(int) - np.array(self.RED)).sum(axis=2) < 120).any())   # no red left, fringe included
        for (x, y) in ((100, 40), (100, 36), (100, 44)):                                       # the divider is still dark through the star
            self.assertLess(int(out[y, x].max()), 110, (x, y))
        err = np.abs(out.astype(int) - clean.astype(int)).mean(axis=2)
        self.assertLess(float(err[25:115, 80:120].mean()), 12.0)                            # close to the unmarked figure

    def test_outline_stays_between_the_two_sides(self):
        clean, marked = self.figure()
        out = self.run_tool(marked)
        win = (slice(96, 112), slice(48, 66))                                                 # the ring passes under the second star
        dark = lambda im: int((im[win].max(axis=2) < 110).sum())
        self.assertGreater(dark(out), 0.7 * dark(clean))
        self.assertLess(abs(int(out[96, 70, 0]) - int(clean[96, 70, 0])), 40)                  # the fill inside is the shaded one

    def test_corner_hidden_under_a_mark_is_restored(self):
        import cv2
        img = np.full((120, 160, 3), 255, np.uint8)
        pts = np.array([[20, 20], [80, 60], [20, 100]], np.int32)                               # a sharp tip pointing right at (80, 60)
        cv2.polylines(img, [pts], False, (30, 30, 30), 4, cv2.LINE_AA)
        clean = img.copy()
        cv2.circle(img, (76, 60), 13, self.RED, -1, cv2.LINE_AA)
        out = self.run_tool(img)
        dark = lambda im: int((im[50:71, 66:90].max(axis=2) < 110).sum())
        self.assertGreater(dark(out), 0.7 * dark(clean))

    def test_a_line_that_only_touches_the_mark_is_not_joined_through_clean_pixels(self):
        import cv2
        img = np.full((120, 160, 3), 220, np.uint8)
        cv2.line(img, (10, 100), (40, 100), (30, 30, 30), 4)                                    # two unrelated strokes either side of
        cv2.line(img, (120, 20), (150, 20), (30, 30, 30), 4)                                    # a hollow mark
        cv2.circle(img, (80, 60), 22, self.RED, 5, cv2.LINE_AA)
        out = self.run_tool(img)
        self.assertGreater(int((out[40:80, 60:100].min(axis=2) > 150).sum()), 1500)             # the inside stays plain, no dark bar drawn

    def test_bigger_shapes_are_left_alone(self):
        clean, marked = self.figure()
        marked = marked.copy()
        marked[10:40, 150:190] = self.RED                                                      # a large red block: not a mark
        out = self.run_tool(marked, max_area=200)
        self.assertTrue((out[10:40, 150:190] == np.array(self.RED)).all())

    def test_no_colour_or_no_match_is_no_change(self):
        clean, marked = self.figure()
        self.assertIs(self.run_tool(marked, colors=''), marked)
        self.assertIs(self.run_tool(clean), clean)

    def test_inpaint_method_and_input_untouched(self):
        clean, marked = self.figure()
        before = marked.copy()
        out = self.run_tool(marked, method='inpaint')
        self.assertTrue(np.array_equal(marked, before))
        self.assertFalse((np.abs(out.astype(int) - np.array(self.RED)).sum(axis=2) < 120).any())


class MaskSelectionTests(unittest.TestCase):
    """Selections that are not rectangles: magic wand, lasso, invert, grow / shrink, feather, and tools obeying the mask."""

    def picture(self):
        img = np.full((60, 80, 3), 240, np.uint8)
        img[10:30, 10:30] = (200, 40, 40)                  # a red square
        img[10:30, 50:70] = (205, 45, 45)                  # a nearly identical one, not connected to the first
        img[40:55, 10:70] = (40, 40, 200)                  # a blue bar
        return img

    def test_wand_contiguous_and_global(self):
        img = self.picture()
        near = il.wand_mask(img, 20, 20, 30, contiguous=True)
        self.assertEqual(int(near.sum()), 400)
        self.assertFalse(near[20, 60])
        both = il.wand_mask(img, 20, 20, 30, contiguous=False)
        self.assertTrue(both[20, 60] and int(both.sum()) == 800)
        self.assertEqual(int(il.wand_mask(img, 20, 20, 5, contiguous=False).sum()), 400)        # tight: only the identical one
        self.assertEqual(int(il.wand_mask(img, 999, 5, 30).sum()), 0)

    def test_polygon_mask(self):
        m = il.polygon_mask((50, 50), [(10, 10), (40, 10), (40, 40), (10, 40)])
        self.assertTrue(m[20, 20] and not m[5, 5] and not m[45, 45])
        self.assertEqual(int(il.polygon_mask((50, 50), [(1, 1), (5, 5)]).sum()), 0)

    def test_combine_invert_and_empty(self):
        shape = (60, 80)
        a = il.combine_selection(None, il.wand_mask(self.picture(), 20, 20, 30), 'replace', shape)
        self.assertEqual(tuple(a), (10, 10, 30, 30))
        b = il.combine_selection(a, il.polygon_mask(shape, [(40, 5), (75, 5), (75, 35), (40, 35)]), 'add', shape)
        self.assertEqual(tuple(b), (10, 5, 76, 36))                                       # a polygon covers its boundary pixels
        c = il.combine_selection(b, il.polygon_mask(shape, [(0, 0), (35, 0), (35, 60), (0, 60)]), 'subtract', shape)
        self.assertEqual(tuple(c), (40, 5, 76, 36))
        d = il.combine_selection(b, il.polygon_mask(shape, [(0, 0), (35, 0), (35, 60), (0, 60)]), 'intersect', shape)
        self.assertEqual(tuple(d), (10, 10, 30, 30))
        self.assertIsNone(il.combine_selection(a, il.wand_mask(self.picture(), 20, 20, 30), 'subtract', shape))
        inv = il.invert_selection(a, shape)
        self.assertEqual(float(inv.mask.sum() + a.mask.sum()), 60 * 80)
        self.assertIsNone(il.invert_selection(il.invert_selection(None, shape), shape))      # nothing selected inverts to... nothing
        sub = il.combine_selection(None, il.wand_mask(self.picture(), 20, 20, 30), 'subtract', shape)   # all but the red square
        self.assertEqual(int(sub.mask.sum()), 60 * 80 - 400)

    def test_rectangle_selection_inverts_to_a_frame(self):
        inv = il.invert_selection((10, 10, 30, 30), (60, 80))
        self.assertEqual(tuple(inv), (0, 0, 80, 60))
        self.assertEqual(float(inv.mask[20, 20]), 0.0)
        self.assertEqual(float(inv.mask[5, 5]), 1.0)

    def test_grow_shrink_feather(self):
        shape = (60, 80)
        sel = il.combine_selection(None, il.wand_mask(self.picture(), 20, 20, 30), 'replace', shape)
        grown = il.grow_selection(sel, 3, shape)
        self.assertEqual(tuple(grown), (7, 7, 33, 33))
        shrunk = il.grow_selection(sel, -3, shape)
        self.assertEqual(tuple(shrunk), (13, 13, 27, 27))
        self.assertIsNone(il.grow_selection(sel, -30, shape))
        soft = il.feather_selection(sel, 6, shape)
        self.assertTrue(0.0 < float(soft.mask[10, 20]) < 1.0)                                # the edge fades
        self.assertGreater(float(soft.mask[20, 20]), 0.9)
        self.assertGreater(soft[2] - soft[0], 20)                                           # and the box grew with it

    def test_tool_obeys_the_mask_and_keeps_the_input(self):
        img = self.picture()
        before = img.copy()
        sel = il.combine_selection(None, il.wand_mask(img, 20, 20, 30), 'replace', img.shape[:2])
        out = il.apply_tool("Invert", img, {}, sel)
        self.assertTrue(np.array_equal(img, before))
        self.assertTrue((out[10:30, 10:30] == 255 - np.array([200, 40, 40])).all())
        self.assertTrue(np.array_equal(out[10:30, 50:70], img[10:30, 50:70]))               # the other red square untouched
        self.assertTrue(np.array_equal(out[:, 30:], img[:, 30:]))
        tri = il.combine_selection(None, il.polygon_mask(img.shape[:2], [(0, 0), (79, 0), (0, 59)]), 'replace', img.shape[:2])
        out2 = il.apply_tool("Invert", img, {}, tri)
        self.assertTrue((out2[1, 1] == 255 - img[1, 1]).all() and (out2[58, 78] == img[58, 78]).all())      # inside / outside the triangle

    def test_feathered_selection_blends(self):
        img = np.full((40, 40, 3), 100, np.uint8)
        sel = il.feather_selection(il.combine_selection(None, il.polygon_mask(img.shape[:2], [(10, 10), (30, 10), (30, 30), (10, 30)]),
                                                        'replace', img.shape[:2]), 8, img.shape[:2])
        out = il.apply_tool("Invert", img, {}, sel)
        edge, centre = int(out[10, 20, 0]), int(out[20, 20, 0])
        self.assertTrue(100 < edge < centre <= 155)                                          # inverted = 155; the edge is in between

    def test_tool_on_a_layer_uses_the_mask_in_layer_coordinates(self):
        rgba = np.dstack([np.full((30, 30, 3), 50, np.uint8), np.full((30, 30), 255, np.uint8)])
        layer = il.Layer('image', "L", 20, 10, rgba=rgba)
        sel = il.combine_selection(None, il.polygon_mask((60, 80), [(25, 15), (45, 15), (45, 25), (25, 25)]), 'replace', (60, 80))
        reg = il.layer_region(sel, layer)
        out = il.apply_tool_to_layer("Invert", rgba, {}, reg)
        self.assertTrue((out[5:15, 5:25, :3] == 205).all())                                  # canvas (25..45, 15..25) -> layer (5..25, 5..15)
        self.assertTrue((out[0:4, :, :3] == 50).all() and (out[:, :, 3] == 255).all())

    def test_brush_and_clone_obey_the_mask(self):
        img = np.full((40, 40, 3), 200, np.uint8)
        sel = il.combine_selection(None, il.polygon_mask((40, 40), [(0, 0), (19, 0), (19, 39), (0, 39)]), 'replace', (40, 40))
        il.paint_segment(img, (2, 20), (38, 20), 8, (0, 0, 0), sel)
        self.assertTrue((img[20, 5] == 0).all() and (img[20, 30] == 200).all())
        src = np.full((40, 40, 3), 0, np.uint8)
        dst = np.full((40, 40, 3), 200, np.uint8)
        il.stamp_clone(dst, src, 10, 20, 0, 0, 12, 100, 100, sel)
        il.stamp_clone(dst, src, 30, 20, 0, 0, 12, 100, 100, sel)
        self.assertTrue((dst[20, 10] == 0).all() and (dst[20, 30] == 200).all())

    def test_a_mask_of_another_size_is_ignored(self):
        img = self.picture()
        sel = il.MaskRegion((0, 0, 20, 20), np.ones((10, 10), np.float32))
        out = il.apply_tool("Invert", img, {}, sel)
        self.assertTrue((out[0:20, 0:20] == 255 - img[0:20, 0:20]).all() and (out[30:, :] == img[30:, :]).all())

    def test_outline_follows_the_mask(self):
        m = np.zeros((40, 40), np.float32)
        m[10:20, 10:30] = 1.0
        lines = il.mask_outline(m)
        self.assertEqual(len(lines), 1)
        xs, ys = lines[0][:, 0], lines[0][:, 1]
        self.assertTrue(xs.min() <= 10.6 and xs.max() >= 29 and ys.min() <= 10.6 and ys.max() >= 19)


class ToneColourTests(unittest.TestCase):
    """Levels, Curves, Auto contrast, CLAHE, Even out illumination, Hue / saturation, Colour balance, White balance."""

    def ramp(self):
        x = np.linspace(0, 255, 256, dtype=np.uint8)
        return np.repeat(np.stack([x] * 3, axis=-1)[None, :, :], 8, axis=0)

    def test_defaults_change_nothing(self):
        img = gradient()
        for name in ("Levels", "Curves", "Hue / saturation", "Colour balance", "White balance"):
            self.assertIs(il.apply_tool(name, img, {}), img, name)

    def test_levels_stretch_gamma_and_channel(self):
        r = self.ramp()
        out = il.apply_tool("Levels", r, dict(in_black=50, in_white=200))
        row = out[0, :, 0]
        self.assertEqual((int(row[50]), int(row[200]), int(row[0]), int(row[255])), (0, 255, 0, 255))
        self.assertLess(abs(int(row[125]) - 128), 3)
        self.assertGreater(int(il.apply_tool("Levels", r, dict(gamma=2.0))[0, 100, 0]), 100)
        self.assertLess(int(il.apply_tool("Levels", r, dict(gamma=0.5))[0, 100, 0]), 100)
        red = il.apply_tool("Levels", r, dict(channel='Red', in_black=100))
        self.assertTrue(np.array_equal(red[..., 1:], r[..., 1:]) and int(red[0, 100, 0]) == 0)
        low = il.apply_tool("Levels", r, dict(out_black=100, out_white=150))
        self.assertEqual((int(low[0, 0, 0]), int(low[0, 255, 0])), (100, 150))

    def test_curve_goes_through_its_points_and_is_monotone(self):
        c = il.monotone_curve([0, 64, 128, 192, 255], [0, 40, 128, 220, 255])
        self.assertEqual([int(c[i]) for i in (0, 64, 128, 192, 255)], [0, 40, 128, 220, 255])
        self.assertTrue((np.diff(c.astype(int)) >= 0).all())
        flat = il.monotone_curve([0, 64, 128, 192, 255], [10, 10, 10, 10, 10])
        self.assertTrue((flat == 10).all())

    def test_curves_lighten_and_darken(self):
        r = self.ramp()
        self.assertGreater(int(il.apply_tool("Curves", r, dict(mid=160))[0, 128, 0]), 150)
        self.assertLess(int(il.apply_tool("Curves", r, dict(mid=90))[0, 128, 0]), 100)
        inv = il.apply_tool("Curves", r, dict(black=255, white=0, shadows=191, mid=127, high=63))
        self.assertLess(abs(int(inv[0, 0, 0]) - 255) + abs(int(inv[0, 255, 0])), 3)

    def test_auto_contrast_stretches(self):
        img = (np.random.RandomState(0).rand(40, 40, 3) * 60 + 80).astype(np.uint8)
        out = il.apply_tool("Auto contrast", img, dict(clip=0.5))
        self.assertGreater(int(out.max()) - int(out.min()), 240)
        cast = img.copy()
        cast[..., 0] = np.clip(cast[..., 0].astype(int) + 60, 0, 255)
        fixed = il.apply_tool("Auto contrast", cast, dict(clip=0.5, per_channel=True))
        means = fixed.reshape(-1, 3).mean(axis=0)
        self.assertLess(float(means.max() - means.min()), 12.0)
        flat = np.full((10, 10, 3), 100, np.uint8)
        self.assertIs(il.apply_tool("Auto contrast", flat, {}), flat)

    def test_clahe_raises_local_contrast(self):
        rs = np.random.RandomState(1)
        img = np.clip(110 + rs.randn(64, 64, 3) * 4, 0, 255).astype(np.uint8)
        out = il.apply_tool("Local contrast (CLAHE)", img, dict(clip_limit=4.0, tiles=4))
        self.assertEqual(out.shape, img.shape)
        self.assertGreater(float(out.std()), float(img.std()) * 1.5)

    def test_even_illumination_flattens_a_gradient(self):
        x = np.linspace(0.4, 1.0, 200, dtype=np.float32)[None, :, None]
        img = _u8 = np.clip(np.ones((100, 200, 3), np.float32) * 200 * x, 0, 255).astype(np.uint8)
        out = il.apply_tool("Even out illumination", img, dict(radius=60))
        self.assertLess(float(out.mean(axis=(0, 2)).std()), 0.4 * float(img.mean(axis=(0, 2)).std()))
        self.assertIs(il.apply_tool("Even out illumination", img, dict(strength=0)), img)

    def test_hue_saturation(self):
        red = np.full((4, 4, 3), (200, 40, 40), np.uint8)
        grey = il.apply_tool("Hue / saturation", red, dict(saturation=-100))
        self.assertLess(int(grey.max(axis=2).max() - grey.min(axis=2).min()), 4)
        cyan = il.apply_tool("Hue / saturation", red, dict(hue=180))
        self.assertTrue(int(cyan[0, 0, 0]) < int(cyan[0, 0, 1]) and int(cyan[0, 0, 0]) < int(cyan[0, 0, 2]))
        self.assertTrue((il.apply_tool("Hue / saturation", red, dict(lightness=100)) == 255).all())
        self.assertTrue((il.apply_tool("Hue / saturation", red, dict(lightness=-100)) == 0).all())

    def test_colour_balance_ranges(self):
        r = self.ramp()
        out = il.apply_tool("Colour balance", r, dict(tones='shadows', red=100))
        self.assertGreater(int(out[0, 20, 0]) - int(r[0, 20, 0]), 20)
        self.assertLess(int(out[0, 250, 0]) - int(r[0, 250, 0]), 6)
        hi = il.apply_tool("Colour balance", r, dict(tones='highlights', blue=-100))
        self.assertLess(int(hi[0, 250, 2]), 215)
        self.assertEqual(int(hi[0, 5, 2]), int(r[0, 5, 2]))

    def test_white_balance(self):
        img = np.zeros((20, 20, 3), np.uint8)
        img[...] = (150, 120, 100)
        out = il.apply_tool("White balance", img, dict(method='picked neutral', neutral='#967864'))
        self.assertLess(int(out.max() - out.min()), 3)
        half = il.apply_tool("White balance", img, dict(method='picked neutral', neutral='#967864', strength=50))
        self.assertTrue(int(half[0, 0].max() - half[0, 0].min()) < int(img[0, 0].max() - img[0, 0].min()))
        rs = np.random.RandomState(2)
        tinted = np.clip(rs.rand(30, 30, 3) * 120 + np.array([60, 20, 0]), 0, 255).astype(np.uint8)
        gw = il.apply_tool("White balance", tinted, dict(method='gray world'))
        m = gw.reshape(-1, 3).mean(axis=0)
        self.assertLess(float(m.max() - m.min()), 4.0)
        bright = il.apply_tool("White balance", tinted, dict(method='brightest areas'))
        self.assertEqual(bright.shape, tinted.shape)
        self.assertIs(il.apply_tool("White balance", img, dict(method='picked neutral', neutral='')), img)

    def test_tools_respect_a_working_area(self):
        r = self.ramp()
        out = il.apply_tool("Levels", r, dict(gamma=2.0), (0, 0, 100, 8))
        self.assertTrue(np.array_equal(out[:, 100:], r[:, 100:]) and not np.array_equal(out[:, :100], r[:, :100]))

    def test_histogram_counts(self):
        img = np.zeros((10, 10, 3), np.uint8)
        img[:5] = (255, 0, 128)
        h = il.histogram_counts(img)
        self.assertEqual(h.shape, (3, 256))
        self.assertEqual((int(h[0, 255]), int(h[0, 0]), int(h[2, 128]), int(h.sum(axis=1)[0])), (50, 50, 50, 100))
        mask = np.zeros((10, 10), bool)
        mask[:5] = True
        self.assertEqual(int(il.histogram_counts(img, mask)[0, 0]), 0)
        self.assertEqual(int(il.histogram_counts(img, np.zeros((10, 10), bool)).sum()), 0)


class ShapeLayerTests(unittest.TestCase):
    """Shapes and the scale bar as editable layers, the project file with them, and the measuring functions."""

    def shape_params(self, **kw):
        p = dict(shape='line', stroke='#ff0000', width=4, dash='solid', head=16, filled=False, fill='#ffffff', opacity=100,
                 x0=20, y0=30, x1=120, y1=30)
        p.update(kw)
        return p

    def test_line_is_a_layer_with_points_relative_to_its_box(self):
        L = il._shape_layer(self.shape_params(), None, None)
        self.assertEqual(L.kind, 'shape')
        rgba = L.raster()
        self.assertTrue((rgba[..., 3] > 0).any())
        ys, xs = np.nonzero(rgba[..., 3] > 128)
        self.assertEqual((int(xs.min()) + L.x, int(xs.max()) + L.x + 1), (18, 122))          # 20..120 plus the round ends (half the width)
        self.assertLessEqual(abs(int(ys.mean()) + L.y - 30), 1)
        self.assertTrue((rgba[ys[0], xs[0], :3] == (255, 0, 0)).all())
        boxes = il._shape_boxes(L, None)
        self.assertEqual((boxes['x0'], boxes['y0'], boxes['x1'], boxes['y1']), (20, 30, 120, 30))
        moved = L.replace(x=L.x + 50, y=L.y + 10)
        b2 = il._shape_boxes(moved, None)
        self.assertEqual((b2['x0'], b2['y0'], b2['x1'], b2['y1']), (70, 40, 170, 40))          # moving the layer moves the boxes
        again = il._shape_layer({k: v for k, v in b2.items()}, moved, None)
        self.assertEqual((again.x, again.y), (moved.x, moved.y))

    def test_degenerate_shapes_are_refused(self):
        self.assertIsNone(il._shape_layer(self.shape_params(x1=20, y1=30), None, None))
        self.assertIsNone(il._shape_layer(self.shape_params(shape='rectangle', y1=30), None, None))
        self.assertIsNone(il.render_shape('line', 5, 5, 50, 5, opacity=0))

    def test_arrow_has_a_head_wider_than_the_line(self):
        plain = il._shape_layer(self.shape_params(), None, None).raster()
        arrow = il._shape_layer(self.shape_params(shape='arrow'), None, None).raster()
        thick = lambda r: int((r[..., 3] > 128).sum(axis=0).max())
        self.assertGreater(thick(arrow), thick(plain) + 6)
        both = il._shape_layer(self.shape_params(shape='double arrow'), None, None).raster()
        cols = (both[..., 3] > 128).sum(axis=0)
        xs = np.nonzero(cols)[0]
        self.assertTrue(cols[xs.min():xs.min() + 16].max() > 8 and cols[xs.max() - 15:xs.max() + 1].max() > 8)     # a head at each end

    def test_rectangle_and_ellipse_fill_and_outline(self):
        r = il._shape_layer(self.shape_params(shape='rectangle', y1=90, filled=True, fill='#00ff00'), None, None)
        rgba = r.raster()
        cy, cx = rgba.shape[0] // 2, rgba.shape[1] // 2
        self.assertTrue((rgba[cy, cx, :3] == (0, 255, 0)).all() and rgba[cy, cx, 3] == 255)
        hollow = il._shape_layer(self.shape_params(shape='rectangle', y1=90), None, None).raster()
        self.assertEqual(int(hollow[hollow.shape[0] // 2, hollow.shape[1] // 2, 3]), 0)
        e = il._shape_layer(self.shape_params(shape='ellipse', y1=90, filled=True), None, None).raster()
        self.assertEqual(int(e[2, 2, 3]), 0)                                                 # the corner of the box is outside the ellipse
        self.assertEqual(int(e[e.shape[0] // 2, e.shape[1] // 2, 3]), 255)

    def test_dashed_line_has_gaps(self):
        solid = il._shape_layer(self.shape_params(x1=220), None, None).raster()
        dashed = il._shape_layer(self.shape_params(x1=220, dash='dashed'), None, None).raster()
        row = lambda r: (r[r.shape[0] // 2, :, 3] > 128)
        self.assertTrue(row(solid)[8:-8].all())
        self.assertGreater(int((~row(dashed)[8:-8]).sum()), 20)

    def test_opacity_and_composite(self):
        L = il._shape_layer(self.shape_params(opacity=50), None, None)
        base = np.full((80, 160, 3), 255, np.uint8)
        out = il.composite(base, (L,))
        self.assertTrue(120 < int(out[30, 70, 1]) < 140 and int(out[30, 70, 0]) == 255)         # half red over white

    def scale_params(self, **kw):
        p = dict(length=10.0, label='', label_pos='above', thickness=6, color='#000000', size=20, font='default', opacity=100,
                 units_per_px=0.0, x=30, y=40)
        p.update(kw)
        return p

    def test_scale_bar_uses_the_calibration(self):
        self.assertIsNone(il._scalebar_layer(self.scale_params(), None, None))                # no scale: nothing to draw
        L = il._scalebar_layer(self.scale_params(), None, (0.5, "um"))
        self.assertEqual(L.kind, 'scalebar')
        self.assertAlmostEqual(L.params['px_len'], 20.0)                                        # 10 um at 0.5 um / px
        self.assertEqual(L.params['text'], "10 um")
        bar_rows = (L.raster()[..., 3] > 200).sum(axis=1)
        self.assertEqual(int(bar_rows.max()), 20)                                               # the bar itself is 20 px wide
        own = il._scalebar_layer(self.scale_params(units_per_px=0.25, label='scale'), None, (0.5, "um"))
        self.assertAlmostEqual(own.params['px_len'], 40.0)
        self.assertEqual(own.params['text'], "scale")
        none = il._scalebar_layer(self.scale_params(label_pos='none'), None, (1.0, "mm"))
        self.assertEqual(none.raster().shape[0], 6 + 2)
        below = il._scalebar_layer(self.scale_params(label_pos='below'), None, (1.0, "mm"))
        above = il._scalebar_layer(self.scale_params(label_pos='above'), None, (1.0, "mm"))
        self.assertEqual(below.raster().shape, above.raster().shape)
        self.assertFalse(np.array_equal(below.raster(), above.raster()))
        edited = il._scalebar_layer(self.scale_params(length=20.0), L, (0.5, "um"))
        self.assertAlmostEqual(edited.params['px_len'], 40.0)
        self.assertEqual(il._scalebar_boxes(edited, (0.5, "um"))['length'], 20.0)

    def test_project_keeps_shapes_scale_bars_and_the_scale(self):
        base = np.full((120, 200, 3), 200, np.uint8)
        shape = il._shape_layer(self.shape_params(shape='ellipse', y1=90, filled=True, dash='dotted'), None, None)
        bar = il._scalebar_layer(self.scale_params(), None, (0.5, "um"))
        path = os.path.join(tempfile.mkdtemp(), "p.ifl")
        il.save_project(path, base, (shape, bar), None, (0.5, "um"))
        b2, layers, dpi = il.load_project(path)
        self.assertEqual([L.kind for L in layers], ['shape', 'scalebar'])
        self.assertTrue(np.array_equal(il.composite(b2, layers), il.composite(base, (shape, bar))))
        self.assertEqual(layers[0].params['dash'], 'dotted')
        self.assertEqual(il.project_scale(path), (0.5, "um"))
        il.save_project(path, base, (), None, None)
        self.assertIsNone(il.project_scale(path))
        self.assertIsNone(il.project_scale(os.path.join(os.path.dirname(path), "missing.ifl")))

    def test_vector_layers_hold_no_pixels(self):
        self.assertEqual(il.Layer('shape', "s", 0, 0, params={}).nbytes, 0)

    def test_measurements(self):
        self.assertAlmostEqual(il.polyline_length([(0, 0), (3, 4), (3, 10)]), 11.0)
        self.assertEqual(il.polyline_length([(1, 1)]), 0.0)
        self.assertAlmostEqual(il.polygon_area([(0, 0), (10, 0), (10, 5), (0, 5)]), 50.0)
        self.assertAlmostEqual(il.polygon_area([(0, 5), (10, 5), (10, 0), (0, 0)]), 50.0)
        self.assertEqual(il.polygon_area([(0, 0), (1, 1)]), 0.0)
        self.assertAlmostEqual(il.angle_between((10, 0), (0, 0), (0, 10)), 90.0)
        self.assertAlmostEqual(il.angle_between((10, 0), (0, 0), (-10, 0)), 180.0)
        self.assertEqual(il.angle_between((0, 0), (0, 0), (1, 1)), 0.0)
        self.assertEqual(il.format_measure(100, 'length'), "100.0 px")
        self.assertEqual(il.format_measure(100, 'length', (0.5, "um")), "100.0 px = 50 um")
        self.assertEqual(il.format_measure(100, 'area', (0.5, "um")), "100.0 px2 = 25 um2")
        self.assertEqual(il.format_measure(45, 'angle', (0.5, "um")), "45.0 deg")


class CleanCorrectTests(unittest.TestCase):
    """Reduce noise, Morphology, Find edges, Auto straighten, Perspective correction, Canvas size, Crop to aspect, Lens distortion."""

    def page(self, angle=0.0):
        """A light page with dark rules and blocks of 'text' lines, optionally tilted (counter-clockwise, degrees)."""
        import cv2
        img = np.full((300, 400, 3), 245, np.uint8)
        for y in range(40, 270, 22):
            cv2.line(img, (40, y), (360, y), (20, 20, 20), 3)
            for x in range(40, 340, 40):
                cv2.rectangle(img, (x + 4, y - 11), (x + 24, y - 3), (30, 30, 30), -1)
        if angle:
            m = cv2.getRotationMatrix2D((200, 150), angle, 1.0)
            img = cv2.warpAffine(img, m, (400, 300), borderValue=(245, 245, 245), flags=cv2.INTER_CUBIC)
        return img

    def test_noise_reduction_methods(self):
        rs = np.random.RandomState(3)
        clean = np.full((60, 60, 3), 120, np.uint8)
        clean[:, 30:] = 200                                                     # an edge
        noisy = np.clip(clean + rs.randn(60, 60, 3) * 14, 0, 255).astype(np.uint8)
        for method in ('median', 'bilateral', 'non-local means'):
            out = il.apply_tool("Reduce noise", noisy, dict(method=method, strength=12, radius=2))
            err_in = float(np.abs(noisy.astype(int) - clean).mean())
            err_out = float(np.abs(out.astype(int) - clean).mean())
            self.assertLess(err_out, err_in * 0.75, method)
            self.assertGreater(float(out[:, 40:].mean()) - float(out[:, :20].mean()), 60, method)     # the edge survives
        speck = clean.copy()
        speck[10, 10] = 255
        speck[40, 20] = 0
        self.assertEqual(int(il.apply_tool("Reduce noise", speck, dict(method='median', radius=1))[10, 10, 0]), 120)

    def test_morphology(self):
        img = np.full((40, 40, 3), 255, np.uint8)
        img[18:22, 5:35] = 0                                                    # a dark line 4 px thick
        thick = il.apply_tool("Morphology", img, dict(op='erode', radius=2))
        self.assertGreater(int((thick[..., 0] == 0).sum()), int((img[..., 0] == 0).sum()))      # erode grows the dark
        thin = il.apply_tool("Morphology", img, dict(op='dilate', radius=2))
        self.assertEqual(int((thin[..., 0] == 0).sum()), 0)                                    # the 4 px line is eaten by a 5 px light
        speck = np.zeros((30, 30, 3), np.uint8)
        speck[10, 10] = 255
        self.assertEqual(int(il.apply_tool("Morphology", speck, dict(op='open', radius=1)).sum()), 0)    # a light speck is opened away
        edge = il.apply_tool("Morphology", img, dict(op='outline (gradient)', radius=1))
        self.assertGreater(int(edge.sum()), 0)

    def test_find_edges(self):
        img = np.full((60, 60, 3), 255, np.uint8)
        img[20:40, 20:40] = 0
        for method in ('Canny', 'Sobel', 'Laplacian'):
            out = il.apply_tool("Find edges", img, dict(method=method, blur=0.0))
            self.assertTrue(out.shape == img.shape and out.dtype == np.uint8)
            self.assertEqual(int(out[5, 5, 0]), 255, method)                               # flat page: white
            self.assertLess(int(out[19:22, 30, 0].min()), 128, method)                      # the border of the square: dark line
        flat = np.full((20, 20, 3), 90, np.uint8)
        self.assertTrue((il.apply_tool("Find edges", flat, dict(method='Canny'))[..., 0] == 255).all())
        light = il.apply_tool("Find edges", img, dict(method='Sobel', blur=0.0, dark_on_light=False))
        self.assertEqual(int(light[5, 5, 0]), 0)

    def test_skew_estimate_and_auto_straighten(self):
        for tilt in (-4.0, 2.5, 6.0):
            est = il.estimate_skew(self.page(tilt), 10.0)
            self.assertLess(abs(est + tilt), 0.4, (tilt, est))                              # it is the rotation that levels it
        self.assertLess(abs(il.estimate_skew(self.page(0.0), 10.0)), 0.3)
        self.assertEqual(il.estimate_skew(np.full((50, 50, 3), 255, np.uint8)), 0.0)
        tilted = self.page(3.0)
        out = il.apply_tool("Auto straighten", tilted, dict(max_angle=10.0))
        self.assertIn("deg", il.TOOL_NOTES.pop("Auto straighten"))
        self.assertLess(abs(il.estimate_skew(out, 10.0)), 0.4)
        level = self.page(0.0)
        self.assertIs(il.apply_tool("Auto straighten", level, dict(max_angle=10.0)), level)
        hough = il.estimate_skew(self.page(4.0), 10.0, 'hough')
        self.assertLess(abs(hough + 4.0), 0.8)

    def test_corner_order_and_perspective(self):
        pts = [(300, 200), (20, 30), (310, 40), (10, 210)]
        q = il.order_corners(pts)
        self.assertEqual([tuple(map(int, p)) for p in q], [(20, 30), (310, 40), (300, 200), (10, 210)])
        import cv2
        img = np.full((260, 340, 3), 200, np.uint8)
        quad = np.array([(20, 30), (310, 40), (300, 200), (10, 210)], np.int32)
        cv2.fillPoly(img, [quad], (30, 120, 220))
        cv2.circle(img, (160, 120), 30, (250, 250, 20), -1)
        out = il.apply_tool("Perspective correction", img, dict(tl_x=20, tl_y=30, tr_x=310, tr_y=40, br_x=300, br_y=200, bl_x=10, bl_y=210))
        self.assertTrue(280 < out.shape[1] < 320 and 160 < out.shape[0] < 190)                 # about the lengths of the sides
        self.assertTrue((out[3:-3, 3:-3] != 200).any(axis=2).all() or float(np.abs(out[5:-5, 5:-5].astype(int) - 200).sum(axis=2).min()) > 0)
        inside = np.abs(out[4:-4, 4:-4].astype(int) - np.array([30, 120, 220])).sum(axis=2) < 40
        yellow = np.abs(out.astype(int) - np.array([250, 250, 20])).sum(axis=2) < 40
        self.assertGreater(float(inside.mean() + yellow.mean()), 0.95)                          # only the quad's content is left
        self.assertTrue(yellow[yellow.shape[0] // 2 - 5:yellow.shape[0] // 2 + 5, yellow.shape[1] // 2 - 5:yellow.shape[1] // 2 + 5].all())
        fixed = il.apply_tool("Perspective correction", img, dict(tl_x=20, tl_y=30, tr_x=310, tr_y=40, br_x=300, br_y=200, bl_x=10, bl_y=210,
                                                                   out_w=100, out_h=50))
        self.assertEqual(fixed.shape[:2], (50, 100))
        self.assertIs(il.apply_tool("Perspective correction", img, {}), img)

    def test_canvas_size(self):
        img = np.full((20, 30, 3), 100, np.uint8)
        out = il.apply_tool("Canvas size", img, dict(left=5, top=3, right=10, bottom=2, fill='white'))
        self.assertEqual(out.shape[:2], (25, 45))
        self.assertTrue((out[3:23, 5:35] == 100).all() and (out[0, 0] == 255).all() and (out[-1, -1] == 255).all())
        black = il.apply_tool("Canvas size", img, dict(left=2, fill='black'))
        self.assertTrue((black[:, :2] == 0).all())
        edge = img.copy()
        edge[:, 0] = (10, 20, 30)
        self.assertTrue((il.apply_tool("Canvas size", edge, dict(left=4, fill='edge'))[:, :4] == (10, 20, 30)).all())
        colour = il.apply_tool("Canvas size", img, dict(top=2, fill='picked colour', color='#ff0000'))
        self.assertTrue((colour[:2] == (255, 0, 0)).all())
        cut = il.apply_tool("Canvas size", img, dict(left=-5, right=-5, top=-2))
        self.assertEqual(cut.shape[:2], (18, 20))
        self.assertTrue((cut == 100).all())
        self.assertIs(il.apply_tool("Canvas size", img, dict(left=-100)), img)             # a cut that leaves nothing does nothing
        self.assertIs(il.apply_tool("Canvas size", img, {}), img)
        self.assertEqual(il.TOOLS["Canvas size"].layer_shift(dict(left=5, top=3), img.shape), (5, 3))

    def test_aspect_crop(self):
        self.assertEqual(il.aspect_crop_box(400, 300, 1, 1), (50, 0, 350, 300))
        self.assertEqual(il.aspect_crop_box(400, 300, 16, 9, 'top'), (0, 0, 400, 225))
        self.assertEqual(il.aspect_crop_box(400, 300, 16, 9, 'bottom'), (0, 75, 400, 300))
        self.assertEqual(il.aspect_crop_box(300, 400, 3, 2, 'centre'), (0, 100, 300, 300))
        self.assertEqual(il.aspect_crop_box(400, 300, 4, 3), (0, 0, 400, 300))
        img = np.zeros((300, 400, 3), np.uint8)
        out = il.apply_tool("Crop to aspect ratio", img, dict(ratio='1 : 1'))
        self.assertEqual(out.shape[:2], (300, 300))
        tall = il.apply_tool("Crop to aspect ratio", img, dict(ratio='custom', custom_w=1, custom_h=2, portrait=False))
        self.assertEqual(tall.shape[:2], (300, 150))
        swapped = il.apply_tool("Crop to aspect ratio", img, dict(ratio='3 : 2', portrait=True))
        self.assertEqual(swapped.shape[:2], (300, 200))
        shift = il.TOOLS["Crop to aspect ratio"].layer_shift(dict(ratio='1 : 1', custom_w=1, custom_h=1, portrait=False, anchor='centre'), img.shape)
        self.assertEqual(shift, (-50, 0))

    def test_lens_distortion(self):
        import cv2
        img = np.full((120, 160, 3), 255, np.uint8)
        for x in range(0, 160, 20):
            cv2.line(img, (x, 0), (x, 119), (0, 0, 0), 1)
        self.assertIs(il.apply_tool("Lens distortion", img, {}), img)
        out = il.apply_tool("Lens distortion", img, dict(k1=-0.2))
        self.assertEqual(out.shape, img.shape)
        self.assertTrue(np.array_equal(out[60, 78:82], img[60, 78:82]) or abs(float(out[60, 76:84].mean()) - float(img[60, 76:84].mean())) < 30)  # the centre hardly moves
        self.assertFalse(np.array_equal(out, img))
        self.assertTrue(np.array_equal(il.undistort_radial(img, 0.0, 0.0, 100.0), img))
        zoomed = il.apply_tool("Lens distortion", img, dict(zoom=120))
        self.assertEqual(zoomed.shape, img.shape)


class WorkflowTests(unittest.TestCase):
    """History steps and recipes, running a recipe on a folder, parsing a step from text, settings and recent files."""

    def test_history_records_steps_and_recipe_follows_undo(self):
        h = il.History(np.zeros((10, 10, 3), np.uint8))
        h.push("Levels", np.ones((10, 10, 3), np.uint8), None, il.tool_step("Levels", dict(gamma=2.0)))
        h.push("Eraser brush", np.full((10, 10, 3), 2, np.uint8))
        h.push("Invert", np.full((10, 10, 3), 3, np.uint8), None, il.tool_step("Invert", {}))
        steps, skipped = h.recipe()
        self.assertEqual([st['tool'] for st in steps], ["Levels", "Invert"])
        self.assertEqual(skipped, ["Eraser brush"])
        h.undo()
        self.assertEqual([st['tool'] for st in h.recipe()[0]], ["Levels"])                   # an undone step leaves the recipe
        h.push("Blur (Gaussian)", np.full((10, 10, 3), 4, np.uint8), None, il.tool_step("Blur (Gaussian)", dict(radius=2.0)))
        self.assertEqual([st['tool'] for st in h.recipe()[0]], ["Levels", "Blur (Gaussian)"])  # a new step after an undo replaces the old branch
        self.assertEqual(len(h.steps), len(h.states))

    def test_history_limit_keeps_steps_aligned_and_counts_the_loss(self):
        h = il.History(np.zeros((100, 100, 3), np.uint8), limit_bytes=70000)
        for i in range(6):
            h.push("s%d" % i, np.full((100, 100, 3), i + 1, np.uint8), None, il.tool_step("Invert", {}))
        self.assertEqual(len(h.steps), len(h.states))
        self.assertGreater(h.lost, 0)

    def test_recipe_file_roundtrip_and_validation(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "r.ifr")
        steps = [il.tool_step("Levels", dict(in_black=20, gamma=1.3)), il.tool_step("Invert", {}, (0, 0, 5, 5))]
        il.save_recipe(path, steps, "demo")
        self.assertEqual(il.load_recipe(path), steps)
        self.assertFalse(os.path.exists(path + ".tmp"))
        with self.assertRaises(ValueError):
            il.save_recipe(path, [il.tool_step("No such tool", {})])
        with self.assertRaises(ValueError):
            il.save_recipe(path, [il.tool_step("Eraser brush", {})])                          # a brush cannot run on its own
        bad = os.path.join(d, "bad.ifr")
        with open(bad, "w") as f:
            f.write("{not json")
        with self.assertRaises(ValueError):
            il.load_recipe(bad)
        other = os.path.join(d, "other.ifr")
        with open(other, "w") as f:
            f.write('{"format": "something else", "steps": []}')
        with self.assertRaises(ValueError):
            il.load_recipe(other)
        newer = os.path.join(d, "newer.ifr")
        with open(newer, "w") as f:
            f.write('{"format": "ImageForge recipe", "version": 99, "steps": []}')
        with self.assertRaises(ValueError):
            il.load_recipe(newer)

    def test_run_recipe_matches_applying_the_tools_by_hand(self):
        img = gradient(40, 60)
        steps = [il.tool_step("Levels", dict(in_black=30, in_white=220, gamma=1.4)), il.tool_step("Invert", {}),
                 il.tool_step("Resize", dict(width=30, height=20))]
        by_hand = il.apply_tool("Resize", il.apply_tool("Invert", il.apply_tool("Levels", img, dict(in_black=30, in_white=220, gamma=1.4)), {}),
                                dict(width=30, height=20))
        out = il.run_recipe(img, steps)
        self.assertTrue(np.array_equal(out, by_hand) and out.shape[:2] == (20, 30))
        self.assertTrue(np.array_equal(il.run_recipe(img, []), img))

    def test_run_recipe_region_is_clipped_to_the_picture(self):
        img = np.full((20, 20, 3), 100, np.uint8)
        out = il.run_recipe(img, [il.tool_step("Invert", {}, (10, 10, 500, 500))])
        self.assertTrue((out[10:, 10:] == 155).all() and (out[:10] == 100).all())
        self.assertTrue(np.array_equal(il.run_recipe(img, [il.tool_step("Invert", {}, (30, 30, 40, 40))]), img))

    def test_run_recipe_with_layers_and_geometry(self):
        img = np.full((60, 100, 3), 200, np.uint8)
        bar = il.layer_step("Scale bar", dict(length=20.0, label='', label_pos='none', thickness=6, color='#000000', size=12, font='default',
                                               opacity=100, units_per_px=0.0, x=10, y=40), (1.0, "um"))
        out = il.run_recipe(img, [bar])
        self.assertTrue((out[42:46, 12:30] == 0).all() and (out[:30] == 200).all())          # the bar (1 px of margin round it) is merged into the picture
        moved = il.run_recipe(img, [bar, il.tool_step("Canvas size", dict(left=5, top=7, fill='white'))])
        self.assertEqual(moved.shape[:2], (67, 105))
        self.assertTrue((moved[49:53, 17:35] == 0).all())                                     # a layer moves with the canvas
        flipped = il.run_recipe(img, [bar, il.tool_step("Flip", dict(axis='horizontal'))])
        self.assertTrue((flipped[42:46, 70:88] == 0).all())                                   # a geometry tool merges the layers first

    def test_parse_step_text(self):
        st = il.parse_step_text("levels: gamma=1.5, in_black=20")
        self.assertEqual((st['tool'], st['params']), ("Levels", dict(gamma=1.5, in_black=20)))
        self.assertEqual(il.parse_step_text("Invert")['params'], {})
        self.assertIs(il.parse_step_text("Auto straighten:expand=false")['params']['expand'], False)
        self.assertEqual(il.parse_step_text("Hue / saturation:hue=30")['tool'], "Hue / saturation")
        with self.assertRaises(ValueError):
            il.parse_step_text("Nope:x=1")
        with self.assertRaises(ValueError):
            il.parse_step_text("Levels:wrong=1")

    def test_batch_on_a_folder(self):
        d = tempfile.mkdtemp()
        src, out = os.path.join(d, "in"), os.path.join(d, "out")
        os.makedirs(os.path.join(src, "sub"))
        for name in ("a.png", "b.jpg", os.path.join("sub", "c.png")):
            Image.fromarray(gradient(30, 40)).save(os.path.join(src, name))
        with open(os.path.join(src, "notes.txt"), "w") as f:
            f.write("not a picture")
        with open(os.path.join(src, "broken.png"), "wb") as f:
            f.write(b"not a png")
        self.assertEqual([os.path.basename(q) for q in il.list_images(src)], ["a.png", "b.jpg", "broken.png"])
        self.assertEqual(len(il.list_images(src, recursive=True)), 4)
        steps = [il.tool_step("Invert", {}), il.tool_step("Resize", dict(width=20, height=10))]
        seen = []
        res = il.run_batch(steps, il.list_images(src, True), out, fmt="png", suffix="_x", root_dir=src, progress=lambda i, n, s: seen.append((i, n)))
        self.assertEqual(seen[0], (0, 4))
        ok = [r for r in res if r[2] == "ok"]
        self.assertEqual(len(ok), 3)
        self.assertEqual([r[0] for r in res if r[1] is None], [os.path.join(src, "broken.png")])
        self.assertTrue(os.path.isfile(os.path.join(out, "a_x.png")) and os.path.isfile(os.path.join(out, "b_x.png")))
        self.assertTrue(os.path.isfile(os.path.join(out, "sub", "c_x.png")))                  # the sub-folder is kept
        got = np.array(Image.open(os.path.join(out, "a_x.png")))
        self.assertEqual(got.shape[:2], (10, 20))
        again = il.run_batch(steps, [os.path.join(src, "a.png")], out, fmt="png", suffix="_x", root_dir=src)
        self.assertTrue(again[0][1].endswith("a_x_1.png"))                                    # no overwrite: a free name
        over = il.run_batch(steps, [os.path.join(src, "a.png")], out, fmt="png", suffix="_x", overwrite=True, root_dir=src)
        self.assertTrue(over[0][1].endswith("a_x.png"))
        same = il.batch_output_path(os.path.join(src, "a.png"), src, "same", "", False)
        self.assertNotEqual(os.path.normcase(same), os.path.normcase(os.path.join(src, "a.png")))      # never over the original

    def test_settings_and_recent_files(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "deep", "settings.json")
        self.assertEqual(il.load_settings(path), {})
        files = []
        for n in range(12):
            f = os.path.join(d, "f%d.png" % n)
            open(f, "wb").close()
            files.append(f)
        st = {}
        for f in files:
            il.add_recent(st, f)
        il.add_recent(st, files[3])
        self.assertEqual(len(st['recent']), 10)
        self.assertEqual(st['recent'][0], os.path.abspath(files[3]))
        self.assertEqual(len(set(st['recent'])), 10)
        st['geometry'] = "1200x800+10+10"
        self.assertTrue(il.save_settings(st, path))
        self.assertEqual(il.load_settings(path), st)
        os.remove(files[3])
        self.assertNotIn(os.path.abspath(files[3]), il.recent_files(st))
        with open(path, "w") as f:
            f.write("{broken")
        self.assertEqual(il.load_settings(path), {})
        self.assertFalse(il.save_settings({"a": 1}, os.path.join(d, "f0.png", "x", "s.json")))      # a path that cannot exist: ignored

    def test_command_line(self):
        d = tempfile.mkdtemp()
        src, out = os.path.join(d, "in"), os.path.join(d, "out")
        os.makedirs(src)
        Image.fromarray(gradient(30, 40)).save(os.path.join(src, "a.png"))
        self.assertEqual(il.cli(["--batch", src, out, "--apply", "Invert", "--format", "png", "--suffix", "_i"]), 0)
        got = np.array(Image.open(os.path.join(out, "a_i.png")))
        self.assertTrue(np.array_equal(got, 255 - gradient(30, 40)))
        self.assertEqual(il.cli(["--batch", src, out, "--apply", "No such tool"]), 2)
        self.assertEqual(il.cli(["--batch", src, out]), 2)                                   # nothing to do
        self.assertEqual(il.cli(["--batch", os.path.join(d, "missing"), out, "--apply", "Invert"]), 2)
        self.assertIsNone(il.cli([]))
        rp = os.path.join(d, "r.ifr")
        il.save_recipe(rp, [il.tool_step("Invert", {})])
        self.assertEqual(il.cli(["--batch", src, os.path.join(d, "out2"), "--recipe", rp, "--apply", "Invert"]), 0)
        again = np.array(Image.open(os.path.join(d, "out2", "a.png")))
        self.assertTrue(np.array_equal(again, gradient(30, 40)))                             # two inversions give the original back

    def test_save_image_png_level_and_jpeg_quality(self):
        d = tempfile.mkdtemp()
        img = np.random.RandomState(0).randint(0, 255, (60, 60, 3), np.uint8)
        a, b = os.path.join(d, "a.png"), os.path.join(d, "b.png")
        il.save_image(img, a, None, 95, 1)
        il.save_image(img, b, None, 95, 9)
        self.assertTrue(np.array_equal(np.array(Image.open(a)), img) and np.array_equal(np.array(Image.open(b)), img))
        j1, j2 = os.path.join(d, "q10.jpg"), os.path.join(d, "q95.jpg")
        il.save_image(img, j1, None, 10)
        il.save_image(img, j2, None, 95)
        self.assertLess(os.path.getsize(j1), os.path.getsize(j2))


class AITests(unittest.TestCase):
    """Enlarge (AI): the command lines, finding the programs, a fake program standing in for the real one, failure and cancel."""

    class FakeProc:
        """Stands in for subprocess.Popen: 'enlarges' the input by pixel repetition and writes the output where -o says."""
        calls = []
        mode = 'ok'

        def __init__(self, cmd, **kw):
            AITests.FakeProc.calls.append(cmd)
            self.returncode = 0
            src, dst = cmd[cmd.index("-i") + 1], cmd[cmd.index("-o") + 1]
            s = int(cmd[cmd.index("-s") + 1])
            if AITests.FakeProc.mode == 'fail':
                self.returncode = 3
                kw['stderr'].write("vkCreateInstance failed\n"); kw['stderr'].flush()
                return
            im = Image.open(src)
            if AITests.FakeProc.mode == 'slow':
                self.slow = True
                return
            im.resize((im.size[0] * s, im.size[1] * s), Image.NEAREST).save(dst)

        slow = False

        def poll(self):
            return None if self.slow else self.returncode

        def terminate(self):
            self.slow = False
            self.returncode = 1

        def wait(self, timeout=None):
            return self.returncode

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "realesrgan-ncnn-vulkan-20220424-windows")
        os.makedirs(os.path.join(self.dir, "models"))
        for n in ("realesrgan-ncnn-vulkan.exe",):
            open(os.path.join(self.dir, n), "w").close()
        self.w2 = os.path.join(self.tmp.name, "waifu2x-ncnn-vulkan-20250915-windows")
        os.makedirs(self.w2)
        open(os.path.join(self.w2, "waifu2x-ncnn-vulkan.exe"), "w").close()
        self.old = (il._popen, il.AI_DIR)
        il._popen = AITests.FakeProc
        il.AI_DIR = None
        il.AI_CANCEL.clear()
        AITests.FakeProc.calls, AITests.FakeProc.mode = [], 'ok'

    def tearDown(self):
        il._popen, il.AI_DIR = self.old
        il.AI_CANCEL.clear()
        self.tmp.cleanup()

    def test_find_programs_in_the_parent_folder_or_their_own(self):
        self.assertTrue(il.find_ai_program('realesrgan', self.tmp.name).endswith("realesrgan-ncnn-vulkan.exe"))
        self.assertTrue(il.find_ai_program('waifu2x', self.tmp.name).endswith("waifu2x-ncnn-vulkan.exe"))
        self.assertTrue(il.find_ai_program('waifu2x', self.w2).endswith("waifu2x-ncnn-vulkan.exe"))
        self.assertIsNone(il.find_ai_program('waifu2x', self.dir))
        il.set_ai_dir(self.tmp.name)
        self.assertIsNotNone(il.find_ai_program('realesrgan'))

    def test_commands(self):
        exe = os.path.join(self.w2, "waifu2x-ncnn-vulkan.exe")
        c = il.ai_command("waifu2x photo (2x, gentler)", exe, "a.png", "b.png", 2, 3)
        self.assertEqual((c[c.index("-n") + 1], c[c.index("-s") + 1]), ("3", "2"))
        self.assertTrue(c[c.index("-m") + 1].endswith("models-upconv_7_photo"))
        r = il.ai_command("Real-ESRGAN photo (4x, most detail)", os.path.join(self.dir, "realesrgan-ncnn-vulkan.exe"), "a.png", "b.png", 2, 1)
        self.assertEqual((r[r.index("-n") + 1], r[r.index("-s") + 1]), ("realesrgan-x4plus", "4"))     # always the model's own scale

    def test_enlarge_sizes_and_input_untouched(self):
        img = np.random.default_rng(0).integers(0, 255, (12, 20, 3)).astype(np.uint8)
        ref = img.copy()
        il.set_ai_dir(self.tmp.name)
        out = il.apply_tool("Enlarge (AI)", img, {'method': "waifu2x photo (2x, gentler)", 'scale': '4x'})
        self.assertEqual(out.shape, (48, 80, 3))
        self.assertEqual(out.dtype, np.uint8)
        self.assertTrue(np.array_equal(img, ref))
        out2 = il.apply_tool("Enlarge (AI)", img, {'method': "Real-ESRGAN photo (4x, most detail)", 'scale': '2x'})       # x4 model, reduced to 2x
        self.assertEqual(out2.shape, (24, 40, 3))
        self.assertIn("20x12 -> 40x24", il.TOOL_NOTES["Enlarge (AI)"])

    def test_missing_program_and_failure_and_cancel(self):
        img = np.zeros((8, 8, 3), np.uint8)
        with self.assertRaises(il.ToolUnavailable):
            il.ai_enlarge(img, "waifu2x photo (2x, gentler)", 2, 1, folder=self.dir)             # that folder has only Real-ESRGAN
        AITests.FakeProc.mode = 'fail'
        with self.assertRaises(RuntimeError) as cm:
            il.ai_enlarge(img, "waifu2x photo (2x, gentler)", 2, 1, folder=self.tmp.name)
        self.assertIn("vkCreateInstance", str(cm.exception))
        AITests.FakeProc.mode = 'slow'
        il.AI_CANCEL.set()
        with self.assertRaises(il.Cancelled):
            il.ai_enlarge(img, "waifu2x photo (2x, gentler)", 2, 1, folder=self.tmp.name)

    def test_tool_is_external_geometry_in_the_layout(self):
        t = il.TOOLS["Enlarge (AI)"]
        self.assertTrue(t.external and t.geometry and not t.preview)
        self.assertIn("Enlarge (AI)", il.TOOL_LAYOUT["Size and orientation"])


class GenerativeTests(unittest.TestCase):
    """Remove object (generative): a fake OpenRouter stands in for the paid service. Checks the request, the alignment of the answer, the paste (only over
    the object), the cost bookkeeping, the stops (no key, no words, no selection, the money limit) and that errors never contain the key."""
    KEY = "sk-or-test-secret"

    @staticmethod
    def scene():
        rng = np.random.default_rng(5)
        bg = cv2.GaussianBlur(rng.integers(0, 255, (400, 600, 3)).astype(np.uint8), (0, 0), 1.2)
        for _ in range(120):
            cv2.circle(bg, (int(rng.integers(0, 600)), int(rng.integers(0, 400))), int(rng.integers(3, 12)), tuple(int(v) for v in rng.integers(0, 255, 3)), -1)
        return bg

    def setUp(self):
        self.bg = self.scene()
        self.img = self.bg.copy()
        self.img[170:230, 120:180] = (255, 0, 0)                  # object 1: a red square
        self.img[170:230, 420:480] = (255, 0, 0)                  # object 2
        self.calls, self.mode = [], 'ok'
        self.old = (il._urlopen, il.openrouter_key, dict(il.OR_STATE))
        il._urlopen = self.fake
        il.openrouter_key = lambda: self.KEY
        il.OR_STATE['credit'], il.OR_STATE['prices'] = 5.0, {}
        il.AI_CANCEL.clear()

    def tearDown(self):
        il._urlopen, il.openrouter_key = self.old[0], self.old[1]
        il.OR_STATE.clear()
        il.OR_STATE.update(self.old[2])
        il.AI_CANCEL.clear()

    def fake(self, req, timeout=None):
        import io as _io, json as _json, base64 as _b64, urllib.error as _ue
        url = req.full_url.replace(il.OPENROUTER_API, "")
        self.calls.append((url, req.get_header("Authorization")))
        if url == "/credits":
            return _io.BytesIO(_json.dumps({"data": {"total_credits": 20, "total_usage": 9.9}}).encode())
        if url == "/models":
            return _io.BytesIO(_json.dumps({"data": [{"id": "google/gemini-2.5-flash-image", "pricing": {"prompt": "0.0000003", "image_output": "0.00003"}}]}).encode())
        if self.mode == 'http402':
            raise _ue.HTTPError(req.full_url, 402, "Payment Required", {}, _io.BytesIO(b'{"error": {"message": "Insufficient credits"}}'))
        body = _json.loads(req.data)
        self.last_body = body
        if self.mode == 'noimage':
            return _io.BytesIO(_json.dumps({"choices": [{"message": {"content": "I cannot do that"}}], "usage": {"cost": 0.001}}).encode())
        uri = body["messages"][0]["content"][1]["image_url"]["url"]
        sent = np.asarray(Image.open(_io.BytesIO(_b64.b64decode(uri.split(",", 1)[1]))).convert("RGB"))
        red = ((sent[..., 0] > 200) & (sent[..., 1] < 60) & (sent[..., 2] < 60)).astype(np.uint8) * 255
        red = cv2.dilate(red, np.ones((9, 9), np.uint8))
        filled = cv2.cvtColor(cv2.inpaint(cv2.cvtColor(sent, cv2.COLOR_RGB2BGR), red, 6, cv2.INPAINT_TELEA), cv2.COLOR_BGR2RGB)
        h, w = filled.shape[:2]
        M = np.float32([[0.9, 0, 6], [0, 0.9, 4]])                                   # the model hands the picture back rescaled and shifted
        out = cv2.warpAffine(filled, M, (int(w * 1.1), int(h * 1.1)), borderMode=cv2.BORDER_REFLECT)
        buf = _io.BytesIO()
        Image.fromarray(out).save(buf, "PNG")
        return _io.BytesIO(_json.dumps({"choices": [{"message": {"content": "", "images": [{"image_url": {"url": "data:image/png;base64," + _b64.b64encode(buf.getvalue()).decode()}}]}}],
                                        "usage": {"cost": 0.0391}}).encode())

    def sel(self, *boxes):
        m = np.zeros(self.img.shape[:2], np.float32)
        for x0, y0, x1, y1 in boxes:
            m[y0:y1, x0:x1] = 1
        return il.MaskRegion((min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)), m)

    def chat_calls(self):
        return [c for c in self.calls if c[0] == "/chat/completions"]

    def test_one_object_is_removed_and_the_rest_is_untouched(self):
        out = il.generative_remove(self.img, self.sel((115, 165, 185, 235)), "the red square", grow=10)
        self.assertEqual(len(self.chat_calls()), 1)
        self.assertEqual(self.chat_calls()[0][1], "Bearer " + self.KEY)
        self.assertEqual(int(((out[165:235, 115:185, 0] > 200) & (out[165:235, 115:185, 1] < 60)).sum()), 0)       # the red is gone
        self.assertTrue(np.array_equal(out[:, 300:410], self.img[:, 300:410]))                                      # far from the object: identical
        self.assertTrue(np.array_equal(out[:60], self.img[:60]))
        self.assertTrue((out[170:230, 420:480] == (255, 0, 0)).all())                                              # the second object was not selected
        self.assertIn("1 request, 0.0391 USD", il.TOOL_NOTES["Remove object (generative)"])
        self.assertAlmostEqual(il.OR_STATE['credit'], 5.0 - 0.0391, places=4)
        self.assertIn("remove the red square", self.last_body["messages"][0]["content"][0]["text"])
        self.assertEqual(self.last_body["model"], "google/gemini-2.5-flash-image")

    def test_the_filled_area_looks_like_the_background(self):
        out = il.generative_remove(self.img, self.sel((115, 165, 185, 235)), "the red square", grow=10)
        err = np.abs(out[185:215, 135:165].astype(int) - self.bg[185:215, 135:165].astype(int)).mean()
        before = np.abs(self.img[185:215, 135:165].astype(int) - self.bg[185:215, 135:165].astype(int)).mean()
        self.assertLess(err, before)                                                                                 # closer to the real background than the red was

    def test_two_pieces_are_two_requests_and_the_money_limit_stops(self):
        two = self.sel((115, 165, 185, 235), (415, 165, 485, 235))
        il.generative_remove(self.img, two, "a red square", grow=10)
        self.assertEqual(len(self.chat_calls()), 2)
        self.assertIn("2 requests", il.TOOL_NOTES["Remove object (generative)"])
        self.calls.clear()
        il.generative_remove(self.img, two, "a red square", grow=10, max_cost=0.05)
        self.assertEqual(len(self.chat_calls()), 1)                                                                  # a second request would pass the limit
        self.assertIn("1 request", il.TOOL_NOTES["Remove object (generative)"])

    def test_stops_before_spending(self):
        with self.assertRaises(ValueError):
            il.generative_remove(self.img, None, "the red square")
        il.openrouter_key = lambda: None
        with self.assertRaises(il.ToolUnavailable):
            il.generative_remove(self.img, self.sel((115, 165, 185, 235)), "the red square")
        il.openrouter_key = lambda: self.KEY
        il.AI_CANCEL.set()
        with self.assertRaises(il.Cancelled):
            il.generative_remove(self.img, self.sel((115, 165, 185, 235)), "the red square")
        self.assertEqual(self.chat_calls(), [])                                                                      # nothing was sent in any of these

    def test_an_empty_description_uses_the_default_sentence(self):
        il.generative_remove(self.img, self.sel((115, 165, 185, 235)), "   ", grow=10)
        text = self.last_body["messages"][0]["content"][0]["text"]
        self.assertIn("remove " + il.GEN_DEFAULT_WHAT + ".", text)
        self.assertNotIn("it is at the centre of the picture", text)                       # the default already says it
        il.generative_remove(self.img, self.sel((115, 165, 185, 235)), "the red square", grow=10)
        self.assertIn("remove the red square (it is at the centre of the picture).", self.last_body["messages"][0]["content"][0]["text"])

    def test_errors_are_plain_and_never_contain_the_key(self):
        self.mode = 'http402'
        with self.assertRaises(RuntimeError) as cm:
            il.generative_remove(self.img, self.sel((115, 165, 185, 235)), "the red square")
        self.assertIn("402", str(cm.exception))
        self.assertIn("Insufficient credits", str(cm.exception))
        self.assertNotIn(self.KEY, str(cm.exception))
        self.mode = 'noimage'
        with self.assertRaises(RuntimeError) as cm:
            il.generative_remove(self.img, self.sel((115, 165, 185, 235)), "the red square")
        self.assertIn("no picture", str(cm.exception))

    def test_an_answer_that_cannot_be_aligned_is_not_pasted(self):
        crop = self.bg[8:392, 8:392]
        other = np.random.default_rng(99).integers(0, 255, crop.shape).astype(np.uint8)
        with self.assertRaises(RuntimeError):
            il.gen_paste(crop, crop, other, np.zeros(crop.shape[:2], bool), 5)

    def test_pieces_and_estimate_and_credit(self):
        big = np.zeros((3000, 4000, 3), np.uint8)
        m = np.zeros((3000, 4000), np.float32)
        m[1000:2200, 1800:2200] = 1
        pcs = il.gen_pieces(big, il.MaskRegion((1800, 1000, 2200, 2200), m), 8)
        self.assertEqual(len(pcs), 1)
        pc = pcs[0]
        self.assertTrue(0 <= pc['x0'] and pc['x0'] + pc['side'] <= 4000 and 0 <= pc['y0'] and pc['y0'] + pc['side'] <= 3000)
        self.assertEqual(pc['sent'], il.GEN_MAX_SIDE)                                                               # a big crop is shrunk before it is sent
        self.assertEqual(pc['hole'].shape, (pc['side'], pc['side']))
        self.assertAlmostEqual(il.gen_estimate("google/gemini-2.5-flash-image"), 1290 * 3e-5 + 1400 * 3e-7, places=6)
        self.assertAlmostEqual(il.gen_estimate("google/gemini-2.5-flash-image", 3), 3 * (1290 * 3e-5 + 1400 * 3e-7), places=6)
        self.assertAlmostEqual(il.or_credit(self.KEY), 10.1, places=6)

    def test_key_lookup(self):
        import unittest.mock as mock
        real = self.old[1]
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "  abc  "}):
            self.assertEqual(real(), "abc")
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(il.sys, "platform", "linux"):
            os.environ.pop("OPENROUTER_API_KEY", None)
            self.assertIsNone(real())

    def test_tool_registration_and_guide(self):
        t = il.TOOLS["Remove object (generative)"]
        self.assertTrue(t.external and t.takes_region and not t.preview)
        self.assertIn("Remove object (generative)", il.TOOL_LAYOUT["Clean-up"])
        self.assertIn("COSTS MONEY", t.doc)
        for needle in ("OPENROUTER_API_KEY", "MONEY AND PRIVACY", "Clean brush", "Remove marks", "Remove object (AI)", "Undo"):
            self.assertIn(needle, il.OBJECT_GUIDE)


class RemoveObjectTests(unittest.TestCase):
    """Remove object (AI): the selection is the input, every separate piece is filled on its own, only the widened hole changes."""

    class FakeSession:
        calls = 0

        def run(self, names, feeds):
            RemoveObjectTests.FakeSession.calls += 1
            img = feeds["image"][0]                                       # (3, 512, 512) in 0..1
            out = np.zeros_like(img)
            out[...] = np.array([0.1, 0.8, 0.1], np.float32)[:, None, None]     # a flat green: easy to find
            return [out[None] * 255.0]

    def setUp(self):
        self.old = (il._lama_session, il.find_ai_model)
        il._lama_session = lambda path: RemoveObjectTests.FakeSession()
        il.find_ai_model = lambda prefix, folder=None: __file__
        RemoveObjectTests.FakeSession.calls = 0
        self.img = np.full((400, 600, 3), 120, np.uint8)

    def tearDown(self):
        il._lama_session, il.find_ai_model = self.old

    def test_region_bool(self):
        self.assertIsNone(il.region_bool(None, (10, 10, 3)))
        r = il.region_bool((2, 3, 5, 7), (10, 10, 3))
        self.assertEqual((int(r.sum()), bool(r[3, 2]), bool(r[7, 5])), (12, True, False))
        mask = np.zeros((10, 10), np.float32); mask[:, :4] = 1
        self.assertEqual(int(il.region_bool(il.MaskRegion((0, 0, 4, 10), mask), (10, 10, 3)).sum()), 40)

    def test_no_selection_and_no_model(self):
        with self.assertRaises(ValueError):
            il.lama_remove(self.img, None)
        il.find_ai_model = lambda prefix, folder=None: None
        with self.assertRaises(il.ToolUnavailable):
            il.lama_remove(self.img, (10, 10, 50, 50))

    def test_only_the_widened_hole_changes(self):
        out = il.lama_remove(self.img, (100, 100, 140, 140), grow=0, grain=False)
        self.assertEqual(out.shape, self.img.shape)
        self.assertTrue((out[120, 120] != 120).any())                         # the hole took the model's output
        self.assertTrue(np.array_equal(out[:60], self.img[:60]))               # far away: untouched
        self.assertTrue(np.array_equal(out[:, 300:], self.img[:, 300:]))
        wide = il.lama_remove(self.img, (100, 100, 140, 140), grow=50, grain=False)
        self.assertTrue((wide[100 - 15, 120] != 120).any())                    # widening by 50 % of 40 px reaches 20 px outside
        self.assertFalse((out[100 - 15, 120] != 120).any())

    def test_each_piece_is_filled_on_its_own(self):
        mask = np.zeros((400, 600), np.float32); mask[50:90, 50:90] = 1; mask[300:340, 500:540] = 1
        out = il.lama_remove(self.img, il.MaskRegion((50, 50, 540, 340), mask), grow=10, grain=False)
        self.assertEqual(RemoveObjectTests.FakeSession.calls, 2)
        self.assertIn("2 pieces", il.TOOL_NOTES["Remove object (AI)"])
        self.assertTrue(np.array_equal(out[150:250, 200:400], self.img[150:250, 200:400]))   # nothing in between

    def test_grain_adds_noise_at_the_level_of_the_surroundings(self):
        rng = np.random.default_rng(0)
        noisy = np.clip(120 + rng.normal(0, 6, (400, 600, 3)), 0, 255).astype(np.uint8)
        flat = il.lama_remove(noisy, (200, 150, 260, 210), grow=0, grain=False)
        grainy = il.lama_remove(noisy, (200, 150, 260, 210), grow=0, grain=True)
        self.assertLess(flat[160:200, 210:250].astype(float).std(axis=(0, 1)).mean(), 1.0)
        self.assertGreater(grainy[160:200, 210:250].astype(float).std(axis=(0, 1)).mean(), 2.0)

    def test_tool_through_apply_tool_gets_the_area_and_does_not_merge(self):
        out = il.apply_tool("Remove object (AI)", self.img, {'grow': 0, 'grain': False}, (100, 100, 140, 140))
        self.assertTrue((out[120, 120] != 120).any())
        t = il.TOOLS["Remove object (AI)"]
        self.assertTrue(t.external and t.takes_region and not t.preview)
        self.assertIn("Remove object (AI)", il.TOOL_LAYOUT["Clean-up"])


class PortraitTests(unittest.TestCase):
    """Portrait retouch: smooth only the skin, light on the face, darker surroundings; faces found or given."""

    def scene(self):
        rng = np.random.default_rng(3)
        img = np.full((300, 400, 3), (60, 90, 120), np.uint8)
        yy, xx = np.mgrid[0:300, 0:400]
        face = ((xx - 200) / 70.0) ** 2 + ((yy - 140) / 90.0) ** 2 < 1
        img[face] = (205, 150, 125)                                          # skin colour
        img = np.clip(img.astype(int) + rng.normal(0, 8, img.shape), 0, 255).astype(np.uint8)
        return img, face

    def test_all_zero_changes_nothing(self):
        img, _ = self.scene()
        self.assertIs(il.portrait_retouch_faces(img, [(200, 140, 140)], 0, 0, 0, 0), img)
        self.assertIs(il.portrait_retouch_faces(img, [], 40, 15, 25, 0), img)

    def test_skin_is_smoothed_but_the_background_texture_is_not(self):
        img, face = self.scene()
        out = il.portrait_retouch_faces(img, [(200, 140, 140)], 100, 0, 0, 0)
        core = face & (np.abs(np.mgrid[0:300, 0:400][1] - 200) < 40) & (np.abs(np.mgrid[0:300, 0:400][0] - 140) < 50)
        self.assertLess(out[core].astype(float).std(axis=0).mean(), 0.7 * img[core].astype(float).std(axis=0).mean())
        far = np.zeros(face.shape, bool); far[:40, :60] = True
        self.assertTrue(np.array_equal(out[far], img[far]))

    def test_light_and_surround(self):
        img, face = self.scene()
        lit = il.portrait_retouch_faces(img, [(200, 140, 140)], 0, 40, 0, 0)
        self.assertGreater(lit[140, 200].astype(int).sum(), img[140, 200].astype(int).sum())
        dark = il.portrait_retouch_faces(img, [(200, 140, 60)], 0, 0, 60, 0)
        self.assertLess(dark[290, 5].astype(int).sum(), img[290, 5].astype(int).sum())            # far corner darker
        self.assertLessEqual(abs(int(dark[140, 200].sum()) - int(img[140, 200].sum())), 12)        # the face itself hardly changes

    def test_warmth_shifts_red_against_blue(self):
        img, _ = self.scene()
        w = il.portrait_retouch_faces(img, [(200, 140, 140)], 0, 0, 0, 40)
        self.assertGreater(int(w[140, 200, 0]), int(img[140, 200, 0]))
        self.assertLess(int(w[140, 200, 2]), int(img[140, 200, 2]))

    def test_tool_with_a_given_face_and_with_none_found(self):
        img, _ = self.scene()
        out = il.apply_tool("Portrait retouch", img, {'faces': '200 140 140', 'size': 0, 'smooth': 60, 'light': 20, 'surround': 30, 'warmth': 0})
        self.assertEqual((out.dtype, out.shape), (np.uint8, img.shape))
        self.assertFalse(np.array_equal(out, img))
        il.apply_tool("Portrait retouch", img, None)                                   # nobody to find in a synthetic picture
        self.assertIn("no face found", il.TOOL_NOTES["Portrait retouch"])

    def test_parse_and_format_faces(self):
        self.assertEqual(il.parse_faces("200 140 140; 50, 60, 30"), [(200.0, 140.0, 140.0), (50.0, 60.0, 30.0)])
        self.assertEqual(il.parse_faces("10 20", 99), [(10.0, 20.0, 99.0)])                            # no width: the default one
        self.assertEqual(il.parse_faces("10 20", 0, (100, 400, 3)), [(10.0, 20.0, 22.0)])               # no default: 22 % of the short side
        self.assertEqual(il.parse_faces("abc; 5; 1 2 x;\n7 8 9"), [(7.0, 8.0, 9.0)])                  # unreadable entries are skipped
        self.assertEqual(il.parse_faces(""), [])
        f = [(440.4, 526.2, 189.6), (12, 80, 150)]
        self.assertEqual(il.parse_faces(il.format_faces(f)), [(440.0, 526.0, 190.0), (12.0, 80.0, 150.0)])

    def test_several_listed_faces_are_all_kept_bright(self):
        img, _ = self.scene()
        both = il.apply_tool("Portrait retouch", img, {'faces': '100 140 60; 300 140 60', 'size': 0, 'smooth': 0, 'light': 0, 'surround': 60, 'warmth': 0})
        one = il.apply_tool("Portrait retouch", img, {'faces': '100 140 60', 'size': 0, 'smooth': 0, 'light': 0, 'surround': 60, 'warmth': 0})
        self.assertLess(int(one[140, 300].sum()), int(both[140, 300].sum()))                              # a second face keeps its surroundings lit

    def test_find_faces_on_nothing(self):
        self.assertEqual(il.find_faces(np.full((200, 200, 3), 128, np.uint8)), [])


class DodgeBurnTests(unittest.TestCase):
    """Dodge / burn: lighten or darken by hand, limited to a tonal range, colours kept, alpha untouched."""

    def grad(self):
        g = np.tile(np.linspace(0, 255, 256).astype(np.uint8), (8, 1))
        return np.stack([g, g, g], axis=2)

    def test_dodge_lightens_burn_darkens(self):
        img = np.full((6, 6, 3), 100, np.uint8)
        w = np.ones((6, 6), np.float32)
        self.assertTrue((il.tone_paint(img, w, 'dodge (lighten)', 'all', 50) > 100).all())
        self.assertTrue((il.tone_paint(img, w, 'burn (darken)', 'all', 50) < 100).all())

    def test_zero_weight_changes_nothing(self):
        img = self.grad()
        self.assertTrue(np.array_equal(il.tone_paint(img, np.zeros(img.shape[:2], np.float32), 'dodge (lighten)', 'all', 100), img))

    def test_exposure_scales_the_effect(self):
        img = np.full((4, 4, 3), 100, np.uint8)
        w = np.ones((4, 4), np.float32)
        weak, strong = (int(il.tone_paint(img, w, 'dodge (lighten)', 'all', e)[0, 0, 0]) for e in (20, 80))
        self.assertTrue(100 < weak < strong <= 255)
        self.assertEqual(int(il.tone_paint(img, w, 'dodge (lighten)', 'all', 100)[0, 0, 0]), 255)

    def test_ranges(self):
        img = self.grad()
        w = np.ones(img.shape[:2], np.float32)
        d = il.tone_paint(img, w, 'dodge (lighten)', 'shadows', 100).astype(int) - img
        self.assertGreater(d[0, 20, 0], 0)                       # shadows move
        self.assertLess(d[0, 250, 0], d[0, 20, 0])               # highlights hardly move under 'shadows'
        h = il.tone_paint(img, w, 'burn (darken)', 'highlights', 100).astype(int) - img
        self.assertLess(h[0, 240, 0], h[0, 20, 0])               # highlights darken, shadows stay
        self.assertEqual(h[0, 0, 0], 0)

    def test_black_and_white_cannot_overflow(self):
        for v in (0, 255):
            img = np.full((3, 3, 3), v, np.uint8)
            for eff in il.TONE_EFFECTS:
                out = il.tone_paint(img, np.ones((3, 3), np.float32), eff, 'all', 100)
                self.assertEqual(out.dtype, np.uint8)
                self.assertEqual(out.shape, img.shape)

    def test_colour_is_kept(self):
        img = np.full((3, 3, 3), (200, 60, 60), np.uint8)
        out = il.tone_paint(img, np.ones((3, 3), np.float32), 'burn (darken)', 'all', 50).astype(float)
        self.assertAlmostEqual(out[0, 0, 0] / out[0, 0, 1], 200 / 60.0, delta=0.15)

    def test_rgba_keeps_alpha_and_invisible_pixels(self):
        img = np.zeros((2, 2, 4), np.uint8)
        img[..., :3] = 100
        img[0, 0, 3] = 255
        out = il.tone_paint(img, np.ones((2, 2), np.float32), 'dodge (lighten)', 'all', 50)
        self.assertEqual(out.shape, (2, 2, 4))
        self.assertTrue(np.array_equal(out[..., 3], img[..., 3]))
        self.assertGreater(out[0, 0, 0], 100)
        self.assertEqual(out[1, 1, 0], 100)                      # transparent: nothing to see, nothing changed

    def test_tool_is_registered_in_retouch(self):
        t = il.TOOLS["Dodge / burn"]
        self.assertEqual((t.group, t.interactive), ("Retouch", 'paint'))
        self.assertIn("Dodge / burn", il.TOOL_LAYOUT["Retouch"])


class DrawingTests(unittest.TestCase):
    """Pencil and paintbrush strokes, fill bucket and gradient: pure functions, on RGB and RGBA, with a rectangle or a shape selection."""

    def stroke(self, base, p0, p1, size, colour, hardness=100, opacity=100, aliased=False, square=False, region=None):
        cover = np.zeros(base.shape[:2], np.float32)
        box = il.stamp_cover(cover, p0, p1, size, hardness, square, aliased)
        w = cover * (opacity / 100.0)
        rw = il.region_weight_box(region, base.shape, (0, 0, base.shape[1], base.shape[0]))
        if rw is not None:
            w = w * rw
        return il.paint_over(base, colour, w), cover, box

    def test_pencil_is_hard_and_jagged(self):
        base = np.full((20, 40, 3), 255, np.uint8)
        out, cover, box = self.stroke(base, (5, 10), (30, 10), 1, (255, 0, 0), aliased=True)
        self.assertTrue(set(np.unique(cover)) <= {0.0, 1.0})
        self.assertTrue((out[10, 5:31] == (255, 0, 0)).all())
        self.assertTrue((out[9] == 255).all() and (out[11] == 255).all())                      # exactly one pixel thick
        diag, cov, _ = self.stroke(base, (2, 2), (17, 17), 1, (0, 0, 0), aliased=True)
        self.assertEqual(int((cov > 0).sum()), 16)                                              # a connected one-pixel diagonal, no gaps
        thick, _, _ = self.stroke(base, (10, 10), (10, 10), 5, (0, 0, 0), aliased=True)
        self.assertEqual(int((thick[..., 0] == 0).sum()), 21)                                   # a round tip of diameter 5
        sq, _, _ = self.stroke(base, (10, 10), (10, 10), 5, (0, 0, 0), aliased=True, square=True)
        self.assertEqual(int((sq[..., 0] == 0).sum()), 25)

    def test_brush_edge_fades_with_hardness_and_overlap_does_not_build_up(self):
        base = np.full((40, 60, 3), 255, np.uint8)
        soft, cover, _ = self.stroke(base, (10, 20), (50, 20), 16, (0, 0, 0), hardness=0)
        self.assertTrue(cover[20, 30] >= 0.9 and 0 < cover[20 + 5, 30] < 1 and cover[20 + 12, 30] == 0)
        hard, cov2, _ = self.stroke(base, (10, 20), (50, 20), 16, (0, 0, 0), hardness=100)
        self.assertGreater(int((cov2 > 0.9).sum()), int((cover > 0.9).sum()))
        back, cov3, _ = self.stroke(base, (10, 20), (50, 20), 16, (0, 0, 0), hardness=100, opacity=40)     # one stroke, 40 %
        self.assertTrue((back[20, 15:46] == back[20, 15]).all())                                 # the same grey all along: no build-up
        self.assertTrue(140 < int(back[20, 30, 0]) < 160)
        loop = np.zeros((40, 60), np.float32)
        il.stamp_cover(loop, (10, 20), (50, 20), 16, 100)
        il.stamp_cover(loop, (50, 20), (10, 20), 16, 100)                                        # going back over it
        self.assertLessEqual(float(loop.max()), 1.0)

    def test_paint_over_rgba(self):
        rgba = np.zeros((6, 6, 4), np.uint8)
        out = il.paint_over(rgba, (200, 50, 10), np.full((6, 6), 1.0, np.float32))
        self.assertTrue((out[..., :3] == (200, 50, 10)).all() and (out[..., 3] == 255).all())      # over see-through: opaque colour
        half = il.paint_over(rgba, (200, 50, 10), np.full((6, 6), 0.5, np.float32))
        self.assertTrue((half[..., :3] == (200, 50, 10)).all() and 126 <= int(half[0, 0, 3]) <= 129)
        solid = np.zeros((6, 6, 4), np.uint8)
        solid[..., :3], solid[..., 3] = 100, 255
        mix = il.paint_over(solid, (200, 200, 200), np.full((6, 6), 0.5, np.float32))
        self.assertTrue((mix[..., 3] == 255).all() and 148 <= int(mix[0, 0, 0]) <= 152)
        none = il.paint_over(solid, (1, 2, 3), np.zeros((6, 6), np.float32))
        self.assertTrue(np.array_equal(none, solid))

    def test_working_area_limits_a_stroke(self):
        base = np.full((20, 40, 3), 255, np.uint8)
        out, _, _ = self.stroke(base, (2, 10), (38, 10), 4, (0, 0, 0), region=(0, 0, 20, 20))
        self.assertTrue((out[10, 5] == 0).all() and (out[10, 30] == 255).all())
        sel = il.combine_selection(None, il.polygon_mask((20, 40), [(0, 0), (19, 0), (19, 19), (0, 19)]), 'replace', (20, 40))
        faded = il.feather_selection(sel, 8, (20, 40))
        out2, _, _ = self.stroke(base, (2, 10), (38, 10), 4, (0, 0, 0), region=faded)
        self.assertTrue(int(out2[10, 5, 0]) < 40 and int(out2[10, 30, 0]) > 240 and 60 < int(out2[10, 19, 0]) < 200)       # fades across the edge

    def test_flood_fill(self):
        img = np.full((40, 60, 3), 255, np.uint8)
        img[:, 30:32] = 0                                                                         # a wall splitting the page
        img[10:15, 5:10] = (250, 250, 250)                                                        # a nearly white patch
        out = il.flood_fill(img, 5, 5, (255, 0, 0), tolerance=10, grow=0)
        self.assertTrue((out[5, 5] == (255, 0, 0)).all() and (out[5, 40] == 255).all())            # the other side is not joined
        self.assertTrue((out[12, 7] == (255, 0, 0)).all())                                         # within the tolerance
        tight = il.flood_fill(img, 5, 5, (255, 0, 0), tolerance=2, grow=0)
        self.assertTrue((tight[12, 7] == (250, 250, 250)).all())
        both = il.flood_fill(img, 5, 5, (255, 0, 0), tolerance=10, contiguous=False, grow=0)
        self.assertTrue((both[5, 40] == (255, 0, 0)).all() and (both[:, 30:32] == 0).all())
        grown = il.flood_fill(img, 5, 5, (255, 0, 0), tolerance=10, grow=1)
        self.assertTrue((grown[5, 29] == (255, 0, 0)).all() and (grown[5, 30] == (255, 0, 0)).all())      # the fringe of the wall is covered
        half = il.flood_fill(img, 5, 5, (0, 0, 0), tolerance=10, opacity=50, grow=0)
        self.assertTrue(125 <= int(half[5, 5, 0]) <= 130)
        self.assertIs(il.flood_fill(img, 5, 5, (255, 255, 255), tolerance=2, grow=0), img)         # already that colour
        self.assertIs(il.flood_fill(img, 500, 5, (1, 2, 3)), img)
        self.assertIs(il.flood_fill(img, 5, 5, (1, 2, 3), region=(40, 0, 60, 40)), img)             # outside the working area
        part = il.flood_fill(img, 5, 5, (255, 0, 0), tolerance=10, grow=0, region=(0, 0, 20, 40))
        self.assertTrue((part[5, 5] == (255, 0, 0)).all() and (part[5, 25] == 255).all())

    def test_flood_fill_on_a_layer_counts_transparency(self):
        rgba = np.zeros((20, 20, 4), np.uint8)
        rgba[5:15, 5:15] = (10, 200, 10, 255)
        out = il.flood_fill(rgba, 1, 1, (0, 0, 255), tolerance=10, grow=0)                         # the see-through part
        self.assertTrue((out[1, 1] == (0, 0, 255, 255)).all() and (out[10, 10] == (10, 200, 10, 255)).all())
        inside = il.flood_fill(rgba, 10, 10, (255, 0, 0), tolerance=10, grow=0)
        self.assertTrue((inside[10, 10] == (255, 0, 0, 255)).all() and inside[1, 1, 3] == 0)

    def test_gradients(self):
        img = np.full((20, 100, 3), 128, np.uint8)
        g = il.gradient_fill(img, (0, 10), (99, 10), (0, 0, 0), (255, 255, 255), 'linear')
        self.assertTrue(int(g[5, 0, 0]) <= 2 and int(g[5, 99, 0]) >= 253 and 120 <= int(g[5, 50, 0]) <= 135)
        self.assertTrue((np.diff(g[5, :, 0].astype(int)) >= 0).all())
        rev = il.gradient_fill(img, (0, 10), (99, 10), (0, 0, 0), (255, 255, 255), 'linear', 'background to foreground')
        self.assertGreater(int(rev[5, 0, 0]), 250)
        fade = il.gradient_fill(img, (0, 10), (99, 10), (255, 0, 0), (0, 0, 255), 'linear', 'foreground to transparent')
        self.assertTrue(int(fade[5, 0, 0]) > 250 and np.array_equal(fade[5, 99], img[5, 99]))      # fully red at the start, the picture at the end
        rad = il.gradient_fill(img, (50, 10), (60, 10), (0, 0, 0), (255, 255, 255), 'radial')
        self.assertTrue(int(rad[10, 50, 0]) <= 2 and int(rad[10, 90, 0]) >= 253)
        refl = il.gradient_fill(img, (50, 10), (90, 10), (0, 0, 0), (255, 255, 255), 'reflected')
        self.assertTrue(abs(int(refl[5, 30, 0]) - int(refl[5, 70, 0])) <= 2)                        # symmetric about the start
        ang = il.gradient_fill(img, (50, 10), (60, 10), (0, 0, 0), (255, 255, 255), 'angle')
        self.assertTrue(int(ang[10, 70, 0]) <= 5)
        self.assertIs(il.gradient_fill(img, (3, 3), (3, 3), (0, 0, 0), (255, 255, 255)), img)
        half = il.gradient_fill(img, (0, 10), (99, 10), (0, 0, 0), (0, 0, 0), opacity=50)
        self.assertTrue(62 <= int(half[5, 5, 0]) <= 66)
        area = il.gradient_fill(img, (0, 10), (99, 10), (0, 0, 0), (255, 255, 255), region=(0, 0, 40, 20))
        self.assertTrue(np.array_equal(area[:, 40:], img[:, 40:]) and not np.array_equal(area[:, :40], img[:, :40]))
        layer = np.zeros((20, 100, 4), np.uint8)
        gl = il.gradient_fill(layer, (0, 10), (99, 10), (0, 0, 0), (255, 255, 255))
        self.assertTrue((gl[..., 3] == 255).all())

    def test_shortcut_table_is_complete_and_consistent(self):
        self.assertGreater(len(il.SHORTCUTS), 25)
        for row in il.SHORTCUTS:
            self.assertEqual(len(row), 4)
            self.assertTrue(all(isinstance(x, str) and x.strip() for x in row), row)
        keys = " ".join(r[1] for r in il.SHORTCUTS)
        for needed in ("Ctrl+Z", "Space + drag", "Ctrl + click", "X / D", "Esc", "Ctrl+0"):
            self.assertIn(needed, keys)
        self.assertEqual(len({(r[0], r[1]) for r in il.SHORTCUTS}), len(il.SHORTCUTS))      # no row twice

    def test_step_size(self):
        self.assertEqual(il.step_size(10, 1), 11)
        self.assertEqual(il.step_size(10, -1), 9)
        self.assertEqual(il.step_size(100, 1), 110)
        self.assertEqual(il.step_size(3, -1), 2)
        self.assertEqual(il.step_size(1, -1), 1)                                           # never below the minimum
        self.assertEqual(il.step_size(400, 1, 1, 400), 400)
        self.assertEqual(il.step_size(395, 1, 1, 400), 400)
        n = 5
        for _ in range(60):
            n = il.step_size(n, 1, 1, 500)
        self.assertEqual(n, 500)                                                           # keeps growing until the top

    def test_clear_region(self):
        rgb = np.full((20, 30, 3), 100, np.uint8)
        out = il.clear_region(rgb, (255, 255, 255), (5, 5, 15, 15))
        self.assertTrue((out[5:15, 5:15] == 255).all() and (out[:5] == 100).all() and (out[:, 20:] == 100).all())
        self.assertIs(il.clear_region(rgb, (255, 255, 255), None), rgb)
        self.assertIs(il.clear_region(rgb, (100, 100, 100), (0, 0, 10, 10)), rgb)            # already that colour
        self.assertIs(il.clear_region(rgb, (1, 2, 3), (40, 40, 50, 50)), rgb)               # outside
        rgba = np.zeros((20, 30, 4), np.uint8)
        rgba[..., :3], rgba[..., 3] = 100, 255
        gone = il.clear_region(rgba, None, (5, 5, 15, 15))
        self.assertTrue((gone[5:15, 5:15, 3] == 0).all() and (gone[:5, :, 3] == 255).all() and (gone[..., :3] == 100).all())
        filled = il.clear_region(rgba, (9, 8, 7), (5, 5, 15, 15))
        self.assertTrue((filled[5:15, 5:15, :3] == (9, 8, 7)).all() and (filled[5:15, 5:15, 3] == 255).all())
        shape = il.feather_selection(il.combine_selection(None, il.polygon_mask((20, 30), [(5, 5), (24, 5), (24, 14), (5, 14)]), 'replace', (20, 30)), 4, (20, 30))
        soft = il.clear_region(rgb, (255, 255, 255), shape)
        self.assertTrue(int(soft[10, 14, 0]) > 240 and 100 < int(soft[10, 4, 0]) < 220 and int(soft[18, 28, 0]) < 110)

    def test_tool_layout_covers_every_built_in_tool(self):
        listed = {name: g for g, names in il.TOOL_LAYOUT.items() for name in names}
        self.assertEqual(len(listed), sum(len(v) for v in il.TOOL_LAYOUT.values()))          # no tool listed twice
        for name, tool in il.TOOLS.items():
            if name in listed:
                self.assertEqual(tool.group, listed[name], name)                              # the decorator and the table agree
            elif tool.group in il.TOOL_LAYOUT:
                self.fail("%s is in the group %s but not in TOOL_LAYOUT" % (name, tool.group))
        for name in listed:
            if name != "Threshold (black & white)":                                           # that one comes from a plug-in
                self.assertIn(name, il.TOOLS)
        mouse = {g for g, names in il.TOOL_LAYOUT.items() if any(il.TOOLS[n].interactive or il.TOOLS[n].layer for n in names if n in il.TOOLS)}
        self.assertEqual(mouse, {"Draw", "Retouch", "Annotate", "Layers"})                    # the groups of mouse tools, and only those

    def test_toolbox_icons_and_search(self):
        names = [n for _, ns in il.TOOLBOX for n in ns]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(il.TOOLBOX[0][0], "Select")                                           # the selection tools come first
        for n in names:
            if n not in ("Rectangle", "Magic wand", "Lasso", "Hand"):
                self.assertIn(n, il.TOOLS)                                                   # every toolbox button is a real tool
                self.assertTrue(il.TOOLS[n].interactive or il.TOOLS[n].layer, n)               # and a mouse tool, not a one-shot operation
        mouse = {t.name for t in il.TOOLS.values() if t.interactive or t.layer}
        self.assertEqual(mouse, {n for n in names if n in il.TOOLS})                           # every mouse tool has its button
        icons = {n: il.tool_icon(n) for n in names}
        for n, im in icons.items():
            self.assertEqual(im.size, (26, 26))
            self.assertGreater(int(np.asarray(im)[..., 3].astype(int).sum()), 26 * 8, n)       # something is drawn
        self.assertEqual(len({im.tobytes() for im in icons.values()}), len(icons))             # all different
        self.assertEqual(il.tool_icon("No such tool", 32).size, (32, 32))
        self.assertEqual(il.search_tools(""), [])
        self.assertEqual(il.search_tools("curv"), ["Curves"])
        self.assertEqual(il.search_tools("ROTATE"), ["Rotate 90 / 180", "Rotate (any angle)"])
        self.assertIn("Pencil", il.search_tools("draw pen"))                                   # words may come from the group
        self.assertEqual(il.search_tools("zzzz"), [])
        self.assertEqual(il.search_tools("blur")[0], "Blur (Gaussian)")

    def test_tools_are_registered_as_interactive(self):
        for name in ("Pencil", "Paintbrush", "Fill bucket", "Gradient"):
            self.assertTrue(il.TOOLS[name].interactive, name)
            self.assertEqual(il.TOOLS[name].group, "Draw")
        self.assertEqual(il.TOOLS["Pencil"].interactive, 'paint')


class PickedColourFillTests(unittest.TestCase):
    def test_erase_colours_can_paint_with_a_picked_colour(self):
        img = np.full((20, 40, 3), 235, np.uint8)
        img[5:10, 5:30] = (200, 0, 0)
        out = il.apply_tool("Erase / keep colours", img,
                            {'colors': '#c80000', 'tolerance': 30, 'fill': 'picked colour', 'fill_color': '#336699'})
        self.assertTrue((out[5:10, 5:30] == (0x33, 0x66, 0x99)).all())
        self.assertTrue((out[0] == 235).all())
        keep = il.apply_tool("Erase / keep colours", img,
                             {'colors': '#c80000', 'tolerance': 30, 'mode': 'keep only', 'fill': 'picked colour',
                              'fill_color': '#00ff00'})
        self.assertTrue((keep[5:10, 5:30] == (200, 0, 0)).all() and (keep[0] == (0, 255, 0)).all())

    def test_on_a_layer_the_alpha_is_kept_when_painting_with_a_colour(self):
        a = np.zeros((20, 40, 4), np.uint8)
        a[..., :3] = 255
        a[5:15, 5:35] = (200, 0, 0, 255)
        out = il.apply_tool_to_layer("Erase / keep colours", a, {'colors': '#c80000', 'tolerance': 30,
                                                                 'fill': 'picked colour', 'fill_color': '#0000ff'})
        self.assertTrue((out[5:15, 5:35, :3] == (0, 0, 255)).all())
        self.assertTrue(np.array_equal(out[..., 3], a[..., 3]))


class IntersectTests(unittest.TestCase):
    def test_intersect_rect(self):
        self.assertEqual(il.intersect_rect((0, 0, 10, 10), (5, 5, 20, 20)), (5, 5, 10, 10))
        self.assertEqual(il.intersect_rect((0, 0, 10, 10), (2, 3, 4, 5)), (2, 3, 4, 5))
        r = il.intersect_rect((0, 0, 10, 10), (20, 20, 30, 30))
        self.assertTrue(r[2] <= r[0] or r[3] <= r[1])                                  # no overlap: empty


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
