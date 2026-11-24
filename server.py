#!/usr/bin/env python3
"""小账本的本地服务：提供页面，并把数据直接读写到 data.json。"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
import threading
import uuid
import webbrowser
from datetime import date, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data.json"
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY_BYTES = 6 * 1024 * 1024
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CATEGORIES = {"餐饮", "交通", "购物", "学习", "居住", "娱乐", "医疗", "其他"}
TASK_SCHEDULES = {"daily", "weekly", "dates"}
WEEKDAYS = set(range(7))
DATA_LOCK = threading.RLock()


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def empty_data() -> dict:
    return {"version": 1, "records": [], "tasks": [], "checkins": []}


def valid_date(value: str) -> bool:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def normalise_record(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ApiError(400, "记录格式不正确")

    record_date = str(raw.get("date", "")).strip()
    if not valid_date(record_date):
        raise ApiError(400, "日期格式必须为 YYYY-MM-DD")

    item = str(raw.get("item", "")).strip()
    if not item:
        raise ApiError(400, "消费项目不能为空")
    item = item[:60]

    try:
        amount = round(float(raw.get("amount")), 2)
    except (TypeError, ValueError):
        raise ApiError(400, "金额格式不正确")
    if not math.isfinite(amount) or amount <= 0 or amount > 99999999.99:
        raise ApiError(400, "金额必须大于 0 且不超过 99,999,999.99")

    category = str(raw.get("category", "其他")).strip()
    if category not in CATEGORIES:
        category = "其他"

    record_id = str(raw.get("id") or uuid.uuid4())
    if not record_id or len(record_id) > 100:
        record_id = str(uuid.uuid4())

    try:
        created_at = int(raw.get("createdAt") or now_ms())
    except (TypeError, ValueError):
        created_at = now_ms()

    return {
        "id": record_id,
        "date": record_date,
        "item": item,
        "amount": amount,
        "category": category,
        "createdAt": created_at,
    }


def normalise_records(raw_records: object) -> list[dict]:
    if not isinstance(raw_records, list):
        raise ApiError(400, "records 必须是数组")

    records: list[dict] = []
    seen_ids: set[str] = set()
    for raw in raw_records:
        record = normalise_record(raw)
        if record["id"] in seen_ids:
            record["id"] = str(uuid.uuid4())
        seen_ids.add(record["id"])
        records.append(record)

    records.sort(key=lambda record: (record["date"], record["createdAt"]))
    return records


def normalise_task(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ApiError(400, "任务格式不正确")

    name = str(raw.get("name", "")).strip()
    if not name:
        raise ApiError(400, "任务名称不能为空")
    name = name[:40]

    schedule = str(raw.get("schedule", "daily")).strip()
    if schedule not in TASK_SCHEDULES:
        schedule = "daily"

    weekdays: list[int] = []
    raw_weekdays = raw.get("weekdays", [])
    if isinstance(raw_weekdays, list):
        for value in raw_weekdays:
            try:
                day = int(value)
            except (TypeError, ValueError):
                continue
            if day in WEEKDAYS and day not in weekdays:
                weekdays.append(day)
    weekdays.sort()

    dates: list[str] = []
    raw_dates = raw.get("dates", [])
    if isinstance(raw_dates, list):
        for value in raw_dates:
            text = str(value).strip()
            if valid_date(text) and text not in dates:
                dates.append(text)
    dates.sort()

    if schedule == "weekly" and not weekdays:
        raise ApiError(400, "按周重复的任务至少选择一天")
    if schedule == "dates" and not dates:
        raise ApiError(400, "按日期重复的任务至少选择一个日期")

    task_id = str(raw.get("id") or uuid.uuid4())
    if not task_id or len(task_id) > 100:
        task_id = str(uuid.uuid4())

    try:
        created_at = int(raw.get("createdAt") or now_ms())
    except (TypeError, ValueError):
        created_at = now_ms()

    return {
        "id": task_id,
        "name": name,
        "schedule": schedule,
        "weekdays": weekdays,
        "dates": dates,
        "createdAt": created_at,
    }


def normalise_checkin(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ApiError(400, "打卡记录格式不正确")

    task_id = str(raw.get("taskId", "")).strip()
    if not task_id:
        raise ApiError(400, "打卡记录缺少任务 id")

    checkin_date = str(raw.get("date", "")).strip()
    if not valid_date(checkin_date):
        raise ApiError(400, "打卡日期格式必须为 YYYY-MM-DD")

    checkin_id = str(raw.get("id") or uuid.uuid4())
    if not checkin_id or len(checkin_id) > 100:
        checkin_id = str(uuid.uuid4())

    try:
        created_at = int(raw.get("createdAt") or now_ms())
    except (TypeError, ValueError):
        created_at = now_ms()

    return {
        "id": checkin_id,
        "taskId": task_id,
        "date": checkin_date,
        "createdAt": created_at,
    }


def read_data_locked() -> dict:
    if not DATA_FILE.exists():
        return empty_data()

    try:
        with DATA_FILE.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise ApiError(500, f"无法读取 data.json：{error}")

    if isinstance(payload, list):
        raw_records = payload
        raw_tasks: list = []
        raw_checkins: list = []
    elif isinstance(payload, dict):
        raw_records = payload.get("records", [])
        raw_tasks = payload.get("tasks", [])
        raw_checkins = payload.get("checkins", [])
    else:
        raise ApiError(500, "data.json 的格式不正确")

    if not isinstance(raw_records, list):
        raise ApiError(500, "data.json 里的 records 必须是数组")
    if not isinstance(raw_tasks, list):
        raw_tasks = []
    if not isinstance(raw_checkins, list):
        raw_checkins = []

    records = [normalise_record(record) for record in raw_records]
    records.sort(key=lambda record: (record["date"], record["createdAt"]))
    tasks = [normalise_task(task) for task in raw_tasks]
    tasks.sort(key=lambda task: task["createdAt"])
    checkins = [normalise_checkin(item) for item in raw_checkins]
    checkins.sort(key=lambda item: (item["date"], item["createdAt"]))
    return {"version": 1, "records": records, "tasks": tasks, "checkins": checkins}


def write_data_locked(data: dict) -> None:
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(
            prefix=".data-", suffix=".tmp", dir=str(ROOT)
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, DATA_FILE)
        temp_path = None
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def ensure_data_file() -> None:
    with DATA_LOCK:
        if not DATA_FILE.exists():
            write_data_locked(empty_data())


def backup_payload(data: dict) -> dict:
    return {
        "app": "小账本",
        "version": 1,
        "exportedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "records": data["records"],
        "tasks": data["tasks"],
        "checkins": data["checkins"],
    }


class LedgerHandler(SimpleHTTPRequestHandler):
    server_version = "LittleLedger/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        # 只保留简短日志，避免终端被资源请求刷屏。
        print(f"[小账本] {fmt % args}")

    def send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_api_error(self, error: ApiError) -> None:
        self.send_json(error.status, {"error": error.message})

    def read_json_body(self) -> object:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError(400, "请求长度不正确")
        if length <= 0:
            raise ApiError(400, "请求内容为空")
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "导入文件太大了")

        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "JSON 格式不正确")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            if path == "/api/health":
                self.send_json(200, {"ok": True, "dataFile": str(DATA_FILE)})
                return

            if path == "/api/data":
                with DATA_LOCK:
                    payload = read_data_locked()
                self.send_json(200, payload)
                return

            if path == "/api/backup":
                with DATA_LOCK:
                    data = read_data_locked()
                body = json.dumps(
                    backup_payload(data), ensure_ascii=False, indent=2
                ).encode("utf-8")
                filename = f"ledger-backup-{date.today().isoformat()}.json"
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header(
                    "Content-Disposition", f'attachment; filename="{filename}"'
                )
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return

            if path == "/data.json":
                with DATA_LOCK:
                    payload = read_data_locked()
                body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return

            super().do_GET()
        except ApiError as error:
            self.send_api_error(error)
        except Exception as error:  # noqa: BLE001 - 保证 API 总能返回 JSON
            self.send_api_error(ApiError(500, f"服务器内部错误：{error}"))

    def do_HEAD(self) -> None:
        # 让 /api/backup 等接口也支持 HEAD 探测；静态文件仍走标准 HEAD。
        if urlparse(self.path).path.startswith("/api/"):
            self.do_GET()
        else:
            super().do_HEAD()

    def do_POST(self) -> None:
        path = urlparse(self.path).path

        try:
            body = self.read_json_body()

            if path == "/api/records":
                if not isinstance(body, dict):
                    raise ApiError(400, "记录格式不正确")
                record = normalise_record(body)
                record["id"] = str(uuid.uuid4())
                record["createdAt"] = now_ms()
                with DATA_LOCK:
                    data = read_data_locked()
                    records = data["records"]
                    records.append(record)
                    records.sort(
                        key=lambda item: (item["date"], item["createdAt"])
                    )
                    data["records"] = records
                    write_data_locked(data)
                self.send_json(201, {"record": record})
                return

            if path == "/api/tasks":
                if not isinstance(body, dict):
                    raise ApiError(400, "任务格式不正确")
                task = normalise_task(body)
                task["id"] = str(uuid.uuid4())
                task["createdAt"] = now_ms()
                with DATA_LOCK:
                    data = read_data_locked()
                    data["tasks"].append(task)
                    data["tasks"].sort(key=lambda item: item["createdAt"])
                    write_data_locked(data)
                self.send_json(201, {"task": task})
                return

            if path == "/api/checkins":
                if not isinstance(body, dict):
                    raise ApiError(400, "打卡记录格式不正确")
                task_id = str(body.get("taskId", "")).strip()
                checkin_date = str(body.get("date", "")).strip()
                checked = bool(body.get("checked"))
                if not task_id:
                    raise ApiError(400, "缺少任务 id")
                if not valid_date(checkin_date):
                    raise ApiError(400, "打卡日期格式必须为 YYYY-MM-DD")

                with DATA_LOCK:
                    data = read_data_locked()
                    if not any(task["id"] == task_id for task in data["tasks"]):
                        raise ApiError(404, "没有找到这个任务")
                    existing = next(
                        (
                            item
                            for item in data["checkins"]
                            if item["taskId"] == task_id and item["date"] == checkin_date
                        ),
                        None,
                    )
                    if checked and existing is None:
                        checkin = {
                            "id": str(uuid.uuid4()),
                            "taskId": task_id,
                            "date": checkin_date,
                            "createdAt": now_ms(),
                        }
                        data["checkins"].append(checkin)
                        data["checkins"].sort(
                            key=lambda item: (item["date"], item["createdAt"])
                        )
                        write_data_locked(data)
                        self.send_json(200, {"added": True, "checkin": checkin})
                        return
                    if not checked and existing is not None:
                        data["checkins"] = [
                            item
                            for item in data["checkins"]
                            if not (item["taskId"] == task_id and item["date"] == checkin_date)
                        ]
                        write_data_locked(data)
                        self.send_json(
                            200, {"removed": True, "checkinId": existing["id"]}
                        )
                        return
                    self.send_json(200, {"changed": False, "checkin": existing})
                return

            if path == "/api/import":
                if isinstance(body, dict):
                    raw_records = body.get("records", [])
                    raw_tasks = body.get("tasks")
                    raw_checkins = body.get("checkins")
                else:
                    raw_records = body
                    raw_tasks = None
                    raw_checkins = None
                records = normalise_records(raw_records)
                with DATA_LOCK:
                    current = read_data_locked()
                    if isinstance(raw_tasks, list):
                        tasks = [normalise_task(task) for task in raw_tasks]
                    else:
                        tasks = current["tasks"]
                    if isinstance(raw_checkins, list):
                        checkins = [normalise_checkin(item) for item in raw_checkins]
                    else:
                        checkins = current["checkins"]
                    payload = {
                        "version": 1,
                        "records": records,
                        "tasks": tasks,
                        "checkins": checkins,
                    }
                    write_data_locked(payload)
                self.send_json(
                    200,
                    {
                        "version": 1,
                        "records": records,
                        "tasks": tasks,
                        "checkins": checkins,
                        "imported": len(records),
                    },
                )
                return

            raise ApiError(404, "接口不存在")
        except ApiError as error:
            self.send_api_error(error)
        except Exception as error:  # noqa: BLE001
            self.send_api_error(ApiError(500, f"服务器内部错误：{error}"))

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)

        try:
            if parsed.path == "/api/records":
                record_id = parse_qs(parsed.query).get("id", [""])[0]
                if not record_id:
                    raise ApiError(400, "缺少记录 id")

                with DATA_LOCK:
                    data = read_data_locked()
                    records = data["records"]
                    remaining = [record for record in records if record["id"] != record_id]
                    if len(remaining) == len(records):
                        raise ApiError(404, "没有找到这条记录")
                    data["records"] = remaining
                    write_data_locked(data)

                self.send_json(200, {"deleted": record_id})
                return

            if parsed.path == "/api/tasks":
                task_id = parse_qs(parsed.query).get("id", [""])[0]
                if not task_id:
                    raise ApiError(400, "缺少任务 id")

                with DATA_LOCK:
                    data = read_data_locked()
                    remaining = [task for task in data["tasks"] if task["id"] != task_id]
                    if len(remaining) == len(data["tasks"]):
                        raise ApiError(404, "没有找到这个任务")
                    data["tasks"] = remaining
                    data["checkins"] = [
                        item for item in data["checkins"] if item["taskId"] != task_id
                    ]
                    write_data_locked(data)

                self.send_json(200, {"deleted": task_id})
                return

            raise ApiError(404, "接口不存在")
        except ApiError as error:
            self.send_api_error(error)
        except Exception as error:  # noqa: BLE001
            self.send_api_error(ApiError(500, f"服务器内部错误：{error}"))


def server_already_running(port: int) -> bool:
    """检查该端口上是否已经有小账本在运行。"""
    try:
        with urlopen(f"http://{HOST}:{port}/api/health", timeout=0.5) as response:
            return response.status == 200
    except OSError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="小账本本地服务")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口")
    parser.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    args = parser.parse_args()

    url = f"http://{HOST}:{args.port}/"

    if server_already_running(args.port):
        print("")
        print("  小账本已经在运行")
        print(f"  访问地址：{url}")
        print("")
        if not args.no_browser:
            webbrowser.open(url)
        return

    ensure_data_file()

    try:
        server = ThreadingHTTPServer((HOST, args.port), LedgerHandler)
    except OSError as error:
        print(f"启动失败：端口 {args.port} 可能已被占用。{error}")
        print(f"可以换一个端口，例如：python3 server.py --port {args.port + 1}")
        raise SystemExit(1)

    print("")
    print("  小账本已启动")
    print(f"  访问地址：{url}")
    print(f"  数据文件：{DATA_FILE}")
    print("  按 Ctrl+C 停止服务")
    print("")

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n小账本已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
