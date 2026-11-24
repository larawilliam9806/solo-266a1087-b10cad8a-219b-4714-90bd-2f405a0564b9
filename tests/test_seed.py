"""启动播种（默认底表自动导入）的并发与失败语义测试。

场景：多实例共用同一 SQLite 库。
- 读默认文件期间，教务员可在另一实例导入底表并补录成绩；
- 已有底表先提交时，晚到的自动导入整批跳过，新实例沿用现行底表（坏文件也一样）；
- 库仍为空时默认导入失败要报错并回滚，修好文件后可重试。
"""
import json

import pytest

from app import _seed_base_if_empty, create_app
from app import db as dbmod
from app.logic import apply_transcript_batch, import_base_data

# 默认文件（内容陈旧：分值/名称与现行底表不同，用于检验是否被覆盖）
DEFAULT_BASE = {
    "courses": [
        {"course_id": "X", "name": "默认X", "credits": 99},
        {"course_id": "Y", "name": "默认Y", "credits": 88},
    ],
    "prerequisites": [{"course_id": "Y", "prereq_course_id": "X"}],
}

# 教务员导入的现行底表
ADMIN_BASE = {
    "courses": [
        {"course_id": "X", "name": "课程X", "credits": 2},
        {"course_id": "Y", "name": "课程Y", "credits": 3},
    ],
    "prerequisites": [{"course_id": "Y", "prereq_course_id": "X"}],
}

TRANSCRIPT = {
    "student_id": "S1",
    "records": [
        {"record_id": "R1", "course_id": "X",
         "start_date": "2022-01-01", "end_date": "2022-06-01", "passed": True},
        {"record_id": "R2", "course_id": "Y",
         "start_date": "2022-09-01", "end_date": "2023-01-01", "passed": True},
    ],
}


def make_app(db_path, base_file):
    return create_app({
        "TESTING": True,
        "DATABASE": str(db_path),
        "BASE_DATA_FILE": str(base_file),
        "SECRET_KEY": "test",
    })


def write_base_file(tmp_path, payload=None, raw=None):
    p = tmp_path / "base.json"
    p.write_text(raw if raw is not None else json.dumps(payload), encoding="utf-8")
    return p


def courses_of(db_path):
    conn = dbmod.connect(str(db_path))
    try:
        return {r["course_id"]: dict(r) for r in conn.execute("SELECT * FROM courses")}
    finally:
        conn.close()


def ledger_total(db_path, student_id):
    conn = dbmod.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT total_credits FROM student_credits WHERE student_id = ?", (student_id,)
        ).fetchone()
        return row["total_credits"] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------- 正常播种

def test_seed_imports_default_file_into_empty_db(tmp_path):
    base_file = write_base_file(tmp_path, DEFAULT_BASE)
    app = make_app(tmp_path / "app.db", base_file)
    courses = courses_of(tmp_path / "app.db")
    assert courses["X"]["credits"] == 99
    assert courses["Y"]["name"] == "默认Y"
    with app.test_client() as c:
        assert c.get("/api/health").status_code == 200


def test_seed_disabled_without_base_file(tmp_path):
    app = make_app(tmp_path / "app.db", "")
    assert courses_of(tmp_path / "app.db") == {}
    with app.test_client() as c:  # 教务仍可经 API 导入底表
        assert c.post("/api/admin/base", json=ADMIN_BASE).status_code == 200
    assert courses_of(tmp_path / "app.db")["X"]["credits"] == 2


# ---------------------------------------------------------------- 晚到的自动导入

def test_second_instance_skips_seed_and_uses_current_base(tmp_path):
    db_path = tmp_path / "app.db"
    # 第一个实例：教务导入现行底表并补录成绩
    app1 = make_app(db_path, "")
    with app1.test_client() as c:
        assert c.post("/api/admin/base", json=ADMIN_BASE).status_code == 200
        assert c.post("/api/transcripts", json=TRANSCRIPT).status_code == 200
        assert c.get("/api/students/S1").get_json()["total_credits"] == 5

    # 第二个实例：默认文件内容陈旧，启动时自动导入须整批跳过
    base_file = write_base_file(tmp_path, DEFAULT_BASE)
    app2 = make_app(db_path, base_file)
    courses = courses_of(db_path)
    assert courses["X"]["credits"] == 2      # 分值未被默认文件改写
    assert courses["X"]["name"] == "课程X"   # 名称也未被改写（整批未动）
    assert courses["Y"]["credits"] == 3

    # 学号查询：课程明细与核定结果一致
    with app2.test_client() as c:
        s = c.get("/api/students/S1").get_json()
    assert s["total_credits"] == 5
    assert {c["course_id"]: c["credits"] for c in s["credited_courses"]} == {"X": 2, "Y": 3}


def test_seed_committed_during_file_read_is_skipped(tmp_path, monkeypatch):
    """复现竞态：本实例读默认文件期间，另一实例导入底表并补录成绩。

    若播种在读文件时持有写锁，模拟并发导入的 BEGIN IMMEDIATE 会超时失败；
    测试通过即证明读文件不占锁，且迟到导入在写事务内复查后整批跳过。
    """
    db_path = tmp_path / "app.db"
    base_file = write_base_file(tmp_path, DEFAULT_BASE)

    real_load = json.load

    def concurrent_import(f):
        payload = real_load(f)
        conn = dbmod.connect(str(db_path))  # 另一实例的连接
        try:
            conn.execute("BEGIN IMMEDIATE")
            import_base_data(conn, ADMIN_BASE)
            apply_transcript_batch(conn, TRANSCRIPT, batch_id="other-instance")
            conn.execute("COMMIT")
        finally:
            conn.close()
        return payload

    monkeypatch.setattr(json, "load", concurrent_import)
    app = make_app(db_path, base_file)

    courses = courses_of(db_path)
    assert courses["X"]["credits"] == 2      # 现行底表未被迟到的自动导入改写
    assert courses["X"]["name"] == "课程X"
    assert courses["Y"]["credits"] == 3
    assert ledger_total(db_path, "S1") == 5  # 核定底表与课程明细一致
    with app.test_client() as c:
        s = c.get("/api/students/S1").get_json()
    assert s["total_credits"] == 5
    assert {c["course_id"]: c["credits"] for c in s["credited_courses"]} == {"X": 2, "Y": 3}


def test_seed_function_returns_skipped_when_base_exists(tmp_path):
    db_path = tmp_path / "app.db"
    dbmod.init_db_file(str(db_path))
    conn = dbmod.connect(str(db_path))
    try:
        conn.execute("BEGIN IMMEDIATE")
        import_base_data(conn, ADMIN_BASE)
        conn.execute("COMMIT")
    finally:
        conn.close()
    base_file = write_base_file(tmp_path, DEFAULT_BASE)
    assert _seed_base_if_empty(str(db_path), str(base_file)) == "skipped"
    assert courses_of(db_path)["X"]["credits"] == 2


# ---------------------------------------------------------------- 坏文件

def test_corrupt_default_file_skipped_when_base_already_exists(tmp_path):
    db_path = tmp_path / "app.db"
    app1 = make_app(db_path, "")
    with app1.test_client() as c:
        assert c.post("/api/admin/base", json=ADMIN_BASE).status_code == 200

    bad_file = write_base_file(tmp_path, raw="{not json")
    app2 = make_app(db_path, bad_file)  # 坏文件不再重要：沿用现行底表启动
    assert courses_of(db_path)["X"]["credits"] == 2
    with app2.test_client() as c:
        assert c.get("/api/health").status_code == 200


def test_corrupt_default_file_with_empty_db_raises_and_retry_works(tmp_path):
    db_path = tmp_path / "app.db"
    bad_file = write_base_file(tmp_path, raw="{not json")
    with pytest.raises(Exception):
        make_app(db_path, bad_file)
    assert courses_of(db_path) == {}  # 已回滚，库仍为空

    # 修好文件后可重试
    write_base_file(tmp_path, ADMIN_BASE)
    app = make_app(db_path, bad_file)
    assert courses_of(db_path)["X"]["credits"] == 2
    with app.test_client() as c:
        assert c.get("/api/health").status_code == 200


def test_invalid_content_with_empty_db_raises_and_rolls_back(tmp_path):
    db_path = tmp_path / "app.db"
    bad_file = write_base_file(tmp_path, {"courses": []})  # JSON 合法但内容不合规
    with pytest.raises(Exception):
        make_app(db_path, bad_file)
    assert courses_of(db_path) == {}  # 整批回滚，不留半截底表

    write_base_file(tmp_path, ADMIN_BASE)  # 修复内容后重试成功
    make_app(db_path, bad_file)
    assert courses_of(db_path)["X"]["credits"] == 2


def test_missing_default_file_with_empty_db_raises(tmp_path):
    with pytest.raises(Exception):
        make_app(tmp_path / "app.db", tmp_path / "no-such-file.json")
