# -*- coding: utf-8 -*-
"""UIA perception layer: cached snapshot of a window's accessibility tree.

Key facts verified on this machine (Win11 26200):
  * BoundingRectangle from COM is (left, top, WIDTH, HEIGHT) -- not l,t,r,b.
  * BuildUpdatedCache(Subtree) + recursive GetCachedChildren() is ~13x faster
    than reading properties one by one.
  * Background/occluded windows return degraded trees; always activate first.
"""
from __future__ import annotations
import time
import comtypes.client
from comtypes import GUID
try:
    from comtypes.gen import UIAutomationClient as UIA
except ImportError:
    comtypes.client.GetModule('UIAutomationCore.dll')
    from comtypes.gen import UIAutomationClient as UIA

CLSID_CUIAutomation = GUID('{FF48DBA4-60EF-4201-AA87-54103EEF594E}')

PROPS = (
    UIA.UIA_BoundingRectanglePropertyId,
    UIA.UIA_ControlTypePropertyId,
    UIA.UIA_NamePropertyId,
    UIA.UIA_AutomationIdPropertyId,
    UIA.UIA_ClassNamePropertyId,
    UIA.UIA_IsEnabledPropertyId,
    UIA.UIA_IsOffscreenPropertyId,
    UIA.UIA_IsInvokePatternAvailablePropertyId,
    UIA.UIA_IsValuePatternAvailablePropertyId,
    UIA.UIA_IsTogglePatternAvailablePropertyId,
    UIA.UIA_IsSelectionItemPatternAvailablePropertyId,
    UIA.UIA_IsScrollPatternAvailablePropertyId,
    UIA.UIA_IsKeyboardFocusablePropertyId,
    UIA.UIA_HasKeyboardFocusPropertyId,
)

CONTROL_TYPES = {
    50000: 'Button', 50001: 'Calendar', 50002: 'CheckBox', 50003: 'ComboBox',
    50004: 'Edit', 50005: 'Hyperlink', 50006: 'Image', 50007: 'ListItem',
    50008: 'List', 50009: 'Menu', 50010: 'MenuBar', 50011: 'MenuItem',
    50012: 'ProgressBar', 50013: 'RadioButton', 50014: 'ScrollBar',
    50015: 'Slider', 50016: 'Spinner', 50017: 'StatusBar', 50018: 'Tab',
    50019: 'TabItem', 50020: 'Text', 50021: 'ToolBar', 50022: 'ToolTip',
    50023: 'Tree', 50024: 'TreeItem', 50025: 'Custom', 50026: 'Group',
    50027: 'Thumb', 50028: 'DataGrid', 50029: 'DataItem', 50030: 'Document',
    50031: 'SplitButton', 50032: 'Window', 50033: 'Pane', 50034: 'Header',
    50035: 'HeaderItem', 50036: 'Table', 50037: 'TitleBar', 50038: 'Separator',
}

# Only these can meaningfully contain other elements. Calling GetCachedChildren
# on a Text/Image/Button is a wasted cross-process COM round-trip, and those
# leaves are the majority of a large tree.
CONTAINER_TYPES = {
    'Window', 'Pane', 'Group', 'Custom', 'List', 'Tree', 'Table', 'DataGrid',
    'MenuBar', 'Menu', 'ToolBar', 'Tab', 'Document', 'ListItem', 'TreeItem',
    'DataItem', 'Header', 'HeaderItem', 'SplitButton', 'StatusBar',
}

# control types a human can actually interact with
INTERACTIVE = {
    'Button', 'CheckBox', 'ComboBox', 'Edit', 'Hyperlink', 'ListItem',
    'MenuItem', 'RadioButton', 'Slider', 'SplitButton', 'TabItem',
    'TreeItem', 'DataItem', 'HeaderItem', 'Document',
}


class Uia:
    def __init__(self):
        self.u = comtypes.client.CreateObject(CLSID_CUIAutomation,
                                              interface=UIA.IUIAutomation)
        self._cr = None

    def _cache(self):
        if self._cr is None:
            cr = self.u.CreateCacheRequest()
            cr.TreeScope = UIA.TreeScope_Subtree
            cr.TreeFilter = self.u.ControlViewCondition
            for p in PROPS:
                cr.AddProperty(p)
            self._cr = cr
        return self._cr

    @staticmethod
    def _gp(el, pid, default=None):
        try:
            v = el.GetCachedPropertyValue(pid)
            return default if v is None else v
        except Exception:
            return default

    def snapshot(self, hwnd, max_nodes=12000, max_depth=20):
        """Return (nodes, build_ms, walk_ms). nodes is a flat list, tree-ordered."""
        win = self.u.ElementFromHandle(hwnd)
        t0 = time.perf_counter()
        root = win.BuildUpdatedCache(self._cache())
        build_ms = (time.perf_counter() - t0) * 1000

        nodes = []
        stack = [(root, 0, -1)]
        t0 = time.perf_counter()
        while stack and len(nodes) < max_nodes:
            el, depth, parent = stack.pop()
            raw = self._gp(el, UIA.UIA_BoundingRectanglePropertyId)
            rect = None
            if raw and len(raw) == 4:
                l, t, w, h = raw
                if w > 0 and h > 0:
                    rect = (int(l), int(t), int(l + w), int(t + h))
            ctype = CONTROL_TYPES.get(self._gp(el, UIA.UIA_ControlTypePropertyId, -1), '?')
            rec = {
                'idx': len(nodes), 'parent': parent, 'depth': depth,
                'type': ctype,
                'name': (self._gp(el, UIA.UIA_NamePropertyId, '') or '').strip()[:120],
                'aid': (self._gp(el, UIA.UIA_AutomationIdPropertyId, '') or '')[:80],
                'cls': (self._gp(el, UIA.UIA_ClassNamePropertyId, '') or '')[:80],
                'rect': rect,
                'enabled': bool(self._gp(el, UIA.UIA_IsEnabledPropertyId, True)),
                'offscreen': bool(self._gp(el, UIA.UIA_IsOffscreenPropertyId, False)),
                'focusable': bool(self._gp(el, UIA.UIA_IsKeyboardFocusablePropertyId, False)),
                'focused': bool(self._gp(el, UIA.UIA_HasKeyboardFocusPropertyId, False)),
                'invoke': bool(self._gp(el, UIA.UIA_IsInvokePatternAvailablePropertyId, False)),
                'value': bool(self._gp(el, UIA.UIA_IsValuePatternAvailablePropertyId, False)),
                'toggle': bool(self._gp(el, UIA.UIA_IsTogglePatternAvailablePropertyId, False)),
                'select': bool(self._gp(el, UIA.UIA_IsSelectionItemPatternAvailablePropertyId, False)),
                'scroll': bool(self._gp(el, UIA.UIA_IsScrollPatternAvailablePropertyId, False)),
                'el': el,   # live COM element, not serializable -- stripped before logging
            }
            rec['interactive'] = (bool(rec['invoke'] or rec['value'] or rec['toggle']
                                       or rec['select'])
                                  or rec['type'] in INTERACTIVE)
            nodes.append(rec)
            if depth < max_depth and (depth == 0 or ctype in CONTAINER_TYPES):
                try:
                    kids = el.GetCachedChildren()
                except Exception:
                    kids = None
                if kids:
                    for i in range(kids.Length - 1, -1, -1):
                        try:
                            stack.append((kids.GetElement(i), depth + 1, rec['idx']))
                        except Exception:
                            pass
        walk_ms = (time.perf_counter() - t0) * 1000
        return nodes, build_ms, walk_ms


def visible_elements(nodes):
    """Elements a human could see and act on right now."""
    out = []
    for n in nodes:
        if n['rect'] is None or n['offscreen'] or not n['enabled']:
            continue
        if not n['interactive']:
            continue
        out.append(n)
    return out


def coverage(elems, window_rect, grid=24) -> float:
    """Fraction of the window surface that any element actually accounts for.

    A real widget tree explains its own pixels: the editor covers the editor
    area, the page covers the viewport. A canvas app only explains its window
    chrome -- the middle is one unexplained void. This is the signal that
    separates "large window, few controls" from "controls simply not exposed".
    """
    if not window_rect or len(window_rect) != 4:
        return 1.0
    l, t, r, b = window_rect
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        return 1.0
    cells = set()
    for e in elems:
        er = e.get('rect')
        if not er:
            continue
        x0 = max(l, er[0]); y0 = max(t, er[1])
        x1 = min(r, er[2]); y1 = min(b, er[3])
        if x1 <= x0 or y1 <= y0:
            continue
        for gx in range(int((x0 - l) * grid / w), min(grid, int((x1 - l) * grid / w) + 1)):
            for gy in range(int((y0 - t) * grid / h), min(grid, int((y1 - t) * grid / h) + 1)):
                cells.add((gx, gy))
    return len(cells) / float(grid * grid)


def quality_detail(elems, window_rect=None):
    """How much the accessibility tree actually tells us about this window.

    Three ratios, combined as a geometric mean so that any single broken
    dimension drags the score down. Then multiplied by a density factor: a large
    window exposing only a handful of elements is a drawn surface wearing window
    chrome, not a real widget tree -- that is exactly the canvas/game case.
    """
    n = len(elems)
    if n == 0:
        return {'n': 0, 'named': 0.0, 'actionable': 0.0, 'rect': 0.0,
                'coverage': 0.0, 'score': 0.0}
    named = sum(1 for x in elems if x['name']) / float(n)
    act = sum(1 for x in elems
              if x['invoke'] or x['value'] or x['toggle'] or x['select']) / float(n)
    rect = sum(1 for x in elems if x['rect']) / float(n)
    geo = (named * act * rect) ** (1.0 / 3.0) if (named and act and rect) else 0.0

    cov = coverage(elems, window_rect)
    return {'n': n, 'named': round(named, 3), 'actionable': round(act, 3),
            'rect': round(rect, 3), 'coverage': round(cov, 3),
            'score': round(geo * cov, 3)}


def quality_score(elems) -> float:
    return quality_detail(elems)['score']


def fingerprint(nodes):
    """Cheap state signature: what a user would call 'the screen changed'."""
    items = []
    texts = []
    focus = ''
    for n in nodes:
        if n['rect'] is None:
            continue
        items.append((n['type'], n['name'][:40], n['rect']))
        if n['focused']:
            focus = n['name'][:40]
        if n['type'] in ('Edit', 'Document') and n['value'] and len(texts) < 3:
            v = read_text(n, limit=200)
            if v is not None:
                texts.append(v)
    names = tuple(n['name'] for n in nodes[:250] if n['rect'] and n['name'])
    items.sort()
    return {'sig': hash(tuple(items[:250])), 'texts': tuple(texts), 'focus': focus,
            'count': len(items), 'names': names}


def diff_summary(before, after):
    """Human/model readable description of what changed."""
    parts = []
    if before['texts'] != after['texts']:
        parts.append('内容变为 %r' % (after['texts'][0][:60] if after['texts'] else '空'))
    added = [x for x in after['names'] if x not in set(before['names'])][:2]
    gone = [x for x in before['names'] if x not in set(after['names'])][:2]
    if added or gone:
        seg = []
        if gone:
            seg.append('消失 %r' % (gone,))
        if added:
            seg.append('出现 %r' % (added,))
        parts.append('文本' + '，'.join(seg))
    if before['focus'] != after['focus']:
        parts.append('焦点变为 %r' % after['focus'])
    if before['count'] != after['count']:
        parts.append('元素数 %d→%d' % (before['count'], after['count']))
    if before['sig'] != after['sig'] and not parts:
        parts.append('界面结构有变化')
    return '；'.join(parts) if parts else '无变化'


def read_text(node, limit=240):
    """Read an editable control's content.

    ValuePattern covers plain edit boxes. Word and Outlook documents expose
    their body through TextPattern instead; returning None there would leave the
    agent blind to the very document it is supposed to edit.
    """
    try:
        from comtypes.gen.UIAutomationClient import (
            UIA_ValuePatternId, IUIAutomationValuePattern)
        p = node['el'].GetCurrentPattern(UIA_ValuePatternId)
        vp = p.QueryInterface(IUIAutomationValuePattern)
        v = vp.CurrentValue or ''
        return v[:limit]
    except Exception:
        pass
    try:
        from comtypes.gen.UIAutomationClient import (
            UIA_TextPatternId, IUIAutomationTextPattern)
        p = node['el'].GetCurrentPattern(UIA_TextPatternId)
        tp = p.QueryInterface(IUIAutomationTextPattern)
        return (tp.DocumentRange.GetText(limit) or '')[:limit]
    except Exception:
        return None


def to_prompt(elems, limit=180):
    """Compact text rendering for the planner: one line per element."""
    lines = []
    for i, n in enumerate(elems[:limit]):
        lab = n['name'] or n['aid'] or n['cls'] or ''
        lines.append(f"[{i}] {n['type']} \"{lab}\" "
                     f"@({n['rect'][0]},{n['rect'][1]},{n['rect'][2]},{n['rect'][3]})"
                     f"{' *' if n['focused'] else ''}")
    return '\n'.join(lines)
