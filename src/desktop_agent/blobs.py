# -*- coding: utf-8 -*-
"""Find clickable-looking regions on a drawn surface.

The vision model reads text well but localises pixels badly: in testing it put a
button at y=441 when it sat at y=630. Rather than asking it to estimate
coordinates, we segment the frame ourselves and hand it numbered regions, so it
only has to pick a number -- the same precise path used for real widgets.

Method: quantise the frame, then take the bounding box of every colour that
occupies a plausible fraction of the screen with a high fill density. Game and
canvas UIs are made of flat colour panels, so this finds them reliably, and it
costs one pass over a 160x120 thumbnail (~15 ms).
"""
from __future__ import annotations
from collections import Counter

from . import capture


def _code(raw, i):
    return ((raw[i + 2] >> 5) << 6) | ((raw[i + 1] >> 5) << 3) | (raw[i] >> 5)


def _color_name(rgb):
    r, g, b = rgb
    mx, mn = max(rgb), min(rgb)
    if mx < 55:
        return '深色'
    if mn > 195:
        return '白色'
    if mx - mn < 28:
        return '灰色'
    if r >= g and r >= b:
        if g < 90:
            return '红色'
        return '橙色' if g < 175 else '黄色'
    if g >= r and g >= b:
        return '绿色' if b < 130 else '青色'
    return '蓝色' if r < 130 else '紫色'


def detect(hwnd, gw=160, max_regions=10, min_frac=0.003, max_frac=0.40,
           min_density=0.35):
    raw, w, h = capture.sample(hwnd, gw)
    if not raw or w < 8 or h < 8:
        return []
    n = w * h
    total = float(n)

    stats = {}
    for p in range(n):
        i = p * 4
        c = ((raw[i + 2] >> 5) << 6) | ((raw[i + 1] >> 5) << 3) | (raw[i] >> 5)
        x = p % w
        y = p // w
        s = stats.get(c)
        if s is None:
            stats[c] = [1, x, y, x, y]
        else:
            s[0] += 1
            if x < s[1]:
                s[1] = x
            if y < s[2]:
                s[2] = y
            if x > s[3]:
                s[3] = x
            if y > s[4]:
                s[4] = y

    if not stats:
        return []
    bg = max(stats.items(), key=lambda kv: kv[1][0])[0]

    out = []
    for code, (count, l, t, r, b) in stats.items():
        if code == bg:
            continue
        frac = count / total
        bw = r - l + 1
        bh = b - t + 1
        if frac < min_frac or frac > max_frac:
            continue
        if bw < 5 or bh < 5:
            continue
        box = float(bw * bh)
        if box / total > 0.50:
            continue
        if count / box < min_density:     # scattered colour, not a panel
            continue
        out.append({'x': l, 'y': t, 'w': bw, 'h': bh, 'count': count,
                    'code': code, 'density': count / box})

    out.sort(key=lambda d: -d['count'])
    out = out[:max_regions]

    wr = capture.content_rect(hwnd)
    if not wr:
        return []
    W, H = wr[2] - wr[0], wr[3] - wr[1]
    sx, sy = W / float(w), H / float(h)
    for d in out:
        d['rect'] = (int(wr[0] + d['x'] * sx), int(wr[1] + d['y'] * sy),
                     int(wr[0] + (d['x'] + d['w']) * sx),
                     int(wr[1] + (d['y'] + d['h']) * sy))
        # Derive the colour from the quantised bucket, not by sampling the
        # centre pixel -- the centre usually lands on a label drawn on top.
        c = d['code']
        # encoding put R in the high bits, G in the middle, B in the low bits
        d['rgb'] = ((((c >> 6) & 7) << 5) + 16, (((c >> 3) & 7) << 5) + 16,
                    ((c & 7) << 5) + 16)
        d['color'] = _color_name(d['rgb'])
    return out
