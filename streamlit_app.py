#!/usr/bin/env python3
"""
运动负荷监控 — Streamlit Web版 (Supabase)
运动员训练负荷追踪 + ACWR分析 + 数据导入/导出
"""
import streamlit as st
import os, re, json
import pandas as pd
from datetime import datetime, date, timedelta
from io import BytesIO
from supabase import create_client, Client
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

# =========================================================
# Supabase 配置
# =========================================================
SUPABASE_URL = st.secrets.get("SUPABASE_URL", os.environ.get("SUPABASE_URL", ""))
SUPABASE_KEY = st.secrets.get("SUPABASE_SERVICE_KEY", os.environ.get("SUPABASE_SERVICE_KEY", ""))
APP_PASSWORD = st.secrets.get("APP_PASSWORD", os.environ.get("APP_PASSWORD", ""))

def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def get_supabase_read() -> Client:
    anon_key = st.secrets.get("SUPABASE_ANON_KEY", os.environ.get("SUPABASE_ANON_KEY", SUPABASE_KEY))
    return create_client(SUPABASE_URL, anon_key)

# =========================================================
# 密码验证
# =========================================================
if APP_PASSWORD:
    if "auth_ok" not in st.session_state:
        st.session_state.auth_ok = False
    if not st.session_state.auth_ok:
        st.title("🔒 运动负荷监控系统")
        pwd = st.text_input("请输入密码", type="password", label_visibility="collapsed", placeholder="密码")
        if st.button("进入", use_container_width=True):
            if pwd == APP_PASSWORD:
                st.session_state.auth_ok = True
                st.rerun()
            else:
                st.error("密码错误")
        st.stop()

# =========================================================
# 数据操作
# =========================================================
PERIODS = ["早上", "上午", "下午", "晚上"]

def get_or_create_athlete(name="陈伟雄"):
    sb = get_supabase()
    r = sb.table("athletes").select("*").eq("name", name).execute()
    if r.data:
        return r.data[0]
    data = {"name": name, "goal": "摸高315cm", "current_touch_cm": 295, "target_touch_cm": 315}
    r = sb.table("athletes").insert(data).execute()
    return r.data[0]

def update_athlete(aid, **kwargs):
    sb = get_supabase()
    sb.table("athletes").update(kwargs).eq("id", aid).execute()

def add_session(date_val, period, rpe, duration, exercises=None, phase="", notes=""):
    """添加训练课+动作明细"""
    sb = get_supabase()
    athlete = get_or_create_athlete()
    r = sb.table("sessions").insert({
        "athlete_id": athlete["id"], "date": str(date_val),
        "period": period, "rpe": rpe, "duration_min": duration,
        "phase": phase, "notes": notes
    }).execute()
    session_id = r.data[0]["id"]
    if exercises:
        for i, ex in enumerate(exercises):
            sb.table("session_exercises").insert({
                "session_id": session_id,
                "exercise_name": ex.get("name", ""),
                "sets": ex.get("sets", 0),
                "reps": str(ex.get("reps", "")),
                "intensity": str(ex.get("intensity", "")),
                "rest_min": ex.get("rest_min", 0),
                "actual_completion": ex.get("actual", ""),
                "sort_order": i,
            }).execute()
    return session_id

def get_sessions(athlete_id, limit=200):
    sb = get_supabase_read()
    r = sb.table("sessions").select("*").eq("athlete_id", athlete_id).order("date", desc=True).limit(limit).execute()
    return r.data or []

def get_exercises_for_session(session_id):
    sb = get_supabase_read()
    r = sb.table("session_exercises").select("*").eq("session_id", session_id).order("sort_order").execute()
    return r.data or []

def get_all_sessions_with_exercises(athlete_id, limit=200):
    """获取训练课+动作明细（关联查询）"""
    sessions = get_sessions(athlete_id, limit)
    sid_map = {s["id"]: s for s in sessions}
    if not sid_map:
        return sessions
    sb = get_supabase_read()
    ids = list(sid_map.keys())
    r = sb.table("session_exercises").select("*").in_("session_id", ids).order("sort_order").execute()
    for ex in (r.data or []):
        sid = ex["session_id"]
        if sid in sid_map:
            if "exercises" not in sid_map[sid]:
                sid_map[sid]["exercises"] = []
            sid_map[sid]["exercises"].append(ex)
    return list(sid_map.values())

def compute_weekly_loads(athlete_id):
    """计算所有周的ACWR/单调性/训练张力"""
    sb = get_supabase()
    r = sb.table("sessions").select("date,rpe,duration_min").eq("athlete_id", athlete_id).order("date").execute()
    if not r.data:
        return
    df = pd.DataFrame(r.data)
    df["date"] = pd.to_datetime(df["date"])
    df["week"] = df["date"].dt.isocalendar().week.astype(int)
    df["year"] = df["date"].dt.isocalendar().year.astype(int)
    df["projected_load"] = df["rpe"] * df["duration_min"]

    # 按周汇总
    weekly = df.groupby(["year", "week"]).agg(
        total_load=("projected_load", "sum"),
        session_count=("date", "nunique"),
        daily_loads=("projected_load", list)
    ).reset_index()
    weekly["week_start"] = weekly.apply(
        lambda r: pd.Timestamp.fromisocalendar(int(r["year"]), int(r["week"]), 1).date(), axis=1
    )

    # 按天累加负荷，再算平均值和标准差
    daily = df.groupby(["year", "week", "date"]).agg(daily_load=("projected_load", "sum")).reset_index()
    weekly_stats = daily.groupby(["year", "week"]).agg(
        avg_daily=("daily_load", "mean"),
        std_daily=("daily_load", "std")
    ).reset_index()
    weekly = weekly.merge(weekly_stats, on=["year", "week"], how="left")

    for _, row in weekly.iterrows():
        total = row["total_load"]
        avg_d = row["avg_daily"] if pd.notna(row["avg_daily"]) else 0
        std_d = row["std_daily"] if pd.notna(row["std_daily"]) else 1
        ws = row["week_start"]

        # 急性负荷 = 本周总负荷
        acute = total
        # 慢性负荷 = 近4周平均周负荷
        prev = sb.table("weekly_loads").select("total_load").eq("athlete_id", athlete_id).lt("week_start", str(ws)).order("week_start", desc=True).limit(3).execute()
        chronic_loads = [r["total_load"] for r in (prev.data or [])]
        if chronic_loads:
            chronic = (total + sum(chronic_loads)) / (len(chronic_loads) + 1)
        else:
            chronic = total

        acwr = acute / chronic if chronic > 0 else 1.0
        monotony = avg_d / std_d if std_d > 0 else 0
        strain = acute * chronic / 1000

        existing = sb.table("weekly_loads").select("id").eq("athlete_id", athlete_id).eq("week_start", str(ws)).execute()
        payload = {
            "athlete_id": athlete_id, "week_start": str(ws),
            "total_load": round(total, 1), "avg_daily_load": round(avg_d, 1),
            "session_count": int(row["session_count"]),
            "acute_load": round(acute, 1), "chronic_load": round(chronic, 1),
            "acwr": round(acwr, 2), "monotony": round(monotony, 2), "strain": round(strain, 1),
        }
        if existing.data:
            sb.table("weekly_loads").update(payload).eq("id", existing.data[0]["id"]).execute()
        else:
            sb.table("weekly_loads").insert(payload).execute()

def get_weekly_loads(athlete_id, limit=30):
    sb = get_supabase_read()
    r = sb.table("weekly_loads").select("*").eq("athlete_id", athlete_id).order("week_start", desc=True).limit(limit).execute()
    return r.data or []

def get_exercise_library():
    sb = get_supabase_read()
    r = sb.table("exercise_library").select("*").order("category").order("subcategory").execute()
    return r.data or []

def get_all_athletes():
    sb = get_supabase_read()
    r = sb.table("athletes").select("*").order("name").execute()
    return r.data or []

def create_athlete(name, goal="", touch_cm=0, target_cm=0):
    sb = get_supabase()
    try:
        r = sb.table("athletes").insert({
            "name": name, "goal": goal,
            "current_touch_cm": touch_cm, "target_touch_cm": target_cm
        }).execute()
        return r.data[0], None
    except Exception as e:
        return None, str(e)

def get_active_athlete():
    """从session_state获取当前选中的运动员，不存在则用默认"""
    if "athlete_id" not in st.session_state:
        athletes = get_all_athletes()
        if athletes:
            st.session_state.athlete_id = athletes[0]["id"]
            st.session_state.athlete_name = athletes[0]["name"]
        else:
            # 自动创建默认
            a = get_or_create_athlete()
            st.session_state.athlete_id = a["id"]
            st.session_state.athlete_name = a["name"]
    return st.session_state.athlete_id, st.session_state.athlete_name

# =========================================================
# Page: 总览看板
# =========================================================
def page_dashboard():
    st.subheader("📊 训练总览")
    aid, aname = get_active_athlete()
    athlete = get_supabase_read().table("athletes").select("*").eq("id", aid).execute().data[0]

    # 运动员信息卡片
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        with st.expander("🏃 运动员", expanded=True):
            st.metric("摸高", f'{athlete["current_touch_cm"]}cm', f'目标{athlete["target_touch_cm"]}cm')
            new_touch = st.number_input("当前摸高", value=float(athlete["current_touch_cm"]), step=1.0)
            new_target = st.number_input("目标摸高", value=float(athlete["target_touch_cm"]), step=1.0)
            if st.button("更新", use_container_width=True):
                update_athlete(aid, current_touch_cm=new_touch, target_touch_cm=new_target)
                st.success("✓")
                st.rerun()
    with c2:
        with st.expander("💪 力量数据", expanded=True):
            st.metric("高翻 2RM", f'{athlete["clean_2rm"]}kg')
            st.metric("后蹲 2RM", f'{athlete["squat_2rm"]}kg')
            new_clean = st.number_input("高翻", value=float(athlete["clean_2rm"]), step=1.0)
            new_squat = st.number_input("后蹲", value=float(athlete["squat_2rm"]), step=1.0)
            if st.button("更新力量", use_container_width=True):
                update_athlete(aid, clean_2rm=new_clean, squat_2rm=new_squat)
                st.success("✓")
                st.rerun()
    with c3:
        with st.expander("🎯 目标进度", expanded=True):
            gap = athlete["target_touch_cm"] - athlete["current_touch_cm"]
            progress = athlete["current_touch_cm"] / athlete["target_touch_cm"] * 100 if athlete["target_touch_cm"] > 0 else 0
            st.metric("差距", f'{gap}cm')
            st.progress(min(progress / 100, 1.0), text=f"{progress:.1f}%")
    with c4:
        with st.expander("📋 目标", expanded=True):
            st.write(athlete.get("goal", "摸高315cm"))

    # 最近训练
    st.markdown("---")
    col_left, col_right = st.columns([2, 1])
    with col_left:
        st.subheader("📅 最近训练")
    with col_right:
        if st.button("🔄 重新计算周负荷", use_container_width=True):
            compute_weekly_loads(aid)
            st.success("已计算")
            st.rerun()

    sessions = get_all_sessions_with_exercises(aid, 10)
    if sessions:
        for s in sessions[:5]:
            with st.expander(f"**{s['date']}** {s['period']} | RPE {s['rpe']} | {s['duration_min']}min | 负荷{s['rpe']*s['duration_min']}AU", expanded=False):
                if s.get("notes"):
                    st.caption(f"备注: {s['notes']}")
                exs = s.get("exercises", [])
                if exs:
                    ex_df = pd.DataFrame([{
                        "动作": e["exercise_name"],
                        "组数": e["sets"],
                        "次数": e["reps"],
                        "强度": e["intensity"],
                        "间歇": f'{e["rest_min"]}min' if e["rest_min"] else "",
                    } for e in exs])
                    st.dataframe(ex_df, use_container_width=True, hide_index=True)
    else:
        st.info("暂无训练记录，请先录入或导入")

    # 周负荷图表
    st.markdown("---")
    st.subheader("📈 周负荷趋势")
    weekly = get_weekly_loads(aid, 20)
    weekly.reverse()  # 按时间正序
    if weekly:
        df = pd.DataFrame(weekly)
        df["label"] = df["week_start"].apply(lambda x: str(x)[5:])

        # ACWR 图表
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                            subplot_titles=("ACWR (急性:慢性负荷比)", "周总负荷 + 训练张力"),
                            vertical_spacing=0.12, row_heights=[0.5, 0.5])
        # ACWR line
        fig.add_trace(go.Scatter(x=df["label"], y=df["acwr"], mode="lines+markers",
                                 name="ACWR", line=dict(color="#ff6b6b", width=3),
                                 marker=dict(size=8)), row=1, col=1)
        # 危险线
        fig.add_hline(y=1.5, line_dash="dash", line_color="red", row=1, col=1,
                      annotation_text="危险 1.5")
        fig.add_hline(y=1.3, line_dash="dash", line_color="orange", row=1, col=1,
                      annotation_text="警戒 1.3")
        fig.add_hline(y=0.8, line_dash="dash", line_color="green", row=1, col=1,
                      annotation_text="偏低 0.8")

        # 周负荷 + 训练张力
        fig.add_trace(go.Bar(x=df["label"], y=df["total_load"], name="周负荷(AU)",
                             marker_color="#4ecdc4", opacity=0.8), row=2, col=1)
        fig.add_trace(go.Scatter(x=df["label"], y=df["strain"], mode="lines+markers",
                                 name="训练张力", line=dict(color="#9b59b6", width=2),
                                 marker=dict(size=6)), row=2, col=1)

        fig.update_layout(height=500, template="plotly_dark", showlegend=True,
                          margin=dict(l=20, r=20, t=40, b=20))
        fig.update_xaxes(title_text="周")
        st.plotly_chart(fig, use_container_width=True)

        # 周统计表
        st.subheader("📋 周负荷明细")
        df_disp = df[["label", "total_load", "avg_daily_load", "session_count", "acwr", "monotony", "strain"]].copy()
        df_disp.columns = ["周", "总负荷", "日均负荷", "训练次数", "ACWR", "单调性", "训练张力"]
        # 颜色标签
        def acwr_color(v):
            if v > 1.5: return "🔴"
            if v > 1.3: return "🟡"
            if v < 0.8: return "🔵"
            return "🟢"
        df_disp["状态"] = df_disp["ACWR"].apply(acwr_color)
        st.dataframe(df_disp, use_container_width=True, hide_index=True)
    else:
        st.info("暂无周负荷数据，录入训练后会自动计算")

# =========================================================
# Page: 训练录入
# =========================================================
def page_entry():
    st.subheader("📝 训练录入")

    # 常用动作快捷按钮
    common_exercises = ["摸高", "后蹲", "高翻", "冲刺", "跳箱", "保加利亚蹲",
                        "硬拉", "卧推", "引体向上", "实力推", "北欧落", "举重拉"]

    aid, aname = get_active_athlete()

    with st.form("entry_form"):
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            s_date = st.date_input("日期", date.today())
        with col2:
            s_period = st.selectbox("时段", PERIODS)
        with col3:
            s_rpe = st.slider("RPE (训练难度)", 0, 10, 7, help="0=休息, 10=最大努力")
        with col4:
            s_duration = st.number_input("时长(分钟)", 0, 300, 60, step=5)

        s_phase = st.text_input("训练阶段 (可选)", placeholder="如: 准备期/专项发展期/赛季期")
        s_notes = st.text_area("备注 (可选)", placeholder="训练感受、身体状态等")

        st.markdown("**训练动作**")
        st.caption("点击动作名称快速添加，或手动输入")

        # 快捷动作按钮
        ex_cols = st.columns(6)
        added_exercises = []
        for i, ex_name in enumerate(common_exercises):
            with ex_cols[i % 6]:
                if st.form_submit_button(f"➕ {ex_name}"):
                    st.session_state[f"add_ex_{ex_name}"] = True

        # 动态表单 - 可添加多个动作
        ex_count = st.number_input("添加几个动作?", 0, 20, 0, step=1, key="ex_count")
        exercises = []
        for i in range(int(ex_count)):
            col_a, col_b, col_c, col_d, col_e = st.columns([3, 1, 1, 1, 1])
            with col_a:
                name = st.text_input(f"动作名称", key=f"ex_name_{i}", placeholder="如: 摸高")
            with col_b:
                sets = st.number_input(f"组数", 0, 50, 0, key=f"ex_sets_{i}")
            with col_c:
                reps = st.text_input(f"次数", key=f"ex_reps_{i}", placeholder="如: 5-6")
            with col_d:
                intensity = st.text_input(f"强度", key=f"ex_intensity_{i}", placeholder="如: 270-280cm")
            with col_e:
                rest = st.number_input(f"间歇(min)", 0.0, 10.0, 0.0, step=0.5, key=f"ex_rest_{i}")
            if name:
                exercises.append({"name": name, "sets": sets, "reps": reps, "intensity": intensity, "rest_min": rest})

        submitted = st.form_submit_button("💾 保存训练", type="primary", use_container_width=True)
        if submitted:
            if not exercises:
                st.warning("请至少添加一个训练动作")
            else:
                add_session(s_date, s_period, s_rpe, s_duration, exercises, s_phase, s_notes)
                compute_weekly_loads(aid)
                st.success(f"✅ {s_date} {s_period} 训练已保存 ({len(exercises)}个动作)")
                st.rerun()

    # 显示今天已录
    st.markdown("---")
    st.caption("今日已录训练")
    today_sessions = get_sessions(aid, 50)
    today_sessions = [s for s in today_sessions if str(s["date"]) == str(date.today())]
    if today_sessions:
        for s in today_sessions:
            with st.expander(f"{s['period']} | RPE {s['rpe']} | {s['duration_min']}min"):
                exs = get_exercises_for_session(s["id"])
                if exs:
                    for e in exs:
                        st.write(f"  • {e['exercise_name']}: {e['sets']}组×{e['reps']} @{e['intensity']}")
    else:
        st.info("今天还没有训练记录")

# =========================================================
# Page: 导入历史数据
# =========================================================
def page_import():
    st.subheader("📤 导入历史数据")
    st.caption("支持从「陈伟雄负荷监控」Excel导入，或标准CSV/Excel格式")

    uploaded = st.file_uploader("选择文件", type=["xlsx", "xls", "csv"])
    if uploaded:
        tmp_path = f"/tmp/upload_load_{uploaded.name}"
        with open(tmp_path, "wb") as f:
            f.write(uploaded.getbuffer())

        # 检测文件类型
        is_original = "陈伟雄" in uploaded.name or "负荷监控" in uploaded.name

        if is_original:
            st.info("检测到原始监控Excel，将解析训练数据")
            if st.button("解析并导入", type="primary", use_container_width=True):
                with st.spinner("正在解析..."):
                    try:
                        aid, aname = get_active_athlete()
                        count = import_original_excel(tmp_path, aid)
                        compute_weekly_loads(aid)
                        st.success(f"✅ 成功导入 {count} 条训练记录 (含动作明细)")
                    except Exception as e:
                        st.error(f"导入失败: {e}")
        else:
            st.info("标准格式导入")
            # TODO: 支持标准CSV/Excel导入

        os.remove(tmp_path)

    st.markdown("---")
    st.caption("手动导入动作库")
    if st.button("📚 初始化动作库（从Excel解析）", use_container_width=True):
        src = "/Users/workingbook/Desktop/陈伟雄负荷监控2026.5.27.xlsx"
        if os.path.exists(src):
            try:
                df = pd.read_excel(src, sheet_name="动作库", header=None)
                sb = get_supabase()
                count = 0
                # Row 7+ has exercise data
                for i in range(7, df.shape[0]):
                    cat = str(df.iloc[i, 2]) if pd.notna(df.iloc[i, 2]) else ""
                    push = str(df.iloc[i, 3]) if pd.notna(df.iloc[i, 3]) else ""
                    pull = str(df.iloc[i, 4]) if pd.notna(df.iloc[i, 4]) else ""
                    cat_clean = "上肢" if "上肢" in cat else ("下肢" if "下肢" in cat else "全身")
                    for name in [push, pull]:
                        if name and name not in ("nan", ""):
                            try:
                                sb.table("exercise_library").insert({
                                    "name": name.strip(), "category": cat_clean,
                                    "subcategory": "推" if name == push else "拉"
                                }).execute()
                                count += 1
                            except Exception:
                                pass
                st.success(f"已导入 {count} 个动作到动作库")
            except Exception as e:
                st.error(f"解析失败: {e}")
        else:
            st.error("未找到原始Excel文件")

def import_original_excel(filepath, athlete_id=None):
    """解析房力/陈伟雄负荷监控.xlsx - 重点解析负荷Sheet的历史数据"""
    sb = get_supabase()
    if not athlete_id:
        athlete = get_or_create_athlete()
        athlete_id = athlete["id"]
    aid = athlete_id

    # 读取负荷Sheet (真正的历史训练数据)
    df = pd.read_excel(filepath, sheet_name="负荷", header=None)
    count = 0

    # 解析函数：将 YY.M.DD / YY.MM.DD / MM.DD 格式转为date
    def parse_week_date(val):
        if pd.isna(val):
            return None
        s = str(val).strip()
        # 移除多余的换行
        s = s.split('\n')[0].strip()
        # 处理 float 如 22.9.26
        try:
            parts = s.replace('.', ' ').split()
            if len(parts) == 1:
                # 可能是 float: 22.9.26
                pass
        except:
            pass

        # 支持格式: 22.9.26, 24.8.19, 5.25
        import re
        m = re.match(r'(\d+)\.(\d+)\.?(\d+)?', s)
        if m:
            g1, g2, g3 = m.group(1), m.group(2), m.group(3)
            # 判断: 如果第一段是2位数且>12, 是年份
            n1 = int(g1)
            n2 = int(g2)
            if n1 > 31:  # 年份 (22, 24)
                year = 2000 + n1
                month = n2
                day = int(g3) if g3 else 1
            else:  # 可能是月.日 或 月.日
                month = n1
                day = n2
                year = 2026
            try:
                from datetime import date
                return date(year, month, day)
            except:
                return None
        return None

    # 行号迭代
    row = 0
    while row < df.shape[0]:
        val0 = str(df.iloc[row, 0]).strip() if pd.notna(df.iloc[row, 0]) else ""

        # 检测周类型行 (力量周/正式周/减载周/2月期等)
        is_week_header = any(kw in val0 for kw in ["力量周", "正式周", "减载周", "周", "月期", "比赛期"])
        # 也检查该行是否有日期在col 1
        has_dates = False
        if is_week_header:
            d1 = df.iloc[row, 1] if df.shape[1] > 1 else None
            if pd.notna(d1):
                d = parse_week_date(d1)
                if d:
                    has_dates = True

        if not (is_week_header and has_dates):
            row += 1
            continue

        # 这是新的一周数据
        week_type = val0
        dates = {}
        for day_col in [1, 5, 9, 13, 17, 21, 25]:
            if day_col < df.shape[1]:
                d = parse_week_date(df.iloc[row, day_col])
                if d:
                    dates[day_col] = d

        if not dates:
            row += 1
            continue

        # 跳过headers行(如果有)
        next_row = row + 1
        if next_row >= df.shape[0]:
            break

        # 检查下一行是否是子标题行 (含"Drill"/"Laod")
        next_val = str(df.iloc[next_row, 1]).strip() if pd.notna(df.iloc[next_row, 1]) else ""
        if "drill" in next_val.lower() or "load" in next_val.lower():
            next_row += 1

        # 读取4个时段的数据 (早上/上午/下午/晚上)
        for _ in range(4):
            if next_row >= df.shape[0]:
                break
            period_val = str(df.iloc[next_row, 0]).strip() if pd.notna(df.iloc[next_row, 0]) else ""
            if period_val not in ("早上", "上午", "下午", "晚上"):
                next_row += 1
                continue

            # 遍历每一天
            for day_col, d in dates.items():
                drill_col = day_col
                rpe_col = day_col + 1
                time_col = day_col + 2
                load_col = day_col + 3

                drill_val = df.iloc[next_row, drill_col] if drill_col < df.shape[1] else None
                if pd.isna(drill_val) or str(drill_val).strip() in ("", "nan", "无", "00", "0"):
                    continue

                drill_name = str(drill_val).strip()
                if drill_name in ("nan", "", "无", "00", "0"):
                    continue

                # 解析RPE
                try:
                    rpe_v = float(str(df.iloc[next_row, rpe_col]).strip()) if rpe_col < df.shape[1] and pd.notna(df.iloc[next_row, rpe_col]) else 0
                except (ValueError, TypeError):
                    # RPE可能是个范围如 "3-6.5" → 取平均值
                    rpe_str = str(df.iloc[next_row, rpe_col]).strip() if rpe_col < df.shape[1] and pd.notna(df.iloc[next_row, rpe_col]) else "0"
                    import re
                    nums = re.findall(r'[\d.]+', rpe_str)
                    if nums:
                        vals = [float(n) for n in nums]
                        rpe_v = sum(vals) / len(vals)
                    else:
                        rpe_v = 0

                # 解析时长
                try:
                    time_v = float(str(df.iloc[next_row, time_col]).strip()) if time_col < df.shape[1] and pd.notna(df.iloc[next_row, time_col]) else 0
                except (ValueError, TypeError):
                    time_v = 0

                if rpe_v <= 0 or time_v <= 0:
                    continue

                # 写入数据库
                try:
                    sb.table("sessions").insert({
                        "athlete_id": aid, "date": str(d), "period": period_val,
                        "rpe": round(rpe_v, 1), "duration_min": int(round(time_v)),
                        "phase": week_type, "notes": drill_name
                    }).execute()
                    count += 1
                except Exception as e:
                    pass  # skip duplicates

            next_row += 1

        # 跳过summary行 (mean or total) 和 notes行 (备注)
        while next_row < df.shape[0]:
            next_v0 = str(df.iloc[next_row, 0]).strip() if pd.notna(df.iloc[next_row, 0]) else ""
            if next_v0 in ("", "nan") or "mean" in next_v0.lower() or "total" in next_v0.lower() or "备注" in next_v0 or "mean or total" in next_v0:
                next_row += 1
            else:
                break

        row = next_row

    # 也解析周训练安排Sheet (周计划)
    try:
        df_plan = pd.read_excel(filepath, sheet_name="周训练安排", header=None)
        plan_count = 0
        date_cols = {}
        for j in range(df_plan.shape[1]):
            v = df_plan.iloc[7, j]
            if pd.isna(v):
                continue
            if isinstance(v, str):
                v = v.strip()
                if v.replace(".", "").isdigit() and "." in v:
                    parts = v.split(".")
                    try:
                        d = date(2026, int(parts[0]), int(parts[1]))
                        date_cols[j] = d
                    except:
                        pass
            elif isinstance(v, (int, float)):
                m = int(v)
                d_val = int(round((v - m) * 100))
                if 1 <= m <= 12 and 1 <= d_val <= 31:
                    try:
                        d = date(2026, m, d_val)
                        date_cols[j] = d
                    except:
                        pass

        day_groups = [(6, "周一"), (12, "周二"), (18, "周三"),
                       (24, "周四"), (30, "周五"), (36, "周六"), (42, "周日")]

        for col_start, day_name in day_groups:
            if col_start not in date_cols:
                continue
            d = date_cols[col_start]
            exercises = []
            for i in range(16, df_plan.shape[0]):
                ename = df_plan.iloc[i, col_start] if col_start < df_plan.shape[1] else None
                if pd.isna(ename):
                    continue
                ename_str = str(ename).strip()
                if ename_str in ("nan", "") or ename_str.startswith("warm-up") or ename_str.startswith("热身") or ename_str.startswith("技术改善"):
                    continue

                sets_v = df_plan.iloc[i, col_start + 1] if col_start + 1 < df_plan.shape[1] else None
                has_sets = sets_v is not None and pd.notna(sets_v) and str(sets_v).strip() not in ("", "nan")
                if not has_sets:
                    continue

                reps_v = df_plan.iloc[i, col_start + 2] if col_start + 2 < df_plan.shape[1] else None
                inten_v = df_plan.iloc[i, col_start + 3] if col_start + 3 < df_plan.shape[1] else None
                rest_v = df_plan.iloc[i, col_start + 4] if col_start + 4 < df_plan.shape[1] else None
                actual_v = df_plan.iloc[i, col_start + 5] if col_start + 5 < df_plan.shape[1] else None

                try:
                    sets_n = int(float(str(sets_v))) if sets_v is not None and str(sets_v).strip() not in ("", "nan") else 0
                except:
                    sets_n = 0
                reps_s = str(reps_v).strip() if reps_v is not None and str(reps_v).strip() not in ("", "nan") else ""
                inten_s = str(inten_v).strip() if inten_v is not None and str(inten_v).strip() not in ("", "nan") else ""
                try:
                    rest_f = float(str(rest_v)) if rest_v is not None and str(rest_v).strip() not in ("", "nan") else 0
                except:
                    rest_f = 0
                actual_s = str(actual_v).strip() if actual_v is not None and str(actual_v).strip() not in ("", "nan") else ""

                exercises.append({"name": ename_str, "sets": sets_n, "reps": reps_s,
                                  "intensity": inten_s, "rest_min": rest_f, "actual": actual_s})

            if exercises:
                try:
                    rpe = 7
                    duration = min(len(exercises) * 15, 120)
                    sb.table("sessions").insert({
                        "athlete_id": aid, "date": str(d), "period": "下午",
                        "rpe": rpe, "duration_min": duration, "phase": "历史周计划"
                    }).execute()
                    sid = sb.table("sessions").select("id").eq("athlete_id", aid).eq("date", str(d)).eq("period", "下午").order("id", desc=True).limit(1).execute().data[0]["id"]
                    for j, ex in enumerate(exercises):
                        sb.table("session_exercises").insert({
                            "session_id": sid, "exercise_name": ex["name"],
                            "sets": ex["sets"], "reps": ex["reps"],
                            "intensity": ex["intensity"], "rest_min": ex["rest_min"],
                            "actual_completion": ex["actual"], "sort_order": j
                        }).execute()
                    plan_count += 1
                except:
                    pass

        return count + plan_count
    except Exception:
        return count

# =========================================================
# Page: 负荷分析
# =========================================================
def page_analysis():
    st.subheader("📈 负荷深度分析")
    aid, aname = get_active_athlete()

    compute_weekly_loads(aid)
    weekly = get_weekly_loads(aid, 30)
    if not weekly:
        st.info("暂无数据，请先录入训练")
        return

    df = pd.DataFrame(weekly)
    df["label"] = df["week_start"].apply(lambda x: str(x)[5:])
    df = df.sort_values("week_start")

    # ACWR 状态分布饼图
    def acwr_status(v):
        if v > 1.5: return "🔴 负荷过高"
        if v > 1.3: return "🟡 警戒"
        if v < 0.8: return "🔵 负荷不足"
        return "🟢 理想"
    df["acwr_status"] = df["acwr"].apply(acwr_status)

    col1, col2 = st.columns(2)
    with col1:
        fig = px.pie(df, names="acwr_status", title="ACWR 状态分布",
                     color="acwr_status",
                     color_discrete_map={
                         "🔴 负荷过高": "#ff6b6b", "🟡 警戒": "#feca57",
                         "🟢 理想": "#2ed573", "🔵 负荷不足": "#48dbfb"
                     })
        st.plotly_chart(fig, use_container_width=True)

    with col2:
        # 单调性 + 训练张力
        fig2 = make_subplots(specs=[[{"secondary_y": True}]])
        fig2.add_trace(go.Bar(x=df["label"], y=df["monotony"], name="单调性",
                               marker_color="#54a0ff"), secondary_y=False)
        fig2.add_trace(go.Scatter(x=df["label"], y=df["strain"], mode="lines+markers",
                                   name="训练张力", line=dict(color="#ff9f43", width=2)),
                        secondary_y=True)
        fig2.add_hline(y=2.0, line_dash="dash", line_color="red", annotation_text="单调上限2.0")
        fig2.update_layout(title="单调性(TM) & 训练张力(TS)", template="plotly_dark")
        st.plotly_chart(fig2, use_container_width=True)

    # 最近5周详情
    st.subheader("📋 最近5周详细数据")
    recent = df.tail(5).copy()
    display = recent[["label", "total_load", "avg_daily_load", "session_count", "acwr", "monotony", "strain"]]
    display.columns = ["周", "总负荷", "日均负荷", "训练次数", "ACWR", "单调性", "训练张力"]
    display["ACWR状态"] = recent["acwr_status"]
    st.dataframe(display, use_container_width=True, hide_index=True)

    # 原始训练记录
    st.subheader("📋 全部训练记录")
    sessions = get_all_sessions_with_exercises(aid, 200)
    if sessions:
        records = []
        for s in sessions:
            exs = s.get("exercises", [])
            ex_names = ", ".join([e["exercise_name"] for e in exs[:5]])
            if len(exs) > 5:
                ex_names += f"... (+{len(exs)-5})"
            records.append({
                "日期": s["date"], "时段": s["period"],
                "RPE": s["rpe"], "时长": f'{s["duration_min"]}min',
                "负荷(AU)": s["rpe"] * s["duration_min"],
                "动作": ex_names,
                "阶段": s.get("phase", ""),
            })
        df_rec = pd.DataFrame(records)
        st.dataframe(df_rec, use_container_width=True, hide_index=True)

# =========================================================
# Page: 动作库
# =========================================================
def page_library():
    st.subheader("📚 动作库")
    exercises = get_exercise_library()

    if exercises:
        df = pd.DataFrame(exercises)
        df_disp = df[["name", "category", "subcategory", "notes"]]
        df_disp.columns = ["动作名称", "分类", "子分类", "备注"]
        st.dataframe(df_disp, use_container_width=True, hide_index=True)
    else:
        st.info("动作库为空，请到「导入数据」页面初始化")

    with st.expander("➕ 手动添加动作", expanded=False):
        with st.form("add_exercise"):
            ex_name = st.text_input("动作名称 *")
            ex_cat = st.selectbox("分类", ["", "上肢", "下肢", "全身", "增强式", "活动度", "举重衍生"])
            ex_sub = st.text_input("子分类", placeholder="如: 推/拉/蹲")
            ex_notes = st.text_area("备注")
            if st.form_submit_button("添加", use_container_width=True) and ex_name:
                sb = get_supabase()
                try:
                    sb.table("exercise_library").insert({
                        "name": ex_name, "category": ex_cat,
                        "subcategory": ex_sub, "notes": ex_notes
                    }).execute()
                    st.success("已添加")
                    st.rerun()
                except Exception as e:
                    if "duplicate" in str(e).lower():
                        st.warning("动作已存在")
                    else:
                        st.error(str(e))

# =========================================================
# Page: 运动员管理
# =========================================================
def page_athletes():
    st.subheader("🏃 运动员管理")
    athletes = get_all_athletes()

    # 已有运动员列表
    if athletes:
        df = pd.DataFrame(athletes)
        cols = ["name", "goal", "current_touch_cm", "target_touch_cm", "clean_2rm", "squat_2rm"]
        df_disp = df[cols].copy()
        df_disp.columns = ["姓名", "目标", "当前摸高(cm)", "目标摸高(cm)", "高翻2RM", "后蹲2RM"]
        st.dataframe(df_disp, use_container_width=True, hide_index=True)
    else:
        st.info("暂无运动员")

    # 添加新运动员
    with st.expander("➕ 添加新运动员", expanded=not bool(athletes)):
        with st.form("add_athlete"):
            col1, col2 = st.columns(2)
            with col1:
                new_name = st.text_input("姓名 *", placeholder="如: 张三")
                new_goal = st.text_input("目标", placeholder="如: 摸高300cm")
            with col2:
                new_touch = st.number_input("当前摸高(cm)", 0.0, 400.0, 0.0, step=1.0)
                new_target = st.number_input("目标摸高(cm)", 0.0, 400.0, 0.0, step=1.0)
            if st.form_submit_button("添加", type="primary", use_container_width=True) and new_name:
                a, err = create_athlete(new_name, new_goal, new_touch, new_target)
                if a:
                    st.success(f"已添加「{new_name}」")
                    st.rerun()
                else:
                    st.error(err)

    # 编辑当前运动员信息
    if athletes:
        st.markdown("---")
        st.subheader("✏️ 编辑运动员信息")
        aid, aname = get_active_athlete()
        a = next((x for x in athletes if x["id"] == aid), None)
        if a:
            with st.form("edit_athlete"):
                col1, col2 = st.columns(2)
                with col1:
                    e_name = st.text_input("姓名", value=a["name"])
                    e_goal = st.text_input("目标", value=a.get("goal", ""))
                    e_touch = st.number_input("当前摸高(cm)", value=float(a.get("current_touch_cm", 0)), step=1.0)
                    e_target = st.number_input("目标摸高(cm)", value=float(a.get("target_touch_cm", 0)), step=1.0)
                with col2:
                    e_clean = st.number_input("高翻2RM(kg)", value=float(a.get("clean_2rm", 0)), step=1.0)
                    e_squat = st.number_input("后蹲2RM(kg)", value=float(a.get("squat_2rm", 0)), step=1.0)
                    e_weight = st.number_input("体重(kg)", value=float(a.get("body_weight", 78)), step=1.0)
                    e_bf = st.number_input("体脂率(%)", value=float(a.get("body_fat", 0)), step=0.1)
                e_notes = st.text_area("备注", value=a.get("notes", ""))
                if st.form_submit_button("💾 保存修改", type="primary", use_container_width=True):
                    update_athlete(aid, name=e_name, goal=e_goal,
                        current_touch_cm=e_touch, target_touch_cm=e_target,
                        clean_2rm=e_clean, squat_2rm=e_squat,
                        body_weight=e_weight, body_fat=e_bf, notes=e_notes)
                    st.success("已保存")
                    st.rerun()

# =========================================================
# Main App
# =========================================================
st.set_page_config(page_title="运动负荷监控", layout="wide")
st.title("🏋️ 运动负荷监控系统")

if not SUPABASE_URL or not SUPABASE_KEY:
    st.error("❌ 未配置Supabase连接信息")
    st.stop()

# 侧边栏：运动员选择
athletes_list = get_all_athletes()
if athletes_list:
    athlete_names = [a["name"] for a in athletes_list]
    current_id, current_name = get_active_athlete()
    default_idx = next((i for i, a in enumerate(athletes_list) if a["id"] == current_id), 0)
    sel_name = st.sidebar.selectbox("🏃 运动员", athlete_names, index=default_idx)
    sel_athlete = next(a for a in athletes_list if a["name"] == sel_name)
    if sel_athlete["id"] != st.session_state.get("athlete_id"):
        st.session_state.athlete_id = sel_athlete["id"]
        st.session_state.athlete_name = sel_athlete["name"]
        st.rerun()

menu = st.sidebar.radio("导航", [
    "📊 总览看板", "📝 训练录入", "📤 导入数据",
    "📈 负荷分析", "📚 动作库", "🏃 运动员管理"
])

if menu == "📊 总览看板":
    page_dashboard()
elif menu == "📝 训练录入":
    page_entry()
elif menu == "📤 导入数据":
    page_import()
elif menu == "📈 负荷分析":
    page_analysis()
elif menu == "📚 动作库":
    page_library()
elif menu == "🏃 运动员管理":
    page_athletes()

st.sidebar.markdown("---")
st.sidebar.caption(f"当前: {st.session_state.get('athlete_name', '陈伟雄')}")
st.sidebar.caption(f"数据源: Supabase Cloud")
