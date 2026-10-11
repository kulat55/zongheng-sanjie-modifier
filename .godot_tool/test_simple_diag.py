# -*- coding: utf-8 -*-
# 简易模式定位诊断
import importlib.util, subprocess, sys, time, struct

sys.path.insert(0, r"E:\baidu-game\.godot_tool")
spec = importlib.util.spec_from_file_location("mt", r"E:\baidu-game\纵横三界修改器\memory_trainer.py")
mt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mt)

sim = subprocess.Popen([sys.executable, r"E:\baidu-game\.godot_tool\sim_target.py"],
                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, text=True)
try:
    assert 'SIM_READY' in sim.stdout.readline()
    time.sleep(0.3)
    h = mt.open_process(sim.pid)

    import tkinter as tk
    import tkinter.messagebox as mb
    root = tk.Tk()
    root.withdraw()
    app = mt.TrainerApp(root)
    app.h = h
    app.pid = sim.pid

    # mock：第1次弹窗(自动扫描确认) True；第2次(校准) False —— 停在 stage1 后
    n = {'c': 0}
    def fake_ask(*a, **k):
        n['c'] += 1
        print('ASK', n['c'], a[1] if len(a) > 1 else '')
        return n['c'] == 1
    for name in ('askyesno', 'showinfo', 'showwarning', 'showerror'):
        setattr(mb, name, fake_ask)

    app.open_simple()
    for _ in range(10):
        root.update(); time.sleep(0.03)

    app.simple_locate('hp')
    deadline = time.time() + 60
    while time.time() < deadline:
        root.update()
        if app.simple_cands and n['c'] >= 2:
            break
        time.sleep(0.03)
    print('CAND_COUNT=%d' % len(app.simple_cands))
    print('CAND_SAMPLE=', app.simple_cands[:6])
    # 检查 buffer double 地址的值
    for tn, a, v in app.simple_cands:
        if tn == 'double' and a % 0x1000 < 16:
            print('DBL_NEAR_ZERO', hex(a), v)
    # 手动触发掉血后 refine
    sim.stdin.write('down\n'); sim.stdin.flush(); sim.stdout.readline()
    time.sleep(0.3)
    app._simple_refine('hp')
    deadline = time.time() + 60
    while time.time() < deadline:
        root.update()
        if 'hp' in app.simple_addrs:
            break
        if app.simple_status.get().startswith('过滤后'):
            break
        time.sleep(0.03)
    print('STATUS=', app.simple_status.get())
    print('SIMPLE_ADDRS=', {k: [(tn, hex(a)) for tn, a in v['addrs']] for k, v in app.simple_addrs.items()})
    root.destroy()
finally:
    try:
        sim.kill(); sim.wait(timeout=3)
    except Exception:
        pass
