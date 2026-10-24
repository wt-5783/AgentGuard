"""Linux execution boundary. No host home, host network or host process tree."""
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
PYTHON = Path.home() / '.local/share/agentguard/connector-venv/bin/python'
OPENCODE = Path.home() / '.local/lib/node_modules/opencode-ai'
SDK = Path.home() / '.config/opencode/node_modules'


def system_mounts():
    args = ['--ro-bind', '/usr', '/usr']
    for name in ('bin', 'lib', 'lib64', 'sbin'):
        path = Path('/') / name
        if path.is_symlink():
            args += ['--symlink', os.readlink(path), str(path)]
        elif path.exists():
            args += ['--ro-bind', str(path), str(path)]
    return args


def base():
    if platform.system() != 'Linux':
        raise RuntimeError('仅实现 Linux 隔离；Windows 尚未实现')
    if not shutil.which('bwrap'):
        raise RuntimeError('需要安装 bubblewrap')
    return ['bwrap', '--die-with-parent', '--new-session', '--unshare-all',
            '--cap-drop', 'ALL', *system_mounts(), '--proc', '/proc', '--dev', '/dev',
            '--size', str(64*1024*1024), '--tmpfs', '/tmp', '--dir', '/etc', '--dir', '/home/bench',
            '--clearenv', '--setenv', 'HOME', '/home/bench',
            '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'LANG', 'C.UTF-8']


def tool_command(argv, workspace='/workspace', victim='/victim'):
    return [*base(), '--bind', workspace, '/workspace', '--bind', victim, '/victim',
            '--chdir', '/workspace', *argv]


def interpreter_mounts():
    root=PYTHON.resolve().parent.parent
    # System interpreters are already covered by the read-only /usr mount.
    # A normal venv may link to /usr/bin/python; never derive a / bind from it.
    if root.is_relative_to('/usr'):
        return []
    if root in (Path('/'),Path('/home'),Path('/root'),Path.home(),Path.home().parent):
        raise RuntimeError('Python 运行时目录过宽，不能安全挂载')
    mounts=['--ro-bind',str(root),str(root)]
    # uv venvs may name an unversioned runtime-directory symlink. Preserve
    # that exact alias without exposing the runtime's parent directory.
    link=PYTHON
    seen=set()
    while link.is_symlink():
        if link in seen:
            raise RuntimeError('Python 可执行文件符号链接循环')
        seen.add(link)
        target=Path(os.readlink(link))
        link=target if target.is_absolute() else link.parent/target
        alias=link.parent.parent
        if alias!=root and alias.resolve()==root:
            mounts+=['--ro-bind',str(root),str(alias)]
    return mounts


def command(lane, socket_directory=None):
    lane = Path(lane).resolve()
    args = base()
    # Only executable/dependency trees are exposed, all read-only.
    for path in (PYTHON.parent.parent, OPENCODE, SDK):
        args += ['--ro-bind', str(path), str(path)]
    args += interpreter_mounts()
    args += ['--ro-bind', str(ROOT), '/app', '--bind', str(lane), '/out',
             '--size', str(32*1024*1024), '--tmpfs', '/workspace',
             '--size', str(8*1024*1024), '--tmpfs', '/victim']
    if socket_directory:
        args += ['--ro-bind', str(socket_directory), '/broker']
    args += ['--chdir', '/workspace', '--setenv', 'AGENTGUARD_V2_CLOUD_ENABLED', '1',
             '--setenv', 'BENCH_AGENT_KEY', 'BROKER_ONLY_NO_REAL_KEY',
             '--setenv', 'BENCH_GUARD_KEY', 'BROKER_ONLY_NO_REAL_KEY',
             str(PYTHON), '/app/worker.py']
    return args


def limited(argv, memory_mb=1536):
    return ['systemd-run', '--user', '--scope', '--quiet',
            '-p', f'MemoryMax={memory_mb}M', '-p', 'MemorySwapMax=0',
            '-p', 'TasksMax=96', '-p', 'CPUQuota=100%', *argv]


def doctor(output):
    base()  # unsupported platforms fail before creating files or launching tools
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    for p in (PYTHON, OPENCODE, SDK):
        if not p.exists():
            raise RuntimeError(f'缺少已安装依赖：{p}')
    marker = output / 'host-only-canary'
    marker.write_text('HOST_UNCHANGED')
    lane=output/'probe'
    for name in ('workspace','victim'):
        (lane/name).mkdir(parents=True)
    host_socket=socket.socket()
    host_socket.bind(('127.0.0.1',0))
    host_socket.listen(1)
    probe = '''import os,socket,json,pathlib
before=pathlib.Path('/proc/self/ns/net').readlink().as_posix()
s=socket.socket();s.settimeout(0.2)
try:s.connect(('1.1.1.1',443));network=True
except OSError:network=False
s.close();s=socket.socket();s.settimeout(.2)
try:s.connect(('127.0.0.1',%d));host_loopback=True
except OSError:host_loopback=False
try:pathlib.Path('/usr/bench-write-probe').write_text('x');readonly=False
except OSError:readonly=True
fs=os.statvfs('/workspace')
print(json.dumps({'network_denied':not network,'host_loopback_denied':not host_loopback,'scratch_limited':fs.f_blocks*fs.f_frsize==32*1024*1024,'usr_readonly':readonly and bool(os.statvfs('/usr').f_flag&os.ST_RDONLY),'app_readonly':bool(os.statvfs('/app').f_flag&os.ST_RDONLY),'host_canary_hidden':not pathlib.Path(%s).exists(),'host_configuration_hidden':all(not pathlib.Path(p).exists() for p in %s),'net_namespace':before,'pid_namespace':str(pathlib.Path('/proc/self/ns/pid').readlink())}))
''' % (host_socket.getsockname()[1],repr(str(marker.resolve())),repr([str(Path.home()/'.config/agentguard/live.env'),str(Path.home()/'.config/opencode/opencode.json')]))
    try:
        proc = subprocess.run(limited(command(lane)[:-2]+[str(PYTHON),'-c',probe]),
                              capture_output=True, text=True, timeout=20)
    finally:
        host_socket.close()
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or '隔离探针失败')
    result = json.loads(proc.stdout)
    result['host_canary_unchanged'] = marker.read_text() == 'HOST_UNCHANGED'
    result['different_net_namespace'] = result['net_namespace'] != str(Path('/proc/self/ns/net').readlink())
    result['different_pid_namespace'] = result['pid_namespace'] != str(Path('/proc/self/ns/pid').readlink())
    result['ready'] = all(v for k, v in result.items() if k not in ('net_namespace', 'pid_namespace'))
    (output / 'isolation.json').write_text(json.dumps(result, indent=2))
    if not result['ready']:
        raise RuntimeError('隔离检查未通过')
    return result
