"""在本讲进程中传递已真实读取的文件标记，保留原生先读再改检查。"""
import hashlib
import inspect
import json
import re
from pathlib import Path
import threading


class CuratorReadAdapter:
    def __init__(self,skills):
        self.skills=skills.resolve(); self.reads={}; self.events=[]; self.lock=threading.RLock()

    def unread_target_files(self, target_name):
        target = self.skills / target_name
        if target.is_symlink() or not target.resolve().is_relative_to(self.skills) or not (target / 'SKILL.md').is_file():
            raise ValueError('归档目标必须是本技能包内的真实 Skill 目录')
        missing = []
        for path in sorted(target.rglob('*')):
            if path.is_symlink():
                raise ValueError('归档目标不得含符号链接')
            if path.is_file() and self.reads.get(path.resolve()) != hashlib.sha256(path.read_bytes()).hexdigest():
                missing.append(str(path.relative_to(self.skills)))
        return missing

    def install(self):
        from tools import skills_tool, skill_manager_tool
        native_view=skills_tool.skill_view; native_manage=skill_manager_tool.skill_manage
        signature = inspect.signature(native_manage)
        adapter=self
        def view(*args,**kwargs):
            result=native_view(*args,**kwargs)
            value=json.loads(result) if isinstance(result,str) else result
            path=Path(value['_source_path']).resolve() if value.get('_source_path') else None
            if value.get('success') and value.get('content') and not value.get('is_binary') and path and path.is_relative_to(adapter.skills):
                raw=value['content'].encode('utf-8')
                if path.read_bytes()!=raw: return result
                with adapter.lock:
                    adapter.reads[path]=hashlib.sha256(raw).hexdigest()
                    adapter.events.append({'file':str(path.relative_to(adapter.skills)),'sha256':adapter.reads[path],'real_skill_view':True})
            return result
        def manage(*args,**kwargs):
            # 与固定上游签名绑定，位置参数也必须接受相同检查；不虚构批量接口。
            parameters = signature.bind(*args, **kwargs)
            parameters.apply_defaults()
            parameters = parameters.arguments
            from tools.skill_provenance import is_background_review
            if is_background_review():
                # 每次只传当前版本、确经 skill_view 返回的正文；未读或已变更均不标记。
                skill_manager_tool._reset_background_review_read_marks()
                with adapter.lock:
                    for path,digest in adapter.reads.items():
                        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==digest:
                            skill_manager_tool.mark_background_review_skill_read(path)
                if parameters.get('action')=='delete' and parameters.get('absorbed_into'):
                    from tools import skill_usage
                    source=parameters.get('name','')
                    if skill_usage.is_curator_managed(source) and not skill_usage.get_record(source).get('pinned'):
                        try:
                            missing = adapter.unread_target_files(parameters['absorbed_into'])
                        except ValueError as exc:
                            return json.dumps({'success': False, 'error': str(exc), '_course_archive_read_check': True}, ensure_ascii=False)
                        if missing:
                            return json.dumps({'success':False,'error':'课程附加检查：归档前须读取目标包当前正文和全部支持文件：'+', '.join(missing), '_course_archive_read_check':True},ensure_ascii=False)
            return native_manage(*args,**kwargs)
        skills_tool.skill_view=view
        skill_manager_tool.skill_manage=manage
