"""核心规则测试：合规判定、计分唯一、更正/撤销重核、失败沿用原账。"""
import pytest

from app import create_app

BASE = {
    "courses": [
        {"course_id": "A", "name": "课程A", "credits": 2},
        {"course_id": "B", "name": "课程B", "credits": 3},
        {"course_id": "C", "name": "课程C", "credits": 4},
    ],
    "prerequisites": [
        {"course_id": "B", "prereq_course_id": "A"},
        {"course_id": "C", "prereq_course_id": "B"},
    ],
}


@pytest.fixture()
def client(tmp_path):
    app = create_app({
        "TESTING": True,
        "DATABASE": str(tmp_path / "test.db"),
        "BASE_DATA_FILE": "",          # 测试不自动播种，走导入接口
        "SECRET_KEY": "test",
    })
    with app.test_client() as c:
        r = c.post("/api/admin/base", json=BASE)
        assert r.status_code == 200, r.get_json()
        yield c


def upload(client, student_id, records):
    return client.post("/api/transcripts", json={"student_id": student_id, "records": records})


def rec(rid, course, start, end, passed=True):
    return {"record_id": rid, "course_id": course, "start_date": start, "end_date": end, "passed": passed}


def snap(client, sid):
    return client.get(f"/api/students/{sid}").get_json()


def audit_of(snapshot, rid):
    return next(r for r in snapshot["records"] if r["record_id"] == rid)


# ---------------------------------------------------------------- 合规与计分

def test_chain_compliant_and_total(client):
    r = upload(client, "S1", [
        rec("R1", "A", "2022-01-01", "2022-06-01"),
        rec("R2", "B", "2022-09-01", "2023-01-01"),
        rec("R3", "C", "2023-03-01", "2023-07-01"),
    ])
    assert r.status_code == 200
    s = snap(client, "S1")
    assert s["total_credits"] == 9
    assert {c["course_id"] for c in s["credited_courses"]} == {"A", "B", "C"}
    # 逐单前置课依据单号
    ev_b = audit_of(s, "R2")["evidence"]
    assert ev_b == [{"prereq_course_id": "A", "prereq_name": "课程A", "record_id": "R1", "missing": False}]
    ev_c = audit_of(s, "R3")["evidence"]
    assert ev_c[0]["record_id"] == "R2"


def test_missing_prereq_is_flagged(client):
    upload(client, "S1", [rec("R2", "B", "2022-09-01", "2023-01-01")])
    s = snap(client, "S1")
    row = audit_of(s, "R2")
    assert row["compliant"] is False
    assert row["counted"] is False
    assert row["evidence"][0]["missing"] is True
    assert row["evidence"][0]["record_id"] is None
    assert s["total_credits"] == 0


def test_prereq_must_end_before_start(client):
    # 前置课结课日 == 开课日：不满足“开课前已结课”
    upload(client, "S1", [
        rec("R1", "A", "2022-01-01", "2022-09-01"),
        rec("R2", "B", "2022-09-01", "2023-01-01"),
    ])
    assert audit_of(snap(client, "S1"), "R2")["compliant"] is False


def test_failed_prereq_not_usable(client):
    upload(client, "S1", [
        rec("R1", "A", "2022-01-01", "2022-06-01", passed=False),
        rec("R2", "B", "2022-09-01", "2023-01-01"),
    ])
    s = snap(client, "S1")
    assert audit_of(s, "R2")["compliant"] is False
    assert s["total_credits"] == 0  # A 未通过也不计分


def test_evidence_record_must_itself_be_compliant(client):
    # 再造一条底表：A 需要前置课 P（无成绩），则 A 不合规，B 也不能拿 A 当依据
    client.post("/api/admin/base", json={
        "courses": [{"course_id": "P", "name": "课程P", "credits": 1}],
        "prerequisites": [{"course_id": "A", "prereq_course_id": "P"}],
    })
    upload(client, "S1", [
        rec("R1", "A", "2022-01-01", "2022-06-01"),
        rec("R2", "B", "2022-09-01", "2023-01-01"),
    ])
    s = snap(client, "S1")
    assert audit_of(s, "R1")["compliant"] is False
    assert audit_of(s, "R2")["compliant"] is False
    assert s["total_credits"] == 0


def test_same_course_counted_once(client):
    upload(client, "S1", [
        rec("R1", "A", "2022-01-01", "2022-06-01"),
        rec("R2", "A", "2022-07-01", "2022-12-01"),
    ])
    s = snap(client, "S1")
    assert s["total_credits"] == 2
    assert len(s["credited_courses"]) == 1
    assert s["credited_courses"][0]["source_record_id"] == "R1"  # 结课最早者计分


def test_failed_course_not_counted_but_compliant(client):
    upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01", passed=False)])
    s = snap(client, "S1")
    row = audit_of(s, "R1")
    assert row["compliant"] is True and row["counted"] is False
    assert s["total_credits"] == 0


# ---------------------------------------------------------------- 更正与撤销

def test_correction_by_record_id(client):
    upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    assert snap(client, "S1")["total_credits"] == 2
    r = upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01", passed=False)])  # 同号更正
    assert r.status_code == 200
    assert r.get_json()["corrected"] == 1
    assert snap(client, "S1")["total_credits"] == 0


def test_correction_rejects_other_student_and_revoked(client):
    upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    r = upload(client, "S2", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    assert r.status_code == 400
    client.post("/api/records/R1/revoke")
    r = upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    assert r.status_code == 400


def test_revoke_with_substitute_updates_evidence(client):
    upload(client, "S1", [
        rec("R1", "A", "2022-01-01", "2022-06-01"),
        rec("R1b", "A", "2022-02-01", "2022-07-01"),   # 合规旧单，可替代
        rec("R2", "B", "2022-09-01", "2023-01-01"),
    ])
    s = snap(client, "S1")
    assert audit_of(s, "R2")["evidence"][0]["record_id"] == "R1"
    r = client.post("/api/records/R1/revoke")
    assert r.status_code == 200
    s = snap(client, "S1")
    # 有合规旧单替代：依据单号更新，学分不变
    assert audit_of(s, "R2")["evidence"][0]["record_id"] == "R1b"
    assert audit_of(s, "R2")["compliant"] is True
    assert s["total_credits"] == 5


def test_revoke_cascades_to_dependents(client):
    upload(client, "S1", [
        rec("R1", "A", "2022-01-01", "2022-06-01"),
        rec("R2", "B", "2022-09-01", "2023-01-01"),
        rec("R3", "C", "2023-03-01", "2023-07-01"),
    ])
    assert snap(client, "S1")["total_credits"] == 9
    client.post("/api/records/R1/revoke")
    s = snap(client, "S1")
    assert audit_of(s, "R2")["compliant"] is False   # 依据缺失
    assert audit_of(s, "R3")["compliant"] is False   # 连锁：B 的依据不合规
    assert s["total_credits"] == 0
    # 补交新 A 单后逐级恢复，且依据单号更新为新单
    upload(client, "S1", [rec("R9", "A", "2022-01-01", "2022-06-01")])
    s = snap(client, "S1")
    assert audit_of(s, "R2")["evidence"][0]["record_id"] == "R9"
    assert audit_of(s, "R3")["compliant"] is True
    assert s["total_credits"] == 9


def test_revoke_twice_conflict(client):
    upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    assert client.post("/api/records/R1/revoke").status_code == 200
    assert client.post("/api/records/R1/revoke").status_code == 409
    assert client.post("/api/records/NOPE/revoke").status_code == 404


# ---------------------------------------------------------------- 事务与校验

def test_failed_upload_keeps_original_ledger(client):
    upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    before = snap(client, "S1")
    assert before["total_credits"] == 2
    # 批内一条坏单（结课日早于开课日）→ 整批不入账
    r = upload(client, "S1", [
        rec("R2", "B", "2022-09-01", "2023-01-01"),
        rec("R3", "C", "2023-07-01", "2023-03-01"),
    ])
    assert r.status_code == 400
    after = snap(client, "S1")
    assert after["total_credits"] == 2
    assert [x["record_id"] for x in after["records"]] == ["R1"]


def test_unknown_course_rejected(client):
    r = upload(client, "S1", [rec("R1", "ZZZ", "2022-01-01", "2022-06-01")])
    assert r.status_code == 400
    assert snap(client, "S1")["records"] == []


def test_bad_date_rejected(client):
    r = upload(client, "S1", [rec("R1", "A", "2022-13-01", "2022-06-01")])
    assert r.status_code == 400


def test_duplicate_record_id_in_batch_rejected(client):
    r = upload(client, "S1", [
        rec("R1", "A", "2022-01-01", "2022-06-01"),
        rec("R1", "A", "2022-01-01", "2022-06-01"),
    ])
    assert r.status_code == 400


def test_student_isolation(client):
    upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    upload(client, "S2", [rec("R2", "B", "2022-09-01", "2023-01-01")])
    # 补交只复核该生：S2 不能用 S1 的成绩作前置依据
    s2 = snap(client, "S2")
    assert audit_of(s2, "R2")["compliant"] is False
    assert s2["total_credits"] == 0
    assert snap(client, "S1")["total_credits"] == 2


def test_query_reads_committed_ledger(client):
    upload(client, "S1", [rec("R1", "A", "2022-01-01", "2022-06-01")])
    r = client.get("/api/students/S1")
    assert r.status_code == 200
    body = r.get_json()
    assert body["total_credits"] == 2
    assert body["ledger_updated_at"] is not None
    # 网页查询可渲染
    assert client.get("/students/S1").status_code == 200
    assert client.get("/").status_code == 200
