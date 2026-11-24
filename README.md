# 旧成绩补交 · 学分认定核对系统

教务员补交旧成绩时，用本系统核对哪些学分能认定。Flask 网页 + SQLite 底表，
`docker compose` 一键启动。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:5000
```

首次启动自动从 `data/base_data.json` **一次性导入课程、学分与必修前置课底表**
（仅当课程表为空时导入；数据库文件持久化在 `app_data` 卷中）。

本地开发（无需 Docker）：

```bash
pip install -r requirements-dev.txt
python run.py          # http://localhost:5000
python -m pytest tests/ -v
```

## 业务规则

- **修读合规**：该生的每门必修前置课，都存在一张**开课前已结课**（前置课结课日
  严格早于本课开课日）、**已通过**、且**本身也修读合规**的成绩单作为依据。
- **计学分**：合规且通过才计；同一学生同一门课只计一次（取结课最早、单号最小者）。
- **更正**：按原单号重新上传即更正（已撤销的单号不可更正）。
- **撤销**：按单号撤销后只复核该生——有合规旧单可替代则依据单号自动更新；
  否则受影响的后续课逐级重核为不合规，学分相应回收。
- **入账**：成绩与核定底表（总学分 / 已认定课程 / 逐单核对结果）在同一事务写入；
  计算或保存失败整体回滚，**沿用原账**。查询只读已生效的核定底表，不做实时重算。
- **复核范围**：补交、更正、撤销均只重核该学生；底表变更才在同事务内重核全部学生。

## 网页

| 路径 | 说明 |
|---|---|
| `/` | 按学号查询入口 |
| `/students/<学号>` | 总学分、已认定课程、逐单前置课依据单号/缺项、撤销按钮 |
| `/upload` | 粘贴或选择文件，按学生分批上传 JSON 成绩单 |
| `/admin/base` | 查看/导入课程、学分、必修前置课底表 |

## JSON API

### 导入底表（一次性）

```bash
curl -X POST localhost:5000/api/admin/base -H 'Content-Type: application/json' -d @data/base_data.json
```

```json
{
  "courses": [{"course_id": "CS101", "name": "程序设计基础", "credits": 3}],
  "prerequisites": [{"course_id": "CS201", "prereq_course_id": "CS101"}]
}
```

### 分批上传成绩单（同号即更正）

```bash
curl -X POST localhost:5000/api/transcripts -H 'Content-Type: application/json' -d @examples/transcript_S001.json
```

```json
{
  "student_id": "S001",
  "records": [
    {"record_id": "R1001", "course_id": "CS101",
     "start_date": "2022-09-01", "end_date": "2023-01-10", "passed": true}
  ]
}
```

每单含：单号 `record_id`（全局唯一）、课程 `course_id`、开课日 `start_date`、
结课日 `end_date`（YYYY-MM-DD，且不早于开课日）、是否通过 `passed`。
整批校验+复核任一失败则整批不入账。

### 按单号撤销

```bash
curl -X POST localhost:5000/api/records/R1001/revoke
```

### 按学号查询（直接读核定底表）

```bash
curl localhost:5000/api/students/S001
```

返回总学分、已认定课程（含计分依据单号）、逐单合规/计分状态及每门前置课的
依据单号或缺项标记。

## 演示流程

```bash
# S001：五门课链条合规 → 18 学分
curl -X POST localhost:5000/api/transcripts -H 'Content-Type: application/json' -d @examples/transcript_S001.json
# S002：CS201 修读时 CS101 尚未开课 → 不合规缺项，只认 CS101 的 3 学分
curl -X POST localhost:5000/api/transcripts -H 'Content-Type: application/json' -d @examples/transcript_S002.json
# 撤销 S001 的 CS101 → CS201/CS301 连锁不合规，18 → 8
curl -X POST localhost:5000/api/records/R1001/revoke
# 补交替代 CS101 单 → 依据单号自动更新为新单，链条恢复回 18
curl -X POST localhost:5000/api/transcripts -H 'Content-Type: application/json' -d \
  '{"student_id":"S001","records":[{"record_id":"R1006","course_id":"CS101","start_date":"2022-09-01","end_date":"2023-01-10","passed":true}]}'
```

## 结构

```
app/
  __init__.py   应用工厂：建库、首启播种底表
  db.py         SQLite 连接与建表（底表 + 成绩单 + 核定底表）
  logic.py      导入/入账/撤销/按生重核引擎（纯函数式，便于测试）
  routes.py     网页与 JSON API，写操作统一事务
  templates/    查询、上传、底表管理页面
data/base_data.json   内置底表（首启自动导入）
examples/             示例成绩单
tests/test_app.py     18 个规则测试
Dockerfile / docker-compose.yml
```

环境变量：`DATABASE`（SQLite 路径，默认 `data/app.db`）、
`BASE_DATA_FILE`（首启播种的底表 JSON）、`SECRET_KEY`。
