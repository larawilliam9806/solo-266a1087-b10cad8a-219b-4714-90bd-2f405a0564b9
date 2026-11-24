"""应用工厂：建库、按需播种底表、注册路由。"""
import json
import os

from flask import Flask

from . import db as dbmod
from .logic import import_base_data


def _base_table_ready(conn):
    """课程底表是否已有数据。连接为自动提交模式，每次调用都读到最新已提交状态。"""
    return conn.execute("SELECT COUNT(*) AS c FROM courses").fetchone()["c"] > 0


def _seed_base_if_empty(db_path, base_file):
    """首次启动时把默认底表 JSON 一次性导入空库。

    返回 "seeded"（已导入）/ "skipped"（已有现行底表，整批跳过）/
    "disabled"（未配置默认文件）。

    多实例共用同一 SQLite 库时的并发规则：
    - 读默认文件不持有数据库写锁，教务员此时仍可在其他实例经网页导入底表、
      补录成绩；
    - 写事务（BEGIN IMMEDIATE）内复查课程表：若已有底表先提交，晚到的自动
      导入整批跳过，本实例直接沿用现行底表启动——即使默认文件已损坏也同样
      跳过；
    - 若库仍为空而默认导入失败（文件缺失/损坏/内容不合规）：报错并回滚，
      库保持为空，修好文件后重启即可重试。
    """
    if not base_file:
        return "disabled"
    conn = dbmod.connect(db_path)
    try:
        if _base_table_ready(conn):
            return "skipped"
        # 读文件不占用数据库锁，网页导入底表/补录成绩不受影响
        try:
            with open(base_file, encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError) as e:  # 文件缺失/不可读/JSON 非法
            # 读文件期间已有底表提交：坏文件不再重要，沿用现行底表
            if _base_table_ready(conn):
                return "skipped"
            raise RuntimeError(
                f"默认底表文件 {base_file} 读取失败，数据库仍为空：{e}"
                "（修好文件后重启即可重试）"
            ) from e
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 复查：读文件期间其他实例可能已导入底表，晚到的自动导入整批跳过
            if _base_table_ready(conn):
                conn.execute("ROLLBACK")
                return "skipped"
            import_base_data(conn, payload)
        except Exception:
            dbmod.rollback_quietly(conn)
            raise
        conn.execute("COMMIT")
        return "seeded"
    finally:
        conn.close()


def create_app(test_config=None):
    app = Flask(__name__)
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-secret"),
        DATABASE=os.environ.get("DATABASE", os.path.join(base_dir, "data", "app.db")),
        BASE_DATA_FILE=os.environ.get("BASE_DATA_FILE", os.path.join(base_dir, "data", "base_data.json")),
    )
    if test_config:
        app.config.update(test_config)

    dbmod.init_db_file(app.config["DATABASE"])
    seed = _seed_base_if_empty(app.config["DATABASE"], app.config["BASE_DATA_FILE"])
    if seed == "seeded":
        app.logger.info("已从 %s 一次性导入课程/学分/前置课底表", app.config["BASE_DATA_FILE"])
    elif seed == "skipped":
        app.logger.info("课程底表已存在，默认底表自动导入整批跳过，沿用现行底表")

    app.teardown_appcontext(dbmod.close_db)

    from .routes import bp
    app.register_blueprint(bp)
    return app
