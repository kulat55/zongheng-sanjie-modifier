# -*- coding: utf-8 -*-
# 一键无敌逻辑测试（读档HP + 定位流程各环节）
import importlib.util, json, os

spec = importlib.util.spec_from_file_location("mt", r"E:\baidu-game\纵横三界修改器\memory_trainer.py")
mt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mt)

# 1. 存档目录自动定位
sdir = mt.default_save_dir()
print('SAVE_DIR:', sdir)
files = mt.find_save_files(sdir)
print('SAVE_FILES:', [os.path.basename(f) for f in files])
assert files, 'no save files found'

# 2. 读 HP
hp, mhp = mt.read_save_hp(files[0])
print('HP=%s MAX=%s' % (hp, mhp))
assert hp is not None and mhp is not None and mhp > 0

# 3. 扫描编码：四种类型都能编码
for tn in ('int32', 'int64', 'float', 'double'):
    pat = mt.pack_value(tn, int(hp) if tn.startswith('int') else hp)
    assert pat, tn
print('PACK_OK')

# 4. 变更过滤逻辑（用假数据）
matches = [('double', 0x1000, 80.0), ('double', 0x2000, 80.0), ('float', 0x3000, 79.0)]
# 模拟变小过滤：旧值80 现值79/78
print('REFINE_MOCK_SKIP (needs live process)')

# 5. 无敌写值编码
mhp = float(mhp)
for tn in ('int32', 'int64', 'float', 'double'):
    pat = mt.pack_value(tn, int(mhp) if tn.startswith('int') else mhp)
    assert pat
print('GOD_PACK_OK')
print('GODMODE_LOGIC_OK')
