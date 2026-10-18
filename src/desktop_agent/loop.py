# -*- coding: utf-8 -*-
"""Agent loop: observe -> plan -> act -> log, under three independent brakes."""
from __future__ import annotations
import json, os, threading, time, ctypes

from .perception import (Uia, visible_elements, to_prompt, read_text,
                         fingerprint, diff_summary, quality_detail)
from .actions import Actions
from .llm import Planner
from .capture import grab_window, signature, sig_diff, window_rect, content_rect, grab_screen
from . import macro as macro_store
from . import profiles
from . import config
from .guard import Guard
from .overlay import Overlay
from .window import (ensure_foreground, list_windows_ex, activate, window_title,
                     find_by_cls, find_window, find_window_prefer,
                     win_key_of, pid_of_window)  # noqa: F401
from .window import is_window as user32_is_window

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = config.RUNS_DIR

IMG_WIDTH = int(os.environ.get('GUI_AGENT_IMG_W', '1280'))

COLOR_TARGET = (255, 70, 70)
COLOR_DONE = (70, 220, 140)
COLOR_ARMED = (255, 120, 40)

SYSTEM = '''你是 Windows 桌面操作代理。每一轮你会收到当前窗口的控件清单，你需要决定下一步动作。

控件清单每行格式：
  [序号] 控件类型 "名称" @(左,上,右,下)  (* 表示当前焦点)

可用动作（只输出一个 JSON 对象）：
  {"thinking":"简短推理","action":"click","index":序号,"target":"该序号的名称原文"}
  {"thinking":"...","action":"right_click","index":序号,"target":"该序号的名称原文"}   # 只要弹右键菜单就用它，不要用 click 加参数
  {"thinking":"...","action":"double_click","index":序号,"target":"该序号的名称原文"}
  {"thinking":"...","action":"drag","from":起点序号,"target":"起点名称","to":终点序号}
  {"thinking":"...","action":"drag","x1":截图像素X,"y1":截图像素Y,"x2":截图像素X,"y2":截图像素Y}
  {"thinking":"...","action":"type","index":序号,"target":"该序号的名称原文","text":"要输入的文字"}
  {"thinking":"...","action":"paste","text":"要输入的文字"}   # 剪贴板粘贴
  {"thinking":"...","action":"set_cell","cell":"B2","text":"120"}   # Excel：一步写一个单元格
  {"thinking":"...","action":"select_cell","cell":"B2"}             # Excel：只选中单元格（不写入）
  {"thinking":"...","action":"format_cell","cell":"B2","format":"percent|bold|currency"}  # Excel：套数字/字体格式
  {"thinking":"...","action":"set_font_size","size":"16"}           # Word：设置字号
  {"thinking":"...","action":"save_as","path":"D:\\\\dir\\\\name.docx"}   # 一步完成另存为
  {"thinking":"...","action":"set_text","index":序号,"target":"该序号的名称原文","text":"要写入的全部内容"}
  {"thinking":"...","action":"hotkey","keys":["ctrl","s"]}
  {"thinking":"...","action":"scroll","amount":-3}
  {"thinking":"...","action":"click_at","x":截图像素X,"y":截图像素Y}
  {"thinking":"...","action":"right_click_at","x":截图像素X,"y":截图像素Y}
  {"thinking":"...","action":"double_click_at","x":截图像素X,"y":截图像素Y}
  {"thinking":"...","action":"look"}
  {"thinking":"...","action":"look_full"}   # 整屏截图：菜单/下拉/画廊弹出时用（窗口截图拍不到它们）
  {"thinking":"...","action":"switch","window":W序号,"target":"该窗口标题原文"}
  {"thinking":"...","action":"wait"}
  {"thinking":"...","action":"done","message":"完成说明"}
  {"thinking":"...","action":"fail","message":"失败原因"}

规则：
1. 优先用序号定位，不要猜坐标。凡是带 index 的动作，必须同时给出 target，内容是控件清单里该序号后面引号内的原文，一字不差。系统会核对，对不上就不执行。
2. type 是「在光标处插入」；set_text 是「整体替换控件内容」，只有明确要覆盖全部内容时才用 set_text。
3. 任务完成立刻 done，不要画蛇添足。
4. 只输出 JSON。
5. 判断成败要看「可编辑控件当前内容」，不要看标签页上的“未修改/已修改”，那只是保存状态，与你的输入无关。
7. 若控件清单里找不到你要的元素（界面是自绘的、游戏、画布），用 look 请求一张截图，再用 click_at 按坐标点击。
6. 同一个动作不要连续重复超过 2 次；没有进展就换方法，或直接 done/fail。
8. 具体软件的交互约定会按当前进程追加在下面；有冲突时以那份约定为准。'''

BATCH_RULE = '''
7. 如果接下来几步非常确定（例如连续按数字键），可以一次返回 steps 数组，
   但长度绝不能超过 4 个动作：
   {"thinking":"...","steps":[{"action":"click","index":3,"target":"一"},{"action":"click","index":4,"target":"二"}]}
   每一步仍必须带 target。超过 4 个会被整批拒绝。不确定时一次只给一个动作。'''


def _parse(txt):
    try:
        return json.loads(txt)
    except Exception:
        i, j = txt.find('{'), txt.rfind('}')
        if i >= 0 and j > i:
            try:
                return json.loads(txt[i:j + 1])
            except Exception:
                pass
    return None


class Agent:
    def __init__(self, hwnd, task, max_steps=100, dry_run=False, use_vision=False,
                 record_frames=True, memory_cap_gb=3.0, on_event=None):
        self.on_event = on_event
        self.hwnd = hwnd
        self.task = task
        self.max_steps = max_steps
        self.dry_run = dry_run
        self.use_vision = use_vision
        self.record_frames = record_frames

        self.uia = Uia()
        self.act = Actions(self.uia, dry_run=dry_run)
        self.guard = Guard(memory_limit_gb=memory_cap_gb, target_hwnd=hwnd,
                           hooks=os.environ.get('GUI_AGENT_HOOKS') == '1')
        self._beat = time.time()
        self._watchdog()
        self.overlay = None
        self.planner = Planner()

        stamp = time.strftime('%Y%m%d_%H%M%S')
        self.run_dir = os.path.join(RUNS, stamp)
        os.makedirs(os.path.join(self.run_dir, 'frames'), exist_ok=True)
        self.events = open(os.path.join(self.run_dir, 'events.jsonl'), 'w', encoding='utf-8')
        self._frame_idx = 0
        self.frame_bytes = 0
        self._recent = []
        self._prefetch = None        # snapshot taken right after the last action
        self._no_change = 0
        self._look = False           # model asked for a screenshot
        self._look_full = False      # ... of the whole desktop (menus live outside the window)
        self._last_png = None        # last screenshot actually sent
        self._last_png_sig = None    # its pixel signature, for reuse
        self._uia_hostile = False    # this window's UIA is slow AND useless
        self._img_rect = None        # rect of the image the model last saw
        self._img_size = None        # pixel size of that image
        self._literal = ''           # target text extracted from the instruction
        self._acted = 0              # actions executed so far
        self.use_macro = os.environ.get('GUI_AGENT_NO_MACRO') != '1'
        self._wins = []
        self.cls = ''
        self.trace = []              # replayable action record of this run
        self.started = time.time()

    # ---- plumbing ------------------------------------------------------
    def _watchdog(self, limit=240.0):
        """A wedged agent must never outlive its welcome.

        Every mechanism this agent installs (topmost overlay, hotkey, any hook)
        is removed when the process dies. So if the loop stops making progress,
        kill the process rather than leave the desktop in a half-controlled
        state that the user has to power-cycle out of.
        """
        def run():
            while True:
                time.sleep(5)
                if time.time() - self._beat > limit:
                    try:
                        os._exit(3)
                    except Exception:
                        pass
        threading.Thread(target=run, daemon=True, name='agent-watchdog').start()

    def _record(self, **kw):
        kw['t'] = round(time.time() - self.started, 3)
        self.events.write(json.dumps(kw, ensure_ascii=False) + '\n')
        self.events.flush()
        if self.on_event is not None:
            try:
                self.on_event(kw)
            except Exception:
                pass

    def _frame(self, png):
        if not self.record_frames or not png:
            return
        path = os.path.join(self.run_dir, 'frames', '%04d.png' % self._frame_idx)
        with open(path, 'wb') as f:
            f.write(png)
        self.frame_bytes += len(png)
        self._frame_idx += 1

    def _paint(self, boxes):
        if self.overlay is None:
            return
        try:
            self.overlay.boxes(boxes)
        except Exception:
            pass

    # ---- main ----------------------------------------------------------
    def run(self):
        self.guard.arm()
        try:
            self.overlay = Overlay()
        except Exception:
            self.overlay = None
        w, h = (self.overlay.w, self.overlay.h) if self.overlay else (0, 0)
        armed_frame = [(0, 0, w, h, COLOR_ARMED, 0)] if self.overlay else []

        history = []
        result = {'status': 'max_steps'}
        try:
            nodes0, elems0, b0, w0 = self._observe()
            self._prefetch = (nodes0, elems0, b0, w0)
            self.cls = (nodes0[0]['cls'] if nodes0 else '') or ''
            self._win_key = self.win_key()
            self._pre_names = [n['name'] for n in nodes0[:120] if n['name']]
            try:
                cands = macro_store.extract_params(self.task)
                if cands and len(cands[0]) >= 4:
                    self._literal = cands[0]
            except Exception:
                self._literal = ''

            # ---- zero-token path: try a recorded macro first --------------
            macro_done = False
            if self.use_macro:
                # A broken macro must never take the whole run down with it.
                try:
                    m, params = macro_store.find(self.task, self._win_key)
                    if m is None:
                        # No template matched, but several skills could apply.
                        # One cheap call to pick beats N planning calls later.
                        m, params = self._choose_skill()
                        if m is not None:
                            self._record(event='skill_chosen',
                                         template=m.get('template'))
                    if m:
                        r = macro_store.replay(self, m, params)
                        self._record(event='macro', ok=r['ok'], message=r['message'],
                                     done=r['done'], total=r['total'],
                                     template=m.get('template'))
                        print('  macro: %s — %s' % ('HIT' if r['ok'] else 'miss', r['message']))
                        if r['ok']:
                            macro_store.bump_hit(m)
                            result = {'status': 'done', 'message': r['message'], 'via': 'macro'}
                            macro_done = True
                        elif r['done']:
                            # Partial replay: those steps already landed. Tell the
                            # planner so it resumes instead of redoing them.
                            history.append(
                                '宏回放已执行前 %d/%d 步（%s），随后中止：%s。'
                                '这些步骤已经生效，请从当前界面继续，不要重复。'
                                % (r['done'], r['total'],
                                   '；'.join(r['detail'][-4:]), r['message']))
                except Exception as e:
                    self._record(event='macro_error', err=repr(e))
                    print('  macro: error %r — falling back to planner' % (e,))

            for step in ([] if macro_done else range(self.max_steps)):
                self._beat = time.time()
                self._follow_dialog()
                if self.guard.aborted:
                    result = {'status': 'aborted', 'reason': self.guard.reason()}
                    break

                if self._prefetch is not None:
                    nodes, elems, build, walk = self._prefetch
                    self._prefetch = None
                else:
                    ensure_foreground(self.hwnd)   # a background window reports an empty tree
                    nodes, elems, build, walk = self._observe()
                fp_before = fingerprint(nodes)
                listing = to_prompt(elems, limit=140)
                title = nodes[0]['name'] if nodes else (window_title(self.hwnd) or '')

                # The model is blind to widget contents unless we hand them over;
                # without this it cannot tell success from failure.
                contents = []
                for n in elems:
                    # Word/Outlook bodies expose TextPattern, not ValuePattern,
                    # so do not gate this on n['value'].
                    if n['type'] in ('Edit', 'Document'):
                        v = read_text(n)
                        if v is not None:
                            contents.append('  <%s> = %r' % (n['name'] or n['type'], v))
                    if len(contents) >= 3:
                        break
                self._wins = [w for w in list_windows_ex() if w[1] and w[0] != self.hwnd][:8]
                body = ('任务：%s\n\n当前窗口：%s\n' % (self.task, title))
                if self._wins:
                    body += '其他窗口（要操作它们就用 switch 切换）：\n'
                    for i, w in enumerate(self._wins):
                        body += '  [W%d] %s — %s\n' % (i, w[4], w[1][:50])
                body += '控件清单：\n%s\n' % listing
                if contents:
                    body += '\n可编辑控件当前内容：\n%s\n' % '\n'.join(contents)
                body += '\n已完成步骤：%s\n请给出下一步。' % ('；'.join(history[-6:]) or '无')
                user = body

                # ---- vision policy --------------------------------------
                # Spend an image only when text alone is likely insufficient:
                # a window with almost no accessibility tree, a stuck run, or an
                # explicit request from the model.
                q = quality_detail(elems, nodes[0]['rect'] if nodes else None)
                poor = q['score'] < 0.35
                stuck = self._no_change >= 2
                send_image = bool(self.use_vision or poor or stuck or self._look)
                reused = False
                full = self._look_full
                self._look_full = False
                if send_image or self.record_frames:
                    if send_image:
                        self._paint([])          # never let our annotations leak into the model's view
                    if full:
                        # Menus and galleries are separate windows; only a
                        # desktop capture shows what is actually on screen.
                        png, img_w, img_h = grab_screen(1600)
                        sw = ctypes.windll.user32.GetSystemMetrics(0)
                        sh = ctypes.windll.user32.GetSystemMetrics(1)
                        self._img_rect = (0, 0, sw, sh)
                        self._img_size = (img_w, img_h)
                        self._last_png = None
                        self._last_png_sig = None
                    else:
                        try:
                            cur_sig = signature(self.hwnd, 64, 36)
                        except Exception:
                            cur_sig = None
                        self._img_rect = content_rect(self.hwnd)
                        if (cur_sig is not None and cur_sig == self._last_png_sig
                                and self._last_png):
                            png = self._last_png
                            reused = True
                        else:
                            png, img_w, img_h = grab_window(self.hwnd, IMG_WIDTH)
                            self._img_size = (img_w, img_h)
                            self._last_png = png
                            self._last_png_sig = cur_sig
                    self._frame(png)
                    if send_image:
                        self._paint(armed_frame)
                # On a drawn surface the tree cannot locate anything, so we
                # segment the frame and hand the model numbered regions: it only
                # has to pick a number instead of guessing pixels.
                if send_image and poor:
                    try:
                        from .blobs import detect as detect_regions
                        regs = detect_regions(self.hwnd)
                    except Exception:
                        regs = []
                    if regs:
                        lines = []
                        for r in regs:
                            label = '%s块 %dx%d (#%02x%02x%02x)' % (
                                r.get('color', '色'), r['w'], r['h'],
                                r['rgb'][0], r['rgb'][1], r['rgb'][2])
                            synthetic = {
                                'idx': len(elems), 'parent': -1, 'depth': 1,
                                'type': 'Region', 'name': label, 'aid': '', 'cls': '',
                                'rect': r['rect'], 'enabled': True, 'offscreen': False,
                                'focusable': False, 'focused': False,
                                'invoke': False, 'value': False, 'toggle': False,
                                'select': False, 'scroll': False, 'interactive': True,
                            }
                            elems.append(synthetic)
                            lines.append('  [%d] %s' % (synthetic['idx'], label))
                        if lines:
                            user += ('\n画面区域（本地检测的色块，用序号点击比猜坐标准）：\n'
                                     + '\n'.join(lines) + '\n')
                            self._record(step=step, event='regions', n=len(regs))

                if send_image:
                    self._record(step=step, event='vision', reason=(
                        'full-screen' if full else
                        'poor-uia' if poor else 'stuck' if stuck else
                        'model-request' if self._look else 'always'),
                        quality=q, reused=reused)
                    self._look = False

                t0 = time.perf_counter()
                plan = None
                resp = None
                for attempt in range(2):
                    msgs = [{'role': 'system', 'content': self._system()},
                            {'role': 'user', 'content': user}]
                    if attempt:
                        msgs.append({'role': 'assistant', 'content': resp['text'] or '(空)'})
                        msgs.append({'role': 'user', 'content':
                                     '上一条不是合法 JSON（或为空）。请只输出一个 JSON 对象，不要任何解释。'})
                    resp = self.planner.chat(msgs, image_png=png if send_image else None)
                    plan = _parse(resp['text'])
                    if plan is not None:
                        break
                    self._record(step=step, event='bad_json', attempt=attempt,
                                 finish=resp.get('finish_reason', ''),
                                 raw=(resp['text'] or '')[:200])
                api_ms = (time.perf_counter() - t0) * 1000

                if plan is None:
                    history.append('模型两次都未给出合法 JSON')
                    continue

                action = (plan.get('action') or '').lower()
                self._record(step=step, event='plan', plan=plan,
                             prompt_tokens=resp['prompt_tokens'],
                             completion_tokens=resp['completion_tokens'],
                             api_ms=resp['ms'], obs_ms=round(build + walk, 1),
                             n_elems=len(elems))

                if action == 'done' or action == 'fail':
                    result = {'status': action, 'message': plan.get('message', '')}
                    break

                batch = plan.get('steps')
                if isinstance(batch, list) and batch:
                    # Never truncate a batch: cutting it in half leaves the UI in
                    # a half-applied state and the model re-plans the same prefix
                    # forever. Reject the whole batch and ask for a smaller one.
                    limit = 4 if os.environ.get('GUI_AGENT_BATCH') == '1' else 1
                    if len(batch) > limit:
                        self._record(step=step, event='batch_rejected', size=len(batch),
                                     limit=limit)
                        history.append('你给了 %d 个动作，超过上限 %d，整批未执行。'
                                       '请重新给出不超过 %d 个动作的一批。'
                                       % (len(batch), limit, limit))
                        continue
                    n = self._run_batch(batch, step, armed_frame, limit=limit)
                    if n == 0:
                        history.append('上一步给出的动作无法执行（目标找不到或窗口未激活）')
                    else:
                        history.append('执行了 %d 个动作' % n)
                    continue

                if action == 'look':
                    self._look = True
                    history.append('请求截图')
                    continue

                if action == 'look_full':
                    self._look = True
                    self._look_full = True
                    history.append('请求整屏截图')
                    continue

                if action == 'switch':
                    i = plan.get('window')
                    if not isinstance(i, int) or not (0 <= i < len(getattr(self, '_wins', []))):
                        history.append('switch 的 window 序号无效，请重新选择')
                        continue
                    hwnd, wt_, proc, pid, cls = self._wins[i]
                    # Same guard as element indices: the model must name the window
                    # it means, otherwise an off-by-one switches the wrong app.
                    want_t = (plan.get('target') or '').strip()
                    if want_t and want_t not in wt_ and wt_ not in want_t:
                        self._record(step=step, event='switch_mismatch',
                                     index=i, want=want_t, got=wt_)
                        history.append('窗口序号 %d 是「%s」，不是「%s」，已拒绝切换。'
                                       % (i, wt_[:30], want_t))
                        continue
                    activate(hwnd, wait=0.35)
                    self.hwnd, self.cls = hwnd, cls
                    self.guard.set_target(hwnd)
                    self._prefetch = None
                    self._record(step=step, event='switch', to=wt_[:40], cls=cls)
                    history.append('切换到窗口「%s」' % wt_[:30])
                    continue

                if self.guard.aborted:
                    result = {'status': 'aborted', 'reason': self.guard.reason()}
                    break

                idx = plan.get('from') if action == 'drag' else plan.get('index')
                node = elems[idx] if isinstance(idx, int) and 0 <= idx < len(elems) else None

                # Index/name cross-check: the model must say which control it means.
                # A mismatched pair means it miscounted -- refuse rather than click blind.
                want = (plan.get('target') or '').strip()
                got = ((node or {}).get('name') or '').strip()
                if node is not None and want and got and want not in got and got not in want:
                    self._record(step=step, event='target_mismatch', want=want, got=got, index=idx)
                    history.append('你给的序号 %s 对应的控件是「%s」，不是「%s」，已拒绝执行。'
                                   % (idx, got, want))
                    continue

                boxes = list(armed_frame)
                if node and node['rect']:
                    l, t, r, b = node['rect']
                    boxes.append((l, t, r, b, COLOR_TARGET, 40))
                self._paint(boxes)

                if action in ('click', 'type', 'hotkey', 'set_cell'):
                    # Blind keystrokes into whatever happens to be focused is how
                    # an agent types into the wrong application. Refuse instead.
                    if not ensure_foreground(self.hwnd):
                        self._record(step=step, event='focus_failed')
                        history.append('无法把目标窗口切到前台，本次动作已跳过')
                        continue
                sig_before = None
                if action in ('click', 'type', 'set_text', 'hotkey'):
                    try:
                        sig_before = signature(self.hwnd)
                    except Exception:
                        sig_before = None

                t0 = time.perf_counter()
                ok = self.execute(action, plan, elems)
                act_ms = (time.perf_counter() - t0) * 1000

                # ---- post-action verification -------------------------------
                changed, note, pixel = None, '', None
                if action in ('click', 'type', 'set_text', 'set_cell', 'paste'):
                    time.sleep(0.16)
                    try:
                        nodes2, _, b2, w2 = self._observe()
                        fp_after = fingerprint(nodes2)
                        changed = fp_after != fp_before
                        note = diff_summary(fp_before, fp_after)
                        self._prefetch = (nodes2, visible_elements(nodes2), b2, w2)
                    except Exception:
                        changed = None

                    # Independent signal: UIA says "nothing changed", but did the
                    # pixels move? That distinguishes a dead click from an
                    # animation or a self-drawn surface UIA cannot see.
                    if sig_before is not None and changed is False:
                        try:
                            pixel = round(sig_diff(sig_before, signature(self.hwnd)), 4)
                        except Exception:
                            pixel = None
                        if pixel is not None and pixel > 0.004:
                            note = (note + '；' if note and note != '无变化' else '') + \
                                   '结构无变化但画面有变化(%.3f)' % pixel

                    # A dead click (invoke reported success but nothing moved) is
                    # retried once as a real coordinate click before we bother the model.
                    if changed is False and action == 'click' and node and node['rect']:
                        r = node['rect']
                        self.act.click((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
                        time.sleep(0.16)
                        try:
                            nodes3, _, b3, w3 = self._observe()
                            fp3 = fingerprint(nodes3)
                            changed = fp3 != fp_before
                            note = '改用坐标点击后：' + diff_summary(fp_before, fp3)
                            self._prefetch = (nodes3, visible_elements(nodes3), b3, w3)
                        except Exception:
                            pass

                tgt = (node['name'] or node['type']) if node else (plan.get('target') or '')
                self._record(step=step, event='act', action=action, ok=ok,
                             act_ms=round(act_ms, 1), changed=changed, delta=note,
                             pixel_diff=pixel, target=tgt, keys=plan.get('keys'),
                             img_size=self._img_size, img_rect=self._img_rect,
                             plan_xy=[plan.get('x'), plan.get('y'), plan.get('x1'), plan.get('y1')])
                if ok:
                    self._acted += 1
                # Local completion check: if the instruction named a literal and
                # an editable control now holds it, skip the confirmation call.
                if self._literal and self._acted and changed:
                    try:
                        nodes_c, _, _, _ = self._observe()
                        for n2 in nodes_c:
                            if n2['type'] in ('Edit', 'Document') and n2['value']:
                                v = read_text(n2) or ''
                                if self._literal in v:
                                    self._record(step=step, event='local_done',
                                                 literal=self._literal)
                                    result = {'status': 'done', 'via': 'local',
                                              'message': '界面内容已包含目标文本，本地判定完成'}
                                    break
                    except Exception:
                        pass
                    if result.get('via') == 'local':
                        break
                if ok and action in ('click', 'right_click', 'double_click', 'drag', 'type', 'set_text',
                                     'hotkey', 'scroll', 'click_at', 'right_click_at',
                                     'double_click_at', 'set_cell', 'paste', 'save_as',
                                     'select_cell', 'format_cell', 'set_font_size'):
                    rec = {'action': action, 'cls': self.cls, 'win': self.win_key(),
                           'hwnd': self.hwnd, 'wtitle': title[:80]}
                    # Remember which control had focus: a bare Ctrl+A replayed
                    # into an unfocused window does nothing at all.
                    # Only input-like controls are worth restoring: UIA often marks
                    # the window itself as focused, and re-focusing that steals
                    # focus from the edit field the next step depends on.
                    fn = next((n for n in nodes if n.get('focused') and n['name']), None)
                    if fn is not None and fn['type'] not in (
                            'Window', 'Pane', 'TitleBar', 'Group', 'Custom', 'Image'):
                        rec['focus'] = fn['name']
                    if tgt and action in ('click', 'type', 'set_text'):
                        rec['target'] = tgt
                    for k in ('text', 'keys', 'amount', 'x', 'y'):
                        if plan.get(k) is not None:
                            rec[k] = plan[k]
                    self.trace.append(rec)
                detail = action
                if action == 'hotkey':
                    detail += ' ' + '+'.join(plan.get('keys', []))
                elif action == 'click' and node is not None:
                    detail += ' <%s>' % (node['name'] or node['type'])[:20]
                elif action == 'type':
                    detail += ' "%s"' % (plan.get('text', '')[:20])
                elif action == 'set_cell':
                    detail += ' %s ← "%s"' % (plan.get('cell', '?'),
                                              plan.get('text', '')[:24])
                elif action == 'paste':
                    detail += ' "%s"' % (plan.get('text', '')[:24])
                if changed is False:
                    detail += ' → 界面无变化(!)'
                    self._no_change += 1
                    if pixel is not None and pixel > 0.004:
                        self._no_change = 0   # pixels moved: it is not a dead click
                elif changed is True:
                    self._no_change = 0
                history.append('%s%s%s' % (detail, '' if ok else '(失败)',
                                           (' → ' + note) if note else ''))
                if self._no_change >= 2:
                    history.append('!! 连续 %d 次动作都没有产生界面变化：你点的元素可能是错的，'
                                   '请重新挑选序号，或改用别的动作。' % self._no_change)
                self._recent.append(detail)
                if len(self._recent) >= 3 and len(set(self._recent[-3:])) == 1:
                    history.append('!! 你已连续 3 次执行相同动作 %s，没有进展。'
                                   '请换一种做法，或直接 done/fail。' % detail)
                print('  step %d: %-7s ok=%-5s api=%4.0fms obs=%4.0fms act=%5.0fms  %s'
                      % (step, action, ok, api_ms, build + walk, act_ms,
                         plan.get('thinking', '')[:46]))
                self._record(step=step, event='progress', action=action, ok=ok,
                             api_ms=round(api_ms), obs_ms=round(build + walk),
                             act_ms=round(act_ms), n_elems=len(elems),
                             thinking=plan.get('thinking', ''))
                time.sleep(0.12)
            if result.get('status') == 'done' and not result.get('via') and self.trace:
                macro_store.save(self.task, self._win_key, self.trace,
                                 {'steps': len(self.trace),
                                  'pre': getattr(self, '_pre_names', [])})
                self._record(event='macro_saved', steps=len(self.trace))
        finally:
            meta = {
                'task': self.task, 'hwnd': self.hwnd,
                'status': result.get('status'), 'message': result.get('message', ''),
                'via': result.get('via', 'llm'),
                'reason': result.get('reason', ''),
                'elapsed_s': round(time.time() - self.started, 2),
                'calls': self.planner.calls,
                'prompt_tokens': self.planner.prompt_tokens,
                'completion_tokens': self.planner.completion_tokens,
                'frames': self._frame_idx, 'frame_bytes': self.frame_bytes,
                'run_dir': self.run_dir,
            }
            with open(os.path.join(self.run_dir, 'meta.json'), 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False, indent=1)
            self._record(event='end', **{k: v for k, v in meta.items() if k != 'run_dir'})
            self.events.close()
            if self.overlay:
                self._paint([(0, 0, w, h, COLOR_DONE, 0)] if result.get('status') == 'done' else [])
                time.sleep(0.25)
                self.overlay.close()
            self.guard.disarm()
        return result

    def _choose_skill(self):
        try:
            cands, literals = macro_store.candidates(self.task, self._win_key)
        except Exception as e:
            self._record(event='skill_probe', err=repr(e))
            return None, None
        self._record(event='skill_probe', candidates=len(cands), literals=literals,
                     win_key=self._win_key)
        if len(cands) < 1:
            return None, None
        listing = []
        for i, m in enumerate(cands[:6], 1):
            listing.append('%d) 动作：%s\n   原始指令：%s'
                           % (i, m.get('summary') or macro_store.summarize(m.get('steps') or []),
                              m.get('sample_task', '')))
        prompt = ('任务：%s\n窗口：%s\n候选技能：\n%s\n\n'
                  '判断标准：只比较「动作」是否就是任务要求的操作。'
                  '技能记录时用的具体文字、文件名、数字与本次任务不同是正常的，'
                  '不要因为参数值不同就否定它。\n'
                  '如果某个技能的动作与任务要求一致，输出 {"skill": 序号}；'
                  '确实没有能完成任务的，输出 {"skill": 0}。只输出 JSON。'
                  % (self.task, self.cls, '\n'.join(listing)))
        try:
            resp = self.planner.chat([
                {'role': 'system', 'content': '你是技能选择器，只输出 JSON。'},
                {'role': 'user', 'content': prompt}], max_tokens=60)
            plan = _parse(resp['text']) or {}
            idx = int(plan.get('skill') or 0)
            self._record(event='skill_reply', raw=(resp['text'] or '')[:200], idx=idx)
        except Exception:
            return None, None
        if 1 <= idx <= len(cands):
            return cands[idx - 1], literals
        return None, None

    def _system(self):
        # Batch instructions are only advertised when batching is actually
        # enabled; advertising them while truncating to one action deadlocks
        # the loop (the model keeps re-sending the same leading step).
        text = SYSTEM
        if os.environ.get('GUI_AGENT_BATCH') == '1':
            text += BATCH_RULE
        # Load only the knowledge that applies to the window being driven.
        proc = (getattr(self, '_win_key', '') or '').split('|')[0]
        prof = profiles.for_process(proc)
        if prof:
            text += '\n' + prof
        return text

    def _resolve(self, elems, st):
        from .macro import _match
        tgt = (st.get('target') or '').strip()
        if tgt:
            return _match(elems, tgt)
        i = st.get('index')
        if isinstance(i, int) and 0 <= i < len(elems):
            return elems[i]
        return None

    def _run_batch(self, steps, step_no, armed_frame, limit=2):
        """Execute a short action batch, verifying every step.

        Targets are re-resolved by name against a fresh snapshot, so a stale
        index can never misclick. The batch aborts on the first failure, and
        also when the element count jumps -- that means a dialog or a new panel
        appeared and the model's remaining steps were planned for another screen.
        """
        executed = 0
        base_count = None
        for st in steps[:limit]:
            if self.guard.aborted:
                break
            action = (st.get('action') or '').lower()
            if action not in ('click', 'right_click', 'double_click', 'drag', 'type',
                              'set_text', 'hotkey', 'scroll', 'click_at',
                              'right_click_at', 'double_click_at', 'set_cell', 'paste'):
                break
            nodes, elems, _b, _w = self._observe()
            if base_count is None:
                base_count = len(elems)
            elif base_count and abs(len(elems) - base_count) > max(3, base_count * 0.25):
                self._record(step=step_no, event='batch_abort', reason='ui changed',
                             before=base_count, after=len(elems))
                break
            fp = fingerprint(nodes)
            node = self._resolve(elems, st if action != 'drag' else
                                 dict(st, index=st.get('from')))
            if action in ('click', 'right_click', 'double_click', 'drag',
                          'type', 'set_text') and node is None:
                self._record(step=step_no, event='batch_abort', reason='target missing',
                             target=st.get('target', ''))
                break
            if action in ('click', 'type', 'hotkey') and not ensure_foreground(self.hwnd):
                self._record(step=step_no, event='batch_abort', reason='focus failed')
                break
            plan = dict(st)
            if node is not None:
                plan['index'] = elems.index(node)
                if action == 'drag':
                    plan['from'] = plan['index']   # re-resolved by name, not the stale index
                if node.get('rect'):
                    r = node['rect']
                    self._paint(list(armed_frame) + [(r[0], r[1], r[2], r[3], COLOR_TARGET, 40)])
            ok = self.execute(action, plan, elems)
            changed = None
            if action in ('click', 'type', 'set_text', 'set_cell', 'paste'):
                time.sleep(0.14)
                nodes2, _e2, _b2, _w2 = self._observe()
                changed = fingerprint(nodes2) != fp
            tgt = (node['name'] or node['type']) if node else (plan.get('target') or '')
            self._record(step=step_no, event='act', action=action, ok=ok, batch=True,
                         changed=changed, target=tgt, keys=plan.get('keys'))
            if not ok or changed is False:
                self._no_change = self._no_change + 1 if changed is False else 0
                break
            rec = {'action': action, 'cls': self.cls, 'win': self.win_key(),
                   'hwnd': self.hwnd, 'wtitle': (nodes[0]['name'] if nodes else '')[:80]}
            if tgt and action in ('click', 'type', 'set_text'):
                rec['target'] = tgt
            for k in ('text', 'keys', 'amount', 'x', 'y'):
                if plan.get(k) is not None:
                    rec[k] = plan[k]
            self.trace.append(rec)
            executed += 1
        return executed

    def _norm_pt(self, px, py):
        """The model reads pixel positions off the screenshot it was given, so we
        take pixels in image space and map them to the desktop ourselves."""
        r, sz = self._img_rect, self._img_size
        if r and sz and sz[0] and sz[1]:
            return (r[0] + px * (r[2] - r[0]) / float(sz[0]),
                    r[1] + py * (r[3] - r[1]) / float(sz[1]))
        sw, sh = ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
        return (px, py)

    def _set_font_size(self, plan, elems):
        """Type a size into the ribbon's font-size box.

        The box is an editable combo: clicking it, selecting the old value and
        typing the new one is how a person does it, and it works without the
        dropdown.
        """
        size = str(plan.get('size') or '').strip()
        if not size:
            return False
        box = None
        for n in elems:
            nm = n.get('name') or ''
            if n['type'] in ('ComboBox', 'Edit') and ('字号' in nm or 'font size' in nm.lower()):
                box = n
                break
        if box is None or not box.get('rect'):
            return False
        r = box['rect']
        self.act.click((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
        time.sleep(0.2)
        self.act.hotkey('ctrl', 'a')
        self.act.paste_text(size)
        time.sleep(0.15)
        self.act.hotkey('enter')
        time.sleep(0.2)
        return True

    _FORMAT_WORDS = {
        'percent': ('百分比', '%'),
        'bold': ('加粗', 'B'),
        'currency': ('货币', '¥'),
        'number': ('常规数字格式', '数字'),
    }

    def _format_cell(self, plan, elems):
        """Select a cell, then press the ribbon button for the format.

        A keyboard shortcut here is silent when it misses: the model reports
        success while the file is unchanged. Clicking the named ribbon button
        gives the UI a chance to actually respond, and the button is addressable
        through the accessibility tree.
        """
        cell = str(plan.get('cell') or '').strip()
        fmt = str(plan.get('format') or 'percent').lower()
        words = self._FORMAT_WORDS.get(fmt)
        if not cell or not words:
            return False
        if not self._set_cell({'cell': cell}, elems):
            return False
        time.sleep(0.25)
        nodes, elems2, _b, _w = self._observe()
        cand = None
        for n in elems2:
            nm = n.get('name') or ''
            if any(wd in nm for wd in words) and n['type'] in (
                    'Button', 'SplitButton', 'MenuItem', 'ToggleButton', 'RadioButton'):
                if cand is None or len(nm) < len(cand['name'] or ''):
                    cand = n
        if cand is None:
            return False
        return self.act.click_node(cand)

    def _save_as(self, plan, elems):
        """Save-as as one action: F12, fill the filename box, Enter, confirm format.

        Multi-window dialogs are where step-by-step plans fall apart (open the
        dialog, lose it, reopen it). Bundling it keeps the model out of the loop.
        """
        path = str(plan.get('path') or '').strip()
        if not path:
            return False
        self.act.hotkey('f12')
        time.sleep(0.9)
        self._follow_dialog()                 # the dialog is a different window
        nodes, elems2, _b, _w = self._observe()
        box = None
        for n in elems2:
            nm = n.get('name') or ''
            if '文件名' in nm or 'file name' in nm.lower():
                box = n
                break
        if box is not None and box.get('rect'):
            r = box['rect']
            self.act.click((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
            time.sleep(0.2)
        self.act.hotkey('ctrl', 'a')
        self.act.paste_text(path)
        time.sleep(0.25)
        self.act.hotkey('enter')              # save
        time.sleep(1.4)
        # Word may ask "升级到最新文件格式"; accept it.
        self.act.hotkey('enter')
        time.sleep(0.6)
        return True

    def _set_cell(self, plan, elems):
        """Write one spreadsheet cell as a single atomic action.

        Doing this as click -> paste -> Enter -> click takes five coordinated
        steps and the model loses track of which cell is being edited (it ends
        up concatenating values). Driving the Name Box makes it deterministic.
        """
        cell = str(plan.get('cell') or '').strip()
        text = plan.get('text', '')
        if not cell:
            return False
        box = None
        for n in elems:
            if n['type'] == 'ComboBox' and (n.get('aid') == '13'
                                            or '名称框' in (n.get('name') or '')):
                box = n
                break
        if box is None:
            return False
        # Click the box and type into it like a human. Setting its value through
        # ValuePattern only changes the text; Excel never performs the jump, so
        # the next paste lands wherever the selection happened to be.
        # If Excel is mid-edit, clicking the Name Box does not take; the address
        # ends up typed into the cell instead and everything after it lands wrong.
        self.act.hotkey('escape')
        time.sleep(0.15)
        r = box['rect']
        self.act.click((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
        time.sleep(0.2)
        self.act.hotkey('ctrl', 'a')
        self.act.paste_text(cell)
        time.sleep(0.25)
        self.act.hotkey('enter')          # jump to the address
        time.sleep(0.25)
        if text:
            ok = self.act.paste_text(text)
            time.sleep(0.15)
            self.act.hotkey('enter')      # commit the cell
            time.sleep(0.1)
            return ok
        return True

    def _drag(self, plan, node, elems):
        # Element-to-element drag, or normalised coordinates for drawn surfaces.
        to_i = plan.get('to')
        dst = elems[to_i] if isinstance(to_i, int) and 0 <= to_i < len(elems) else None
        if node is not None and dst is not None and node.get('rect') and dst.get('rect'):
            a, b = node['rect'], dst['rect']
            return self.act.drag((a[0] + a[2]) // 2, (a[1] + a[3]) // 2,
                                 (b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
        if all(plan.get(k) is not None for k in ('x1', 'y1', 'x2', 'y2')):
            x1, y1 = self._norm_pt(int(plan['x1']), int(plan['y1']))
            x2, y2 = self._norm_pt(int(plan['x2']), int(plan['y2']))
            return self.act.drag(x1, y1, x2, y2)
        return False

    def _follow_dialog(self):
        """Jump to a modal dialog opened by the same process.

        Save-as / open dialogs are separate windows. An agent pinned to the main
        window sees nothing happen and reports success without doing anything.
        """
        try:
            fg = ctypes.windll.user32.GetForegroundWindow()
            if not fg or fg == self.hwnd:
                return
            key = self.win_key_of(fg)
            if not key or key.split('|')[0] != (self._win_key or '').split('|')[0]:
                return
            if not user32_is_window(fg):
                return
            self.hwnd = fg
            self.guard.set_target(fg)
            self._prefetch = None
            self._uia_hostile = False
            self._record(event='dialog_follow', hwnd=fg, cls=key)
        except Exception:
            pass

    @staticmethod
    def win_key_of(hwnd):
        try:
            return win_key_of(hwnd)
        except Exception:
            return ''

    def _popup_elements(self):
        """Elements of small visible windows owned by the same process.

        Dropdown menus, galleries and context menus are separate top-level
        windows. Looking only at the main window means the agent never sees the
        menu it just opened -- which is why ribbon sub-items were unreachable.
        """
        out = []
        pid = pid_of_window(self.hwnd)
        if not pid:
            return out
        for h, _title, _proc, p, _cls in list_windows_ex():
            if p != pid or h == self.hwnd or not user32_is_window(h):
                continue
            r = window_rect(h)
            if not r:
                continue
            w, hgt = r[2] - r[0], r[3] - r[1]
            if w < 40 or hgt < 20 or w > 1400 or hgt > 1200:
                continue
            try:
                nodes, _b, _w = self.uia.snapshot(h)
            except Exception:
                continue
            if len(nodes) < 2:
                continue
            for n in visible_elements(nodes):
                out.append(n)
        return out

    def _observe(self):
        # A DX12 game answers UIA in ~850 ms and returns two nodes. Asking again
        # every step wastes most of the step budget, so we ask once and remember.
        if self._uia_hostile:
            return [], [], 0, 0
        try:
            nodes, build, walk = self.uia.snapshot(self.hwnd)
            if len(nodes) < 4 and build > 300:
                self._uia_hostile = True
                self._record(event='uia_hostile', build_ms=round(build), nodes=len(nodes))
        except Exception as e:
            # The window can be destroyed or recreated (dialogs, UWP hosts,
            # closing apps). Re-acquire it instead of killing the whole run.
            self._record(event='observe_error', err=repr(e)[:120], hwnd=self.hwnd)
            key = getattr(self, '_win_key', '') or ''
            proc, _, cls = key.partition('|')
            hwnd, _title = find_window(proc, cls) if proc or cls else (0, '')
            if not hwnd:
                return [], [], 0, 0
            self.hwnd = hwnd
            try:
                nodes, build, walk = self.uia.snapshot(hwnd)
            except Exception:
                return [], [], 0, 0
        elems = visible_elements(nodes)
        try:
            pop = self._popup_elements()
            for n in pop:
                n['idx'] = len(elems)
                elems.append(n)
        except Exception:
            pass
        return nodes, elems, build, walk

    def win_key(self) -> str:
        """Stable identity for this window: 'process|class'."""
        for hwnd, title, proc, pid, c in list_windows_ex():
            if hwnd == self.hwnd:
                return '%s|%s' % (proc, c)
        return '|%s' % self.cls

    def switch_to_win(self, key: str, want_title: str = '', prefer_hwnd: int = 0) -> bool:
        """Follow the exact window a macro step was recorded against."""
        proc, _, cls = key.partition('|')
        hwnd, _title = find_window_prefer(proc, cls, want_title, prefer_hwnd)
        if not hwnd:
            return False
        activate(hwnd, wait=0.35)
        self.hwnd, self.cls = hwnd, cls
        self.guard.set_target(hwnd)
        self._prefetch = None
        return True

    def switch_to_cls(self, cls: str) -> bool:
        hwnd, _title = find_by_cls(cls)
        if not hwnd:
            return False
        activate(hwnd, wait=0.35)
        self.hwnd, self.cls = hwnd, cls
        self.guard.set_target(hwnd)
        self._prefetch = None
        return True

    def execute(self, action, plan, elems):
        idx = plan.get('from') if action == 'drag' else plan.get('index')
        node = elems[idx] if isinstance(idx, int) and 0 <= idx < len(elems) else None
        try:
            if action == 'click':
                btn = (plan.get('button') or 'left').lower()
                return bool(node) and self.act.click_node(node, right=(btn == 'right'))
            if action == 'double_click':
                return bool(node) and self.act.double_click_node(node)
            if action == 'right_click':
                return bool(node) and self.act.click_node(node, right=True)
            if action == 'drag':
                return self._drag(plan, node, elems)
            if action == 'type':
                # Keyboard injection at the caret. Never SetValue: that replaces
                # the whole field and silently destroys existing content.
                text = plan.get('text', '')
                # Re-focusing an already focused edit control collapses the
                # selection made by a preceding Ctrl+A, turning "replace" into
                # "insert". Only take focus when we do not already have it.
                if node is not None and not node.get('focused'):
                    self.act.focus(node)
                    time.sleep(0.08)
                return self.act.type_text(text)
            if action == 'set_text':
                # Explicit whole-value replacement.
                text = plan.get('text', '')
                if node is not None and node['type'] in ('Edit', 'Document') and node['value']:
                    # Focus first: ValuePattern writes without giving the control
                    # keyboard focus, so a following Enter would go elsewhere
                    # (e.g. into the page instead of the address bar).
                    self.act.focus(node)
                    time.sleep(0.06)
                    return self.act.set_value(node, text)
                return False
            if action == 'set_font_size':
                return self._set_font_size(plan, elems)
            if action == 'format_cell':
                return self._format_cell(plan, elems)
            if action == 'select_cell':
                return self._set_cell({'cell': plan.get('cell')}, elems)
            if action == 'save_as':
                return self._save_as(plan, elems)
            if action == 'set_cell':
                return self._set_cell(plan, elems)
            if action == 'paste':
                return self.act.paste_text(plan.get('text', ''))
            if action == 'hotkey':
                return self.act.hotkey(*plan.get('keys', []))
            if action in ('click_at', 'right_click_at'):
                x, y = self._norm_pt(int(plan.get('x', 0)), int(plan.get('y', 0)))
                right = action == 'right_click_at' or (plan.get('button') or '').lower() == 'right'
                return self.act.click(x, y, right=right)
            if action == 'double_click_at':
                x, y = self._norm_pt(int(plan.get('x', 0)), int(plan.get('y', 0)))
                return self.act.double_click(x, y)
            if action == 'scroll':
                return self.act.scroll(int(plan.get('amount', -3)))
            if action == 'wait':
                time.sleep(0.5)
                return True
        except Exception as e:
            self._record(event='act_error', err=repr(e))
            return False
        return False
