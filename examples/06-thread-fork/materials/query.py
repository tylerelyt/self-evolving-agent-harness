"""仅查询本目录的构造材料；不连接任何服务。"""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("entry", nargs="?")
parser.add_argument("--list", action="store_true")
parser.add_argument("--request-id")
args = parser.parse_args()
directory = Path(__file__).resolve().parent
path = directory / ("current.json" if (directory / "current.json").is_file() else "data.json")
data = json.loads(path.read_text(encoding="utf-8"))
if args.list:
    print(json.dumps({"说明": data["说明"], "entries": [k for k in data if k != "说明"]}, ensure_ascii=False))
elif args.entry not in data or args.entry == "说明":
    parser.exit(2, "本目录没有这个查询入口：" + str(args.entry) + "\n")
else:
    value = data[args.entry]
    if args.request_id:
        if args.entry != "legacy_payment_logs":
            parser.error("--request-id 仅用于应用日志入口")
        value = {**value, "samples": [r for r in value["samples"] if r["request_id"] == args.request_id]}
    print(json.dumps({"说明": data["说明"], "entry": args.entry, "data": value}, ensure_ascii=False, indent=2))
