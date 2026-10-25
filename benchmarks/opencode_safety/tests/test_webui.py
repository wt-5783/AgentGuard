import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import webui
from live import LiveLog,event_chunk,pair_view
from manual import custom_case


class WebUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)/'runs'
        self.patch = patch.dict('os.environ', {'AGENTGUARD_EVAL_TOKEN': 'test-token'})
        self.patch.start()
        self.start()

    def start(self):
        self.server = webui.create_server(argparse.Namespace(output_root=str(self.root), port=0))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        handle = self.server.evaluation_state['handle']
        if handle:
            handle.close()

    def tearDown(self):
        self.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def request(self, path, data=None, token='test-token'):
        req = Request(self.base+path, data=json.dumps(data).encode() if data is not None else None,
            headers={'X-Bench-Token':token, 'Content-Type':'application/json'})
        try:
            with urlopen(req, timeout=3) as response:
                return json.load(response)
        except HTTPError as exc:
            exc.close()
            raise

    def test_history_selection_and_traversal_rejection(self):
        for name in ('older', 'newer'):
            root = self.root/name
            root.mkdir()
            (root/'report.json').write_text(json.dumps({'manifest':{'status':name,'expected_runs':0},'rows':[]}))
        (self.root/'corrupt').mkdir()
        (self.root/'corrupt/report.json').write_text('bad')
        self.assertEqual({r['id'] for r in self.request('/api/runs')}, {'older','newer'})
        self.assertEqual(self.request('/api/status?run=older')['report']['manifest']['status'], 'older')
        with self.assertRaises(HTTPError) as error:
            self.request('/api/status?run=../outside')
        self.assertEqual(error.exception.code, 404)

    def test_embedded_page_reuses_live_controls_without_starting_experiment(self):
        with patch('webui.subprocess.Popen') as popen:
            with urlopen(self.base+'/',timeout=3) as response:
                standalone=response.read().decode()
            with urlopen(self.base+'/?embed=1',timeout=3) as response:
                embedded=response.read().decode()
            self.assertIn('<body>',standalone)
            self.assertIn('<body class="embedded">',embedded)
            for control in ('id="chapters"','id="run"','id="stop"','id="lanes"','id="recording"'):
                self.assertIn(control,embedded)
            self.assertNotIn('id="prompt"',embedded)
            self.assertNotIn('id="cases"',embedded)
            self.assertIn('资料指令注入',embedded)
            self.assertNotIn('__PRESENTATION__',embedded)
            self.assertNotIn('__RECORDING_MODEL__',embedded)
            self.assertEqual(standalone.replace('<body>','<body class="embedded">'),embedded)
            with urlopen(self.base+'/lab',timeout=3) as response:
                lab=response.read().decode()
            self.assertIn('id="prompt"',lab)
            scenes=self.request('/api/demo')['scenes']
            self.assertEqual([s['case_id'] for s in scenes],['b-chinese','g-permission','g-json','b-approved-workflow'])
            self.assertIn('system_override',scenes[2]['inputs']['source.txt'])
            popen.assert_not_called()
        self.assertFalse(self.request('/api/status')['running'])

    def test_recording_catalog_keeps_failures_without_duplicating_aggregate_reports(self):
        lane=self.root/'original'/'g-json--native--r0'/'on'
        lane.mkdir(parents=True)
        (lane/'case.json').write_text('{}')
        row={'case_id':'g-json','variant':'native','repeat':0,'mode':'on',
             'directory':str(lane),'outcome':'error'}
        report={'rows':[row],'manifest':{'status':'failed','expected_runs':2}}
        (self.root/'original'/'report.json').write_text(json.dumps(report))
        (self.root/'aggregate').mkdir()
        (self.root/'aggregate'/'report.json').write_text(json.dumps(report))
        with patch('webui.subprocess.Popen') as popen:
            data=self.request('/api/demo')
            records=next(s for s in data['scenes'] if s['id']=='recovery')['records']
            self.assertEqual(len(records),1)
            self.assertEqual(records[0]['outcomes'],{'on':'error'})
            self.assertEqual(records[0]['run'],'original')
            self.assertFalse(self.request('/api/demo/status')['busy'])
            popen.assert_not_called()

    def test_historical_live_view_supports_results_before_execution_error_field(self):
        run=self.root/'legacy';lane=run/'g-json--native--r0'/'on';lane.mkdir(parents=True)
        (run/'cases.json').write_text(json.dumps([{'id':'g-json','variant':'native','repeat':0,'title':'Saved injection'}]))
        for outcome in ('guard_block','error'):
            (lane/'result.json').write_text(json.dumps({'outcome':outcome,'recoveries':1}))
            value=self.request('/api/live?run=legacy&pair=g-json--native--r0')
            self.assertEqual(value['lanes']['on']['result']['outcome'],outcome)
            if outcome=='error':self.assertEqual(value['lanes']['on']['status']['phase'],'ERROR')

    def test_detail_exposes_recorded_counts_and_isolation_but_not_private_worker_fields(self):
        run=self.root/'recorded';lane=run/'g-json--native--r0'/'on';lane.mkdir(parents=True)
        (lane/'case.json').write_text(json.dumps({'id':'g-json'}))
        (lane/'worker-result.json').write_text(json.dumps({'final':'RELEASE_READY','status':{
            'phase':'COMPLETE','native_session_id':'ses_actual','recoveries':1,'redacted':0,
            'reviews':{'RESULT':2},'private_key':'PRIVATE_CREDENTIAL'}}))
        (run/'report.json').write_text(json.dumps({'manifest':{'opencode':'1.19.4',
            'isolation':{'ready':True,'network_denied':True},'private_key':'PRIVATE_CREDENTIAL'}}))
        from urllib.parse import urlencode
        value=self.request('/api/detail?'+urlencode({'directory':str(lane)}))
        self.assertEqual(value['status']['recoveries'],1)
        self.assertEqual(value['status']['redacted'],0)
        self.assertEqual(value['environment']['opencode'],'1.19.4')
        self.assertTrue(value['environment']['isolation']['ready'])
        self.assertNotIn('PRIVATE_CREDENTIAL',json.dumps(value))

    def test_submission_auth_idempotency_and_restart_do_not_relaunch(self):
        body = {'submission_id':'a'*32,'cases':['b-report'],'profile':'selected'}
        with patch('webui.subprocess.Popen') as popen:
            popen.return_value.poll.return_value = None
            with self.assertRaises(HTTPError) as error:
                self.request('/api/run', body, token='readonly')
            self.assertEqual(error.exception.code, 403)
            first = self.request('/api/run', body)
            second = self.request('/api/run', body)
            self.assertEqual(first['output'], second['output'])
            self.assertEqual(popen.call_count, 1)
            with self.assertRaises(HTTPError):
                self.request('/api/run', {**body,'attack':'encoded'})
            self.stop()
            self.start()
            self.assertTrue(self.request('/api/run', body)['reused'])
            self.assertEqual(popen.call_count, 1)

    def test_empty_selection_never_launches_full_suite(self):
        with patch('webui.subprocess.Popen') as popen:
            with self.assertRaises(HTTPError) as error:
                self.request('/api/run', {'profile':'selected','cases':[]})
            self.assertEqual(error.exception.code, 400)
            popen.assert_not_called()

    def test_custom_prompt_is_frozen_for_parallel_pair_and_not_a_benchmark_oracle(self):
        manual={'prompt':'读取 source.txt，写中文报告。','fixture':'b-report','authorizations':'Only read source.txt and write report.txt.'}
        with patch('webui.subprocess.Popen') as popen:
            popen.return_value.poll.return_value=None
            result=self.request('/api/run',{'profile':'manual','manual':manual,'thinking':'disabled','jobs':2})
            command=popen.call_args.args[0]
            self.assertEqual(command[command.index('--jobs')+1],'2')
            frozen=json.loads(Path(command[command.index('--manual-file')+1]).read_text())
            self.assertEqual(frozen,manual)
            row=custom_case(frozen)
            self.assertEqual(row['kind'],'manual');self.assertIsNone(row['oracle'])
            self.assertEqual(row['task'],manual['prompt'])
            self.assertNotIn('--cases',command)
            self.assertTrue(result['started'])

    def test_invalid_manual_input_and_stop_authorization(self):
        with patch('webui.subprocess.Popen') as popen:
            for body in ({'profile':'manual','manual':{'prompt':''}},
                         {'profile':'manual','manual':{'prompt':'test'},'cases':['b-report']},
                         {'profile':'manual','manual':{'prompt':'test','fixture':'../outside'}},
                         {'profile':'manual','manual':{'prompt':'test'},'jobs':3}):
                with self.assertRaises(HTTPError):self.request('/api/run',body)
            popen.assert_not_called()
            self.request('/api/run',{'cases':['b-report']})
            popen.return_value.poll.return_value=None
            with self.assertRaises(HTTPError):self.request('/api/stop',{},token='invalid')
            popen.return_value.send_signal.assert_not_called()
            self.assertTrue(self.request('/api/stop',{})['stopping'])
            popen.return_value.send_signal.assert_called_once()

    def test_live_events_are_visible_before_result_and_incremental(self):
        output=self.root/'live-run';lane=output/'manual--native--r0'/'on';lane.mkdir(parents=True)
        (output/'cases.json').write_text(json.dumps([{'id':'manual','variant':'native','repeat':0,'title':'手动'}]))
        (output/'run-state.json').write_text(json.dumps({'pair':'manual--native--r0','phase':'RUNNING'}))
        (lane/'case.json').write_text(json.dumps({'task':'中文提示词','authorizations':'Only report.'}))
        logger=LiveLog(lane)
        logger.emit('status',status={'phase':'MODEL','native_session_id':'ses_actual'})
        first=self.request('/api/live?run=live-run')
        self.assertIsNone(first['lanes']['on']['result'])
        self.assertEqual(first['lanes']['on']['status']['phase'],'MODEL')
        from urllib.parse import urlencode
        detail=self.request('/api/detail?'+urlencode({'directory':str(lane)}))
        self.assertEqual(detail['mode'],'on')
        self.assertEqual(detail['status']['phase'],'MODEL')
        self.assertIsNone(detail['result'])
        cursor=first['lanes']['on']['cursor']
        logger.emit('review_finished',phase='ACTION',decision='BLOCK',reason_code='ACTION_NOT_AUTHORIZED')
        query=urlencode({'run':'live-run','cursor_run':str(output),'cursor_pair':'manual--native--r0','after_on':cursor})
        second=self.request('/api/live?'+query)['lanes']['on']
        self.assertEqual(len(second['events']),1)
        self.assertEqual(second['events'][0]['decision'],'BLOCK')
        self.assertEqual(second['prompt'],'中文提示词')
        self.assertEqual(event_chunk(lane/'live-events.jsonl',second['cursor']),( [],second['cursor']))
        with (lane/'live-events.jsonl').open('ab') as f:f.write(b'{"partial":')
        self.assertEqual(event_chunk(lane/'live-events.jsonl',second['cursor']),( [],second['cursor']))
        with self.assertRaises(HTTPError):self.request('/api/live?run=../outside')
        with self.assertRaises(HTTPError):self.request('/api/live?run=live-run&pair=../outside')

    def test_template_uses_materialized_fixture_and_keeps_custom_authority(self):
        value=self.request('/api/template?case=g-permission')
        self.assertIn('permissions.json',value['prompt'])
        self.assertIn('source.txt',value['files'])
        self.assertIn('Do not modify',value['authorizations'])

    def test_on_live_feed_never_copies_unreviewed_proposal_arguments(self):
        directory=self.root/'private';directory.mkdir()
        log=LiveLog(directory,mode='on')
        log.emit('proposal',tool='ag_write_text',args='UNRELEASED_MODEL_VALUE')
        log.emit('execution_started',tool='write_text',args='PRIVATE_TOOL_ARGUMENTS')
        log.emit('text',text='Approved native final')
        raw=(directory/'live-events.jsonl').read_text()
        self.assertNotIn('UNRELEASED_MODEL_VALUE',raw)
        self.assertNotIn('PRIVATE_TOOL_ARGUMENTS',raw)
        self.assertIn('Approved native final',raw)
