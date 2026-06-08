"""
Flask Web 应用 — 性能优化版
核心改进：
1. CSV 文件一次性加载进内存缓存，不再重复读磁盘
2. 建立"小时索引矩阵"——一次遍历建好所有24小时的床位占用状态
3. 预计算4天对比数据，后续接口都从内存读取
4. 所有大屏接口 O(1) 响应
"""

import json
import os
import sys
import threading
from datetime import datetime, timedelta
from collections import defaultdict

import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__))))
from data.data_loader import load_data, enhance_with_building_floor, get_flat_bed_list
from data.data_generator import generate_all_hospitals

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
GENERATED_DIR = os.path.join(ROOT_DIR, "generated_data")

app = Flask(
    __name__,
    template_folder=os.path.join(ROOT_DIR, "templates"),
    static_folder=os.path.join(ROOT_DIR, "static"),
)
app.secret_key = "hospital-mem-dashboard-v2-secret-key"

# ── 全局状态 ──
data_store = None
generation_progress = {}
generation_running = False

# ════════════════════════════════════════════════════════
# 缓存层：CSV 内存缓存 + 小时索引矩阵
# ════════════════════════════════════════════════════════
csv_cache = {}          # {hospital_id: {"patients": DataFrame, "maintenance": DataFrame}}
hourly_matrix_cache = {}  # {hospital_id: {date_str: [24-hours]}}
daily_comp_cache = {}     # {hospital_id: {date_str: [4-days-comparison]}}
dept_summary_cache = None  # {date_str: {"hours": [...]}}
hospital_geo_cache = None  # {date_str: [...]}
cache_built = False
cache_lock = threading.Lock()

screen_config = {
    "screens": [
        {"id": 1, "title": "各科室病床使用率", "duration": 10, "enabled": True},
        {"id": 2, "title": "医院床位占用率", "duration": 10, "enabled": True},
        {"id": 3, "title": "各科室空闲病床数", "duration": 10, "enabled": True},
        {"id": 4, "title": "病床分布图", "duration": 10, "enabled": True},
        {"id": 5, "title": "科室占用率对称双轴", "duration": 10, "enabled": True},
        {"id": 6, "title": "香港医院资源总览", "duration": 10, "enabled": True},
    ],
    "simulated_date": "2026-01-15",
}


def _init_data():
    global data_store
    if data_store is None:
        data = load_data()
        data = enhance_with_building_floor(data)
        data_store = data
    return data_store


# ── CSV 缓存加载 ──

def _load_csv_cached(hospital_id: str):
    """缓存加载 CSV，避免重复读磁盘"""
    if hospital_id in csv_cache:
        return csv_cache[hospital_id]

    pat_df = pd.DataFrame()
    maint_df = pd.DataFrame()
    for f in os.listdir(GENERATED_DIR):
        if f.startswith(hospital_id) and "病人" in f:
            pat_df = pd.read_csv(os.path.join(GENERATED_DIR, f), encoding="utf-8-sig")
    for f in os.listdir(GENERATED_DIR):
        if f.startswith(hospital_id) and "维修" in f:
            maint_df = pd.read_csv(os.path.join(GENERATED_DIR, f), encoding="utf-8-sig")

    csv_cache[hospital_id] = {"patients": pat_df, "maintenance": maint_df}
    return csv_cache[hospital_id]


def _clear_cache():
    """清空缓存（重新生成后调用）"""
    global cache_built
    csv_cache.clear()
    hourly_matrix_cache.clear()
    daily_comp_cache.clear()
    dept_summary_cache = None
    hospital_geo_cache = None
    cache_built = False


# ── 核心：一次性构建小时索引矩阵 ──

def _build_hourly_matrix(hospital_id: str, date_str: str) -> list:
    """
    一次遍历构建某医院某天的24小时快照矩阵
    返回: [{"hour": 0, "total_beds": N, "occupied": N, "idle": N, "maintenance": N,
             "occupancy_rate": N, "depts": [...]}, ... 24个]
    """
    data = _init_data()
    hosp = data["hospitals"].get(hospital_id)
    if not hosp:
        return []

    cached = _load_csv_cached(hospital_id)
    pat_df = cached["patients"]
    maint_df = cached["maintenance"]

    sim_date = datetime.strptime(date_str, "%Y-%m-%d")

    # ── 构建床位掩码 per hour ──
    # 24 hours × all_beds 的占用状态
    all_beds = []
    bed_to_dept = {}  # bed_label -> dept details
    for did, dept in hosp["departments"].items():
        for wid, ward in dept["wards"].items():
            for bed_label in ward.get("bed_labels", {}):
                all_beds.append(bed_label)
                # 因为是引用，用简单的字符串映射
                bed_to_dept[bed_label] = {
                    "dept_id": did,
                    "dept_name": dept["name"],
                    "ward_name": ward["name"],
                    "building": bed_label.split("-")[1],
                    "floor": int(bed_label.split("-")[2]),
                }

    if not all_beds:
        return []

    # 床位索引
    bed_index = {bl: i for i, bl in enumerate(all_beds)}
    n_beds = len(all_beds)

    # 初始化24小时的占用位图 (0=空闲, 1=病人占用, 2=维修占用)
    occ_map = [[0] * n_beds for _ in range(24)]

    # ── 一次遍历病人记录，填充小时位图 ──
    if not pat_df.empty:
        for _, r in pat_df.iterrows():
            try:
                adm = datetime.strptime(r["入院时间"], "%Y-%m-%d %H:%M")
                dis = datetime.strptime(r["出院时间"], "%Y-%m-%d %H:%M")
                # 只关心目标日期内的记录
                start_h = max(0, (adm - sim_date).days * 24 + adm.hour)
                end_h = min(23, (dis - sim_date).days * 24 + dis.hour)
                if end_h < 0 or start_h > 23:
                    continue
                start_h = max(0, start_h)
                end_h = min(23, end_h)
                bl = r["床位号"]
                if bl in bed_index:
                    bi = bed_index[bl]
                    for h in range(int(start_h), int(end_h) + 1):
                        occ_map[h][bi] = 1
            except (ValueError, KeyError):
                pass

    # ── 一次遍历维修记录 ──
    if not maint_df.empty:
        for _, r in maint_df.iterrows():
            try:
                ms = datetime.strptime(r["维修开始时间"], "%Y-%m-%d %H:%M")
                me = datetime.strptime(r["维修结束时间"], "%Y-%m-%d %H:%M")
                start_h = max(0, (ms - sim_date).days * 24 + ms.hour)
                end_h = min(23, (me - sim_date).days * 24 + me.hour)
                if end_h < 0 or start_h > 23:
                    continue
                start_h = max(0, start_h)
                end_h = min(23, end_h)
                bl = r["床位号"]
                if bl in bed_index:
                    bi = bed_index[bl]
                    for h in range(int(start_h), int(end_h) + 1):
                        # 如果已经有病人占用，保留病人占用的标记；否则标记为维修
                        if occ_map[h][bi] == 0:
                            occ_map[h][bi] = 2
            except (ValueError, KeyError):
                pass

    # ── 用位图生成24小时快照 ──
    dept_list = list(hosp["departments"].items())
    hours_result = []
    for h in range(24):
        d_stats = {}
        for did, dept in dept_list:
            d_stats[did] = {"total": 0, "occupied": 0, "maintenance": 0, "idle": 0}

        total = 0
        occ = 0
        maint = 0
        for bi, bl in enumerate(all_beds):
            state = occ_map[h][bi]
            total += 1
            if state == 1:
                occ += 1
            elif state == 2:
                maint += 1
            # 更新科室统计
            dd = bed_to_dept[bl]
            did = dd["dept_id"]
            if did in d_stats:
                d_stats[did]["total"] += 1
                if state == 1:
                    d_stats[did]["occupied"] += 1
                elif state == 2:
                    d_stats[did]["maintenance"] += 1

        idle = total - occ - maint
        rate = round(occ / total * 100, 1) if total > 0 else 0

        dept_snaps = []
        for did, dept in dept_list:
            ds = d_stats.get(did, {})
            d_idle = ds["total"] - ds["occupied"] - ds["maintenance"]
            d_rate = round(ds["occupied"] / ds["total"] * 100, 1) if ds["total"] > 0 else 0
            dept_snaps.append({
                "name": dept["name"],
                "total": ds["total"],
                "occupied": ds["occupied"],
                "idle": d_idle,
                "rate": d_rate,
            })

        hours_result.append({
            "hour": h,
            "total_beds": total,
            "occupied": occ,
            "idle": idle,
            "maintenance": maint,
            "occupancy_rate": rate,
            "depts": dept_snaps,
        })

    return hours_result


def _build_all_cache():
    """
    构建所有医院的缓存矩阵（在首次API请求或数据重新生成后调用）
    构建所有已生成数据的医院的 24h 矩阵 + 4天对比
    """
    global cache_built
    if cache_built:
        return

    with cache_lock:
        if cache_built:
            return

        data = _init_data()
        start_date_str = screen_config.get("simulated_date", "2026-01-01")
        try:
            base_date = datetime.strptime(start_date_str, "%Y-%m-%d")
        except ValueError:
            base_date = datetime.now()

        date_strs = [(base_date - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4)]

        for hid in data["hospitals"]:
            # 检查是否有CSV生成
            csv_paths = [f for f in os.listdir(GENERATED_DIR) if f.startswith(hid) and "病人" in f]
            if not csv_paths:
                continue

            if hid not in hourly_matrix_cache:
                hourly_matrix_cache[hid] = {}

            # 一次性构建所有需要的日期的矩阵
            for ds in date_strs:
                try:
                    hours = _build_hourly_matrix(hid, ds)
                    if hours:
                        hourly_matrix_cache[hid][ds] = hours
                except Exception:
                    pass

        # 从小时矩阵推算出每日对比数据
        _build_daily_comparison_from_cache(date_strs)

        # 预缓存屏5/屏6数据
        _build_dept_summary_cache(date_strs)
        _build_hospital_geo_cache(date_strs)

        cache_built = True


def _build_daily_comparison_from_cache(date_strs):
    """从已经构建好的小时矩阵推算每日对比数据"""
    for hid, date_data in hourly_matrix_cache.items():
        for ds in date_strs:
            hours = date_data.get(ds, [])
            if not hours:
                continue
            dept_avgs = {}
            for h in hours:
                for d in h["depts"]:
                    nm = d["name"]
                    if nm not in dept_avgs:
                        dept_avgs[nm] = {"count": 0, "occupied": 0, "total": 0}
                    dept_avgs[nm]["count"] += 1
                    dept_avgs[nm]["occupied"] += d["occupied"]
                    dept_avgs[nm]["total"] = d["total"]

            avg_occ = round(sum(h["occupied"] for h in hours) / len(hours))
            avg_rate = round(sum(h["occupancy_rate"] for h in hours) / len(hours), 1)
            avg_idle = round(sum(h["idle"] for h in hours) / len(hours))

            dept_list = [
                {"name": nm, "occupied": round(v["occupied"] / v["count"]),
                 "total": v["total"],
                 "rate": round(v["occupied"] / v["count"] / v["total"] * 100, 1) if v["total"] > 0 else 0}
                for nm, v in dept_avgs.items()
            ]

            if hid not in daily_comp_cache:
                daily_comp_cache[hid] = {}
            daily_comp_cache[hid][ds] = {
                "avg_occupied": avg_occ,
                "avg_idle": avg_idle,
                "avg_rate": avg_rate,
                "depts": dept_list,
            }


def _build_dept_summary_cache(date_strs):
    """预缓存跨医院科室汇总的逐小时数据（屏5）"""
    global dept_summary_cache
    dept_summary_cache = {}
    for ds in date_strs:
        # 收集所有医院逐小时数据
        hours_by_dept = {}
        total_free_by_hour = []
        for hid in hourly_matrix_cache:
            hours = hourly_matrix_cache[hid].get(ds, [])
            if not hours:
                continue
            for h, hr in enumerate(hours):
                if h >= 24:
                    break
                if h not in hours_by_dept:
                    hours_by_dept[h] = {}
                if len(total_free_by_hour) <= h:
                    total_free_by_hour.append(0)
                total_free_by_hour[h] += hr["idle"]
                for d in hr["depts"]:
                    nm = d["name"]
                    if nm not in hours_by_dept[h]:
                        hours_by_dept[h][nm] = {"total": 0, "occupied": 0}
                    hours_by_dept[h][nm]["total"] += d["total"]
                    hours_by_dept[h][nm]["occupied"] += d["occupied"]

        result_hours = []
        for h in range(24):
            depts = hours_by_dept.get(h, {})
            dept_list = []
            for nm, info in depts.items():
                rate = round(info["occupied"] / info["total"] * 100, 1) if info["total"] > 0 else 0
                dept_list.append({
                    "name": nm, "total": info["total"],
                    "occupied": info["occupied"],
                    "idle": info["total"] - info["occupied"],
                    "rate": rate,
                })
            result_hours.append({
                "hour": h,
                "total_free": total_free_by_hour[h] if h < len(total_free_by_hour) else 0,
                "depts": dept_list,
            })
        dept_summary_cache[ds] = {"hours": result_hours}


def _build_hospital_geo_cache(date_strs):
    """预缓存医院地理位置与占用数据（屏6）"""
    global hospital_geo_cache
    hospital_geo_cache = {}
    hosps_template = [
        {"id": "H001", "name": "玛丽医院", "lng": 114.125, "lat": 22.255, "region": "香港岛"},
        {"id": "H002", "name": "东区尤德夫人医院", "lng": 114.245, "lat": 22.265, "region": "香港岛"},
        {"id": "H003", "name": "伊利沙伯医院", "lng": 114.190, "lat": 22.305, "region": "九龙"},
        {"id": "H004", "name": "广华医院", "lng": 114.150, "lat": 22.320, "region": "九龙"},
        {"id": "H005", "name": "威尔斯亲王医院", "lng": 114.215, "lat": 22.395, "region": "新界"},
        {"id": "H006", "name": "屯门医院", "lng": 113.975, "lat": 22.425, "region": "新界"},
    ]
    for ds in date_strs:
        result = []
        for h in hosps_template:
            hours = hourly_matrix_cache.get(h["id"], {}).get(ds, [])
            if hours:
                hr = hours[12]
                result.append({**h, "beds": hr["total_beds"], "occupied": hr["occupied"],
                               "idle": hr["idle"], "rate": hr["occupancy_rate"]})
            else:
                result.append({**h, "beds": 0, "occupied": 0, "idle": 0, "rate": 0})
        hospital_geo_cache[ds] = result


# ── 缓存构建包装器 ──

def _ensure_cache():
    """确保缓存已构建"""
    if not cache_built:
        try:
            _build_all_cache()
        except Exception:
            pass


# ── 路由 ──

@app.route("/")
def index():
    return render_template("admin.html", title="🏥 香港医院病床管理后台", config=screen_config)


import json as json_lib

@app.route("/preview")
def preview():
    order = request.args.get("order", "")
    custom_order = []
    if order:
        try:
            custom_order = [int(x.strip()) for x in order.split(",") if x.strip()]
        except Exception:
            pass
    return render_template("big_screen.html", title="🏥 香港医院病床实时监控大屏",
                           config=screen_config, custom_order=custom_order)


@app.route("/preview/<int:screen_id>")
def preview_screen(screen_id):
    if screen_id < 1 or screen_id > 6:
        screen_id = 1
    return render_template("big_screen.html",
                           title=f"🏥 大屏 {screen_id} - 香港医院病床实时监控",
                           config=screen_config, single_screen=screen_id)


# ── API ──

@app.route("/api/init")
def api_init():
    try:
        _init_data()
        beds = get_flat_bed_list(data_store)
        _ensure_cache()
        return jsonify({"success": True, "hospitals": len(data_store["hospitals"]), "beds": len(beds)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/hospitals")
def api_hospitals():
    data = _init_data()
    return jsonify([{
        "id": hid, "name": hosp["name"], "region": hosp["region"],
        "departments": len(hosp["departments"]),
        "wards": sum(len(d["wards"]) for d in hosp["departments"].values()),
        "beds": sum(len(w.get("bed_labels", {})) for d in hosp["departments"].values() for w in d["wards"].values()),
    } for hid, hosp in data["hospitals"].items()])


@app.route("/api/dept_list")
def api_dept_list():
    hid = request.args.get("hospital", "")
    data = _init_data()
    if hid and hid in data["hospitals"]:
        return jsonify([{"id": did, "name": d["name"]} for did, d in data["hospitals"][hid]["departments"].items()])
    depts, seen = [], set()
    for hosp in data["hospitals"].values():
        for did, d in hosp["departments"].items():
            if d["name"] not in seen:
                seen.add(d["name"])
                depts.append({"id": did, "name": d["name"]})
    return jsonify(depts)


# ── API: 数据生成 ──

@app.route("/api/generate", methods=["POST"])
def api_generate():
    global generation_running, generation_progress
    if generation_running:
        return jsonify({"success": False, "error": "正在生成中，请等待完成"})

    start_str = request.json.get("start_date", "2026-01-01")
    end_str = request.json.get("end_date", "2026-01-31")
    try:
        start_date = datetime.strptime(start_str, "%Y-%m-%d")
        end_date = datetime.strptime(end_str, "%Y-%m-%d").replace(hour=23, minute=59)
    except ValueError as e:
        return jsonify({"success": False, "error": f"日期格式错误: {e}"})

    generation_running = True
    generation_progress = {}

    def _progress_cb(hid, hname, pct):
        generation_progress[hid] = {"hospital_name": hname, "pct": round(pct, 1), "status": "running"}

    def _run():
        global generation_running, data_store
        try:
            # 1. 清除旧CSV文件
            for f in os.listdir(GENERATED_DIR):
                if f.endswith(".csv"):
                    os.remove(os.path.join(GENERATED_DIR, f))
            _clear_cache()

            data = _init_data()
            results = generate_all_hospitals(data, start_date, end_date, GENERATED_DIR, progress_callback=_progress_cb)
            for r in results:
                hid = r["hospital_id"]
                if hid in generation_progress:
                    generation_progress[hid]["status"] = "completed"
                    generation_progress[hid]["stats"] = r["stats"]
            screen_config["simulated_date"] = start_str
            # 生成完成后重建缓存
            _clear_cache()
            _ensure_cache()
        except Exception as e:
            for hid in generation_progress:
                generation_progress[hid]["status"] = "error"
                generation_progress[hid]["error"] = str(e)
        finally:
            generation_running = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"success": True, "message": "生成已启动"})


@app.route("/api/generate_progress")
def api_generate_progress():
    return jsonify({"running": generation_running, "progress": generation_progress})


# ── API: 已生成数据 ──

@app.route("/api/generated_files")
def api_generated_files():
    files = []
    for f in sorted(os.listdir(GENERATED_DIR)):
        if f.endswith(".csv"):
            fp = os.path.join(GENERATED_DIR, f)
            files.append({
                "filename": f, "size": os.path.getsize(fp),
                "size_str": _format_size(os.path.getsize(fp)),
                "modified": datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M"),
            })
    return jsonify(files)


def _format_size(size):
    for unit in ["B", "KB", "MB"]:
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


@app.route("/api/download/<path:filename>")
def api_download(filename):
    fp = os.path.join(GENERATED_DIR, filename)
    if not os.path.exists(fp):
        return jsonify({"error": "文件不存在"}), 404
    return send_file(fp, mimetype="text/csv", as_attachment=True, download_name=filename)


@app.route("/api/download_all")
def api_download_all():
    import zipfile
    from io import BytesIO
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(os.listdir(GENERATED_DIR)):
            if f.endswith(".csv"):
                zf.write(os.path.join(GENERATED_DIR, f), arcname=f)
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"医院数据_{datetime.now().strftime('%Y%m%d%H%M')}.zip")


@app.route("/api/query_patients")
def api_query_patients():
    hid = request.args.get("hospital", "")
    dept = request.args.get("dept", "")
    keyword = request.args.get("keyword", "")
    if not hid:
        return jsonify({"error": "请指定医院"}), 400
    cached = _load_csv_cached(hid)
    df = cached["patients"]
    if df.empty:
        return jsonify({"error": "暂无可查询的数据", "data": []})
    if dept:
        df = df[df["科室名称"] == dept]
    if keyword:
        df = df[df["病人姓名"].str.contains(keyword, na=False)]
    return jsonify({"total": len(df), "data": df.head(500).to_dict(orient="records")})


@app.route("/api/query_maintenance")
def api_query_maintenance():
    hid = request.args.get("hospital", "")
    if not hid:
        return jsonify({"error": "请指定医院"}), 400
    cached = _load_csv_cached(hid)
    df = cached["maintenance"]
    if df.empty:
        return jsonify({"error": "暂无可查询的数据", "data": []})
    return jsonify({"total": len(df), "data": df.head(500).to_dict(orient="records")})


# ════════════════════════════════════════════════════════
# 大屏 API — 全部从缓存读取，O(1) 响应
# ════════════════════════════════════════════════════════

def _get_hourly(hid: str, date_str: str) -> list:
    """从缓存中获取小时矩阵，未命中时按需构建"""
    _ensure_cache()
    # 按需构建未缓存的日期
    if hid in hourly_matrix_cache and date_str not in hourly_matrix_cache[hid]:
        try:
            hours = _build_hourly_matrix(hid, date_str)
            if hours:
                hourly_matrix_cache.setdefault(hid, {})[date_str] = hours
                # 顺便构建这个日期对应的每日对比
                data = _init_data()
                if hid in data["hospitals"]:
                    dept_avgs = {}
                    for h in hours:
                        for d in h["depts"]:
                            nm = d["name"]
                            if nm not in dept_avgs:
                                dept_avgs[nm] = {"count": 0, "occupied": 0, "total": 0}
                            dept_avgs[nm]["count"] += 1
                            dept_avgs[nm]["occupied"] += d["occupied"]
                            dept_avgs[nm]["total"] = d["total"]
                    avg_occ = round(sum(h["occupied"] for h in hours) / len(hours))
                    avg_rate = round(sum(h["occupancy_rate"] for h in hours) / len(hours), 1)
                    avg_idle = round(sum(h["idle"] for h in hours) / len(hours))
                    depts = [{"name": nm, "occupied": round(v["occupied"] / v["count"]), "total": v["total"],
                              "rate": round(v["occupied"] / v["count"] / v["total"] * 100, 1) if v["total"] > 0 else 0}
                             for nm, v in dept_avgs.items()]
                    daily_comp_cache.setdefault(hid, {})[date_str] = {
                        "avg_occupied": avg_occ, "avg_idle": avg_idle,
                        "avg_rate": avg_rate, "depts": depts,
                    }
        except Exception:
            pass
    return hourly_matrix_cache.get(hid, {}).get(date_str, [])


def _get_daily_comp(hid: str, date_str: str, days: int = 3) -> list:
    """从缓存中获取每日对比数据"""
    _ensure_cache()
    base = datetime.strptime(date_str, "%Y-%m-%d")
    result = []
    for offset in range(days + 1):
        ds = (base - timedelta(days=offset)).strftime("%Y-%m-%d")
        label = "今天" if offset == 0 else f"{offset}天前"
        comp = daily_comp_cache.get(hid, {}).get(ds, None)
        if comp:
            result.append({"date": ds, "label": label, **comp})
        else:
            hours = _get_hourly(hid, ds)
            if hours:
                avg_occ = round(sum(h["occupied"] for h in hours) / len(hours))
                avg_rate = round(sum(h["occupancy_rate"] for h in hours) / len(hours), 1)
                avg_idle = round(sum(h["idle"] for h in hours) / len(hours))
                dept_avgs = {}
                for h in hours:
                    for d in h["depts"]:
                        nm = d["name"]
                        if nm not in dept_avgs: dept_avgs[nm] = {"count": 0, "occupied": 0, "total": 0}
                        dept_avgs[nm]["count"] += 1
                        dept_avgs[nm]["occupied"] += d["occupied"]
                        dept_avgs[nm]["total"] = d["total"]
                depts = [{"name": nm, "occupied": round(v["occupied"] / v["count"]), "total": v["total"],
                          "rate": round(v["occupied"] / v["count"] / v["total"] * 100, 1) if v["total"] > 0 else 0}
                         for nm, v in dept_avgs.items()]
                result.append({"date": ds, "label": label, "avg_occupied": avg_occ, "avg_idle": avg_idle,
                               "avg_rate": avg_rate, "depts": depts})
    return result


@app.route("/api/hourly_series")
def api_hourly_series():
    hid = request.args.get("hospital", "")
    date_str = request.args.get("date", screen_config["simulated_date"])
    if not hid:
        return jsonify({"error": "请指定医院"}), 400
    data = _init_data()
    if hid not in data["hospitals"]:
        return jsonify({"error": "医院不存在"}), 404
    hours = _get_hourly(hid, date_str)
    return jsonify({hid: {"hospital_name": data["hospitals"][hid]["name"],
                          "region": data["hospitals"][hid]["region"], "hours": hours}})


@app.route("/api/daily_comparison")
def api_daily_comparison():
    hid = request.args.get("hospital", "")
    date_str = request.args.get("date", screen_config["simulated_date"])
    days = int(request.args.get("days", 3))
    if not hid:
        return jsonify({"error": "请指定医院"}), 400
    return jsonify(_get_daily_comp(hid, date_str, days))


@app.route("/api/all_hospitals_occupancy_hourly")
def api_all_hospitals_occupancy_hourly():
    date_str = request.args.get("date", screen_config["simulated_date"])
    data = _init_data()
    result = {}
    for hid in data["hospitals"]:
        hours = _get_hourly(hid, date_str)
        if hours:
            result[hid] = {"hospital_name": data["hospitals"][hid]["name"],
                           "region": data["hospitals"][hid]["region"], "hours": hours}
    return jsonify(result)


@app.route("/api/bed_map_hourly")
def api_bed_map_hourly():
    hid = request.args.get("hospital", "")
    date_str = request.args.get("date", screen_config["simulated_date"])
    hour = int(request.args.get("hour", 12))

    if not hid:
        return jsonify({"error": "请指定医院"}), 400
    data = _init_data()
    if hid not in data["hospitals"]:
        return jsonify({"error": "医院不存在"}), 404

    hours = _get_hourly(hid, date_str)
    if not hours or hour >= len(hours):
        hour = 0

    hr = hours[hour]
    hosp = data["hospitals"][hid]

    # 对于地图需要详细床位数据，直接从CSV构建
    cached = _load_csv_cached(hid)
    pat_df = cached["patients"]
    maint_df = cached["maintenance"]
    sim_date = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=hour, minute=0, second=0)

    # 构建床位占用位图
    occupied_beds = set()
    maint_beds = set()
    if not pat_df.empty:
        for _, r in pat_df.iterrows():
            try:
                adm = datetime.strptime(r["入院时间"], "%Y-%m-%d %H:%M")
                dis = datetime.strptime(r["出院时间"], "%Y-%m-%d %H:%M")
                if adm <= sim_date <= dis:
                    occupied_beds.add(r["床位号"])
            except (ValueError, KeyError):
                pass
    if not maint_df.empty:
        for _, r in maint_df.iterrows():
            try:
                ms = datetime.strptime(r["维修开始时间"], "%Y-%m-%d %H:%M")
                me = datetime.strptime(r["维修结束时间"], "%Y-%m-%d %H:%M")
                if ms <= sim_date <= me:
                    maint_beds.add(r["床位号"])
            except (ValueError, KeyError):
                pass

    wards_data = []
    for did, dept in hosp["departments"].items():
        for wid, ward in dept["wards"].items():
            beds_info = []
            for bed_label, orig_no in ward.get("bed_labels", {}).items():
                bl = bed_label
                status = "idle"
                if bl in occupied_beds:
                    status = "occupied"
                elif bl in maint_beds:
                    status = "maintenance"
                beds_info.append({
                    "bed_label": bed_label, "original_no": orig_no,
                    "building": bl.split("-")[1], "floor": int(bl.split("-")[2]),
                    "status": status,
                })

            # 按楼、楼层分组
            buildings = {}
            for b in beds_info:
                bl, fl = b["building"], b["floor"]
                if bl not in buildings: buildings[bl] = {}
                if fl not in buildings[bl]: buildings[bl][fl] = []
                buildings[bl][fl].append(b)

            wards_data.append({
                "ward_id": wid, "ward_name": ward["name"], "dept_name": dept["name"],
                "total_beds": len(beds_info), "buildings": buildings,
                "stats": {"占用": len([b for b in beds_info if b["status"] == "occupied"]),
                          "空闲": len([b for b in beds_info if b["status"] == "idle"]),
                          "维修": len([b for b in beds_info if b["status"] == "maintenance"])},
            })

    return jsonify({
        "hospital_id": hid, "hospital_name": hosp["name"],
        "region": hosp["region"], "hour": hour,
        "wards_data": wards_data,
        "total_beds": hr["total_beds"], "occupied": hr["occupied"],
        "idle": hr["idle"], "maintenance": hr["maintenance"],
    })


# ── 大屏配置 ──

@app.route("/api/screen_config", methods=["GET", "POST"])
def api_screen_config():
    global screen_config
    if request.method == "POST":
        config = request.json
        screen_config.update({
            "screens": config.get("screens", screen_config["screens"]),
            "simulated_date": config.get("simulated_date", screen_config["simulated_date"]),
        })
        # 日期改变时重建缓存
        _clear_cache()
        return jsonify({"success": True})
    return jsonify(screen_config)


@app.route("/api/set_simulated_date", methods=["POST"])
def api_set_simulated_date():
    date_str = request.json.get("date", "2026-01-01")
    screen_config["simulated_date"] = date_str
    _clear_cache()
    return jsonify({"success": True})


@app.route("/api/dept_summary")
def api_dept_summary():
    """各科室占用情况汇总（跨医院同名科室合并）"""
    data = _init_data()
    _ensure_cache()
    date_str = request.args.get("date", screen_config["simulated_date"])

    # 收集所有科室数据
    dept_by_name = {}  # dept_name -> {total_beds, occupied, hospitals: set}
    for hid, hosp in data["hospitals"].items():
        hours = _get_hourly(hid, date_str)
        if not hours:
            continue
        # 用中午12点代表当天
        hr = hours[12]
        for d in hr["depts"]:
            nm = d["name"]
            if nm not in dept_by_name:
                dept_by_name[nm] = {"name": nm, "total_beds": 0, "occupied": 0, "hospitals": set()}
            dept_by_name[nm]["total_beds"] += d["total"]
            dept_by_name[nm]["occupied"] += d["occupied"]
            dept_by_name[nm]["hospitals"].add(hosp["name"])

    result = []
    for nm, info in dept_by_name.items():
        rate = round(info["occupied"] / info["total_beds"] * 100, 1) if info["total_beds"] > 0 else 0
        result.append({
            "name": nm,
            "total_beds": info["total_beds"],
            "occupied": info["occupied"],
            "idle": info["total_beds"] - info["occupied"],
            "rate": rate,
            "hospital_count": len(info["hospitals"]),
        })
    result.sort(key=lambda x: x["rate"], reverse=True)
    return jsonify(result)


@app.route("/api/hospital_occupancy")
def api_hospital_occupancy():
    """各医院当月占用率"""
    data = _init_data()
    _ensure_cache()
    date_str = request.args.get("date", screen_config["simulated_date"])
    result = []
    for hid, hosp in data["hospitals"].items():
        hours = _get_hourly(hid, date_str)
        if not hours:
            continue
        hr = hours[12]
        dept_count = len(hosp["departments"])
        ward_count = sum(len(d["wards"]) for d in hosp["departments"].values())
        bed_count = sum(len(w.get("bed_labels", {})) for d in hosp["departments"].values() for w in d["wards"].values())
        result.append({
            "id": hid, "name": hosp["name"], "region": hosp["region"],
            "departments": dept_count, "wards": ward_count, "beds": bed_count,
            "occupied": hr["occupied"], "idle": hr["idle"],
            "rate": hr["occupancy_rate"],
        })
    return jsonify(result)


# ── API: 屏5 — 跨医院科室汇总逐小时数据 ──
@app.route("/api/dept_summary_hourly")
def api_dept_summary_hourly():
    """跨医院同名科室合并的逐小时数据（左轴占用率、右轴空闲数）"""
    date_str = request.args.get("date", screen_config["simulated_date"])
    _init_data()
    _ensure_cache()
    # 从缓存读取
    if dept_summary_cache and date_str in dept_summary_cache:
        return jsonify(dept_summary_cache[date_str])
    # 降级：现场计算
    data = _init_data()
    hours_by_dept = {}
    total_free_by_hour = []
    for hid in data["hospitals"]:
        hours = _get_hourly(hid, date_str)
        if not hours:
            continue
        for h, hr in enumerate(hours):
            if h >= 24:
                break
            if h not in hours_by_dept:
                hours_by_dept[h] = {}
            if len(total_free_by_hour) <= h:
                total_free_by_hour.append(0)
            total_free_by_hour[h] += hr["idle"]
            for d in hr["depts"]:
                nm = d["name"]
                if nm not in hours_by_dept[h]:
                    hours_by_dept[h][nm] = {"total": 0, "occupied": 0}
                hours_by_dept[h][nm]["total"] += d["total"]
                hours_by_dept[h][nm]["occupied"] += d["occupied"]
    result_hours = []
    for h in range(24):
        depts = hours_by_dept.get(h, {})
        dept_list = []
        for nm, info in depts.items():
            rate = round(info["occupied"] / info["total"] * 100, 1) if info["total"] > 0 else 0
            dept_list.append({"name": nm, "total": info["total"], "occupied": info["occupied"],
                              "idle": info["total"] - info["occupied"], "rate": rate})
        result_hours.append({"hour": h, "total_free": total_free_by_hour[h] if h < len(total_free_by_hour) else 0,
                             "depts": dept_list})
    return jsonify({"hours": result_hours})


# ── API: 屏6 — 医院地理位置与占用数据 ──
@app.route("/api/hospital_geo")
def api_hospital_geo():
    """返回6家医院的地理位置、占用率、空闲床位数"""
    _init_data()
    _ensure_cache()
    date_str = request.args.get("date", screen_config["simulated_date"])

    # 从缓存读取
    if hospital_geo_cache and date_str in hospital_geo_cache:
        return jsonify(hospital_geo_cache[date_str])

    hosps = [
        {"id": "H001", "name": "玛丽医院", "lng": 114.125, "lat": 22.255, "region": "香港岛"},
        {"id": "H002", "name": "东区尤德夫人医院", "lng": 114.245, "lat": 22.265, "region": "香港岛"},
        {"id": "H003", "name": "伊利沙伯医院", "lng": 114.190, "lat": 22.305, "region": "九龙"},
        {"id": "H004", "name": "广华医院", "lng": 114.150, "lat": 22.320, "region": "九龙"},
        {"id": "H005", "name": "威尔斯亲王医院", "lng": 114.215, "lat": 22.395, "region": "新界"},
        {"id": "H006", "name": "屯门医院", "lng": 113.975, "lat": 22.425, "region": "新界"},
    ]

    for h in hosps:
        hours = _get_hourly(h["id"], date_str)
        if hours:
            hr = hours[12]
            h["beds"] = hr["total_beds"]
            h["occupied"] = hr["occupied"]
            h["idle"] = hr["idle"]
            h["rate"] = hr["occupancy_rate"]
        else:
            h.update({"beds": 0, "occupied": 0, "idle": 0, "rate": 0})
    return jsonify(hosps)


@app.route("/api/hospital_idle_depts")
def api_hospital_idle_depts():
    """返回各医院各科室的空闲床位数（用于屏5日志流）"""
    _init_data()
    _ensure_cache()
    date_str = request.args.get("date", screen_config["simulated_date"])
    hour = int(request.args.get("hour", 12))

    result = {}
    for hid, data_hosp in data_store["hospitals"].items():
        hours = _get_hourly(hid, date_str)
        if not hours or hour >= len(hours):
            continue
        hr = hours[hour]
        depts = []
        for d in hr["depts"]:
            depts.append({
                "name": d["name"],
                "total": d["total"],
                "occupied": d["occupied"],
                "idle": d["idle"],
            })
        result[hid] = {
            "name": data_hosp["name"],
            "depts": depts,
        }
    return jsonify(result)


# ── 启动前预加载缓存 ──
def _warmup():
    """预加载缓存"""
    try:
        _init_data()
        _ensure_cache()
        print(f"✅ 缓存预热完成")
    except Exception:
        pass


if __name__ == "__main__":
    print(f"🏥 香港医院病床管理后台启动")
    print(f"   http://127.0.0.1:8989")
    # 异步预热
    threading.Thread(target=_warmup, daemon=True).start()
    app.run(host="0.0.0.0", port=8989, debug=True, use_reloader=False)
