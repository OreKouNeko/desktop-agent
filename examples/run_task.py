# -*- coding: utf-8 -*-
"""Run one task against a window, from the command line.

    python examples/run_task.py --title "report.docx" --task "把文档内容替换成：本周总结"
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

from desktop_agent import config
from desktop_agent.loop import Agent
from desktop_agent.window import hwnd_by_title, activate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--title', required=True, help='窗口标题的一部分')
    ap.add_argument('--task', required=True, help='自然语言指令')
    ap.add_argument('--max-steps', type=int, default=None)
    a = ap.parse_args()

    if not config.has_api_key():
        print('没有找到 API Key。请先运行 desktop-agent 界面保存，或设置环境变量 DESKTOP_AGENT_API_KEY。')
        return 2

    hwnd, title = hwnd_by_title(a.title)
    if not hwnd:
        print('找不到窗口:', a.title)
        return 2
    print('目标窗口:', title)
    activate(hwnd, wait=0.5)

    steps = a.max_steps or config.load_settings().get('max_steps', 12)
    agent = Agent(hwnd, a.task, max_steps=steps)
    t0 = time.time()
    result = agent.run()
    print('结果:', result)
    print('耗时 %.1fs  调用 %d 次  记录 %s'
          % (time.time() - t0, agent.planner.calls, agent.run_dir))
    return 0 if result.get('status') == 'done' else 1


if __name__ == '__main__':
    sys.exit(main())
