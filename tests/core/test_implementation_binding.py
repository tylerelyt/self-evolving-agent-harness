"""One frozen six-hook implementation must own every capstone stage."""
from contextlib import nullcontext,redirect_stdout,redirect_stderr
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from apps.oncall.paths import prepare
prepare()
from apps.oncall.implementation import (HOOKS,REFERENCE,bind_implementation,
                                       implementation_evidence,load_implementation,verify_stage_check)
from apps.oncall import workflow
from fact_handoff import handoff_report,read_evidence,verify_saved_handoff
import fleet


def source(tag="selected", *, fail=None):
    return '\n'.join(f'def {name}(*args,**kwargs):\n    '+
                     (f'raise RuntimeError("selected {name} failed")' if name==fail else f'return {tag!r}')
                     for name in HOOKS)+'\ndef run(*args):\n    raise AssertionError("learner run must not orchestrate")\n'


class ImplementationBindingTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.input=self.root/'learner.py'
        self.input.write_text(source())

    def bind(self):
        return bind_implementation(self.input,self.root/'implementation')

    def test_solutions_is_six_hooks_without_reference_run(self):
        module=load_implementation(ROOT/'workshop/capstone/lab/solutions/self_improvement_agent.py')
        self.assertTrue(all(callable(getattr(module,name,None)) for name in HOOKS))
        self.assertFalse(hasattr(module,'run'))

    def test_source_edits_do_not_change_frozen_hooks(self):
        selected,binding=self.bind()
        self.input.write_text(source('edited after binding'))
        module=load_implementation(selected)
        for name in HOOKS:self.assertEqual(getattr(module,name)(),'selected')
        evidence=implementation_evidence(selected,binding)
        self.assertTrue(evidence['all_hooks_executed'])
        self.assertEqual(set(evidence['executed_hooks']),set(HOOKS))
        self.assertEqual(evidence['sha256'],hashlib.sha256(selected.read_bytes()).hexdigest())
        self.assertEqual(evidence['failed_calls'],0)

    def test_changed_snapshot_is_rejected_at_load_and_call(self):
        selected,binding=self.bind()
        module=load_implementation(selected)
        selected.write_bytes(selected.read_bytes()+b'\n# changed\n')
        for callback in (lambda:load_implementation(selected),module.patrol_config,
                         lambda:implementation_evidence(selected,binding)):
            with self.assertRaisesRegex(RuntimeError,'changed'):callback()

    def test_missing_hook_fails_without_reference_substitution(self):
        self.input.write_text('def patrol_config(): return {}\n')
        selected,_=self.bind()
        with self.assertRaisesRegex(ValueError,'route_alert'):load_implementation(selected)

    def test_hook_failure_is_recorded_and_propagates(self):
        self.input.write_text(source(fail='recall_memory'))
        selected,binding=self.bind()
        module=load_implementation(selected)
        with self.assertRaisesRegex(RuntimeError,'selected recall_memory failed'):
            module.recall_memory()
        evidence=implementation_evidence(selected,binding)
        self.assertEqual(evidence['failed_calls'],1)
        self.assertIn('recall_memory',evidence['missing_hooks'])
        self.assertFalse(evidence['all_hooks_executed'])

    def test_selected_memory_hook_failure_propagates_through_cli(self):
        self.input.write_text(source(fail='recall_memory'))
        selected,_=self.bind()
        output=self.root/'handoff.json'
        result=subprocess.run([sys.executable,'-E','-S','-B',str(ROOT/'apps/oncall/patrol/fact_handoff.py'),
            '--state',str(self.root/'recall'),'--memory',str(self.root/'memory.sqlite3'),
            '--incident-id','case-1','--service','payments','--environment-id','env-1',
            '--environment-state',str(self.root/'environment'),'--out',str(output),
            '--offline','--implementation',str(selected)],capture_output=True,text=True,timeout=10)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('selected recall_memory failed',result.stderr)
        self.assertFalse(output.exists())

    def test_framework_owns_orchestration_and_uses_same_module_for_all_hooks(self):
        class Runtime:
            def __init__(self,*args,**kwargs):
                self.implementation_file=kwargs['implementation_file']
                self.report={'status':'running','rounds':[{'decision':'publish'},{'adoption_verified':True}]}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def finish(self):self.report['status']='complete'
        seen=[]
        def orchestrate(runtime):
            self.assertNotEqual(Path(runtime.implementation_file),self.input)
            for name in HOOKS:
                seen.append((name,getattr(runtime.implementation,name)()))
        with patch('oncall_runtime.Runtime',Runtime),patch.object(workflow,'run',side_effect=orchestrate):
            report=fleet.run(self.root/'run',{},False,implementation_file=self.input)
        self.assertEqual(seen,[(name,'selected') for name in HOOKS])
        self.assertTrue(report['implementation']['all_hooks_executed'])
        self.assertTrue(report['closed_loop_verified'])

    def test_default_reference_preserves_original_entry(self):
        self.assertIs(load_implementation(REFERENCE),workflow)
        self.assertIs(load_implementation(ROOT/'workshop/capstone/self_improvement_agent.py').run,workflow.run)


class SavedImplementationAuditTests(unittest.TestCase):
    setUp = ImplementationBindingTests.setUp
    bind = ImplementationBindingTests.bind
    def ready_report(self):
        from audit_fleet import audit_implementation
        selected,binding=self.bind()
        module=load_implementation(selected)
        for name in HOOKS:getattr(module,name)()
        report={'mode':'local-script-model','status':'complete','closed_loop_verified':True,
                'rounds':[{'decision':'publish'},{'adoption_verified':True}],
                'implementation':implementation_evidence(selected,binding)}
        return selected,report,audit_implementation

    def test_read_only_audit_reconstructs_hook_provenance(self):
        _,report,audit=self.ready_report()
        self.assertEqual(audit(self.root,report)['status'],'passed')

    def test_audit_rejects_saved_source_tampering(self):
        selected,report,audit=self.ready_report()
        selected.write_text('raise AssertionError("audit must not import this source")')
        with self.assertRaisesRegex(RuntimeError,'changed'):audit(self.root,report)

    def test_audit_rejects_missing_hook_receipt(self):
        selected,report,audit=self.ready_report()
        path=selected.parent/'calls.jsonl'
        calls=[json.loads(l) for l in path.read_text().splitlines()]
        path.write_text(''.join(json.dumps(c)+'\n' for c in calls
                                if not(c['hook']=='recall_memory' and c['status']=='finished')))
        with self.assertRaises((ValueError,RuntimeError)):audit(self.root,report)

    def test_audit_rejects_false_claim_of_publication_and_adoption(self):
        _,report,audit=self.ready_report()
        report['rounds']=[{'decision':'keep','adoption_verified':False}]
        with self.assertRaisesRegex((ValueError,RuntimeError),'Closed-loop'):
            audit(self.root,report)

    def test_audit_rejects_receipts_bound_to_another_source(self):
        selected,report,audit=self.ready_report()
        path=selected.parent/'calls.jsonl'
        calls=[json.loads(l) for l in path.read_text().splitlines()]
        calls[0]['sha256']='another-source'
        path.write_text(''.join(json.dumps(c)+'\n' for c in calls))
        with self.assertRaisesRegex(RuntimeError,'selected source'):audit(self.root,report)


class RollbackImplementationBindingTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.selected=self.root/'selected.py'
        self.selected.write_text(source())

    def test_runtime_finish_forwards_selected_source_to_rollback(self):
        from oncall_runtime import Runtime
        runtime=Runtime(self.root,{},False,rehearse_rollback=True,implementation_file=self.selected)
        with patch('oncall_runtime.run_rollback_rehearsal',side_effect=RuntimeError('stop after dispatch')) as rollback:
            with self.assertRaisesRegex(RuntimeError,'stop after dispatch'):runtime.finish()
        self.assertEqual(rollback.call_args.kwargs['implementation_file'],self.selected)

    def test_rollback_forwards_selected_source_to_fresh_patrol_round(self):
        from skillclaw_evolve import FORMAL_GROUP
        old,new=b'old method',b'new method'
        oldsha,newsha=(hashlib.sha256(x).hexdigest() for x in (old,new))
        formal=self.root/'formal'
        entry=Path(fleet.ONCALL_SKILL)/'SKILL.md'
        current=formal/'shared'/FORMAL_GROUP/'skills'/entry
        current.parent.mkdir(parents=True);current.write_bytes(new)
        seed=formal/'versions'/oldsha/entry
        seed.parent.mkdir(parents=True);seed.write_bytes(old)
        receivers={'instance-a':self.root/'receiver'}
        target=receivers['instance-a']/entry
        target.parent.mkdir(parents=True);target.write_bytes(new)
        environment=self.root/'environment';environment.mkdir()
        report={'seed_release':{'sha256':oldsha},'rounds':[
            {'decision':'publish','release':{'sha256':newsha},'source_sessions':{}},
            {'adoption_verified':True,'formal_sha256':newsha,'source_sessions':{}}]}
        with patch('skillclaw_evolve.release',return_value={'receiver_errors':{},'receivers':{'instance-a':{}}}), \
             patch.object(fleet,'set_faults',return_value={'instance-a':1}), \
             patch.object(fleet,'wait_for_metrics'), \
             patch.object(fleet,'patrol_round',side_effect=RuntimeError('stop before tools')) as patrol:
            with self.assertRaisesRegex(RuntimeError,'stop before tools'):
                fleet.run_rollback_rehearsal(self.root,report,{'instance-a':environment},receivers,
                    {'instance-a':{'window_end':1}}, {},False,implementation_file=self.selected)
        self.assertEqual(patrol.call_args.kwargs['implementation_file'],self.selected)

    def test_rollback_patrol_process_receives_the_same_implementation(self):
        receiver=self.root/'receiver';entry=receiver/fleet.ONCALL_SKILL/'SKILL.md'
        entry.parent.mkdir(parents=True);entry.write_bytes(b'known method')
        expected=hashlib.sha256(entry.read_bytes()).hexdigest()
        child=SimpleNamespace(wait=lambda **kwargs:0)
        with patch('skillclaw_bridge.capture',return_value=nullcontext('http://127.0.0.1:1')), \
             patch.object(fleet,'process',side_effect=lambda *a,**k:nullcontext(child)) as process, \
             patch.object(fleet,'read_alert',return_value={'incident_id':'incident-1'}), \
             patch.object(fleet,'connect',side_effect=RuntimeError('stop after child')):
            with self.assertRaisesRegex(RuntimeError,'stop after child'):
                fleet.patrol_round(self.root/'round',{'instance-a':self.root/'env'},
                    {'instance-a':receiver},expected,{'base_url':'http://127.0.0.1:1','api_key':'local','model':'script'},
                    False,implementation_file=self.selected)
        args=process.call_args.args
        self.assertEqual(args[args.index('--implementation')+1],str(self.selected))


class StageCheckBindingTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.path=self.root/'check.json'
        self.sha=hashlib.sha256(b'selected source').hexdigest()
        self.check={'schema':'oncall-workshop-check-v1','status':'passed','stage':4,
                    'mode':'local-script-model','implementation':{'sha256':self.sha},
                    'checks':[{'stage':stage,'status':'passed'} for stage in range(1,5)]}

    def write(self,value=None):
        self.path.write_text(json.dumps(value or self.check))

    def test_cumulative_check_matches_selected_source_and_mode(self):
        self.write()
        receipt=verify_stage_check(self.path,self.sha,live=False)
        self.assertEqual(receipt['sha256'],hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(receipt['implementation_sha256'],self.sha)
        self.assertEqual(receipt['status'],'passed')

    def test_stale_source_mode_missing_stage_and_failure_are_rejected(self):
        cases=[]
        wrong=deepcopy(self.check);wrong['implementation']['sha256']='other';cases.append(wrong)
        wrong=deepcopy(self.check);wrong['mode']='live';cases.append(wrong)
        wrong=deepcopy(self.check);wrong['checks'].pop(1);cases.append(wrong)
        wrong=deepcopy(self.check);wrong['checks'][2]['status']='failed';cases.append(wrong)
        wrong=deepcopy(self.check);wrong['status']='failed';cases.append(wrong)
        wrong=deepcopy(self.check);wrong['stage']=3;cases.append(wrong)
        for case in cases:
            with self.subTest(case=case):
                self.write(case)
                with self.assertRaisesRegex(ValueError,'stage 4'):
                    verify_stage_check(self.path,self.sha,live=False)

    def test_cli_final_gate_rejects_a_complete_run_without_adoption(self):
        with redirect_stdout(io.StringIO()), patch.object(fleet,'model_connection',return_value=nullcontext('http://127.0.0.1:1')), \
             patch.object(fleet,'run',return_value={'status':'complete','closed_loop_verified':False}):
            with self.assertRaisesRegex(SystemExit,'later adoption'):
                fleet.main(['--out',str(self.root/'run'),'--verify-closed-loop'])

    def test_cli_final_gate_accepts_verified_closed_loop(self):
        with redirect_stdout(io.StringIO()), patch.object(fleet,'model_connection',return_value=nullcontext('http://127.0.0.1:1')), \
             patch.object(fleet,'run',return_value={'status':'complete','closed_loop_verified':True}):
            fleet.main(['--out',str(self.root/'run'),'--verify-closed-loop'])

    def test_continuous_entry_cannot_claim_finite_stage_gate(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
            fleet.main(['--out',str(self.root/'run'),'--serve','--verify-closed-loop'])
        self.assertEqual(failure.exception.code,2)


class MemoryReceiptBindingTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.scope={'incident_id':'case-1','service':'payments','environment_id':'env-1'}
        self.session='independent-handoff'
        snapshot={'source':'live_lab_http_requests','service':'payments','environment_id':'env-1',
                  'window_start':100.,'window_end':130.,'requests':30,'error_rate':0.,'p95_ms':1.}
        self.rows=[
            {'id':1,'role':'user','content':'scope only','tool_calls':[],'tool_call_id':None},
            {'id':2,'role':'assistant','content':None,'tool_calls':[{'id':'lookup','function':{'name':'memory_lookup'}}],'tool_call_id':None},
            {'id':3,'role':'tool','content':json.dumps({'fact':None,'historical_only':True}),'tool_calls':[],'tool_call_id':'lookup'},
            {'id':4,'role':'assistant','content':None,'tool_calls':[{'id':'current','function':{'name':'service_snapshot'}}],'tool_call_id':None},
            {'id':5,'role':'tool','content':json.dumps(snapshot),'tool_calls':[],'tool_call_id':'current'},
            {'id':6,'role':'assistant','content':json.dumps(handoff_report(None,snapshot)),'tool_calls':[],'tool_call_id':None},
        ]
        self.result=read_evidence(self.rows,'scope only',self.scope,self.session)

    def persist(self,rows=None):
        home=self.root/'hermes';home.mkdir(exist_ok=True)
        with sqlite3.connect(home/'state.db') as db:
            db.execute('CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,tool_calls TEXT,tool_call_id TEXT)')
            for row in rows or self.rows:
                db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',(row['id'],self.session,row['role'],row['content'],json.dumps(row['tool_calls']),row['tool_call_id']))

    def test_valid_return_is_reconstructed_from_saved_receipts(self):
        self.persist()
        self.assertEqual(verify_saved_handoff(self.root,self.scope,self.result),self.result)

    def test_forged_dict_without_native_session_is_rejected(self):
        with self.assertRaises((ValueError,sqlite3.Error)):
            verify_saved_handoff(self.root,self.scope,self.result)
        self.assertFalse((self.root/'hermes/state.db').exists())

    def test_forged_return_cannot_override_saved_tools(self):
        self.persist()
        altered=deepcopy(self.result);altered['current_status']='abnormal'
        with self.assertRaisesRegex(ValueError,'persisted'):
            verify_saved_handoff(self.root,self.scope,altered)

    def test_cross_environment_snapshot_is_rejected(self):
        rows=deepcopy(self.rows)
        body=json.loads(rows[4]['content']);body['environment_id']='other';rows[4]['content']=json.dumps(body)
        self.persist(rows)
        with self.assertRaisesRegex(RuntimeError,'scope'):
            verify_saved_handoff(self.root,self.scope,self.result)

    def test_reordered_tool_trace_is_rejected(self):
        rows=deepcopy(self.rows)
        rows[1]['tool_calls'][0]['function']['name']='service_snapshot'
        self.persist(rows)
        with self.assertRaisesRegex(RuntimeError,'memory and then'):
            verify_saved_handoff(self.root,self.scope,self.result)


if __name__=='__main__':unittest.main()
