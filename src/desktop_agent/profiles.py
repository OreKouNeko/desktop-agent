# -*- coding: utf-8 -*-
"""Per-application operating knowledge.

These rules are not general agent behaviour: they are the interaction model of
one specific program (how Excel commits a cell, how PowerPoint enters text
edit). Keeping them here instead of in the global prompt means the model only
sees the ones that apply to the window it is actually driving, which keeps the
prompt short and stops rules from leaking between apps.
"""
from __future__ import annotations

EXCEL = '''
【Excel 操作约定】
- 选区域也用 select_cell，cell 直接写范围，例如 {"action":"select_cell","cell":"A2:A4"}。
  不要拖拽选区，也不要手点名称框——这两件事都不稳。
- 排序：先 select_cell 选中区域，点「数据」选项卡，再点「升序」或「降序」按钮。
  绝对不要用手工逐个改写单元格来"实现排序"——那会写坏数据。
- 给单元格设置格式一律用 format_cell：{"action":"format_cell","cell":"A1","format":"percent"}。
  format 可选 percent / bold / currency。它会自己选中单元格再点功能区按钮，比快捷键可靠。
  不要用 set_cell 去"选中"单元格——它结尾会回车，光标会落到下一格。
- 填单元格一律用 set_cell 动作，一步一格：{"action":"set_cell","cell":"A1","text":"产品"}。
- 不要用 click/click_at 去点单元格，也不要用 paste + Enter 组合（非常容易串格）。
- set_cell 返回成功就代表那一格已经写好，不要重复写同一格，也不要反复点击同一格。
- 公式直接写，例如 set_cell A4 text="=SUM(A2:A3)"。
'''

POWERPOINT = '''
【PowerPoint 操作约定】
- 幻灯片内容不会暴露成控件，只能靠画面操作。
- 新增幻灯片用 Ctrl+M。输入文字用 paste，不要用 type。
- 单击标题区域只是选中形状，必须双击才进入文字编辑；进入后 paste 写入，写完按 Esc 退出。
- 标题占位符通常显示「单击此处添加标题」。
- 写入一次就够，不要反复重写；页数和标题都对了就直接 done。
- 换版式：右键左侧幻灯片缩略图 → 菜单里选「版式」→ 在展开的版式库里点目标版式（例如「标题和内容」）。
  版式库是弹出层，窗口截图拍不到，需要时用 look_full 看整屏。
'''

WORD = '''
【Word 操作约定】
- 插入表格：点「插入」选项卡 → 点「表格」→ 在菜单里选「插入表格…」打开对话框，
  填列数和行数后确定。不要用那个用鼠标拖格子的小网格选择器。
- 正文是可编辑控件：点一次就够，点完不要再重复点击。
- Ctrl+End 到文末，Ctrl+Home 到文首，Ctrl+A 全选。
- 输入文字一律用 paste（剪贴板），不要用 type：type 在 Office 里会偶发丢掉最后一个字符。
- 加粗 Ctrl+B，斜体 Ctrl+I，居中 Ctrl+E。
- 设置字号用 set_font_size：{"action":"set_font_size","size":"16"}，它会自己点字号框并输入。
- 另存为直接用 save_as 动作，一步完成：{"action":"save_as","path":"完整路径含扩展名"}。不要自己按 F12 再找输入框。
'''

PROFILES = {
    'EXCEL.EXE': EXCEL,
    'POWERPNT.EXE': POWERPOINT,
    'WINWORD.EXE': WORD,
}


def for_process(proc: str) -> str:
    return PROFILES.get((proc or '').upper(), '')
