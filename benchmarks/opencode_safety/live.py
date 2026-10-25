"""Trusted, append-only display events and small incremental browser reads."""
import json
from pathlib import Path
import threading
import time

from scoring import atomic_text

_log=None


def configure(directory,mode=None):
    global _log
    _log=LiveLog(directory,mode)
    return _log


def emit(kind,**data):
    if _log is not None:
        _log.emit(kind,**data)


def display_args(args):
    return json.dumps(args,ensure_ascii=False)[:6000]


def read_json(path,default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError,ValueError):
        return default


class LiveLog:
    def __init__(self,directory,mode=None):
        self.directory=Path(directory)
        self.mode=mode
        self.lock=threading.Lock()
        self.sequence=0

    def emit(self,kind,**data):
        with self.lock:
            if self.mode=='on' and kind in ('proposal','execution_started'):
                data.pop('args',None)
            self.sequence+=1
            event={'seq':self.sequence,'time':time.time(),'kind':kind,**data}
            with (self.directory/'live-events.jsonl').open('a') as f:
                f.write(json.dumps(event,ensure_ascii=False)+'\n')
            if kind=='status':
                atomic_text(self.directory/'live-state.json',json.dumps(event,ensure_ascii=False))


def event_chunk(path,offset=0):
    """Only complete lines advance the byte cursor; never expose partial JSON."""
    path=Path(path)
    if not path.is_file():
        return [],0
    size=path.stat().st_size
    if offset<0 or offset>size:
        raise ValueError('无效事件位置')
    with path.open('rb') as f:
        f.seek(offset)
        raw=f.read(256*1024)
    end=raw.rfind(b'\n')+1
    return [json.loads(line) for line in raw[:end].splitlines() if line],offset+end


def pair_view(output,pair=None,offsets=None,cursor_pair=None):
    output=Path(output)
    state=read_json(output/'run-state.json',{})
    cases=read_json(output/'cases.json',[])
    choices=[{'key':f'{c["id"]}--{c["variant"]}--r{c["repeat"]}',
              'title':c['title'],'case_id':c['id']} for c in cases]
    key=pair or state.get('pair') or (choices[-1]['key'] if choices else None)
    if key is not None and key not in {c['key'] for c in choices}:
        raise ValueError('未知双组任务')
    if cursor_pair is not None and cursor_pair!=key:
        offsets={}
    value={'pair':key,'pairs':choices,'run_state':state,'lanes':{}}
    for mode in ('off','on'):
        lane=output/key/mode if key else None
        events,cursor=event_chunk(lane/'live-events.jsonl',(offsets or {}).get(mode,0)) if lane else ([],0)
        case=read_json(lane/'case.json',{}) if lane else {}
        result=read_json(lane/'result.json') if lane else None
        worker=read_json(lane/'worker-result.json',{}) if lane else {}
        live=read_json(lane/'live-state.json',{}) if lane else {}
        host=read_json(lane/'lane-state.json',{}) if lane else {}
        status=live.get('status') or worker.get('status') or {'phase':host.get('phase','WAITING')}
        if result:
            # Early saved runs predate execution_error. Their explicit error outcome
            # remains a failure; missing metadata must not crash historical viewing.
            failed=result.get('execution_error') or result.get('outcome')=='error'
            status=dict(status,phase='ERROR' if failed else status.get('phase','COMPLETE'))
        if host.get('phase') in ('ERROR','INTERRUPTED'):
            status=dict(status,phase=host['phase'])
        value['lanes'][mode]={'status':status,'events':events,'cursor':cursor,'result':result,
            'prompt':case.get('task'),'authorizations':case.get('authorizations'),
            'fixture':case.get('source'),'final':worker.get('final'),
            'directory':str(lane) if lane and lane.exists() else None}
    return value
