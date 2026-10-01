"""四个只读查询入口；读取同目录的当前教学材料。"""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("entry", choices=("legacy_release_history", "legacy_payment_logs",
                                     "legacy_payment_db_pool", "legacy_payment_runtime_config"))
parser.add_argument("--request-id")
parser.add_argument("--limit", type=int, default=20)
args = parser.parse_args()
data = json.loads(Path(__file__).with_name("current.json").read_text(encoding="utf-8"))
result = data[args.entry]
if args.entry == "legacy_payment_logs":
    result = [row for row in result if not args.request_id or row["request_id"] == args.request_id][:args.limit]
print(json.dumps(result, ensure_ascii=False, indent=2))
