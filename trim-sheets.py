#!/usr/bin/env python3
"""Crop gallery-sheet PNGs to their content length -- trims the dead whitespace at
the bottom (and tight margins), keeping full width. Background color is sampled from
a corner so it works on any theme. Usage: trim-sheets.py [dir]  (default ~/Desktop/sol-gallery-sheets)"""
import os, sys, glob
from PIL import Image, ImageChops

d = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/Desktop/sol-gallery-sheets")
PAD = 28  # px of breathing room kept below the last content
for path in sorted(glob.glob(os.path.join(d, "*.png"))):
    im = Image.open(path).convert("RGB")
    bg_color = im.getpixel((4, im.height - 4))            # bottom corner = background
    bg = Image.new("RGB", im.size, bg_color)
    bbox = ImageChops.difference(im, bg).getbbox()        # content bounding box
    if not bbox:
        continue
    bottom = min(im.height, bbox[3] + PAD)
    if bottom < im.height - 2:                            # only if there's slack to cut
        im.crop((0, 0, im.width, bottom)).save(path)
        print("trimmed %-52s %dpx -> %dpx" % (os.path.basename(path), im.height, bottom))
    else:
        print("kept    %s (no slack)" % os.path.basename(path))
