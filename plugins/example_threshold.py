"""Example plug-in: a black-and-white threshold.

Every .py file in this folder is run at start-up. `tool`, `Param`, `np`, `cv2` and `Image` are already
available, so a new tool is just a function with the decorator:

    @tool(name, group, [Param(key, label, kind, default, lo, hi, ...)], geometry=False, preview=True)
    def my_tool(img, <one argument per Param key>):
        ...                      # img: (H, W, 3) uint8, do not modify it in place
        return new_img           # or `img` itself when nothing changes

Param kinds: 'int', 'float' (lo, hi, step, slider=True), 'bool', 'choice' (choices=(...)). A tool with
geometry=True changes the whole image and ignores the selection; any other tool is limited to the selection.
A file whose name starts with an underscore is skipped.
"""


@tool("Threshold (black & white)", "Tone", [
    Param('level', "Level (0 = Otsu)", 'int', 128, 0, 255, slider=True),
    Param('invert', "Invert", 'bool', False)])
def threshold(img, level, invert):
    """Pixels brighter than the level become white, the others black. Level 0 picks it automatically (Otsu)."""
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    flag = cv2.THRESH_BINARY_INV if invert else cv2.THRESH_BINARY
    if level == 0:
        flag |= cv2.THRESH_OTSU
    _, bw = cv2.threshold(gray, level, 255, flag)
    return cv2.cvtColor(bw, cv2.COLOR_GRAY2RGB)
