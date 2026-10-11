# -*- coding: utf-8 -*-
# 装备管理功能端到端测试
import importlib.util, json, os, tempfile, shutil

spec = importlib.util.spec_from_file_location("se", r"E:\baidu-game\纵横三界修改器\save_editor.py")
se = importlib.util.module_from_spec(spec)
spec.loader.exec_module(se)

# 1. 装备表加载
table = se.load_equipment_table()
assert len(table) >= 985, len(table)
assert 'ryjgb' in table and table['ryjgb']['名字'] == '如意金箍棒'
assert 'ptxzg' in table and table['ptxzg']['power'] == 4  # randi_range(4,6)->4
assert 'xhmt' in table and table['xhmt']['成长率'] == 1.2  # snapped(randf_range(1.2,1.4),0.1)->1.2
print('TABLE_OK items=%d' % len(table))

# 2. 用真实存档结构做加背包/穿装测试
real = json.load(open(r"C:\Users\lenovo\AppData\Roaming\Godot\app_userdata\造梦西游 · 圆梦计划：纵横三界篇 (4.5)\saves\save_1.json", encoding='utf-8'))
data = json.loads(json.dumps(real))  # 深拷贝，不动真实档

# 2a. 加武器（不可堆叠）→ 装备分类
ok, msg = se.add_equipment_to_backpack(data, 'ryjgb', 1)
assert ok, msg
cd = data['character_data']
items = cd['backpack_items']
new = [it for it in items if it.get('item_data', {}).get('id') == 'ryjgb']
assert len(new) == 1, items[-3:]
entry = new[0]
assert entry['category'] == 'equipment'
assert entry['item_data']['id'] == 'ryjgb'
assert entry['item_data']['数量'] == 1
# category_states 同步
cs = data['category_states']['equipment']
p, s = int(entry['page']), int(entry['slot_index'])
assert str(s) in cs[p], (p, s, cs[p].keys())
assert cs[p][str(s)]['id'] == 'ryjgb'
print('ADD_WEAPON_OK', msg)

# 2b. 再加一件武器 → 不冲突（新空位）
ok2, msg2 = se.add_equipment_to_backpack(data, 'jcbj', 1)
assert ok2, msg2
ids = [it.get('item_data', {}).get('id') for it in cd['backpack_items']]
assert ids.count('ryjgb') == 1 and ids.count('jcbj') == 1
print('ADD_WEAPON2_OK', msg2)

# 2c. 加可堆叠道具（新手宝箱已存在1个 → 应合并为 1+5=6）
ok3, msg3 = se.add_equipment_to_backpack(data, 'xsbx', 5)
assert ok3, msg3
n = [it for it in cd['backpack_items'] if it.get('item_data', {}).get('id') == 'xsbx'][0]['item_data']['数量']
assert n == 6, n
print('MERGE_STACKABLE_OK', msg3)

# 2d. 直接穿上：如意金箍棒 → 悟空（武器→槽位0）
ok4, msg4 = se.equip_item_to_role(data, 'wu_kong', 'ryjgb')
assert ok4, msg4
re_data = cd['role_equipment']['roles']['wu_kong']
slot0 = re_data['slots'][0]
assert slot0.get('id') == 'ryjgb', slot0
assert re_data['appearance']['weapon'] == 'res://assets/Art/HeroPicture/Role2AllEquipment/Role_2_Eq_ryjgb.png'
print('EQUIP_WEAPON_OK', msg4)

# 2e. 穿防具 → 槽位2
ok5, msg5 = se.equip_item_to_role(data, 'wu_kong', 'jcbj')  # 家传宝剑是武器
assert ok5, msg5
# 找一件防具
armor_id = next(iid for iid, e in table.items() if e.get('类型') == '防具' and e.get('所属') == '悟空')
ok6, msg6 = se.equip_item_to_role(data, 'wu_kong', armor_id)
assert ok6, msg6
assert cd['role_equipment']['roles']['wu_kong']['slots'][2].get('id') == armor_id
print('EQUIP_ARMOR_OK', msg6, armor_id)

# 2f. 换装：同槽位再穿另一件 → 旧装备应回背包
first = cd['role_equipment']['roles']['wu_kong']['slots'][0]['id']
armors = [iid for iid, e in table.items() if e.get('类型') == '防具' and e.get('所属') == '悟空']
ok7, msg7 = se.equip_item_to_role(data, 'wu_kong', 'jcbj')  # 剑穿槽位0覆盖 ryjgb
assert ok7, msg7
back_ids = [it.get('item_data', {}).get('id') for it in cd['backpack_items']]
assert 'ryjgb' in back_ids, back_ids  # 旧武器回背包
print('REPLACE_OK', msg7)

# 3. 保存 + guard 重算（与现有 write_save 一致）
tmpdir = tempfile.mkdtemp(prefix='equip_test_')
p = os.path.join(tmpdir, 'save_1.json')
with open(p, 'w', encoding='utf-8') as f:
    f.write(se.godot_json(data))
se.write_save(p, data)
back = se.load_save(p)
assert back['guard']['level'] == se.guard_of(back['character_data'].get('level', 1))
assert back['character_data']['role_equipment']['roles']['wu_kong']['slots'][0]['id'] == 'jcbj'
print('SAVE_GUARD_OK')
shutil.rmtree(tmpdir)

print('ALL_EQUIP_TESTS_PASSED')
