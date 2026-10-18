# -*- coding: utf-8 -*-
"""Macro cache with parameterised tasks.

A raw "same task string" cache is nearly useless: change one word and it never
hits. We instead store a TEMPLATE -- quoted fragments of the instruction become
slots -- so "把文档内容替换成「A」" also serves "把文档内容替换成「B」", with the
slot value substituted into the recorded action payloads.

Replay stays defensive: every step re-locates its target by normalised name, and
any missing target or no-op step aborts the replay and hands control back.
"""
from __future__ import annotations
import hashlib, json, os, re, time

from . import config

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = config.MACROS_DIR

QUOTE_RE = re.compile(r'[「『“"\']([^」』”"\']{1,200})[」』”"\']')
COLON_RE = re.compile(r'[：:]\s*([^，；\n]{1,200}?)\s*[。！？.!?]?\s*$')


def _norm(s: str) -> str:
    """Strip volatile parts of a label: digits, whitespace, case."""
    s = (s or '').strip().lower()
    s = re.sub(r'[0-9]+', '#', s)
    s = re.sub(r'\s+', '', s)
    return s


def extract_params(task: str):
    vals = QUOTE_RE.findall(task)
    if not vals:
        m = COLON_RE.search(task.strip())
        if m:
            vals = [m.group(1).strip()]
    seen, out = set(), []
    for v in vals:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def build_template(task: str):
    params = extract_params(task)
    tpl = task
    for i, v in enumerate(params):
        tpl = tpl.replace(v, '{%d}' % i, 1)
    return tpl, params


def _tpl_regex(tpl: str):
    parts = re.split(r'(\{\d+\})', tpl)
    out = []
    for p in parts:
        if re.fullmatch(r'\{\d+\}', p):
            out.append('(.*?)')
        else:
            out.append(re.escape(p))
    return re.compile(''.join(out) + r'\Z', re.S)


def _key(template: str, cls: str) -> str:
    return hashlib.sha1(('%s\x00%s' % (template, cls)).encode('utf-8')).hexdigest()[:16]


def _path(key: str) -> str:
    os.makedirs(DIR, exist_ok=True)
    return os.path.join(DIR, key + '.json')


_VERB = {
    'click': lambda s: '点击「%s」' % (s.get('target') or '控件'),
    'right_click': lambda s: '右键「%s」' % (s.get('target') or '控件'),
    'double_click': lambda s: '双击「%s」' % (s.get('target') or '控件'),
    'type': lambda s: '在「%s」输入文字' % (s.get('target') or '焦点处'),
    'set_text': lambda s: '把「%s」的内容整体替换' % (s.get('target') or '控件'),
    'hotkey': lambda s: '按 ' + '+'.join(s.get('keys') or []),
    'drag': lambda s: '把「%s」拖到「%s」' % (s.get('target') or '对象', s.get('to', '?')),
    'scroll': lambda s: '滚动',
    'click_at': lambda s: '在画面坐标处点击',
}


def summarize(steps):
    """Plain-language description of what a skill DOES.

    The selector used to see only the recorded task wording, so a differently
    phrased instruction looked unrelated even when the action was identical.
    """
    parts = []
    for st in steps:
        fn = _VERB.get(st.get('action'))
        if not fn:
            continue
        try:
            parts.append(fn(st))
        except Exception:
            parts.append(st.get('action', '?'))
    if not parts:
        return '（无描述）'
    head = parts[0]
    rest = parts[1:]
    return head + ('，然后' + '，然后'.join(rest) if rest else '')


def save(task: str, cls: str, steps, meta=None):
    if not steps:
        return None
    tpl, params = build_template(task)
    key = _key(tpl, cls)
    p = _path(key)
    runs = 1
    hits = 0
    if os.path.exists(p):
        try:
            old = json.load(open(p, encoding='utf-8'))
            runs = old.get('runs', 0) + 1
            hits = old.get('hits', 0)
        except Exception:
            pass
    rec = {'template': tpl, 'params': params, 'cls': cls, 'steps': steps,
           'summary': summarize(steps),
           'sample_task': task, 'created': time.strftime('%Y-%m-%d %H:%M:%S'),
           'runs': runs, 'hits': hits, 'meta': meta or {}}
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    return rec


def _all_macros():
    out = []
    if not os.path.isdir(DIR):
        return out
    for fn in os.listdir(DIR):
        if not fn.endswith('.json'):
            continue
        try:
            out.append(json.load(open(os.path.join(DIR, fn), encoding='utf-8')))
        except Exception:
            pass
    return out


def candidates(task: str, cls: str):
    """All skills for this window that could take the task's literals."""
    literals = extract_params(task)
    out = [m for m in _all_macros()
           if m.get('cls') == cls and m.get('steps')
           and len(m.get('params') or []) == len(literals)]
    return out, literals


def find_semantic(task: str, cls: str):
    """Fallback when no template matches: the same window, the same NUMBER of
    literals, and exactly one such skill. Wording becomes irrelevant -- which is
    the point of a skill rather than a recorded string."""
    literals = extract_params(task)
    cands = [m for m in _all_macros()
             if m.get('cls') == cls and m.get('steps')
             and len(m.get('params') or []) == len(literals)]
    if len(cands) != 1:
        return None, None
    return cands[0], literals


def find(task: str, cls: str):
    """Return (macro, new_params) for the best template match, else (None, None)."""
    if not os.path.isdir(DIR):
        return None, None
    best = None
    for fn in os.listdir(DIR):
        if not fn.endswith('.json'):
            continue
        try:
            m = json.load(open(os.path.join(DIR, fn), encoding='utf-8'))
        except Exception:
            continue
        if m.get('cls') != cls or not m.get('steps'):
            continue
        mm = _tpl_regex(m.get('template', '')).match(task)
        if not mm:
            continue
        # exact-string matches win over template matches
        exact = (m.get('sample_task') == task)
        score = (0 if exact else 1, len(m.get('steps', [])))
        if best is None or score < best[0]:
            best = (score, m, list(mm.groups()))
    if best is None:
        # No template match. Do NOT fall back to "the only skill with the same
        # parameter count" -- that replays "replace text" for a task that asked
        # to search. The caller resolves ambiguity with one explicit choice call.
        return None, None
    return best[1], best[2]


def bump_hit(macro):
    try:
        macro['hits'] = macro.get('hits', 0) + 1
        p = _path(_key(macro['template'], macro['cls']))
        if os.path.exists(p):
            with open(p, encoding='utf-8') as f:
                cur = json.load(f)
            cur['hits'] = macro['hits']
            with open(p, 'w', encoding='utf-8') as f:
                json.dump(cur, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _subst(value, old_params, new_params):
    if not isinstance(value, str) or not old_params:
        return value
    for old, new in zip(old_params, new_params):
        if old and old != new:
            value = value.replace(old, new)
    return value


def _jaccard(a, b) -> float:
    sa = {_norm(x) for x in a if x}
    sb = {_norm(x) for x in b if x}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def _match(elems, target):
    if not target:
        return None
    want = target.strip()
    nwant = _norm(want)
    best = None
    for n in elems:
        nm = (n['name'] or '').strip()
        if not nm:
            continue
        if nm == want:
            return n
        if _norm(nm) == nwant:
            return n
        if nwant and (nwant in _norm(nm) or _norm(nm) in nwant):
            score = abs(len(nm) - len(want))
            if best is None or score < best[0]:
                best = (score, n)
    return best[1] if best else None


def replay(agent, macro, new_params=None):
    """Run recorded steps. Returns dict(ok, message, done, total, detail)."""
    from .perception import fingerprint
    old_params = macro.get('params') or []
    new_params = new_params if new_params is not None else old_params
    from .window import is_window
    steps = macro['steps']
    done = 0
    log = []

    # Precondition: refuse to replay if the screen we start from is not the one
    # the macro was recorded on. Half-applying a macro to a different state is
    # worse than falling back to the planner.
    pre = (macro.get('meta') or {}).get('pre') or []
    sim = 1.0
    if pre:
        nodes_c, _e, _b, _w = agent._observe()
        cur = [n['name'] for n in nodes_c[:120] if n['name']]
        sim = _jaccard(pre, cur)
        if sim < 0.55:
            return {'ok': False, 'done': 0, 'total': len(steps), 'detail': [],
                    'message': '起始界面与录制时差异过大（相似度 %.2f），不重放' % sim}
    for i, raw in enumerate(steps):
        if agent.guard.aborted:
            return {'ok': False, 'message': '被中止', 'done': done, 'total': len(steps),
                    'detail': log}
        st = dict(raw)
        for k in ('text', 'target'):
            if st.get(k):
                st[k] = _subst(st[k], old_params, new_params)
        action = st.get('action')

        # follow the exact window the macro was recorded against: prefer the
        # recorded handle, then a title match, then process+class.
        wt_h = st.get('hwnd') or 0
        want_win = st.get('win') or ''
        need_switch = False
        if wt_h and is_window(wt_h) and wt_h != agent.hwnd:
            need_switch = True
        elif want_win and want_win != agent.win_key():
            need_switch = True
        if need_switch:
            if not agent.switch_to_win(want_win, st.get('wtitle', ''), wt_h):
                return {'ok': False, 'message': '第 %d 步找不到窗口 %s' % (i, want_win),
                        'done': done, 'total': len(steps), 'detail': log}

        nodes, elems, _b, _w = agent._observe()
        fp = fingerprint(nodes)
        plan = dict(st)

        if action in ('click', 'type', 'set_text'):
            node = _match(elems, st.get('target'))
            if node is None:
                return {'ok': False, 'message': '第 %d 步找不到控件「%s」' % (i, st.get('target')),
                        'done': done, 'total': len(steps), 'detail': log}
            plan['index'] = elems.index(node)
        elif action == 'click_at':
            if not st.get('x'):
                return {'ok': False, 'message': '第 %d 步缺坐标' % i,
                        'done': done, 'total': len(steps), 'detail': log}
        elif action not in ('hotkey', 'scroll', 'wait', 'set_cell', 'paste'):
            return {'ok': False, 'message': '第 %d 步动作类型不支持重放: %r' % (i, action),
                    'done': done, 'total': len(steps), 'detail': log}

        agent._record(step=i, event='macro_step', action=action,
                      target=st.get('target', ''), keys=st.get('keys'))
        # The live loop activates the target before acting; replay must too,
        # otherwise bare hotkeys such as Ctrl+A land on whatever has focus.
        if action in ('click', 'type', 'set_text', 'hotkey'):
            from .window import ensure_foreground
            if not ensure_foreground(agent.hwnd):
                return {'ok': False, 'message': '第 %d 步无法把目标窗口切到前台' % i,
                        'done': done, 'total': len(steps), 'detail': log}

        # Restore the control that had focus when this step was recorded.
        if st.get('focus'):
            fe = _match(elems, st['focus'])
            if (fe is not None and not fe.get('focused')
                    and fe['type'] not in ('Window', 'Pane', 'TitleBar', 'Group',
                                           'Custom', 'Image')):
                try:
                    agent.act.focus(fe)
                    time.sleep(0.1)
                except Exception:
                    pass
        if not agent.execute(action, plan, elems):
            return {'ok': False, 'message': '第 %d 步执行失败' % i,
                    'done': done, 'total': len(steps), 'detail': log}
        done += 1
        log.append('%s %s' % (action, st.get('target') or st.get('text') or ''))
        time.sleep(0.12)
        if action in ('click', 'type', 'set_text'):
            time.sleep(0.1)
            nodes2, _, _, _ = agent._observe()
            if fingerprint(nodes2) == fp:
                # A no-op FIRST step is usually "already in the desired state"
                # (e.g. the settings page is already open). Tolerate that only
                # when the starting screen matched what we recorded.
                if i == 0 and sim >= 0.75:
                    log.append('第 0 步无变化，视为已在目标状态')
                    continue
                return {'ok': False, 'message': '第 %d 步没有产生界面变化' % i,
                        'done': done, 'total': len(steps), 'detail': log}
    return {'ok': True, 'message': '重放 %d 步成功' % done, 'done': done,
            'total': len(steps), 'detail': log}
