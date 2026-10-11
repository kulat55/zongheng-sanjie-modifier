extends SceneTree

# 验证：修改后的存档（含新增装备）能否被 Godot 正确解析，且 guard 与引擎重算一致
const GUARD_SALT := "zhxys-0.67-save-guard"

static func _canon_json(value) -> String:
	var txt := JSON.stringify(value)
	var parsed = JSON.parse_string(txt)
	if parsed == null:
		return txt
	return JSON.stringify(parsed)

static func _guard_of(value) -> String:
	return (_canon_json(value) + "|" + GUARD_SALT).md5_text()

func _init():
	var path := "res://modified_save.json"
	var f := FileAccess.open(path, FileAccess.READ)
	if f == null:
		print("FAIL_OPEN")
		quit(1)
		return
	var parsed = JSON.parse_string(f.get_as_text())
	if parsed == null:
		print("FAIL_JSON_PARSE")
		quit(1)
		return
	var save_data: Dictionary = parsed
	var cd: Dictionary = save_data.get("character_data", {})
	# 1) 背包装备
	var bag: Array = cd.get("backpack_items", [])
	print("BAG_SIZE=" + str(bag.size()))
	for it in bag:
		if it is Dictionary and it.get("category", "") == "equipment":
			var idata: Dictionary = it.get("item_data", {})
			print("BAG_EQ=" + str(idata.get("id", "?")) + " 名字=" + str(idata.get("名字", "?")))
	# 2) 已穿装备
	var re_data: Dictionary = cd.get("role_equipment", {})
	var roles: Dictionary = re_data.get("roles", {})
	var wk: Dictionary = roles.get("wu_kong", {})
	var slots: Array = wk.get("slots", [])
	print("SLOTS_COUNT=" + str(slots.size()))
	for i in range(slots.size()):
		var s = slots[i]
		if s is Dictionary and not s.is_empty():
			print("SLOT_" + str(i) + "=" + str(s.get("id", "?")) + " " + str(s.get("名字", "?")))
	# 3) category_states 同步
	var cs: Dictionary = save_data.get("category_states", {})
	var eq_pages: Array = cs.get("equipment", [])
	var eq_found := 0
	for pg in eq_pages:
		if pg is Dictionary:
			for k in pg.keys():
				var it: Dictionary = pg[k]
				if it.get("id", "") == "ryjgb" or it.get("id", "") == "shclg":
					eq_found += 1
	print("CATEGORY_EQ_FOUND=" + str(eq_found))
	# 4) guard 重算对比
	var stats: Dictionary = cd.get("player_stats", {})
	var g: Dictionary = save_data.get("guard", {})
	var calc := {
		"level": _guard_of(cd.get("level", 1)),
		"soul": _guard_of(cd.get("soul_count", 0)),
		"soul2p": _guard_of(cd.get("soul_count_2p", 0)),
		"stats": _guard_of(stats),
	}
	var all_ok := true
	for k in calc.keys():
		var is_match: bool = str(calc[k]) == str(g.get(k, ""))
		if not is_match:
			all_ok = false
		print("GUARD_" + k + "_MATCH=" + str(is_match))
	print("GUARD_ALL_MATCH=" + str(all_ok))
	if all_ok and eq_found >= 1:
		print("EQUIP_VERIFY_PASS")
		quit(0)
	else:
		print("EQUIP_VERIFY_FAIL")
		quit(1)
