#!/usr/bin/env python3
"""教学业务样例：目录解析和日志读取，由 Hermes 的真实 terminal 工具执行。

没有接真实巡检平台；只用于观察“先查目录，再使用当时返回的入口”。
"""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["catalog", "logs"])
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--entry")
    args = parser.parse_args()
    catalog = json.loads((args.data / "catalog.json").read_text())
    key = f"{args.service}/{args.region}/{args.env}"
    route = catalog[key]
    if args.action == "catalog":
        result = {"service": args.service, "region": args.region, "env": args.env, **route}
    else:
        if args.entry != route["current"]:
            raise SystemExit("入口已过时或区域、环境不符，请重新查服务目录")
        result = {"entry": args.entry, "window": "02:10-02:15",
                  "message": "支付超时来自下游延迟；告警窗口内出现 3 条超时日志"}
    event = {"action": args.action, "key": key, "entry": args.entry, "result": result}
    with (args.data / "service-events.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
