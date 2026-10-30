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
