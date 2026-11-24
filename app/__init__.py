"""应用工厂：建库、按需播种底表、注册路由。"""
import json
import os

from flask import Flask

from . import db as dbmod
from .logic import import_base_data


def _seed_base_if_empty(db_path, base_file):
    """首次启动时一次性导入课程/学分/必修前置课底表。"""
    if not base_file or not os.path.exists(base_file):
        return False
    conn = dbmod.connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS c FROM courses").fetchone()["c"]
        if n > 0:
            return False
        with open(base_file, encoding="utf-8") as f:
            payload = json.load(f)
        conn.execute("BEGIN IMMEDIATE")
        import_base_data(conn, payload)
        conn.execute("COMMIT")
        return True
    except Exception:
        dbmod.rollback_quietly(conn)
        raise
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
    if _seed_base_if_empty(app.config["DATABASE"], app.config["BASE_DATA_FILE"]):
        app.logger.info("已从 %s 一次性导入课程/学分/前置课底表", app.config["BASE_DATA_FILE"])

    app.teardown_appcontext(dbmod.close_db)

    from .routes import bp
    app.register_blueprint(bp)
    return app
