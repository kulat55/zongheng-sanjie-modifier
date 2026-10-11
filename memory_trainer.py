#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
纵横三界 内存修改器
适用于 Godot 4.x 制作的单机游戏（内存数值扫描 / 修改 / 冻结 / 地址表保存）。
内置「一键无敌」：自动定位角色 HP 并锁定为满血（开关切换）。

用法：先启动游戏，再运行本程序；点“刷新进程”并选择游戏进程后“附加”，
输入游戏里的数值（比如金币）点“首次扫描”，回游戏改变数值，再点“再次扫描”缩小范围，
在结果列表中选中地址，输入新值后“写入选中”（或勾选冻结）。

依赖：仅 Python 标准库（tkinter / ctypes）。
"""
import ctypes
from ctypes import wintypes
import struct
import threading
import queue
import json
import os
import time
import sys
from array import array

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except Exception:
    tk = None

# ============================================================
# Win32 API 封装
# ============================================================
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
# EnumProcesses 在 psapi.dll（部分系统 kernel32 也转发，这里直接取 psapi 更稳）
psapi = ctypes.WinDLL('psapi', use_last_error=True)

PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80
WRITABLE_MASKS = (PAGE_READWRITE, PAGE_WRITECOPY,
                  PAGE_EXECUTE_READWRITE, PAGE_EXECUTE_WRITECOPY)


def is_writable(protect):
    return bool(protect & (PAGE_READWRITE | PAGE_WRITECOPY |
                           PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY))

class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('BaseAddress', ctypes.c_void_p),
        ('AllocationBase', ctypes.c_void_p),
        ('AllocationProtect', wintypes.DWORD),
        ('RegionSize', ctypes.c_size_t),
        ('State', wintypes.DWORD),
        ('Protect', wintypes.DWORD),
        ('Type', wintypes.DWORD),
    ]

kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.WriteProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.VirtualQueryEx.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p,
    ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t
psapi.EnumProcesses.argtypes = [
    ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD)]


def open_process(pid):
    return kernel32.OpenProcess(
        PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION |
        PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION,
        False, pid)


def read_mem(h, addr, size):
    buf = ctypes.create_string_buffer(size)
    nread = ctypes.c_size_t(0)
    if kernel32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size,
                                  ctypes.byref(nread)):
        return buf.raw[:nread.value]
    return None


def write_mem(h, addr, data):
    nwrite = ctypes.c_size_t(0)
    return kernel32.WriteProcessMemory(h, ctypes.c_void_p(addr), data,
                                       len(data), ctypes.byref(nwrite))


def process_image_name(pid):
    """返回进程镜像名（如 xxx.exe），失败返回 None"""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(h)


def list_processes():
    """返回 [(pid, 完整路径), ...]"""
    size = 4096
    arr = (wintypes.DWORD * size)()
    needed = wintypes.DWORD(0)
    psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed))
    n = needed.value // ctypes.sizeof(wintypes.DWORD)
    out = []
    for i in range(n):
        pid = arr[i]
        if pid == 0:
            continue
        name = process_image_name(pid)
        if name:
            out.append((pid, name))
    out.sort(key=lambda x: x[1].lower())
    return out


# ============================================================
# 扫描引擎
# ============================================================
TYPES = {
    'int32':   (4, '<i'),
    'int64':   (8, '<q'),
    'float':   (4, '<f'),
    'double':  (8, '<d'),
}


def pack_value(tname, value):
    size, fmt = TYPES[tname]
    try:
        return struct.pack(fmt, value)
    except (OverflowError, struct.error):
        return None


def unpack_value(tname, raw):
    size, fmt = TYPES[tname]
    try:
        return struct.unpack(fmt, raw[:size])[0]
    except struct.error:
        return None


def iter_memory_regions(h, writable_only=False):
    """按提交的可读内存区迭代，返回 (base, size)；writable_only 时仅返回可写区"""
    max_addr = 0x7FFFFFFFFFFF
    addr = 0
    while addr < max_addr:
        mbi = MEMORY_BASIC_INFORMATION()
        # 注意：本机 Python 3.14 的 ctypes 中 c_void_p(0).value 为 None，
        # 因此全程用 int 跟踪地址，仅在调用 API 时包装成 c_void_p
        r = kernel32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                    ctypes.sizeof(mbi))
        if not r:
            break
        base = mbi.BaseAddress if mbi.BaseAddress is not None else addr
        size = mbi.RegionSize
        if (mbi.State == MEM_COMMIT and size > 0
                and not (mbi.Protect & PAGE_GUARD)
                and (mbi.Protect & 0xFF) != PAGE_NOACCESS
                and not (writable_only and not is_writable(mbi.Protect))):
            yield base, size
        next_addr = base + size
        if next_addr <= addr:
            next_addr = addr + 0x10000
        addr = next_addr


def scan_all(h, patterns, writable_only=False, progress_cb=None, cancel=None):
    """全内存扫描。patterns: [(tname, bytes)] -> [(tname, addr), ...]"""
    results = []
    total = 0
    done = 0
    # 先统计总大小（粗略）
    for _base, size in iter_memory_regions(h, writable_only):
        total += size
    for base, size in iter_memory_regions(h, writable_only):
        if cancel and cancel.is_set():
            break
        off = 0
        while off < size:
            if cancel and cancel.is_set():
                return results
            chunk = min(0x400000, size - off)  # 4MB
            data = read_mem(h, base + off, chunk)
            if data:
                for tname, pat in patterns:
                    pos = data.find(pat)
                    while pos >= 0:
                        results.append((tname, base + off + pos))
                        pos = data.find(pat, pos + 1)
            off += chunk
            done += chunk
            if progress_cb and (done % (4 * 1024 * 1024) == 0):
                progress_cb(done, total)
    return results


SNAPSHOT_TYPECODE = {'int32': 'I', 'int64': 'q', 'float': 'f', 'double': 'd'}
SNAPSHOT_CAP = 1500000


def snapshot_all(h, tnames, writable_only=True, cap=SNAPSHOT_CAP,
                 progress_cb=None, cancel=None):
    """未知初始值模式：记录所有候选地址与当前值 -> [(tname, addr, value)]。
    候选数超过 cap 即停止（提示改用已知数值扫描）。"""
    out = []
    total = 0
    done = 0
    for _base, size in iter_memory_regions(h, writable_only):
        total += size
    for base, size in iter_memory_regions(h, writable_only):
        if cancel and cancel.is_set():
            break
        off = 0
        while off < size:
            if cancel and cancel.is_set():
                return out
            chunk = min(0x400000, size - off)
            data = read_mem(h, base + off, chunk)
            if data:
                for tname in tnames:
                    if len(out) >= cap:
                        return out
                    sz = TYPES[tname][0]
                    usable = len(data) - (len(data) % sz)
                    if usable <= 0:
                        continue
                    arr = array(SNAPSHOT_TYPECODE[tname])
                    arr.frombytes(data[:usable])
                    vals = arr.tolist()
                    base_addr = base + off
                    for i, v in enumerate(vals):
                        if len(out) >= cap:
                            return out
                        out.append((tname, base_addr + i * sz, v))
            off += chunk
            done += chunk
            if progress_cb and (done % (4 * 1024 * 1024) == 0):
                progress_cb(done, total)
    return out


# ============================================================
# 存档读取（用于一键无敌自动定位 HP / max_Hp）
# ============================================================
def default_save_dir():
    """返回游戏存档目录（与 save_editor 相同规则）"""
    cands = []
    base = os.environ.get('APPDATA', '')
    if base:
        cands.append(os.path.join(base, 'Godot', 'app_userdata',
                                  '造梦西游 · 圆梦计划：纵横三界篇 (4.5)', 'saves'))
    cands.append(os.path.join(os.path.expanduser('~'), 'AppData', 'Roaming', 'Godot',
                              'app_userdata', '造梦西游 · 圆梦计划：纵横三界篇 (4.5)', 'saves'))
    for c in cands:
        if os.path.isdir(c):
            return c
    return cands[0] if cands else ''


def find_save_files(save_dir):
    out = []
    if not save_dir or not os.path.isdir(save_dir):
        return out
    for n in sorted(os.listdir(save_dir)):
        if n.startswith('save_') and n.endswith('.json') and '.backup' not in n:
            out.append(os.path.join(save_dir, n))
    return out


def read_save_hp(save_path):
    """返回 (Hp, max_Hp)；失败返回 (None, None)"""
    try:
        with open(save_path, encoding='utf-8') as f:
            data = json.load(f)
        cd = data.get('character_data', {})
        stats = cd.get('player_stats', {})
        hp = stats.get('Hp')
        mhp = stats.get('max_Hp')
        if hp is None or mhp is None:
            return None, None
        return float(hp), float(mhp)
    except Exception:
        return None, None


def read_save_value(save_path, section, key):
    """通用读档数值：section=None 表示 character_data 直接字段"""
    try:
        with open(save_path, encoding='utf-8') as f:
            data = json.load(f)
        cd = data.get('character_data', {})
        if section:
            obj = cd.get(section, {})
        else:
            obj = cd
        v = obj.get(key)
        return float(v) if v is not None else None
    except Exception:
        return None


# ============================================================
# 简易模式字段配置（血量/蓝量/金币/灵魂）
# save: (section, key)；hint: 定位时引导用户让数值变化；dir: 变化方向
# ============================================================
SIMPLE_FIELDS = {
    'hp':   {'label': '血量', 'section': 'player_stats', 'key': 'Hp',
             'hint': '请回游戏让角色被打掉一点血', 'dir': 'dec'},
    'mp':   {'label': '蓝量', 'section': 'player_stats', 'key': 'Mp',
             'hint': '请回游戏释放一个技能（消耗法力）', 'dir': 'dec'},
    'gold': {'label': '金币', 'section': None, 'key': 'gold',
             'hint': '请回游戏花掉一点金币（商店购买）', 'dir': 'dec'},
    'soul': {'label': '灵魂', 'section': None, 'key': 'soul_count',
             'hint': '请回游戏消耗一点灵魂（商店/强化）', 'dir': 'dec'},
}


def refine(h, matches, mode, value=None, progress_cb=None, cancel=None):
    """matches: [(tname, addr, old_value)] -> [(tname, addr, old_value)]"""
    out = []
    n = len(matches)
    for i, (tname, addr, old) in enumerate(matches):
        if cancel and cancel.is_set():
            break
        size = TYPES[tname][0]
        raw = read_mem(h, addr, size)
        if raw is None or len(raw) < size:
            continue
        cur = unpack_value(tname, raw)
        if mode == 'eq':
            if value is not None and cur == value:
                out.append((tname, addr, cur))
        elif mode == 'inc':
            if cur > old:
                out.append((tname, addr, cur))
        elif mode == 'dec':
            if cur < old:
                out.append((tname, addr, cur))
        elif mode == 'unch':
            if cur == old:
                out.append((tname, addr, cur))
        elif mode == 'chg':
            if cur != old:
                out.append((tname, addr, cur))
        if progress_cb and i % 4096 == 0:
            progress_cb(i, n)
    return out


# ============================================================
# GUI
# ============================================================
class TrainerApp:
    def __init__(self, root):
        self.root = root
        root.title('纵横三界 修改器')
        root.geometry('860x620')
        root.minsize(760, 540)

        self.pid = None
        self.h = None          # 进程句柄
        self.matches = []      # [(tname, addr, value)]
        self.results = []      # 与 tree 同步
        self.frozen = {}       # addr -> (tname, value)
        self.freeze_thread = None
        self.stop_freeze = threading.Event()
        self.hp_addrs = []     # [(tname, addr), ...] 无敌：要锁定的 HP 地址
        self.hp_max = None     # 满血值
        self.god_thread = None
        self.stop_god = threading.Event()
        self.simple_addrs = {}   # key -> {'addrs':[(tn,addr)], 'locked_value': float}
        self.simple_lock_thread = None
        self.simple_stop_lock = threading.Event()
        self.simple_cands = []   # 简易定位中的候选 [(tn, addr, value)]
        self.simple_key = None   # 正在定位的字段 key
        self.scan_queue = queue.Queue()
        self.cancel_scan = threading.Event()
        self.scanning = False

        self._build_ui()
        self._poll_queue()
        self.refresh_procs()

    # ---------------- UI ----------------
    def _build_ui(self):
        frm = ttk.Frame(self.root, padding=6)
        frm.pack(fill='both', expand=True)

        # 进程
        p1 = ttk.LabelFrame(frm, text='游戏进程', padding=6)
        p1.pack(fill='x')
        self.proc_var = tk.StringVar()
        self.proc_combo = ttk.Combobox(p1, textvariable=self.proc_var, width=70)
        self.proc_combo.pack(side='left', fill='x', expand=True)
        ttk.Button(p1, text='刷新进程', command=self.refresh_procs).pack(side='left', padx=4)
        self.attach_btn = ttk.Button(p1, text='附加进程', command=self.attach)
        self.attach_btn.pack(side='left', padx=4)
        self.attached_var = tk.StringVar(value='未附加')
        ttk.Label(p1, textvariable=self.attached_var, foreground='#555').pack(side='left', padx=6)

        # 无敌模式
        p1b = ttk.LabelFrame(frm, text='一键无敌', padding=6)
        p1b.pack(fill='x', pady=(6, 0))
        self.god_btn = ttk.Button(p1b, text='开启无敌', command=self.toggle_god)
        self.god_btn.pack(side='left', padx=4)
        self.god_state = tk.StringVar(value='未定位 HP（点「开启无敌」自动定位）')
        ttk.Label(p1b, textvariable=self.god_state, foreground='#C00').pack(side='left', padx=8)
        ttk.Button(p1b, text='重新定位 HP', command=self.locate_hp).pack(side='left', padx=4)
        ttk.Label(p1b, text='满血值(可选):').pack(side='left', padx=(12, 2))
        self.hpmax_entry = ttk.Entry(p1b, width=10)
        self.hpmax_entry.pack(side='left')
        ttk.Label(p1b, text='（自动读档）', foreground='#888').pack(side='left', padx=4)
        # 简易模式入口（独立一行，避免被挤出窗口）
        p1c = ttk.Frame(frm)
        p1c.pack(fill='x', pady=(4, 0))
        ttk.Button(p1c, text='简易模式（血量/蓝量/金币/灵魂）',
                   command=self.open_simple).pack(side='left', padx=4)
        ttk.Label(p1c, text='不会扫描？点这里直接改数值：定位→输入新值→写入/锁定',
                  foreground='#777').pack(side='left', padx=8)

        # 扫描
        p2 = ttk.LabelFrame(frm, text='数值扫描', padding=6)
        p2.pack(fill='x', pady=6)
        ttk.Label(p2, text='数值:').pack(side='left')
        self.value_entry = ttk.Entry(p2, width=14)
        self.value_entry.pack(side='left', padx=4)
        ttk.Label(p2, text='类型:').pack(side='left')
        self.type_combo = ttk.Combobox(p2, state='readonly', width=10,
                                       values=['全部', 'int32', 'int64', 'float', 'double'])
        self.type_combo.current(0)
        self.type_combo.pack(side='left', padx=4)
        self.first_btn = ttk.Button(p2, text='首次扫描', command=self.first_scan)
        self.first_btn.pack(side='left', padx=4)
        self.unknown_btn = ttk.Button(p2, text='未知初始值', command=self.unknown_scan)
        self.unknown_btn.pack(side='left', padx=4)
        self.writable_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(p2, text='仅可写内存', variable=self.writable_var).pack(side='left', padx=4)
        self.autoattach_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(p2, text='自动附加', variable=self.autoattach_var,
                        command=self._toggle_auto_attach).pack(side='left', padx=4)
        self.autorefresh_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(p2, text='自动刷新', variable=self.autorefresh_var,
                        command=self._toggle_auto_refresh).pack(side='left', padx=4)

        # 过滤
        p3 = ttk.Frame(frm)
        p3.pack(fill='x', pady=4)
        self.filter_var = tk.StringVar(value='eq')
        self.refine_val = tk.StringVar()
        for text, mode in [('数值等于', 'eq'), ('变大了', 'inc'), ('变小了', 'dec'),
                           ('没变', 'unch'), ('改变了', 'chg')]:
            ttk.Radiobutton(p3, text=text, value=mode, variable=self.filter_var).pack(side='left')
        ttk.Entry(p3, textvariable=self.refine_val, width=14).pack(side='left', padx=4)
        self.next_btn = ttk.Button(p3, text='再次扫描(缩小范围)', command=self.next_scan)
        self.next_btn.pack(side='left', padx=8)
        ttk.Button(p3, text='清空结果', command=self.clear_results).pack(side='left')

        # 结果列表
        p4 = ttk.LabelFrame(frm, text='扫描结果', padding=6)
        p4.pack(fill='both', expand=True, pady=6)
        cols = ('addr', 'type', 'value', 'frozen')
        self.tree = ttk.Treeview(p4, columns=cols, show='headings', height=14)
        self.tree.heading('addr', text='地址 (十六进制)')
        self.tree.heading('type', text='类型')
        self.tree.heading('value', text='当前值')
        self.tree.heading('frozen', text='冻结')
        self.tree.column('addr', width=170)
        self.tree.column('type', width=70, anchor='center')
        self.tree.column('value', width=130, anchor='center')
        self.tree.column('frozen', width=60, anchor='center')
        ys = ttk.Scrollbar(p4, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.pack(side='left', fill='both', expand=True)
        ys.pack(side='right', fill='y')

        # 编辑
        p5 = ttk.Frame(frm)
        p5.pack(fill='x', pady=4)
        ttk.Label(p5, text='新值:').pack(side='left')
        self.newval_entry = ttk.Entry(p5, width=14)
        self.newval_entry.pack(side='left', padx=4)
        ttk.Button(p5, text='写入选中', command=self.write_selected).pack(side='left', padx=4)
        ttk.Button(p5, text='冻结选中', command=self.freeze_selected).pack(side='left', padx=4)
        ttk.Button(p5, text='解除冻结', command=self.unfreeze_selected).pack(side='left', padx=4)
        ttk.Button(p5, text='读取选中', command=self.read_selected).pack(side='left', padx=4)
        ttk.Button(p5, text='删除选中', command=self.delete_selected).pack(side='left', padx=4)
        ttk.Button(p5, text='保存地址表', command=self.save_list).pack(side='left', padx=4)
        ttk.Button(p5, text='载入地址表', command=self.load_list).pack(side='left', padx=4)

        # 状态栏
        self.status = tk.StringVar(value='就绪')
        bar = ttk.Label(frm, textvariable=self.status, relief='sunken', anchor='w')
        bar.pack(fill='x', pady=(6, 0))

    # ---------------- 进程 ----------------
    def refresh_procs(self):
        try:
            procs = list_processes()
        except Exception as e:
            self.status.set('枚举进程失败: %s' % e)
            return
        self._procs = procs
        labels = ['%s  (PID %d)' % (os.path.basename(p[1]), p[0]) for p in procs]
        self.proc_combo['values'] = labels
        # 自动选中包含“纵横三界”的进程（排除自身：PyInstaller onefile 运行时
        # parent/child 两个进程同名，全部排除；开发时排除当前 PID）
        my_pid = os.getpid()
        my_name = os.path.basename(sys.executable).lower() if getattr(sys, '_MEIPASS', None) else None
        pick = None
        for i, (pid, path) in enumerate(procs):
            if pid == my_pid:
                continue
            if my_name and os.path.basename(path).lower() == my_name:
                continue
            if '纵横三界' in path:
                pick = i
                break
        if pick is not None:
            self.proc_combo.current(pick)
            self.status.set('发现游戏进程: %s' % os.path.basename(procs[pick][1]))
        else:
            self.status.set('未发现“纵横三界”进程，请手动选择')

    def _selected_proc(self):
        idx = self.proc_combo.current()
        if idx < 0 or idx >= len(getattr(self, '_procs', [])):
            return None
        return self._procs[idx]

    def attach(self):
        sel = self._selected_proc()
        if not sel:
            messagebox.showwarning('提示', '请先选择游戏进程')
            return
        pid, path = sel
        h = open_process(pid)
        if not h:
            code = ctypes.get_last_error()
            messagebox.showerror('错误', '附加失败 (错误码 %d)。\n请以管理员身份运行本程序，或检查游戏是否已启动。' % code)
            return
        if self.h:
            kernel32.CloseHandle(self.h)
        self.h = h
        self.pid = pid
        self.attached_var.set('已附加: %s (PID %d)' % (os.path.basename(path), pid))
        self.status.set('附加成功')

    # ---------------- 扫描 ----------------
    def _selected_types(self):
        t = self.type_combo.get()
        if t == '全部':
            return ['int32', 'int64', 'float', 'double']
        return [t]

    def _parse_value(self):
        try:
            return float(self.value_entry.get())
        except ValueError:
            messagebox.showwarning('提示', '请输入有效的数值')
            return None

    def first_scan(self):
        if not self.h:
            messagebox.showwarning('提示', '请先附加游戏进程')
            return
        val = self._parse_value()
        if val is None:
            return
        tnames = self._selected_types()
        patterns = []
        for tn in tnames:
            pat = pack_value(tn, int(val) if tn.startswith('int') else val)
            if pat:
                patterns.append((tn, pat))
        if not patterns:
            messagebox.showwarning('提示', '数值无法编码')
            return
        self._start_scan('first', patterns=patterns, val=val)

    def unknown_scan(self):
        """未知初始值扫描：不输入数值，先快照全部候选，再用 变大/变小/没变/改变 过滤"""
        if not self.h:
            messagebox.showwarning('提示', '请先附加游戏进程')
            return
        if self.matches:
            if not messagebox.askyesno('确认', '已有扫描结果，重新快照会清空当前结果。继续？'):
                return
        tnames = self._selected_types()
        self._start_scan('snapshot', tnames=tnames)

    def _start_scan(self, kind, patterns=None, mode=None, val=None, tnames=None):
        if self.scanning:
            return
        self.scanning = True
        self.cancel_scan = threading.Event()
        self.first_btn.config(state='disabled')
        self.unknown_btn.config(state='disabled')
        self.next_btn.config(state='disabled')
        self.attach_btn.config(state='disabled')
        h = self.h
        writable = self.writable_var.get()
        t = threading.Thread(target=self._scan_worker,
                             args=(kind, h, patterns, mode, val, writable, tnames),
                             daemon=True)
        t.start()

    def _scan_worker(self, kind, h, patterns, mode, val, writable, tnames):
        t0 = time.time()
        try:
            if kind == 'first':
                self.status.set('首次扫描中… (内存较大时可能需要十几秒)')
                res = scan_all(h, patterns, writable_only=writable,
                               progress_cb=lambda d, t: self.scan_queue.put(('prog', d, t)),
                               cancel=self.cancel_scan)
                self.scan_queue.put(('first_done', res, time.time() - t0))
            elif kind == 'snapshot':
                self.status.set('未知初始值快照中…')
                res = snapshot_all(h, tnames, writable_only=writable,
                                   progress_cb=lambda d, t: self.scan_queue.put(('prog', d, t)),
                                   cancel=self.cancel_scan)
                self.scan_queue.put(('snap_done', res, time.time() - t0))
            else:
                self.status.set('缩小范围中…')
                res = refine(h, self.matches, mode, val,
                             progress_cb=lambda d, t: self.scan_queue.put(('prog2', d, t)),
                             cancel=self.cancel_scan)
                self.scan_queue.put(('refine_done', res, time.time() - t0))
        except Exception as e:
            self.scan_queue.put(('error', str(e)))

    def next_scan(self):
        if not self.h:
            messagebox.showwarning('提示', '请先附加游戏进程')
            return
        if not self.matches:
            messagebox.showwarning('提示', '请先进行首次扫描')
            return
        mode = self.filter_var.get()
        val = None
        if mode == 'eq':
            try:
                val = float(self.refine_val.get())
            except ValueError:
                messagebox.showwarning('提示', '请输入用于“数值等于”过滤的数值')
                return
        self._start_scan('refine', mode=mode, val=val)

    def _poll_queue(self):
        try:
            while True:
                msg = self.scan_queue.get_nowait()
                self._handle_msg(msg)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _handle_msg(self, msg):
        kind = msg[0]
        if kind == 'prog':
            done, total = msg[1], msg[2]
            if total:
                self.status.set('扫描中… %d MB / %d MB' % (done // (1024*1024), total // (1024*1024)))
        elif kind == 'prog2':
            done, total = msg[1], msg[2]
            if total:
                self.status.set('过滤中… %d / %d' % (done, total))
        elif kind == 'first_done':
            res, secs = msg[1], msg[2]
            cap = 300000
            if len(res) > cap:
                res = res[:cap]
            self.matches = [(tn, a, None) for tn, a in res]
            self._show_results(self.matches)
            self.scanning = False
            self._enable_btns()
            self.status.set('首次扫描完成: 找到 %d 个地址 (耗时 %.1f 秒)' % (len(res), secs))
        elif kind == 'snap_done':
            res, secs = msg[1], msg[2]
            self.matches = [(tn, a, v) for tn, a, v in res]
            self._show_results(self.matches)
            self.scanning = False
            self._enable_btns()
            capped = len(res) >= SNAPSHOT_CAP
            self.status.set('快照完成: %d 个候选%s (耗时 %.1f 秒)。现在回游戏改数值，再用 变大/变小/没变 过滤'
                            % (len(res), '（已达上限）' if capped else '', secs))
        elif kind == 'refine_done':
            res, secs = msg[1], msg[2]
            self.matches = [(tn, a, v) for tn, a, v in res]
            self._show_results(self.matches)
            self.scanning = False
            self._enable_btns()
            self.status.set('过滤完成: 剩余 %d 个地址 (耗时 %.1f 秒)' % (len(res), secs))
        elif kind == 'hp_locate1':
            res, hp, mhp = msg[1], msg[2], msg[3]
            self._hp_locate_stage1(res, hp, mhp)
        elif kind == 'hp_locate2':
            res = msg[1]
            self._hp_locate_stage2(res)
        elif kind == 'sloc1':
            key, res = msg[1], msg[2]
            self._simple_locate_stage1(key, res)
        elif kind == 'sloc2':
            key, res = msg[1], msg[2]
            self._simple_locate_stage2(key, res)
        elif kind == 'error':
            self.scanning = False
            self._enable_btns()
            self.status.set('扫描出错: %s' % msg[1])

    def _enable_btns(self):
        self.first_btn.config(state='normal')
        self.unknown_btn.config(state='normal')
        self.next_btn.config(state='normal')
        self.attach_btn.config(state='normal')

    # ---------------- 一键无敌 ----------------
    def _read_hp_from_save(self):
        """自动读档找 Hp / max_Hp；返回 (hp, max_hp) 或 (None, None)"""
        sdir = default_save_dir()
        files = find_save_files(sdir)
        for f in files:
            hp, mhp = read_save_hp(f)
            if hp is not None and mhp is not None and mhp > 0:
                return hp, mhp
        return None, None

    def locate_hp(self):
        """定位 HP 地址（向导）：读档拿当前 HP → 全类型扫描 → 掉血校准 → 选定"""
        if not self.h:
            messagebox.showwarning('提示', '请先附加游戏进程')
            return
        if self.scanning:
            messagebox.showwarning('提示', '正在扫描中，请稍候')
            return
        hp, mhp = self._read_hp_from_save()
        if hp is None or hp <= 0:
            ret = _simple_ask('HP 定位', '未从存档读到角色 HP。\n请输入游戏界面上显示的当前生命值（整数或小数）：',
                              entry=True)
            if ret is None:
                return
            try:
                hp = float(ret)
            except ValueError:
                messagebox.showwarning('提示', 'HP 值无效')
                return
            ret2 = _simple_ask('HP 定位', '请输入角色的生命上限 max_Hp（满血值）：', entry=True)
            if ret2 is None:
                return
            try:
                mhp = float(ret2)
            except ValueError:
                messagebox.showwarning('提示', '满血值无效')
                return
        else:
            if not messagebox.askyesno('HP 定位', '从存档读到：当前 HP = %s，满血 = %s\n\n'
                                        '自动扫描内存中等于该 HP 的地址。\n'
                                        '若游戏内角色当前不是这个血量，请先回游戏调整，然后点「是」继续。'
                                        % (hp, mhp)):
                return
        # 满血值
        manual = self.hpmax_entry.get().strip()
        if manual:
            try:
                mhp = float(manual)
            except ValueError:
                pass
        if mhp <= 0:
            mhp = hp
        self.hp_max = mhp
        # 全类型扫描 == hp
        patterns = []
        for tn in ('int32', 'int64', 'float', 'double'):
            pat = pack_value(tn, int(hp) if tn.startswith('int') else hp)
            if pat:
                patterns.append((tn, pat))
        self.status.set('HP 定位：扫描内存中…')
        t = threading.Thread(target=self._locate_scan_worker, args=(patterns, hp, mhp), daemon=True)
        t.start()

    def _locate_scan_worker(self, patterns, hp, mhp):
        try:
            res = scan_all(self.h, patterns, writable_only=True,
                           progress_cb=lambda d, t: self.scan_queue.put(('prog', d, t)),
                           cancel=self.cancel_scan)
            self.scan_queue.put(('hp_locate1', res, hp, mhp))
        except Exception as e:
            self.scan_queue.put(('error', str(e)))

    def _hp_locate_stage1(self, res, hp, mhp):
        """第一步：扫描完成 → 提示掉血校准"""
        self.hp_max = mhp
        cap = 200000
        if len(res) > cap:
            res = res[:cap]
        self.matches = [(tn, a, None) for tn, a in res]
        self._show_results(self.matches)
        if not res:
            self.status.set('HP 定位：未找到匹配地址，请确认游戏内 HP 数值后重试')
            messagebox.showinfo('HP 定位', '未在可写内存中找到等于 %s 的地址。\n'
                                 '请确认已附加游戏、角色 HP 已显示，然后重试。' % hp)
            return
        self.status.set('HP 定位：找到 %d 个候选。回游戏让角色掉血，再点「校准」' % len(res))
        if messagebox.askyesno('HP 定位', '找到 %d 个候选地址。\n\n'
                               '请回游戏让角色被攻击掉一点血（HP 变小），然后点「是」自动校准。'
                               % len(res)):
            self._hp_refine_dec()

    def _hp_refine_dec(self):
        if not self.matches:
            return
        self.status.set('HP 定位：按「变小了」过滤…')
        t = threading.Thread(target=self._hp_refine_worker, daemon=True)
        t.start()

    def _hp_refine_worker(self):
        try:
            res = refine(self.h, self.matches, 'dec',
                         progress_cb=lambda d, t: self.scan_queue.put(('prog2', d, t)),
                         cancel=self.cancel_scan)
            self.scan_queue.put(('hp_locate2', res))
        except Exception as e:
            self.scan_queue.put(('error', str(e)))

    def _hp_locate_stage2(self, res):
        """第二步：变小过滤后选择 HP 地址"""
        self.matches = [(tn, a, v) for tn, a, v in res]
        self._show_results(self.matches)
        if not res:
            self.status.set('HP 定位：过滤后无候选（可能未掉血或类型不符）')
            messagebox.showinfo('HP 定位', '过滤后没有候选。\n可能原因：没掉血、或 HP 类型特殊。\n'
                                 '可重新定位，掉血后再试。')
            return
        if len(res) <= 4:
            self.hp_addrs = [(tn, a) for tn, a, _ in res]
            self._start_god()
            self.status.set('无敌已开启：锁定 %d 个 HP 地址 = 满血 %s' % (len(res), self.hp_max))
        else:
            # 让用户从候选里选
            self.status.set('HP 定位：候选 %d 个，请在结果列表中点选 HP 地址后按「选中即用」' % len(res))
            if messagebox.askyesno('HP 定位', '过滤后还剩 %d 个候选，请在结果列表中选择 HP 所在行，'
                                   '然后点「是」确认使用选中行。' % len(res)):
                rows = self._selected_rows()
                if rows:
                    self.hp_addrs = [(self.matches[i][0], self.matches[i][1]) for i in rows[:8]]
                    self._start_god()
                    self.status.set('无敌已开启：锁定 %d 个地址 = 满血 %s' % (len(self.hp_addrs), self.hp_max))

    def toggle_god(self):
        """一键无敌开关：开启=锁定 HP 满血；关闭=停止写入"""
        if self.god_thread and self.god_thread.is_alive():
            # 关闭
            self.stop_god.set()
            self.god_thread = None
            self.god_btn.config(text='开启无敌')
            self.god_state.set('无敌已关闭（正常受伤）')
            self.status.set('无敌模式已关闭')
            return
        if not self.h:
            messagebox.showwarning('提示', '请先附加游戏进程')
            return
        if not self.hp_addrs:
            # 未定位 → 走向导
            self.locate_hp()
            return
        self._start_god()

    def _start_god(self):
        if not self.hp_addrs or not self.hp_max:
            return
        self.stop_god.clear()
        self.god_thread = threading.Thread(target=self._god_loop, daemon=True)
        self.god_thread.start()
        self.god_btn.config(text='关闭无敌')
        self.god_state.set('无敌已开启：HP 锁定 = %s' % self.hp_max)

    def _god_loop(self):
        while not self.stop_god.is_set():
            if self.h and self.hp_addrs:
                for tn, addr in self.hp_addrs:
                    if tn.startswith('int'):
                        pat = pack_value(tn, int(self.hp_max))
                    else:
                        pat = pack_value(tn, float(self.hp_max))
                    if pat:
                        write_mem(self.h, addr, pat)
            time.sleep(0.08)

    # ---------------- 简易模式（血量/蓝量/金币/灵魂） ----------------
    def open_simple(self):
        """简易模式窗口：每行一个字段，当前值 + 新值 + 写入 + 锁定"""
        if not self.h:
            messagebox.showwarning('提示', '请先附加游戏进程（点「附加进程」）')
            return
        w = tk.Toplevel(self.root)
        w.title('简易模式 - 纵横三界 修改器')
        w.geometry('860x520')
        w.minsize(760, 460)
        w.transient(self.root)

        top = ttk.Frame(w, padding=6)
        top.pack(fill='x')
        ttk.Label(top, text='直接改游戏数值：先点「定位」自动找地址（按提示让数值变化），'
                            '然后输入新值点「写入」，或勾「锁定」持续保持。',
                  foreground='#444').pack(side='left')

        rows = {}  # key -> dict(widgets)
        frm = ttk.LabelFrame(w, text='关键数值', padding=6)
        frm.pack(fill='x', padx=6, pady=4)
        for i, (key, cfg) in enumerate(SIMPLE_FIELDS.items()):
            r = ttk.Frame(frm)
            r.pack(fill='x', pady=2)
            ttk.Label(r, text=cfg['label'], width=6, font=('Microsoft YaHei UI', 10, 'bold')).pack(side='left')
            loc = ttk.Button(r, text='定位', width=5, command=lambda k=key: self.simple_locate(k))
            loc.pack(side='left', padx=2)
            cur = ttk.Label(r, text='未定位', width=26, foreground='#666')
            cur.pack(side='left', padx=4)
            nv = ttk.Entry(r, width=12)
            nv.pack(side='left', padx=2)
            wr = ttk.Button(r, text='写入', width=5, command=lambda k=key: self._simple_write(k))
            wr.pack(side='left', padx=2)
            lk = tk.BooleanVar()
            ttk.Checkbutton(r, text='锁定', variable=lk,
                            command=lambda k=key, v=lk: self._simple_toggle_lock(k, v)).pack(side='left', padx=4)
            rows[key] = {'cur': cur, 'newval': nv, 'locked': lk,
                         'loc_btn': loc, 'write_btn': wr}
        self._simple_rows = rows

        # 候选列表（定位结果，候选多时手动选）
        mid = ttk.LabelFrame(w, text='定位候选（选中的行可点「绑定」到当前定位字段）', padding=6)
        mid.pack(fill='both', expand=True, padx=6, pady=4)
        cols = ('addr', 'type', 'value')
        tree = ttk.Treeview(mid, columns=cols, show='headings', height=7)
        for c, t, wid in (('addr', '地址 (十六进制)', 200), ('type', '类型', 90), ('value', '当前值', 140)):
            tree.heading(c, text=t)
            tree.column(c, width=wid)
        tree.pack(side='left', fill='both', expand=True)
        self.simple_tree = tree

        bot = ttk.Frame(w, padding=6)
        bot.pack(fill='x')
        ttk.Button(bot, text='绑定选中候选', command=self._simple_bind_selected).pack(side='left', padx=4)
        self.simple_status = tk.StringVar(value='就绪')
        ttk.Label(bot, textvariable=self.simple_status, foreground='#555').pack(side='left', padx=8)

        # 当前值定时刷新（只更新“当前”显示，不覆盖用户输入的新值）
        def _refresh_loop():
            for key, rec in list(rows.items()):
                info = self.simple_addrs.get(key)
                if info and info.get('addrs'):
                    vals = []
                    for tn, a in info['addrs'][:3]:
                        raw = read_mem(self.h, a, TYPES[tn][0])
                        v = unpack_value(tn, raw) if raw else None
                        if v is not None:
                            vals.append(v)
                    if vals:
                        t = ' / '.join('%.4g' % x for x in vals)
                        rec['cur'].config(text='当前: ' + t, foreground='#000')
            if tk:
                try:
                    w.after(700, _refresh_loop)
                except Exception:
                    pass

        w.after(700, _refresh_loop)
        self._simple_win = w

    def _simple_bind_selected(self):
        sel = self.simple_tree.selection()
        if not sel:
            messagebox.showwarning('提示', '请先在候选列表选择一行')
            return
        idx = int(sel[0])
        if 0 <= idx < len(self.simple_cands):
            tn, a, v = self.simple_cands[idx]
            key = self.simple_key
            if key:
                self.simple_addrs[key] = {'addrs': [(tn, a)], 'locked_value': float(v), 'locked': False}
                self.simple_status.set('已绑定 %s → %s (%s)' % (key, hex(a), tn))
                self._simple_win.after(0, lambda: self._simple_refresh_row(key))

    def _simple_refresh_row(self, key):
        pass  # 由刷新循环自动更新

    def simple_locate(self, key):
        """简易定位：读档当前值 → 全类型扫描 → 引导数值变化 → 过滤收敛"""
        if not self.h:
            messagebox.showwarning('提示', '请先附加游戏进程')
            return
        if self.scanning:
            messagebox.showwarning('提示', '正在扫描中，请稍候')
            return
        cfg = SIMPLE_FIELDS[key]
        v = None
        sdir = default_save_dir()
        for f in find_save_files(sdir):
            v = read_save_value(f, cfg['section'], cfg['key'])
            if v is not None:
                break
        if v is None or v <= 0:
            ret = _simple_ask('定位 %s' % cfg['label'],
                              '未从存档读到%s当前值。\n请输入游戏界面上显示的当前数值（整数或小数）：' % cfg['label'],
                              entry=True)
            if ret is None:
                return
            try:
                v = float(ret)
            except ValueError:
                messagebox.showwarning('提示', '数值无效')
                return
        else:
            if not messagebox.askyesno('定位 %s' % cfg['label'],
                                       '从存档读到：%s 当前 = %s\n\n自动扫描内存中等于该值的地址。\n'
                                       '若游戏内当前不是这个数值，请先回游戏调整，然后点「是」继续。'
                                       % (cfg['label'], v)):
                return
        patterns = []
        for tn in ('int32', 'int64', 'float', 'double'):
            pat = pack_value(tn, int(v) if tn.startswith('int') else v)
            if pat:
                patterns.append((tn, pat))
        self.simple_key = key
        self.simple_cands = []
        self.simple_tree.delete(*self.simple_tree.get_children())
        self.simple_status.set('扫描内存中…')
        t = threading.Thread(target=self._simple_locate_worker, args=(patterns, key), daemon=True)
        t.start()

    def _simple_locate_worker(self, patterns, key):
        try:
            res = scan_all(self.h, patterns, writable_only=True,
                           progress_cb=lambda d, t: self.scan_queue.put(('prog', d, t)),
                           cancel=self.cancel_scan)
            self.scan_queue.put(('sloc1', key, res))
        except Exception as e:
            self.scan_queue.put(('error', str(e)))

    def _simple_locate_stage1(self, key, res):
        cfg = SIMPLE_FIELDS[key]
        cap = 200000
        if len(res) > cap:
            res = res[:cap]
        self.simple_cands = [(tn, a, None) for tn, a in res]
        self._simple_show_cands()
        if not res:
            self.simple_status.set('未找到匹配地址，请确认游戏内数值后重试')
            messagebox.showinfo('定位失败', '未在可写内存中找到等于当前值的地址。\n请确认已附加游戏、数值已显示，然后重试。')
            return
        self.simple_status.set('找到 %d 个候选。%s，然后点「是」' % (len(res), cfg['hint']))
        if messagebox.askyesno('定位 %s' % cfg['label'], '找到 %d 个候选地址。\n\n%s，然后点「是」自动校准。'
                               % (len(res), cfg['hint'])):
            self._simple_refine(key)

    def _simple_refine(self, key):
        self.simple_status.set('过滤中…')
        t = threading.Thread(target=self._simple_refine_worker, args=(key,), daemon=True)
        t.start()

    def _simple_refine_worker(self, key):
        try:
            # simple_cands 已在 stage1 被回写为掉血前的旧值，直接用于过滤
            mm = [(tn, a, v) for tn, a, v in self.simple_cands if v is not None]
            cfg = SIMPLE_FIELDS[key]
            res = refine(self.h, mm, cfg['dir'],
                         progress_cb=lambda d, t: self.scan_queue.put(('prog2', d, t)),
                         cancel=self.cancel_scan)
            self.scan_queue.put(('sloc2', key, res))
        except Exception as e:
            self.scan_queue.put(('error', str(e)))

    def _simple_locate_stage2(self, key, res):
        cfg = SIMPLE_FIELDS[key]
        self.simple_cands = [(tn, a, v) for tn, a, v in res]
        self._simple_show_cands()
        if not res:
            self.simple_status.set('过滤后无候选（可能没变化或类型不符），可重新定位')
            messagebox.showinfo('定位失败', '过滤后没有候选。\n可能原因：数值没变化、或类型特殊。\n请重新定位。')
            return
        if len(res) <= 4:
            self.simple_addrs[key] = {'addrs': [(tn, a) for tn, a, _ in res],
                                      'locked_value': float(res[0][2]), 'locked': False}
            self.simple_status.set('%s 定位成功：%d 个地址' % (cfg['label'], len(res)))
            messagebox.showinfo('定位成功', '%s 已定位到 %d 个地址，可直接写入/锁定。' % (cfg['label'], len(res)))
        else:
            self.simple_status.set('%s 候选 %d 个：请在候选列表选中一行后点「绑定选中候选」' % (cfg['label'], len(res)))

    def _simple_show_cands(self):
        self.simple_tree.delete(*self.simple_tree.get_children())
        for i, (tn, a, v) in enumerate(self.simple_cands):
            if v is None:
                raw = read_mem(self.h, a, TYPES[tn][0])
                v = unpack_value(tn, raw) if raw else None
                self.simple_cands[i] = (tn, a, v)  # 回写，供 refine 用掉血前旧值
            vt = '' if v is None else ('%.4f' % v if tn in ('float', 'double') else str(v))
            self.simple_tree.insert('', 'end', iid=str(i), values=('%016X' % a, tn, vt))

    def _simple_write(self, key):
        if not self.h:
            return
        info = self.simple_addrs.get(key)
        if not info or not info['addrs']:
            messagebox.showwarning('提示', '请先点「定位」找到该数值的地址')
            return
        cfg = SIMPLE_FIELDS[key]
        v = self._simple_entry_value(key)
        if v is None:
            messagebox.showwarning('提示', '请输入有效的新值')
            return
        ok = 0
        for tn, a in info['addrs']:
            pat = pack_value(tn, int(v) if tn.startswith('int') else v)
            if pat and write_mem(self.h, a, pat):
                ok += 1
        info['locked_value'] = v
        self.simple_status.set('%s 写入成功 %d 个地址 = %s' % (cfg['label'], ok, v))

    def _simple_entry_value(self, key):
        w = getattr(self, '_simple_rows', {}).get(key, {}).get('newval')
        try:
            return float(w.get()) if w is not None else None
        except (ValueError, AttributeError):
            return None

    def _simple_toggle_lock(self, key, var):
        info = self.simple_addrs.get(key)
        if var.get():
            if not info or not info['addrs']:
                var.set(False)
                messagebox.showwarning('提示', '请先点「定位」')
                return
            v = self._simple_entry_value(key)
            if v is None:
                var.set(False)
                messagebox.showwarning('提示', '请先输入要锁定的数值')
                return
            info['locked'] = True
            info['locked_value'] = v
        else:
            if info:
                info['locked'] = False
        if not (self.simple_lock_thread and self.simple_lock_thread.is_alive()):
            self.simple_stop_lock.clear()
            self.simple_lock_thread = threading.Thread(target=self._simple_lock_loop, daemon=True)
            self.simple_lock_thread.start()
        self.simple_status.set('锁定状态已更新')

    def _simple_lock_loop(self):
        while not self.simple_stop_lock.is_set():
            if self.h:
                for key, info in list(self.simple_addrs.items()):
                    if info.get('locked') and info.get('addrs'):
                        v = info['locked_value']
                        for tn, a in info['addrs']:
                            pat = pack_value(tn, int(v) if tn.startswith('int') else v)
                            if pat:
                                write_mem(self.h, a, pat)
            time.sleep(0.08)

    def _simple_refresh_row(self, key):
        info = self.simple_addrs.get(key)
        if info and info.get('addrs'):
            vals = []
            for tn, a in info['addrs'][:3]:
                raw = read_mem(self.h, a, TYPES[tn][0])
                v = unpack_value(tn, raw) if raw else None
                if v is not None:
                    vals.append(v)
            if vals:
                cfg = SIMPLE_FIELDS[key]
                t = ' / '.join('%.4g' % x for x in vals)
                row = getattr(self, '_simple_rows', {}).get(key)
                if row:
                    row['cur'].config(text='当前: ' + t, foreground='#000')

    # ---------------- 自动附加 / 自动刷新 ----------------
    def _toggle_auto_attach(self):
        if self.autoattach_var.get():
            self.status.set('自动附加已开启：检测到游戏进程将自动附加')
            self.root.after(1500, self._auto_attach_tick)
        else:
            self.status.set('自动附加已关闭')

    def _auto_attach_tick(self):
        if not self.autoattach_var.get():
            return
        if self.h is None:
            my_pid = os.getpid()
            my_name = os.path.basename(sys.executable).lower() if getattr(sys, '_MEIPASS', None) else None
            for i, (pid, path) in enumerate(getattr(self, '_procs', [])):
                if pid == my_pid:
                    continue
                if my_name and os.path.basename(path).lower() == my_name:
                    continue
                if '纵横三界' in path:
                    self.proc_combo.current(i)
                    self.attach()
                    break
            if self.h is None:
                self.refresh_procs()
        self.root.after(2000, self._auto_attach_tick)

    def _toggle_auto_refresh(self):
        if self.autorefresh_var.get():
            self.status.set('自动刷新已开启（每 0.5 秒刷新列表数值）')
            self.root.after(500, self._auto_refresh_tick)
        else:
            self.status.set('自动刷新已关闭')

    def _auto_refresh_tick(self):
        if not self.autorefresh_var.get():
            return
        if self.h and self.matches and not self.scanning:
            n = min(len(self.matches), 800)
            for i in range(n):
                tn, addr, _old = self.matches[i]
                raw = read_mem(self.h, addr, TYPES[tn][0])
                v = unpack_value(tn, raw) if raw else None
                if v is not None:
                    self.matches[i] = (tn, addr, v)
            self._show_results(self.matches)
        self.root.after(500, self._auto_refresh_tick)

    # ---------------- 结果展示 ----------------
    def _show_results(self, matches):
        self.tree.delete(*self.tree.get_children())
        # 刷新当前值
        for i, (tn, addr, val) in enumerate(matches):
            if val is None:
                raw = read_mem(self.h, addr, TYPES[tn][0])
                val = unpack_value(tn, raw) if raw else None
                matches[i] = (tn, addr, val)
            frozen = '是' if addr in self.frozen else ''
            vtext = '' if val is None else ('%.4f' % val if tn in ('float', 'double') else str(val))
            self.tree.insert('', 'end', iid=str(i),
                             values=('%016X' % addr, tn, vtext, frozen))

    def _selected_rows(self):
        sel = self.tree.selection()
        out = []
        for iid in sel:
            idx = int(iid)
            if 0 <= idx < len(self.matches):
                out.append(idx)
        return out

    def read_selected(self):
        if not self.h:
            return
        for idx in self._selected_rows():
            tn, addr, _ = self.matches[idx]
            raw = read_mem(self.h, addr, TYPES[tn][0])
            v = unpack_value(tn, raw) if raw else None
            if v is not None:
                self.matches[idx] = (tn, addr, v)
        self._show_results(self.matches)

    def write_selected(self):
        if not self.h:
            messagebox.showwarning('提示', '请先附加游戏进程')
            return
        try:
            newval = self.newval_entry.get().strip()
            if newval == '':
                raise ValueError
            if any(c.isalpha() for c in newval.replace('e', '').replace('E', '').replace('-', '').replace('.', '').replace('+', '')):
                # 十六进制形式？支持 0x 开头
                if newval.lower().startswith('0x'):
                    newval = str(int(newval, 16))
                else:
                    raise ValueError
            nv = float(newval)
        except ValueError:
            messagebox.showwarning('提示', '请输入有效的新值（支持小数，或 0x 开头十六进制）')
            return
        rows = self._selected_rows()
        if not rows:
            messagebox.showwarning('提示', '请先在结果列表中选择地址')
            return
        ok = 0
        for idx in rows:
            tn, addr, _ = self.matches[idx]
            if tn.startswith('int'):
                iv = int(nv)
                pat = pack_value(tn, iv)
            else:
                pat = pack_value(tn, nv)
            if pat and write_mem(self.h, addr, pat):
                ok += 1
                self.matches[idx] = (tn, addr, iv if tn.startswith('int') else nv)
        self._show_results(self.matches)
        self.status.set('写入成功 %d 个地址' % ok)

    def freeze_selected(self):
        if not self.h:
            messagebox.showwarning('提示', '请先附加游戏进程')
            return
        rows = self._selected_rows()
        if not rows:
            messagebox.showwarning('提示', '请先选择地址')
            return
        started = self.freeze_thread and self.freeze_thread.is_alive()
        for idx in rows:
            tn, addr, val = self.matches[idx]
            if val is None:
                raw = read_mem(self.h, addr, TYPES[tn][0])
                val = unpack_value(tn, raw) if raw else None
            if val is not None:
                self.frozen[addr] = (tn, val)
        if not started:
            self.stop_freeze.clear()
            self.freeze_thread = threading.Thread(target=self._freeze_loop, daemon=True)
            self.freeze_thread.start()
        self._show_results(self.matches)
        self.status.set('已冻结 %d 个地址' % len(rows))

    def unfreeze_selected(self):
        for idx in self._selected_rows():
            tn, addr, _ = self.matches[idx]
            self.frozen.pop(addr, None)
        self._show_results(self.matches)

    def _freeze_loop(self):
        while not self.stop_freeze.is_set():
            if self.h and self.frozen:
                for addr, (tn, val) in list(self.frozen.items()):
                    pat = pack_value(tn, val)
                    if pat:
                        write_mem(self.h, addr, pat)
            time.sleep(0.1)

    def delete_selected(self):
        rows = set(self._selected_rows())
        if not rows:
            return
        self.matches = [m for i, m in enumerate(self.matches) if i not in rows]
        self._show_results(self.matches)

    def clear_results(self):
        self.matches = []
        self.tree.delete(*self.tree.get_children())

    # ---------------- 地址表 ----------------
    def save_list(self):
        if not self.matches:
            messagebox.showwarning('提示', '没有可保存的地址')
            return
        path = filedialog.asksaveasfilename(
            defaultextension='.json', initialfile='纵横三界地址表.json',
            filetypes=[('JSON', '*.json')])
        if not path:
            return
        data = [{'addr': a, 'type': tn} for tn, a, _ in self.matches]
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        self.status.set('地址表已保存: %s' % path)

    def load_list(self):
        path = filedialog.askopenfilename(filetypes=[('JSON', '*.json')])
        if not path:
            return
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            matches = []
            for item in data:
                tn = item.get('type', 'int32')
                if tn not in TYPES:
                    tn = 'int32'
                matches.append((tn, int(item['addr'], 0), None))
            self.matches = matches
            self._show_results(matches)
            self.status.set('已载入 %d 个地址' % len(matches))
        except Exception as e:
            messagebox.showerror('错误', '载入失败: %s' % e)


def _simple_ask(title, prompt, entry=False):
    """极简输入弹窗（避免因 import 顺序在打包后失效）；返回输入文本或 None"""
    if tk is None:
        return None
    result = {}

    def _ok():
        result['v'] = e.get() if entry else True
        w.destroy()

    w = tk.Toplevel()
    w.title(title)
    w.attributes('-topmost', True)
    w.geometry('+%d+%d' % (w.winfo_screenwidth() // 2 - 180, w.winfo_screenheight() // 2 - 70))
    ttk.Label(w, text=prompt, wraplength=420, justify='left').pack(padx=12, pady=8)
    e = ttk.Entry(w, width=36)
    if entry:
        e.pack(padx=12, pady=4)
    ttk.Button(w, text='确定', command=_ok).pack(pady=8)
    w.grab_set()
    w.wait_window()
    return result.get('v')


def main():
    if tk is None:
        print('需要 tkinter 支持（标准 Python 自带）')
        sys.exit(1)
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app = TrainerApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
