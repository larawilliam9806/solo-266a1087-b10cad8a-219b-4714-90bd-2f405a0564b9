"""SQLite 连接与建表。底表(课程/学分/前置课) + 成绩单 + 核定底表。"""
import os
import sqlite3

from flask import current_app, g

SCHEMA = """
-- 底表：课程与学分
CREATE TABLE IF NOT EXISTS courses (
    course_id TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    credits   REAL NOT NULL CHECK (credits >= 0)
);

-- 底表：必修前置课
CREATE TABLE IF NOT EXISTS prerequisites (
    course_id        TEXT NOT NULL REFERENCES courses(course_id),
    prereq_course_id TEXT NOT NULL REFERENCES courses(course_id),
    PRIMARY KEY (course_id, prereq_course_id)
);

-- 学生成绩单（含已撤销，留痕）
CREATE TABLE IF NOT EXISTS records (
    record_id  TEXT PRIMARY KEY,            -- 单号全局唯一
    student_id TEXT NOT NULL,
    course_id  TEXT NOT NULL REFERENCES courses(course_id),
    start_date TEXT NOT NULL,               -- 开课日 YYYY-MM-DD
    end_date   TEXT NOT NULL,               -- 结课日 YYYY-MM-DD
    passed     INTEGER NOT NULL,            -- 是否通过 1/0
    status     TEXT NOT NULL DEFAULT 'active',  -- active / revoked
    batch_id   TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_student ON records(student_id);

-- 核定底表：学生总学分（查询直接读这里）
CREATE TABLE IF NOT EXISTS student_credits (
    student_id    TEXT PRIMARY KEY,
    total_credits REAL NOT NULL,
    updated_at    TEXT NOT NULL
);

-- 核定底表：已认定课程（同一生同一课只计一次）
CREATE TABLE IF NOT EXISTS course_credits (
    student_id       TEXT NOT NULL,
    course_id        TEXT NOT NULL,
    source_record_id TEXT NOT NULL,         -- 计分所依据的成绩单号
    PRIMARY KEY (student_id, course_id)
);

-- 核定底表：逐单核对结果（合规性 + 前置课依据单号/缺项）
CREATE TABLE IF NOT EXISTS record_audit (
    record_id  TEXT PRIMARY KEY,
    student_id TEXT NOT NULL,
    course_id  TEXT NOT NULL,
    passed     INTEGER NOT NULL,
    compliant  INTEGER NOT NULL,            -- 修读是否合规
    counted    INTEGER NOT NULL,            -- 是否计入学分
    evidence   TEXT NOT NULL,               -- JSON: {前置课号: 依据单号或null}
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_student ON record_audit(student_id);
"""


def connect(db_path):
    """独立连接（自动提交模式，事务由 BEGIN/COMMIT 显式控制）。"""
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def get_db():
    """请求内共享连接。"""
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def rollback_quietly(db):
    try:
        db.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def init_db_file(db_path):
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
