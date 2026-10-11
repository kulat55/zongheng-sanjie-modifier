#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
纵横三界 修改器 —— 统一启动器
点一个按钮进入对应工具：
  - 内存修改器（实时数值扫描 / 修改 / 冻结 + 一键无敌）
  - 存档编辑器（改存档 / 装备管理 / 存档码等）
"""
import sys
import os

# 打包后 sys._MEIPASS 是解包临时目录；开发时是脚本目录
BASE = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import tkinter as tk
from tkinter import ttk, messagebox


def launch_memory():
    root.destroy()
    try:
        import memory_trainer
        memory_trainer.main()
    except Exception as e:
        _fatal('内存修改器启动失败', e)


def launch_save():
    root.destroy()
    try:
        import save_editor
        save_editor.main()
    except Exception as e:
        _fatal('存档编辑器启动失败', e)


def _fatal(title, e):
    try:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(title, str(e))
        r.destroy()
    except Exception:
        pass
    sys.exit(1)


if __name__ == '__main__':
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    root.title('纵横三界 修改器')
    root.geometry('520x380')
    root.minsize(480, 340)
    root.resizable(False, False)

    top = ttk.Frame(root, padding=18)
    top.pack(fill='both', expand=True)

    ttk.Label(top, text='纵横三界 修改器', font=('Microsoft YaHei UI', 16, 'bold')).pack(pady=(0, 4))
    ttk.Label(top, text='个人测试专用 · 请勿发布', foreground='#999').pack(pady=(0, 16))

    ttk.Button(top, text='内存修改器（扫描数值 / 一键无敌）',
               command=launch_memory).pack(fill='x', pady=6, ipady=10)
    ttk.Label(top, text='实时修改游戏内数值：金币/灵魂/生命…\n「一键无敌」：自动定位 HP，开启=锁满血，关闭=正常受伤',
              foreground='#666', justify='left').pack(fill='x', padx=6)

    ttk.Button(top, text='存档编辑器（改存档 / 装备管理）',
               command=launch_save).pack(fill='x', pady=(14, 6), ipady=10)
    ttk.Label(top, text='直接改存档文件：属性/背包/装备（986件可加可穿）/导出导入存档码…',
              foreground='#666', justify='left').pack(fill='x', padx=6)

    ttk.Button(top, text='退出', command=root.destroy).pack(fill='x', pady=(18, 0))

    root.mainloop()
