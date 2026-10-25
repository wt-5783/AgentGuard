"""Trusted per-task capabilities, recorded in both lane settings."""
from pathlib import PurePosixPath


def write_paths(value):
    if not isinstance(value,list) or len(value)>16:
        raise ValueError('允许写入路径须为最多 16 项的列表')
    result=[]
    for path in value:
        if (not isinstance(path,str) or '\0' in path or len(path)>1000
            or not path.startswith('/workspace/') or '..' in PurePosixPath(path).parts
            or str(PurePosixPath(path))!=path):
            raise ValueError('允许写入路径必须是 /workspace/ 内的规范绝对路径')
        if path not in result:
            result.append(path)
    return result


def target_settings(row):
    paths=write_paths(row['write_paths'])
    labels={'schema_version':'0.1','confidentiality':[],'allowed_uses':None,
        'destination_ids':None,'model_destination_ids':None,'integrity':['external'],'field_restrictions':{}}
    resources=[{'schema_version':'0.1','scope_id':f'output_{i}','kind':'path','root':path,'labels':labels}
               for i,path in enumerate(paths)]
    return {'resources':resources,
        'tool_target_scopes':{'write_text':[f'output_{i}' for i in range(len(paths))],'delete_path':[]},
        'readonly_command_files':row.get('readonly_command_files',[])}
