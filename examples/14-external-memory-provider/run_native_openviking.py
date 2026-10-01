#!/usr/bin/env python3
"""默认只转换消息；--live 才连服务，--commit 单独允许会话提交与抽取。"""
import argparse
import json
from native_openviking import HERE, NativeSession, demo_messages, load_native


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--agent-run', action='store_true', help='真实 AIAgent 自行检索资料并回答；必须同时 --live')
    parser.add_argument('--commit', action='store_true')
    parser.add_argument('--agent', default='investigator')
    parser.add_argument('--user', default='report-team')
    parser.add_argument('--query', default='报表延迟时如何区分采集和入库问题？')
    parser.add_argument('--ingest', action='store_true', help='接入本例构造的纯文本手册')
    parser.add_argument('--read-uri', help='读取服务实际返回的 URI，不预先编造 URI')
    parser.add_argument('--recall-only', action='store_true', help='第二身份只召回，不提交构造输入')
    args = parser.parse_args()
    if (args.commit or args.ingest or args.read_uri or args.recall_only or args.agent_run) and not args.live:
        parser.error('服务操作必须与 --live 一起显式指定')
    if not args.live:
        _, Provider = load_native(HERE / 'runtime/native/offline-home')
        print(json.dumps(Provider._messages_to_openviking_batch(demo_messages(), assistant_peer_id=args.agent),
                         ensure_ascii=False, indent=2))
        return
    with NativeSession(allow_live=True, agent=args.agent, user=args.user, agent_runtime=args.agent_run) as session:
        if args.ingest:
            added = session.tool('viking_add_resource', url=str(HERE / 'resources/report-runbook.md'),
                                 wait=True, timeout=120)
            session.save('resource-receipt.json', added)
            print(json.dumps(added, ensure_ascii=False))
        if args.agent_run:
            report = session.converse(args.query)
            print(json.dumps({'final_response': report['result']['final_response'],
                              'write_verified': report['write_receipt']['write_verified'],
                              'records': str(session.home)}, ensure_ascii=False, indent=2))
        else:
            context = session.manager.prefetch_all(args.query, session_id=session.sid)
            print(json.dumps({'prefetch': context, 'note': '接口实验，不代表 Agent 自主检索。'}, ensure_ascii=False))
            if not args.recall_only:
                print(json.dumps(session.sync(demo_messages()), ensure_ascii=False, indent=2))
        if args.commit:
            print(json.dumps(session.end_session(), ensure_ascii=False, indent=2))
        if not args.agent_run:
            print(json.dumps(session.tool('viking_search', query=args.query, limit=3), ensure_ascii=False, indent=2))
        if args.read_uri:
            for level in ('abstract', 'overview', 'full'):
                print(json.dumps({'level': level, 'result': session.tool('viking_read', uri=args.read_uri, level=level)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
