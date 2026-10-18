# -*- coding: utf-8 -*-
"""Minimal desktop UI: paste a key, pick a window, type an instruction, watch it work."""
from __future__ import annotations

import ctypes
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .. import config
from ..loop import Agent
from ..window import activate, list_windows

# light palette
BG = '#f4f6f9'
CARD = '#ffffff'
LINE = '#e3e7ee'
FG = '#1f2430'
DIM = '#6b7280'
MUTED = '#9aa3b2'
ACCENT = '#2f6fed'
ACCENT_D = '#2560d8'
BAD = '#c0392b'
OK = '#12805c'
WARN = '#b45309'

FONT = ('Microsoft YaHei UI', 9)
FONT_B = ('Microsoft YaHei UI', 9, 'bold')
FONT_H = ('Microsoft YaHei UI', 14, 'bold')
FONT_MONO = ('Consolas', 9)


def card(parent, **kw):
    f = tk.Frame(parent, bg=CARD, highlightbackground=LINE,
                 highlightthickness=1, bd=0)
    f.pack(fill='x', padx=18, pady=(0, 12), **kw)
    return f


def section_label(parent, text):
    tk.Label(parent, text=text, bg=CARD, fg=DIM, font=FONT).pack(
        anchor='w', padx=14, pady=(12, 4))


class App:
    def __init__(self, root):
        self.root = root
        self.agent = None
        self.q = queue.Queue()
        self.windows = []
        settings = config.load_settings()

        root.title('desktop-agent')
        root.geometry('760x820')
        root.configure(bg=BG)
        root.minsize(660, 680)

        # ---------- header ----------
        head = tk.Frame(root, bg=CARD, highlightbackground=LINE, highlightthickness=0)
        head.pack(fill='x')
        inner = tk.Frame(head, bg=CARD)
        inner.pack(fill='x', padx=18, pady=14)
        tk.Label(inner, text='desktop-agent', bg=CARD, fg=FG, font=FONT_H).pack(side='left')
        tk.Label(inner, text='  用自然语言驱动 Windows 桌面', bg=CARD, fg=MUTED,
                 font=FONT).pack(side='left', pady=(6, 0))
        self.status = tk.Label(inner, text='就绪', bg=CARD, fg=DIM, font=FONT)
        self.status.pack(side='right')
        tk.Frame(root, bg=LINE, height=1).pack(fill='x')

        # ---------- api card ----------
        c1 = card(root)
        section_label(c1, 'API Key')
        row = tk.Frame(c1, bg=CARD)
        row.pack(fill='x', padx=14, pady=(0, 8))
        row.columnconfigure(0, weight=1)
        self.key_entry = tk.Entry(row, show='●', bg='#fafbfc', fg=FG, relief='flat',
                                  insertbackground=FG, font=FONT_MONO,
                                  highlightbackground=LINE, highlightthickness=1)
        self.key_entry.grid(row=0, column=0, sticky='ew', ipady=5)
        tk.Button(row, text='保存', command=self.save_key, bg=ACCENT, fg='white',
                  relief='flat', font=FONT, padx=14, cursor='hand2',
                  activebackground=ACCENT_D, activeforeground='white'
                  ).grid(row=0, column=1, padx=(8, 0), sticky='ns')
        self.key_state = tk.Label(row, text='', bg=CARD, fg=MUTED, font=FONT)
        self.key_state.grid(row=0, column=2, padx=(10, 0))

        # ---------- task card ----------
        c2 = card(root)
        section_label(c2, '目标窗口')
        r2 = tk.Frame(c2, bg=CARD)
        r2.pack(fill='x', padx=14)
        r2.columnconfigure(0, weight=1)
        self.combo = ttk.Combobox(r2, state='readonly', font=FONT)
        self.combo.grid(row=0, column=0, sticky='ew', ipady=3)
        tk.Button(r2, text='刷新', command=self.refresh, bg='#eef1f6', fg=FG,
                  relief='flat', font=FONT, padx=14, cursor='hand2'
                  ).grid(row=0, column=1, padx=(8, 0), sticky='ns')

        section_label(c2, '指令')
        self.task = tk.Text(c2, height=3, bg='#fafbfc', fg=FG, relief='flat',
                            insertbackground=FG, font=('Microsoft YaHei UI', 10),
                            wrap='word', padx=8, pady=6,
                            highlightbackground=LINE, highlightthickness=1)
        self.task.pack(fill='x', padx=14)

        r3 = tk.Frame(c2, bg=CARD)
        r3.pack(fill='x', padx=14, pady=(10, 14))
        tk.Label(r3, text='最大步数', bg=CARD, fg=DIM, font=FONT).pack(side='left')
        self.steps = tk.Spinbox(r3, from_=1, to=500, width=6, font=FONT,
                                bg='#fafbfc', fg=FG, relief='flat',
                                highlightbackground=LINE, highlightthickness=1,
                                justify='center')
        self.steps.delete(0, 'end')
        self.steps.insert(0, str(settings.get('max_steps', 100)))
        self.steps.pack(side='left', padx=(8, 20), ipady=3)
        tk.Label(r3, text='步数用完会停在当前状态，可调大后重跑', bg=CARD,
                 fg=MUTED, font=FONT).pack(side='left')

        btns = tk.Frame(root, bg=BG)
        btns.pack(fill='x', padx=18, pady=(0, 12))
        self.start_btn = tk.Button(btns, text='开始执行', command=self.start, bg=ACCENT,
                                   fg='white', relief='flat', font=FONT_B,
                                   padx=26, pady=8, cursor='hand2',
                                   activebackground=ACCENT_D, activeforeground='white')
        self.start_btn.pack(side='left')
        self.stop_btn = tk.Button(btns, text='停止  (Ctrl+Alt+F12)', command=self.stop,
                                  bg='#fdeceb', fg=BAD, relief='flat', font=FONT_B,
                                  padx=18, pady=8, cursor='hand2', state='disabled',
                                  activebackground='#f9d9d6')
        self.stop_btn.pack(side='left', padx=10)

        # ---------- log card ----------
        c3 = tk.Frame(root, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        c3.pack(fill='both', expand=True, padx=18, pady=(0, 18))
        section_label(c3, '运行日志')
        wrap = tk.Frame(c3, bg=CARD)
        wrap.pack(fill='both', expand=True, padx=14, pady=(0, 12))
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self.log = tk.Text(wrap, bg='#fbfcfd', fg=FG, relief='flat', font=FONT_MONO,
                           wrap='word', state='disabled', padx=8, pady=6,
                           highlightbackground=LINE, highlightthickness=1)
        sb = tk.Scrollbar(wrap, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.grid(row=0, column=0, sticky='nsew')
        sb.grid(row=0, column=1, sticky='ns')
        self.log.tag_configure('ok', foreground=OK)
        self.log.tag_configure('bad', foreground=BAD)
        self.log.tag_configure('dim', foreground=MUTED)

        self.refresh()
        self._show_key_state()
        self.root.after(120, self.poll)

    # ---------- behaviour ----------
    def _show_key_state(self):
        if config.has_api_key():
            self.key_state.configure(text='已配置', fg=OK)
        else:
            self.key_state.configure(text='未配置', fg=BAD)

    def save_key(self):
        key = self.key_entry.get().strip()
        if not key:
            messagebox.showwarning('提示', '请先填入 API Key')
            return
        config.save_api_key(key)
        self.key_entry.delete(0, 'end')
        self._show_key_state()
        self.write('API Key 已加密保存', 'dim')

    def write(self, text, tag=None):
        self.log.configure(state='normal')
        self.log.insert('end', text + '\n', tag or ())
        self.log.see('end')
        self.log.configure(state='disabled')

    def refresh(self):
        self.windows = [w for w in list_windows() if w[1]]
        self.combo['values'] = ['%s   [%s]' % (t[:58], p) for _h, t, p, _pid in self.windows]
        if self.windows:
            self.combo.current(0)

    def start(self):
        if not config.has_api_key():
            messagebox.showwarning('提示', '请先保存 API Key')
            return
        idx = self.combo.current()
        if idx < 0:
            messagebox.showwarning('提示', '请选择一个窗口')
            return
        task = self.task.get('1.0', 'end').strip()
        if not task:
            messagebox.showwarning('提示', '请输入指令')
            return
        try:
            steps = int(self.steps.get())
        except ValueError:
            steps = 40
        config.save_settings(max_steps=steps)

        hwnd = self.windows[idx][0]
        activate(hwnd, wait=0.3)
        self.start_btn.configure(state='disabled')
        self.stop_btn.configure(state='normal')
        self.status.configure(text='运行中…', fg=WARN)
        self.write('─' * 56, 'dim')
        self.write('任务: ' + task, 'dim')
        self.write('窗口: ' + self.windows[idx][1], 'dim')

        self.agent = Agent(hwnd, task, max_steps=steps,
                           on_event=lambda ev: self.q.put(ev))
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            res = self.agent.run()
            self.q.put({'event': '__finished__', 'result': res,
                        'run_dir': self.agent.run_dir,
                        'tokens': self.agent.planner.prompt_tokens
                        + self.agent.planner.completion_tokens})
        except Exception as e:
            self.q.put({'event': '__error__', 'err': repr(e)})

    def stop(self):
        if self.agent is not None:
            self.agent.guard.stop.set()
            self.write('!! 收到停止指令', 'bad')

    def poll(self):
        try:
            while True:
                self.handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(120, self.poll)

    def handle(self, ev):
        kind = ev.get('event')
        if kind == '__finished__':
            res = ev['result']
            st = res.get('status')
            color = {'done': OK, 'fail': BAD, 'aborted': BAD}.get(st, WARN)
            msg = res.get('message') or res.get('reason', '')
            self.status.configure(text='%s · %s' % (st, msg[:40]), fg=color)
            self.write('结果: %s  %s' % (st, msg), 'ok' if st == 'done' else 'bad')
            self.write('记录: ' + ev.get('run_dir', ''), 'dim')
            self.start_btn.configure(state='normal')
            self.stop_btn.configure(state='disabled')
        elif kind == '__error__':
            self.status.configure(text='异常', fg=BAD)
            self.write('异常: ' + ev['err'], 'bad')
            self.start_btn.configure(state='normal')
            self.stop_btn.configure(state='disabled')
        elif kind == 'plan':
            plan = ev.get('plan', {})
            self.write('[%d] %-10s %s' % (ev.get('step', -1), plan.get('action', '').upper(),
                                          plan.get('thinking', '')))
        elif kind == 'act' and not ev.get('ok'):
            self.write('      动作失败', 'bad')


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    config.ensure_home()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
