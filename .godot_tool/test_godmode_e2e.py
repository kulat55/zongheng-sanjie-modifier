# -*- coding: utf-8 -*-
# 一键无敌 GUI 层端到端：附加模拟进程 → locate_hp 向导（自动读档→扫描→掉血校准）→
#      锁定写回 → 关闭 → 再开启
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
    assert h, 'attach failed'

    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    app = mt.TrainerApp(root)
    app.h = h
    app.pid = sim.pid
    app.attached_var.set('已附加(测试)')

    # mock 所有弹窗：自动继续，绝不阻塞；第2次确认（掉血校准）前先触发模拟掉血
    import tkinter.messagebox as mb
    calls = {'n': 0}
    def fake_ask(*a, **k):
        calls['n'] += 1
        if calls['n'] >= 2:
            sim.stdin.write('down\n'); sim.stdin.flush()
            assert 'DOWN' in sim.stdout.readline()
        return True
    for name in ('askyesno', 'showinfo', 'showwarning', 'showerror'):
        setattr(mb, name, fake_ask)

    # 走定位向导（自动读档 HP → 扫描 → 弹窗确认(True) → 掉血校准）
    _hp, _mhp = mt.read_save_hp(mt.find_save_files(mt.default_save_dir())[0])
    assert _hp is not None
    app.locate_hp()
    # 等扫描完成 → 校准弹窗被 mock 为 True → 触发 refine
    deadline = time.time() + 120
    located = False
    while time.time() < deadline:
        root.update()
        if app.hp_addrs and (app.god_thread and app.god_thread.is_alive()):
            located = True
            break
        time.sleep(0.03)
    assert located, 'locate did not finish; status=%s' % app.status.get()
    print('LOCATE_OK addrs=%d status=%s' % (len(app.hp_addrs), app.god_state.get()))

    # 模拟掉血（stdin 触发 sim 掉血）→ 后台 god 线程应拉回满血
    sim.stdin.write('down\n'); sim.stdin.flush()
    assert 'DOWN' in sim.stdout.readline()
    time.sleep(0.6)
    dbl_addr = next(a for tn, a in app.hp_addrs if tn == 'double')
    raw = mt.read_mem(h, dbl_addr, 8)
    v = struct.unpack('<d', raw)[0] if raw else None
    print('PULLBACK_WHILE_ON=%s (want %s)' % (v, _hp))
    assert v == _hp, 'god thread not writing'

    # 关闭无敌 → 再掉血不再拉回
    app.toggle_god()
    sim.stdin.write('down\n'); sim.stdin.flush()
    assert 'DOWN' in sim.stdout.readline()
    time.sleep(0.6)
    raw = mt.read_mem(h, dbl_addr, 8)
    v2 = struct.unpack('<d', raw)[0] if raw else None
    print('AFTER_OFF=%s' % v2)
    assert v2 == _hp - 5.0, 'still writing after toggle off: %s' % v2

    # 再开启（不重新定位）
    app.toggle_god()
    time.sleep(0.5)
    raw = mt.read_mem(h, dbl_addr, 8)
    v3 = struct.unpack('<d', raw)[0] if raw else None
    print('REON=%s' % v3)
    assert v3 == _hp, 're-enable failed'

    app.stop_god.set()
    root.destroy()
    print('GODMODE_GUI_E2E_PASS')
finally:
    try:
        sim.kill()
        sim.wait(timeout=3)
    except Exception:
        pass
