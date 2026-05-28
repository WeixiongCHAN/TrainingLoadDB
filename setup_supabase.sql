-- =========================================
-- 运动负荷监控 — Supabase 初始化SQL
-- 运行: Supabase Dashboard → SQL Editor → 粘贴运行
-- 幂等: 跑多少次都不会报错
-- =========================================

-- 1. 运动员表
CREATE TABLE IF NOT EXISTS athletes (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '陈伟雄',
    goal TEXT DEFAULT '摸高315cm',
    current_touch_cm REAL DEFAULT 295,
    target_touch_cm REAL DEFAULT 315,
    clean_2rm REAL DEFAULT 80,
    squat_2rm REAL DEFAULT 130,
    body_fat REAL DEFAULT 0,
    body_weight REAL DEFAULT 78,
    notes TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. 训练课表 (核心)
CREATE TABLE IF NOT EXISTS sessions (
    id BIGSERIAL PRIMARY KEY,
    athlete_id BIGINT NOT NULL REFERENCES athletes(id) ON DELETE CASCADE,
    date DATE NOT NULL,
    period TEXT NOT NULL CHECK(period IN ('早上','上午','下午','晚上')),
    rpe REAL NOT NULL CHECK(rpe >= 0 AND rpe <= 10),
    duration_min INTEGER NOT NULL DEFAULT 0,
    phase TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 3. 训练动作明细 (每个训练课有N个动作)
CREATE TABLE IF NOT EXISTS session_exercises (
    id BIGSERIAL PRIMARY KEY,
    session_id BIGINT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    exercise_name TEXT NOT NULL,
    sets INTEGER DEFAULT 0,
    reps TEXT DEFAULT '',
    intensity TEXT DEFAULT '',
    rest_min REAL DEFAULT 0,
    actual_completion TEXT DEFAULT '',
    sort_order INTEGER DEFAULT 0
);

-- 4. 周负荷统计 (自动计算)
CREATE TABLE IF NOT EXISTS weekly_loads (
    id BIGSERIAL PRIMARY KEY,
    athlete_id BIGINT NOT NULL REFERENCES athletes(id) ON DELETE CASCADE,
    week_start DATE NOT NULL,
    total_load REAL DEFAULT 0,
    avg_daily_load REAL DEFAULT 0,
    session_count INTEGER DEFAULT 0,
    acute_load REAL DEFAULT 0,
    chronic_load REAL DEFAULT 0,
    acwr REAL DEFAULT 1.0,
    monotony REAL DEFAULT 1.0,
    strain REAL DEFAULT 0,
    UNIQUE(athlete_id, week_start)
);

-- 5. 阶段性测试
CREATE TABLE IF NOT EXISTS performance_tests (
    id BIGSERIAL PRIMARY KEY,
    athlete_id BIGINT NOT NULL REFERENCES athletes(id) ON DELETE CASCADE,
    date DATE NOT NULL,
    touch_height_cm REAL,
    cmj_height REAL,
    sprint_10m REAL,
    sprint_30m REAL,
    notes TEXT DEFAULT '',
    UNIQUE(athlete_id, date)
);

-- 6. 动作库
CREATE TABLE IF NOT EXISTS exercise_library (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    category TEXT DEFAULT '',     -- 上肢/下肢/全身
    subcategory TEXT DEFAULT '',  -- 推/拉/蹲/举重衍生/增强式/活动度
    notes TEXT DEFAULT ''
);

-- 7. 训练阶段/周期
CREATE TABLE IF NOT EXISTS training_phases (
    id BIGSERIAL PRIMARY KEY,
    athlete_id BIGINT NOT NULL REFERENCES athletes(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE,
    focus TEXT DEFAULT '',
    notes TEXT DEFAULT ''
);

-- =========================================
-- 索引
-- =========================================
CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(date);
CREATE INDEX IF NOT EXISTS idx_sessions_athlete ON sessions(athlete_id);
CREATE INDEX IF NOT EXISTS idx_exercises_session ON session_exercises(session_id);
CREATE INDEX IF NOT EXISTS idx_weekly_loads_athlete ON weekly_loads(athlete_id);
CREATE INDEX IF NOT EXISTS idx_weekly_loads_week ON weekly_loads(week_start);

-- =========================================
-- RLS (幂等)
-- =========================================
ALTER TABLE athletes ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE session_exercises ENABLE ROW LEVEL SECURITY;
ALTER TABLE weekly_loads ENABLE ROW LEVEL SECURITY;
ALTER TABLE performance_tests ENABLE ROW LEVEL SECURITY;
ALTER TABLE exercise_library ENABLE ROW LEVEL SECURITY;
ALTER TABLE training_phases ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "允许所有人查看" ON athletes;
CREATE POLICY "允许所有人查看" ON athletes FOR SELECT USING (true);
DROP POLICY IF EXISTS "允许所有人查看" ON sessions;
CREATE POLICY "允许所有人查看" ON sessions FOR SELECT USING (true);
DROP POLICY IF EXISTS "允许所有人查看" ON session_exercises;
CREATE POLICY "允许所有人查看" ON session_exercises FOR SELECT USING (true);
DROP POLICY IF EXISTS "允许所有人查看" ON weekly_loads;
CREATE POLICY "允许所有人查看" ON weekly_loads FOR SELECT USING (true);
DROP POLICY IF EXISTS "允许所有人查看" ON performance_tests;
CREATE POLICY "允许所有人查看" ON performance_tests FOR SELECT USING (true);
DROP POLICY IF EXISTS "允许所有人查看" ON exercise_library;
CREATE POLICY "允许所有人查看" ON exercise_library FOR SELECT USING (true);
DROP POLICY IF EXISTS "允许所有人查看" ON training_phases;
CREATE POLICY "允许所有人查看" ON training_phases FOR SELECT USING (true);

DROP POLICY IF EXISTS "仅服务端写入" ON athletes;
CREATE POLICY "仅服务端写入" ON athletes FOR ALL USING (false);
DROP POLICY IF EXISTS "仅服务端写入" ON sessions;
CREATE POLICY "仅服务端写入" ON sessions FOR ALL USING (false);
DROP POLICY IF EXISTS "仅服务端写入" ON session_exercises;
CREATE POLICY "仅服务端写入" ON session_exercises FOR ALL USING (false);
DROP POLICY IF EXISTS "仅服务端写入" ON weekly_loads;
CREATE POLICY "仅服务端写入" ON weekly_loads FOR ALL USING (false);
DROP POLICY IF EXISTS "仅服务端写入" ON performance_tests;
CREATE POLICY "仅服务端写入" ON performance_tests FOR ALL USING (false);
DROP POLICY IF EXISTS "仅服务端写入" ON exercise_library;
CREATE POLICY "仅服务端写入" ON exercise_library FOR ALL USING (false);
DROP POLICY IF EXISTS "仅服务端写入" ON training_phases;
CREATE POLICY "仅服务端写入" ON training_phases FOR ALL USING (false);
