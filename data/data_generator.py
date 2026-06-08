"""
模拟数据生成器
- 生成病人住院信息（入住/出院时间、床位分配）
- 生成病床维修记录
- 满足占用率 ≥75%，维修占比 ~5%
- 单医院单CSV
- 支持进度回调
"""

import csv
import os
import random
import time
from datetime import datetime, timedelta
from collections import defaultdict

import numpy as np

# ── 病人名称池 ──
CHINESE_SURNAMES = [
    "陈", "林", "黄", "张", "李", "王", "吴", "刘", "蔡", "杨",
    "许", "何", "郭", "马", "朱", "郑", "周", "徐", "胡", "梁",
    "宋", "唐", "韩", "曹", "邓", "冯", "萧", "程", "曾", "彭",
    "吕", "苏", "卢", "蒋", "蔡", "贾", "丁", "魏", "叶", "潘",
]

CHINESE_GIVEN = [
    "小明", "志强", "淑芬", "嘉欣", "伟杰", "秀英", "建国", "丽华",
    "子豪", "雅婷", "浩然", "美玲", "俊杰", "敏仪", "伟强", "婉君",
    "静雯", "家辉", "晓明", "瑞芳",
]

ENGLISH_FIRST = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer",
    "Michael", "Linda", "David", "Elizabeth", "William", "Susan",
    "Richard", "Jessica", "Joseph", "Sarah", "Thomas", "Karen",
    "Charles", "Lisa", "Chris", "Emma", "Stephen", "Olivia",
]

ENGLISH_LAST = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia",
    "Miller", "Davis", "Rodriguez", "Martinez", "Wilson", "Anderson",
    "Taylor", "Thomas", "Moore", "Jackson", "Lee", "Chan",
]

PATIENT_TYPES = ["普通住院", "急诊", "手术", "重症监护"]


def _round_to_half_hour(dt: datetime) -> datetime:
    """将时间舍入到最近的整点或半点 (:00 或 :30)"""
    if dt.minute < 15:
        return dt.replace(minute=0, second=0, microsecond=0)
    elif dt.minute < 45:
        return dt.replace(minute=30, second=0, microsecond=0)
    else:
        return dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def mask_name(name: str) -> str:
    """将名字中间部分替换为*，仅显示首尾"""
    if len(name) <= 2:
        # 2个字只显示首字+*
        return name[0] + "*"
    # 显示首尾，中间用*代替
    return name[0] + "*" * (len(name) - 2) + name[-1]


def generate_patient_name() -> str:
    """生成病人姓名（中文或英文），返回原姓名和脱敏姓名"""
    if random.random() < 0.7:  # 70% 中文名
        surname = random.choice(CHINESE_SURNAMES)
        given = random.choice(CHINESE_GIVEN)
        raw = surname + given
    else:  # 30% 英文名
        first = random.choice(ENGLISH_FIRST)
        last = random.choice(ENGLISH_LAST)
        raw = f"{first} {last}"
    masked = mask_name(raw)
    return raw, masked


def generate_hospital_data(
    hospital_id: str,
    hospital_name: str,
    hospital_floors: int,
    wards_list: list,
    start_date: datetime,
    end_date: datetime,
    progress_callback=None,
) -> dict:
    """
    为一个医院生成模拟数据
    wards_list: [(ward_id, ward_name, dept_id, dept_name, bed_labels_dict), ...]
        bed_labels_dict: {bed_label: original_bed_no}
    start_date, end_date: 数据生成时间段（含首尾）
    返回: {"patients": [...], "maintenance": [...], "stats": {...}}
    """
    total_minutes = int((end_date - start_date).total_seconds() / 60)

    random.seed(hash(hospital_id) % (2**31))
    np.random.seed(hash(hospital_id) % (2**31))

    # ── 扁平化所有床位 ──
    all_beds = []
    for wid, wname, did, dname, bed_labels in wards_list:
        for bed_label, orig_no in bed_labels.items():
            all_beds.append({
                "ward_id": wid,
                "ward_name": wname,
                "dept_id": did,
                "dept_name": dname,
                "bed_label": bed_label,
                "orig_no": orig_no,
            })
    total_beds = len(all_beds)
    total_hours = total_minutes // 60

    # ── 波动参数 ──
    hosp_base = random.uniform(0.72, 0.78)
    diurnal_amp = random.uniform(0.12, 0.18)     # 日内波幅(导致一天内波动)
    monthly_amp = random.uniform(0.10, 0.15)      # 月度波幅(导致不同天波动)
    noise_amp = random.uniform(0.03, 0.06)         # 随机噪声

    # ── 按小时生成目标占用率（全院总量驱动） ──
    # 先计算每小时的"意愿占用率"，然后直接用此分配床位
    # 这样全院汇总直接跟随波动曲线

    patients = []
    maintenance = []

    # 将床位打乱，均匀分配
    random.shuffle(all_beds)
    bed_labels_list = [b["bed_label"] for b in all_beds]

    total_hours_gen = int((end_date - start_date).total_seconds() / 3600)
    n_beds = total_beds

    # ═══ 预计算每小时的占用率 ═══
    hourly_rates = []
    for h in range(total_hours_gen):
        current_dt = start_date + timedelta(hours=h)
        hour_of_day = current_dt.hour + current_dt.minute / 60
        day_of_month = current_dt.day

        diurnal = diurnal_amp * np.sin(2 * np.pi * (hour_of_day - 9) / 24)
        monthly = monthly_amp * np.sin(2 * np.pi * (day_of_month - 1) / 15)
        noise = random.gauss(0, noise_amp)

        rate = hosp_base + diurnal + monthly + noise
        rate = min(0.90, max(0.50, rate))
        hourly_rates.append(rate)

    # ═══ 逐小时生成床位占用位图 ═══
    # dim: [hours][beds] — 0=空闲, 1=占用
    occ_map = [[0] * n_beds for _ in range(total_hours_gen)]

    # 各床当前占用结束小时（用于连续性）
    bed_occupied_until = [-1] * n_beds

    for h in range(total_hours_gen):
        target_occ = int(n_beds * hourly_rates[h])

        # 当前已占用床数
        current_occ = sum(1 for bi in range(n_beds) if bed_occupied_until[bi] >= h)

        if current_occ < target_occ:
            # 需要更多占用 → 找空闲床补充
            idle_beds = [bi for bi in range(n_beds) if bed_occupied_until[bi] < h]
            random.shuffle(idle_beds)
            need = target_occ - current_occ
            for bi in idle_beds[:need]:
                # 分配入住：入住时长正态分布 4h-48h
                dur_h = int(np.random.normal(24, 10))  # 均值24h，标准差10h
                dur_h = max(4, min(48, dur_h))
                # 确保不超出总范围
                end_h = min(total_hours_gen - 1, h + dur_h - 1)
                # 考虑维修打断（约5%概率缩短入住）
                if random.random() < 0.05:
                    end_h = min(end_h, h + random.randint(1, 12))
                for ho in range(h, end_h + 1):
                    occ_map[ho][bi] = 1
                bed_occupied_until[bi] = end_h

        elif current_occ > target_occ:
            # 占用过多 → 提前释放部分床位
            excess = current_occ - target_occ
            occupied_beds = [bi for bi in range(n_beds) if bed_occupied_until[bi] >= h]
            random.shuffle(occupied_beds)
            for bi in occupied_beds[:excess]:
                # 终止当前占用
                old_end = bed_occupied_until[bi]
                new_end = h
                # 清理后续位图
                for ho in range(new_end, old_end + 1):
                    if ho < total_hours_gen:
                        occ_map[ho][bi] = 0
                bed_occupied_until[bi] = new_end - 1

    if progress_callback:
        progress_callback(hospital_id, hospital_name, 100.0)

    # ═══ 从位图生成病人住院记录 ═══
    # 合并连续占用的时段
    for bi, bed_info in enumerate(all_beds):
        h = 0
        while h < total_hours_gen:
            if occ_map[h][bi] == 1:
                start_h = h
                while h < total_hours_gen and occ_map[h][bi] == 1:
                    h += 1
                end_h = h - 1
                # 只保留>=4小时的连续占用（太短的不生成记录）
                # 超过48h自动拆分为多条记录
                dur_hours = end_h - start_h + 1
                max_seg = 48
                seg_start = start_h
                while seg_start <= end_h:
                    seg_end = min(seg_start + max_seg - 1, end_h)
                    seg_len = seg_end - seg_start + 1
                    if seg_len < 4:
                        seg_start = seg_end + 1
                        continue

                    adm_dt = start_date + timedelta(hours=seg_start)
                    dis_dt = start_date + timedelta(hours=seg_end + 1)
                    adm_dt = _round_to_half_hour(adm_dt)
                    dis_dt = _round_to_half_hour(dis_dt)

                    raw_name, masked_name = generate_patient_name()
                    patient_type = random.choice(PATIENT_TYPES)

                    patients.append({
                        "医院名称": hospital_name,
                        "科室名称": bed_info["dept_name"],
                        "病房名称": bed_info["ward_name"],
                        "床位号": bed_info["bed_label"],
                        "病人姓名": masked_name,
                        "病人类型": patient_type,
                        "入院时间": adm_dt.strftime("%Y-%m-%d %H:%M"),
                        "出院时间": dis_dt.strftime("%Y-%m-%d %H:%M"),
                    })
                    seg_start = seg_end + 1
            else:
                h += 1

    # ═══ 生成维修记录 ═══
    # 每张床每3天约一次维修机会，使维修占比接近5%
    maint_target_capacity = 0.05 * total_hours_gen * n_beds  # 目标维修床小时数
    maint_hours_generated = 0
    maint_attempts = 0

    for bi in range(n_beds):
        if maint_hours_generated >= maint_target_capacity:
            break
        # 扫一遍空闲时段
        idle_segments = []
        seg_start = -1
        for h in range(total_hours_gen):
            if occ_map[h][bi] == 0:
                if seg_start == -1:
                    seg_start = h
            else:
                if seg_start != -1:
                    if h - seg_start >= 2:
                        idle_segments.append((seg_start, h - 1))
                    seg_start = -1
        if seg_start != -1 and total_hours_gen - seg_start >= 2:
            idle_segments.append((seg_start, total_hours_gen - 1))

        for seg in idle_segments:
            if random.random() > 0.30:  # 每个空闲段30%概率维修
                continue
            max_maint = min(48 * 60, (seg[1] - seg[0] + 1) * 60)  # 分钟
            if max_maint < 60:
                continue
            dur_min = random.randint(60, int(max_maint))
            dur_h = dur_min / 60

            if maint_hours_generated + dur_h > maint_target_capacity * 1.2:
                continue

            seg_len_h = seg[1] - seg[0] + 1
            max_start_offset = max(0, seg_len_h - int(np.ceil(dur_h)))
            start_offset_h = random.randint(0, int(max_start_offset)) if max_start_offset > 0 else 0
            maint_start_h = seg[0] + start_offset_h
            maint_end_h = min(total_hours_gen - 1, int(maint_start_h + np.ceil(dur_h)))

            ms = _round_to_half_hour(start_date + timedelta(hours=maint_start_h))
            me = _round_to_half_hour(start_date + timedelta(hours=maint_end_h))

            maintenance.append({
                "医院名称": hospital_name,
                "科室名称": all_beds[bi]["dept_name"],
                "病房名称": all_beds[bi]["ward_name"],
                "床位号": all_beds[bi]["bed_label"],
                "维修开始时间": ms.strftime("%Y-%m-%d %H:%M"),
                "维修结束时间": me.strftime("%Y-%m-%d %H:%M"),
            })
            maint_hours_generated += (me - ms).total_seconds() / 3600
            maint_attempts += 1

    # ── 统计 ──
    occupied_hours = sum(sum(row) for row in occ_map)
    total_bed_hours = total_hours_gen * n_beds
    actual_occ_rate = occupied_hours / total_bed_hours * 100 if total_bed_hours > 0 else 0
    # 维修占用时间大致估算
    total_maint_minutes = sum(
        (datetime.strptime(m["维修结束时间"], "%Y-%m-%d %H:%M") -
         datetime.strptime(m["维修开始时间"], "%Y-%m-%d %H:%M")).total_seconds() / 60
        for m in maintenance
    )
    actual_maint_rate = total_maint_minutes / (total_minutes * n_beds) * 100 if total_minutes > 0 else 0

    stats = {
        "医院": hospital_name,
        "总床位": total_beds,
        "总容量(床分钟)": total_bed_hours * 60,
        "总占用(床分钟)": occupied_hours * 60,
        "占用率": round(actual_occ_rate, 1),
        "维修占比": round(actual_maint_rate, 1),
        "病人记录数": len(patients),
        "维修记录数": len(maintenance),
    }

    return {"patients": patients, "maintenance": maintenance, "stats": stats}


def save_hospital_csv(
    hospital_id: str,
    hospital_name: str,
    data: dict,
    output_dir: str,
):
    """为一个医院保存CSV"""
    os.makedirs(output_dir, exist_ok=True)

    # 病人信息CSV
    pat_file = os.path.join(output_dir, f"{hospital_id}_{hospital_name}_病人信息.csv")
    if data["patients"]:
        with open(pat_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "医院名称", "科室名称", "病房名称", "床位号",
                "病人姓名", "病人类型", "入院时间", "出院时间",
            ])
            writer.writeheader()
            writer.writerows(data["patients"])

    # 维修记录CSV
    maint_file = os.path.join(output_dir, f"{hospital_id}_{hospital_name}_维修记录.csv")
    if data["maintenance"]:
        with open(maint_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "医院名称", "科室名称", "病房名称", "床位号",
                "维修开始时间", "维修结束时间",
            ])
            writer.writeheader()
            writer.writerows(data["maintenance"])

    return pat_file, maint_file


def generate_all_hospitals(
    data_loader_data: dict,
    start_date: datetime,
    end_date: datetime,
    output_dir: str,
    progress_callback=None,
) -> list:
    """生成所有医院的模拟数据"""
    hospitals = data_loader_data["hospitals"]
    results = []

    for hid, hosp in hospitals.items():
        # 构建ward列表
        wards_list = []
        for did, dept in hosp["departments"].items():
            for wid, ward in dept["wards"].items():
                if "bed_labels" in ward:
                    wards_list.append((
                        wid, ward["name"], did, dept["name"], ward["bed_labels"]
                    ))

        # 获取楼层数
        hfloors = {
            "H001": 9, "H002": 10, "H003": 8,
            "H004": 7, "H005": 9, "H006": 8,
        }.get(hid, 8)

        gen_data = generate_hospital_data(
            hid, hosp["name"], hfloors, wards_list,
            start_date, end_date,
            progress_callback=progress_callback,
        )

        save_hospital_csv(hid, hosp["name"], gen_data, output_dir)

        results.append({
            "hospital_id": hid,
            "hospital_name": hosp["name"],
            "region": hosp["region"],
            "stats": gen_data["stats"],
            "patients_count": len(gen_data["patients"]),
            "maintenance_count": len(gen_data["maintenance"]),
        })

    return results


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__))))
    from data.data_loader import load_data, enhance_with_building_floor

    random.seed(42)
    np.random.seed(42)

    data = load_data()
    data = enhance_with_building_floor(data)

    start = datetime(2026, 1, 1)
    end = datetime(2026, 1, 31, 23, 59)
    out_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "generated_data")

    def cb(hid, name, pct):
        print(f"\r  [{hid}] {name}: {pct:.0f}%", end="")

    results = generate_all_hospitals(data, start, end, out_dir, progress_callback=cb)
    print()
    for r in results:
        s = r["stats"]
        print(f"  {r['hospital_name']}: 占用率{s['占用率']}%, "
              f"维修{s['维修占比']}%, "
              f"病人{s['病人记录数']}条, 维修{s['维修记录数']}条")
