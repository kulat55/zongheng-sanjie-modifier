# -*- coding: utf-8 -*-
# 一键无敌核心链纯函数端到端测试（无 GUI）
# 流程：附加静止HP模拟进程 → 扫描==80 → stdin触发"游戏掉血"(75) → dec过滤保留2个
#       → 无敌写回80（采样锁定）→ 触发再掉血仍被拉回 → 结束
import importlib.util, subprocess, sys, time, struct

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

    # 1. 读档拿 HP/max
    sdir = mt.default_save_dir()
    files = mt.find_save_files(sdir)
    hp, mhp = mt.read_save_hp(files[0])
    print('SAVE_HP=%s MAX=%s' % (hp, mhp))
    assert hp and hp > 0 and mhp >= hp

    # 2. 全类型扫描 == HP
    patterns = []
    for tn in ('int32', 'int64', 'float', 'double'):
        pat = mt.pack_value(tn, int(hp) if tn.startswith('int') else hp)
        if pat:
            patterns.append((tn, pat))
    res = mt.scan_all(h, patterns, writable_only=True)
    print('SCAN_HITS=%d' % len(res))
    assert len(res) >= 2

    # 3. 记录当前值 → 触发掉血(75) → dec 过滤
    matches = []
    for tn, a in res:
        raw = mt.read_mem(h, a, mt.TYPES[tn][0])
        v = mt.unpack_value(tn, raw) if raw else None
        if v is not None:
            matches.append((tn, a, v))
    sim.stdin.write('down\n'); sim.stdin.flush()
    assert 'DOWN' in sim.stdout.readline()
    time.sleep(0.2)
    refined = mt.refine(h, matches, 'dec')
    print('REFINED=%d' % len(refined))
    assert len(refined) == 2, [(tn, hex(a), v) for tn, a, v in refined]
    print('REFINE_DEC_OK', [(tn, hex(a), v) for tn, a, v in refined])
    hp_addrs = [(tn, a) for tn, a, _ in refined]

    # 4. 无敌锁定：写回满血 80，观察拉回（用 double 地址采样）
    hp_addrs = [(tn, a) for tn, a, _ in refined]
    dbl_addr = next(a for tn, a in hp_addrs if tn == 'double')
    locked = 0
    for _ in range(25):
        for tn, a in hp_addrs:
            pat = mt.pack_value(tn, int(mhp) if tn.startswith('int') else mhp)
            if pat:
                mt.write_mem(h, a, pat)
        raw = mt.read_mem(h, dbl_addr, 8)
        v = struct.unpack('<d', raw)[0] if raw else None
        if v == mhp:
            locked += 1
        time.sleep(0.12)
    print('LOCK_SAMPLES=%d/25' % locked)
    assert locked >= 20, 'HP not held at max'

    # 5. 无敌期间再触发掉血 → 持续写回会拉回满血
    sim.stdin.write('down\n'); sim.stdin.flush()
    assert 'DOWN' in sim.stdout.readline()
    for _ in range(5):
        for tn, a in hp_addrs:
            pat = mt.pack_value(tn, int(mhp) if tn.startswith('int') else mhp)
            if pat:
                mt.write_mem(h, a, pat)
        time.sleep(0.12)
    raw = mt.read_mem(h, dbl_addr, 8)
    v = struct.unpack('<d', raw)[0] if raw else None
    print('RE_DAMAGE_PULLBACK=%s' % v)
    assert v == mhp, 'god did not pull back after re-damage'
    print('CORE_CHAIN_PASS')
finally:
    try:
        sim.kill()
        sim.wait(timeout=3)
    except Exception:
        pass
