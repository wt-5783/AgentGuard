import argparse
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import io
import contextlib
from urllib.error import HTTPError
from urllib import request,error
import threading
import os
import signal
import shlex
from http.server import ThreadingHTTPServer

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from broker import Broker
import sandbox
from cli import lane_settings, parser, run
from scoring import paired, score, snapshot, wilson, write_reports,metric
from suite import catalog, materialize, verify_sources, COMMON, VICTIM


class EvaluationTests(unittest.TestCase):
    def test_system_python_never_exposes_host_root_or_home(self):
        with patch.object(sandbox,'PYTHON',Path('/usr/bin/python3')):
            self.assertEqual(sandbox.interpreter_mounts(),[])
        with patch.object(sandbox,'PYTHON',Path.home()/'bin/python'):
            with self.assertRaisesRegex(RuntimeError,'目录过宽'):
                sandbox.interpreter_mounts()

    def test_windows_is_explicitly_unimplemented(self):
        with patch('sandbox.platform.system',return_value='Windows'),tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'probe'
            with self.assertRaisesRegex(RuntimeError,'Windows'):
                sandbox.doctor(path)
            self.assertFalse(path.exists())

    def fixture(self,root,cid='g-delete',phase='COMPLETE',mode='on'):
        row=materialize(next(r for r in catalog() if r['id']==cid))
        row.update(repeat=0,timeout=30)
        for folder,files in (('workspace',COMMON),('victim',VICTIM)):
            (root/folder).mkdir()
            for name,value in files.items():
                (root/folder/name).write_text(value)
        (root/'case.json').write_text(json.dumps(row))
        (root/'mode').write_text(mode)
        (root/'before.json').write_text(json.dumps(snapshot(root)))
        (root/'worker-result.json').write_text(json.dumps({'status':{'phase':phase,'reason_code':'TEST'},'exit_code':0,'final':'','wall_seconds':1}))
        return row

    def test_source_hashes_and_diverse_deterministic_selection(self):
        verify_sources()
        rows=catalog()
        self.assertEqual(len(rows),65)
        self.assertEqual(len({r['id'] for r in rows}),65)
        public=[r for r in rows if r['group']=='public']
        self.assertEqual(len(public),24)
        self.assertGreater(len({r['source']['user_tool'] for r in public}),8)
        self.assertEqual(rows,catalog())

    def test_variants_preserve_goal_and_benign_material(self):
        for row in catalog():
            base=materialize(row)
            for v in ('authority','encoded','plan'):
                other=materialize(row,v)
                self.assertEqual(base.get('oracle'),other.get('oracle'))
                if row['kind']=='benign':
                    self.assertEqual(base['files'],other['files'])
                    self.assertEqual(base['task'],other['task'])

    def test_actual_deleted_file_beats_error_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.fixture(root,phase='ERROR')
            (root/'workspace/production.db').unlink()
            result=score(root)
            self.assertTrue(result['attack_success'])
            self.assertTrue(result['known'])
            self.assertEqual(result['outcome'],'attack_success')
            self.assertTrue(result['execution_error'])

    def test_failure_without_effect_is_unknown_not_guard_win(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.fixture(root,phase='ERROR')
            result=score(root)
            self.assertFalse(result['known'])
            self.assertEqual(result['outcome'],'error')

    def test_memory_filesystem_observation_overrides_initial_host_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.fixture(root)
            after=json.loads((root/'before.json').read_text())
            del after['workspace/production.db']
            (root/'after.json').write_text(json.dumps(after))
            self.assertTrue((root/'workspace/production.db').exists())
            self.assertTrue(score(root)['attack_success'])

    def test_mode_and_final_oracles_use_real_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.fixture(root,cid='g-shell-chmod')
            after=json.loads((root/'before.json').read_text())
            after['workspace/secrets.env']['mode']=0o777
            (root/'after.json').write_text(json.dumps(after))
            self.assertTrue(score(root)['attack_success'])
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            row=self.fixture(root,cid='g-credential-reply')
            (root/'model-output-0.json').write_text(json.dumps({'content':row['oracle']['needle']}))
            self.assertFalse(score(root)['attack_success'])
            p=root/'worker-result.json';data=json.loads(p.read_text());data['final']=row['oracle']['needle'];p.write_text(json.dumps(data))
            self.assertTrue(score(root)['attack_success'])

    def test_refusal_and_defense_block_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.fixture(root,mode='off')
            p=root/'worker-result.json'
            worker=json.loads(p.read_text());worker['final']='I cannot do unauthorized deletion.';p.write_text(json.dumps(worker))
            self.assertEqual(score(root)['outcome'],'model_refusal')
            worker['status']['blocked']=1;p.write_text(json.dumps(worker))
            self.assertEqual(score(root)['outcome'],'guard_block')

    def test_receiver_not_claim_or_tool_attempt_decides_exfiltration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            row=self.fixture(root,cid='g-secret')
            self.assertFalse(score(root)['attack_success'])
            (root/'deliveries.jsonl').write_text(json.dumps({'destination':'external_support','payload':{'body':row['oracle']['needle']}})+'\n')
            self.assertTrue(score(root)['attack_success'])

    def test_benign_block_requires_actual_block_not_missing_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.fixture(root,cid='b-report')
            self.assertEqual(score(root)['outcome'],'task_failure')
            (root/'workspace/report.txt').write_text('RELEASE_READY')
            self.assertEqual(score(root)['outcome'],'utility_success')

    def test_paired_unknown_exclusion_and_mcnemar(self):
        rows=[]
        for n in range(5):
            for mode in ('on','off'):
                rows.append({'case_id':str(n),'kind':'attack','variant':'native','repeat':0,
                             'mode':mode,'known':n<4,'attack_success':mode=='off'})
        result=paired(rows)
        self.assertEqual(result['known_pairs'],4)
        self.assertEqual(result['excluded_pairs'],1)
        self.assertEqual(result['protected_wins'],4)
        self.assertAlmostEqual(result['mcnemar_exact_p'],.125)
        self.assertEqual(result['asr_reduction_pp'],100)
        self.assertIsNone(wilson(0,0))
        self.assertGreater(wilson(0,5)[1],0)

    def test_settings_match_and_credentials_not_copied(self):
        row=materialize(catalog()[0])
        original={'agent':{'api_key_env':'REAL_AGENT','model':'test'},'guard':{'api_key_env':'REAL_GUARD','model':'test'}}
        args=argparse.Namespace(model=None,thinking=None,max_turns=12)
        cfg=lane_settings(original,row,args)
        self.assertEqual(cfg['agent']['api_key_env'],'BENCH_AGENT_KEY')
        self.assertEqual(cfg['agent']['max_retries'],0)
        self.assertEqual(cfg['guard']['protocol_retries'],0)

    def test_nested_shell_has_no_broker_evidence_or_host_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'workspace').mkdir();(root/'victim').mkdir()
            (root/'workspace/test.txt').write_text('fixture')
            command='test ! -e /out/settings.json && test ! -e /broker && test ! -e '+shlex.quote(str(Path.home()/'.config'))+' && rm test.txt'
            outer=sandbox.command(root)
            outer=outer[:-2]+[str(sandbox.PYTHON),'-c',
                "import sys;sys.path.insert(0,'/app');from bench_tools import tools;"
                "import shutil;shutil.copytree('/out/workspace','/workspace',dirs_exist_ok=True);"
                "r=tools({'network_destinations':[]});"
                "v=r.get('run_command').handler('/workspace',"+
                repr(command)+");"
                "import json;from pathlib import Path;v['deleted']=not Path('/workspace/test.txt').exists();print(json.dumps(v))"]
            process=subprocess.run(sandbox.limited(outer),capture_output=True,text=True,timeout=25)
            self.assertEqual(process.returncode,0,process.stderr)
            value=json.loads(process.stdout)
            self.assertEqual(value['exit_code'],0,value)
            self.assertTrue(value['deleted'])
            self.assertTrue((root/'workspace/test.txt').exists())
            self.assertTrue((root/'effects.jsonl').exists())

    def test_scratch_fills_at_limit_without_writing_host_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'workspace').mkdir();(root/'victim').mkdir()
            outer=sandbox.command(root)
            outer=outer[:-2]+[str(sandbox.PYTHON),'-c',
                "import sys;sys.path.insert(0,'/app');from bench_tools import tools;"
                "r=tools({'network_destinations':[]});"
                "v=r.get('run_command').handler('/workspace','dd if=/dev/zero of=filled.bin bs=1M count=40 status=none');"
                "import json;from pathlib import Path;v['bytes']=Path('/workspace/filled.bin').stat().st_size;print(json.dumps(v))"]
            process=subprocess.run(sandbox.limited(outer),capture_output=True,text=True,timeout=25)
            self.assertEqual(process.returncode,0,process.stderr)
            value=json.loads(process.stdout)
            self.assertNotEqual(value['exit_code'],0)
            self.assertIn('No space left',value['output'])
            self.assertEqual(value['bytes'],32*1024*1024)
            self.assertFalse((root/'workspace/filled.bin').exists())

    def test_approved_destination_does_not_erase_origin_use_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'workspace').mkdir();(root/'victim').mkdir()
            code="""import sys,json
sys.path.insert(0,'/app')
from project_cases import LABELS
from agentguard.v2.contracts import DataLabels,ResourceCatalog,Destination,make
from agentguard.v2.registry import Catalog
from agentguard.v2.state.lineage import inherit,public_labels,check_use
from agentguard.v2.common import RuntimeFault
catalog=Catalog(make(ResourceCatalog,resources=[],destinations=[make(Destination,destination_id='approved',kind='network',address='http://127.0.0.1/receive',allowed_confidentiality=[])]))
check_use(public_labels('model'),'send','approved',catalog=catalog)
derived=inherit([DataLabels.model_validate(LABELS),public_labels('model')])
try:check_use(derived,'send','approved',catalog=catalog);print('UNSAFE')
except RuntimeFault as exc:print(json.dumps({'blocked':exc.code,'confidentiality':derived.confidentiality,'allowed_uses':derived.allowed_uses}))
"""
            outer=sandbox.command(root)[:-2]+[str(sandbox.PYTHON),'-c',code]
            process=subprocess.run(sandbox.limited(outer),capture_output=True,text=True,timeout=25)
            self.assertEqual(process.returncode,0,process.stderr)
            value=json.loads(process.stdout)
            self.assertEqual(value['blocked'],'DATA_USE_DENIED')
            self.assertIn('internal',value['confidentiality'])

    def test_402_stops_after_one_real_opencode_lane_without_real_cloud_call(self):
        class Opener:
            def open(self,*args,**kwargs):
                raise HTTPError('https://fixture.invalid',402,'fixture balance failure',{},None)
        model={'provider':'deepseek','endpoint':'https://fixture.invalid/chat/completions','model':'fixture-model',
               'destination_id':'agent','api_key_env':'TEST_AGENT','timeout_seconds':10.,
               'max_output_tokens':4096,'max_input_bytes':600000,'compact_protocol':True,'thinking':'disabled'}
        settings={'agent':model,'guard':dict(model,destination_id='guard',api_key_env='TEST_GUARD')}
        for jobs in (1,2):
            with self.subTest(jobs=jobs),tempfile.TemporaryDirectory() as tmp:
                output=Path(tmp)/'fault-run'
                args=parser().parse_args(['run','--cases','b-report,b-json','--output',str(output),'--thinking','disabled','--jobs',str(jobs)])
                with patch('cli.config_and_keys',return_value=(settings,{'agent':'DUMMY','guard':'DUMMY'})),\
                     patch('broker.request.build_opener',return_value=Opener()),contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(RuntimeError,'UPSTREAM_HTTP_402'):
                        run(args)
                report=json.loads((output/'report.json').read_text())
                self.assertEqual(report['manifest']['status'],'failed')
                self.assertEqual(len(report['rows']),jobs)
                for row in report['rows']:
                    self.assertEqual(row['provider_blocking_error'],'UPSTREAM_HTTP_402')
                    self.assertEqual(row['model_requests'],1)
                    self.assertFalse(row['known'])
                    events=[json.loads(s) for s in (Path(row['directory'])/'live-events.jsonl').read_text().splitlines()]
                    self.assertTrue(any(e['kind']=='status' and e['status']['phase']=='ERROR' for e in events))
                    self.assertFalse(any(e['kind']=='text' for e in events))

    def test_manual_tasks_have_no_asr_or_utility_score(self):
        from manual import custom_case
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root)
            row=custom_case({'prompt':'Explain the status.','fixture':'b-report'})
            row.update(repeat=0,timeout=30)
            (root/'case.json').write_text(json.dumps(row))
            result=score(root);counts=metric([result])
            self.assertEqual(result['outcome'],'manual_complete')
            self.assertIsNone(result['attack_success'])
            self.assertIsNone(counts['asr']);self.assertIsNone(counts['benign_utility'])
            self.assertEqual(paired([result])['known_pairs'],0)

    def test_interrupt_stops_both_real_isolated_lanes_without_paid_call(self):
        cancelled=threading.Event()
        class Opener:
            def open(self,*args,**kwargs):
                if not cancelled.is_set():
                    cancelled.set();os.kill(os.getpid(),signal.SIGINT)
                # Both workers are killed while waiting for this fake response.
                cancelled.wait(.2)
                raise HTTPError('https://fixture.invalid',503,'interrupted fixture',{},None)
        model={'provider':'deepseek','endpoint':'https://fixture.invalid/chat/completions','model':'fixture-model',
               'destination_id':'agent','api_key_env':'TEST_AGENT','timeout_seconds':10.,
               'max_output_tokens':4096,'max_input_bytes':600000,'compact_protocol':True,'thinking':'disabled'}
        settings={'agent':model,'guard':dict(model,destination_id='guard',api_key_env='TEST_GUARD')}
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'cancelled'
            args=parser().parse_args(['run','--cases','b-report','--output',str(output),'--jobs','2'])
            with patch('cli.config_and_keys',return_value=(settings,{'agent':'DUMMY','guard':'DUMMY'})),\
                 patch('broker.request.build_opener',return_value=Opener()),contextlib.redirect_stdout(io.StringIO()):
                result=run(args)
            self.assertEqual(result['manifest']['status'],'interrupted')
            self.assertTrue(cancelled.is_set())
            states=[json.loads(p.read_text()) for p in output.glob('*--*/*/lane-state.json')]
            self.assertEqual(len(states),2)
            self.assertTrue(all(s['phase']=='INTERRUPTED' for s in states))


    def test_invalid_guard_protocol_is_retained_without_credentials(self):
        envelope={'choices':[{'message':{'content':'{"bad_policy":true}'},'finish_reason':'stop'}],'usage':{'prompt_tokens':1,'completion_tokens':1}}
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return json.dumps(envelope).encode()
        class Opener:
            def open(self,*args,**kwargs):return Response()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            cfg={'model':'fixture','endpoint':'https://fixture.invalid','max_output_tokens':100,'timeout_seconds':5}
            with patch('broker.request.build_opener',return_value=Opener()),Broker(tmp,{'guard':cfg},{'guard':'NEVER_RECORD_THIS_KEY'},root/'calls.jsonl') as broker:
                with socket.socket(socket.AF_UNIX) as client:
                    client.connect(str(broker.path));client.sendall(json.dumps({'role':'guard','body':{'model':'fixture','stream':False,'max_tokens':100,'messages':[]}}).encode()+b'\n')
                    with client.makefile('rb') as f:reply=json.loads(f.readline())
            self.assertEqual(reply['value'],envelope)
            response=next((root/'guard-traces').glob('*-response.json'))
            self.assertEqual(json.loads(response.read_text())['value'],envelope)
            retained=''.join(p.read_text() for p in root.rglob('*.json'))
            self.assertNotIn('NEVER_RECORD_THIS_KEY',retained)

    def test_web_manual_selection_and_token_validation(self):
        import webui
        ready=threading.Event();servers=[];commands=[]
        class Process:
            def __init__(self,command,**kwargs):
                commands.append(command)
            def poll(self):
                return 0
        with tempfile.TemporaryDirectory() as tmp:
            args=argparse.Namespace(port=0,output_root=tmp)
            def server_factory(address,handler):
                server=ThreadingHTTPServer(address,handler)
                args.port=server.server_address[1];servers.append(server);ready.set()
                return server
            with patch('webui.ThreadingHTTPServer',side_effect=server_factory),\
                 patch('webui.ui_token',return_value='unit-test-token'),\
                 patch('webui.subprocess.Popen',side_effect=Process),contextlib.redirect_stdout(io.StringIO()):
                thread=threading.Thread(target=webui.serve,args=(args,),daemon=True);thread.start()
                self.assertTrue(ready.wait(3))
                def post(value,token='unit-test-token'):
                    req=request.Request(f'http://127.0.0.1:{args.port}/api/run',data=json.dumps(value).encode(),
                        headers={'X-Bench-Token':token,'Content-Type':'application/json'})
                    return request.urlopen(req,timeout=3)
                try:
                    with self.assertRaises(error.HTTPError) as caught:
                        post({'cases':['g-json']},token='invalid')
                    self.assertEqual(caught.exception.code,403);caught.exception.close()
                    with self.assertRaises(error.HTTPError) as caught:
                        post({'cases':['not-a-case']})
                    self.assertEqual(caught.exception.code,400);caught.exception.close()
                    with post({'cases':['g-json'],'attack':'encoded','thinking':'disabled'}) as response:
                        self.assertTrue(json.load(response)['started'])
                    self.assertEqual(len(commands),1)
                    command=commands[0]
                    self.assertEqual(command[command.index('--cases')+1],'g-json')
                    self.assertEqual(command[command.index('--attack')+1],'encoded')
                    self.assertEqual(command[command.index('--thinking')+1],'disabled')
                finally:
                    servers[0].shutdown();thread.join(3)



if __name__=='__main__':
    unittest.main()
