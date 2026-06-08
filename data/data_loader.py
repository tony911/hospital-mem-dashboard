"""
数据加载器
- 读取 hospital_bed_usage_data_2.xlsx
- 构建医院/科室/病房/床位数据模型
- 补充床位号的楼号和楼层
"""

import os
import random
from collections import defaultdict

import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
EXCEL_PATH = os.path.join(DATA_DIR, "hospital_bed_usage_data_2.xlsx")

BUILDING_LETTERS = ["A", "B", "C", "D", "E"]


def load_data() -> dict:
    """加载Excel数据，返回结构化字典"""
    if not os.path.exists(EXCEL_PATH):
        raise FileNotFoundError(f"Excel文件不存在: {EXCEL_PATH}")

    df_info = pd.read_excel(EXCEL_PATH, sheet_name="医院科室信息")
    df_beds = pd.read_excel(EXCEL_PATH, sheet_name="床位信息")

    # ── 构建医院结构 ──
    hospitals = {}
    wards_info = {}  # ward_id -> {hospital info}

    for _, row in df_info.iterrows():
        hid = row["医院id"]
        hname = row["医院名称"]
        did = row["科室id"]
        dname = row["科室名称"]
        wid = row["病房id"]
        wname = row["病房名称"]
        total_beds = int(row["病房总床数"])

        if hid not in hospitals:
            hospitals[hid] = {
                "id": hid,
                "name": hname,
                "region": row["地区"],
                "departments": {},
            }

        if did not in hospitals[hid]["departments"]:
            hospitals[hid]["departments"][did] = {
                "id": did,
                "name": dname,
                "wards": {},
            }

        hospitals[hid]["departments"][did]["wards"][wid] = {
            "id": wid,
            "name": wname,
            "total_beds": total_beds,
            "beds": [],
        }

        wards_info[wid] = {
            "hospital_id": hid,
            "department_id": did,
            "department_name": dname,
        }

    # ── 构建床位列表 ──
    for _, row in df_beds.iterrows():
        wid = row["病房id"]
        bed_no = int(row["床位号"])
        if wid in wards_info:
            winfo = wards_info[wid]
            hid = winfo["hospital_id"]
            did = winfo["department_id"]
            if hid in hospitals and did in hospitals[hid]["departments"]:
                if wid in hospitals[hid]["departments"][did]["wards"]:
                    hospitals[hid]["departments"][did]["wards"][wid]["beds"].append(bed_no)

    return {
        "hospitals": hospitals,
        "wards_info": wards_info,
        "raw_info": df_info,
        "raw_beds": df_beds,
    }


def _assign_building_floor(wards: list, hospital_floors: int) -> dict:
    """
    为同一医院的病房分配楼号和楼层
    同一病房的床位: 90%同一幢楼, 60%同一层
    返回: {ward_id: {"building": "A", "floor": 3, "beds": {"B-A-3-01": original_bed_no, ...}}}
    """
    result = {}

    for ward_id, ward_data in wards:
        building = random.choice(BUILDING_LETTERS)
        floor = random.randint(1, hospital_floors)
        n_beds = len(ward_data["beds"])

        ward_beds = {}
        for i, bed_no in enumerate(ward_data["beds"]):
            # 同病房床位: 90%相同building
            this_building = building if random.random() < 0.90 else random.choice(BUILDING_LETTERS)
            # 同病房床位: 60%相同floor
            this_floor = floor if random.random() < 0.60 else random.randint(1, hospital_floors)
            # 床位编号
            bed_label = f"{ward_id}-{this_building}-{this_floor}-{bed_no:02d}"
            ward_beds[bed_label] = bed_no

        result[ward_id] = {
            "building": building,
            "floor": floor,
            "beds": ward_beds,
        }

    return result


def enhance_with_building_floor(data: dict) -> dict:
    """为所有医院的床位补充楼号和楼层信息"""
    random.seed(42)  # 固定种子，确保数据生成和读取时标签一致
    hospitals = data["hospitals"]
    all_ward_mapping = {}  # ward_id -> {building, floor, beds{}}

    for hid, hosp in hospitals.items():
        # 获取该医院楼层数
        if hid == "H001":
            hfloors = 9
        elif hid == "H002":
            hfloors = 10
        elif hid == "H003":
            hfloors = 8
        elif hid == "H004":
            hfloors = 7
        elif hid == "H005":
            hfloors = 9
        elif hid == "H006":
            hfloors = 8
        else:
            hfloors = random.randint(5, 10)

        # 收集该医院所有病房
        wards_list = []
        for did, dept in hosp["departments"].items():
            for wid, ward in dept["wards"].items():
                wards_list.append((wid, ward))

        # 分配建筑和楼层
        mapping = _assign_building_floor(wards_list, hfloors)
        all_ward_mapping.update(mapping)

        # 更新ward数据
        for did, dept in hosp["departments"].items():
            for wid, ward in dept["wards"].items():
                if wid in mapping:
                    ward["building"] = mapping[wid]["building"]
                    ward["floor"] = mapping[wid]["floor"]
                    ward["bed_labels"] = mapping[wid]["beds"]

    data["ward_mapping"] = all_ward_mapping
    return data


def get_flat_bed_list(data: dict) -> list:
    """获取扁平化的床位列表，每行一条"""
    beds = []
    for hid, hosp in data["hospitals"].items():
        for did, dept in hosp["departments"].items():
            for wid, ward in dept["wards"].items():
                for bed_label, orig_no in ward.get("bed_labels", {}).items():
                    beds.append({
                        "地区": hosp["region"],
                        "医院id": hid,
                        "医院名称": hosp["name"],
                        "科室id": did,
                        "科室名称": dept["name"],
                        "病房id": wid,
                        "病房名称": ward["name"],
                        "床位标签": bed_label,
                        "原始编号": orig_no,
                        "楼号": bed_label.split("-")[1],
                        "楼层": int(bed_label.split("-")[2]),
                    })
    return beds


if __name__ == "__main__":
    random.seed(42)
    data = load_data()
    data = enhance_with_building_floor(data)
    beds = get_flat_bed_list(data)
    print(f"✅ 数据加载完成")
    print(f"   医院: {len(data['hospitals'])} 所")
    print(f"   床位总数: {len(beds)} 张")
    print(f"   示例床位:")
    for b in beds[:5]:
        print(f"     {b['医院名称']} - {b['科室名称']} - {b['病房名称']} - {b['床位标签']}")
