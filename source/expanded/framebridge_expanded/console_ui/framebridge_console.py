#!/usr/bin/env python3
"""FrameBridge's two-pane physical console, using the existing user's tmux.

No login, privilege, service or desktop configuration is changed here.
The normal entry point is called by an existing authenticated tty1 login.
"""
from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import sys
import time


HERE = Path(__file__).resolve().parent


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--node', default=os.environ.get('FRAMEBRIDGE_NODE', socket.gethostname()))
    p.add_argument('--socket')
    p.add_argument('--session')
    p.add_argument('--jobs-root', type=Path)
    p.add_argument('--apply', action='store_true', help='Set up without attaching a client')
    p.add_argument('--probe', action='store_true', help='Read current pane metadata only')
    p.add_argument('--reflow', action='store_true', help='Resize existing panes without taking focus')
    p.add_argument('--refresh-monitor', action='store_true')
    p.add_argument('--layout', choices=('auto','horizontal','vertical'), default='auto')
    p.add_argument('--monitor', action='store_true', help=argparse.SUPPRESS)
    a = p.parse_args()
    a.node = re.sub(r'[^A-Za-z0-9_-]', '_', a.node)[:64] or 'NODE'
    a.socket = a.socket or str(Path('/run/user') / str(os.getuid()) / (a.node.lower() + '-tty1') / 'console.sock')
    a.session = a.session or a.node.upper() + '-FBTERM'
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,90}', a.session):
        p.error('Session name must contain only letters, digits, underscore and hyphen')
    return a


class Console:
    def __init__(self, args):
        self.a = args
        self.session_target = args.session
        self.window = '=' + args.session + ':0'
        self.env = {k:v for k,v in os.environ.items() if k not in ('TMUX', 'TMUX_PANE')}
        self.env['LC_ALL'] = 'C.UTF-8'

    def tmux(self, *args, check=True):
        r = subprocess.run(['/usr/bin/tmux', '-S', self.a.socket, *args],
                           env=self.env, capture_output=True, text=True, timeout=8)
        if check and r.returncode:
            raise RuntimeError('tmux ' + args[0] + ': ' + r.stderr.strip())
        return r.stdout.strip()

    def exists(self):
        r = subprocess.run(['/usr/bin/tmux', '-S', self.a.socket, 'has-session', '-t', '=' + self.a.session],
                           env=self.env, capture_output=True, timeout=5)
        if r.returncode == 0:
            matches=[line.rsplit('|',1)[1] for line in self.tmux('list-sessions','-F','#{session_name}|#{session_id}').splitlines()
                     if line.rsplit('|',1)[0]==self.a.session]
            if len(matches)!=1:
                raise RuntimeError('Console session identity is ambiguous')
            self.session_target=matches[0]
            windows=[line.split('|',1) for line in self.tmux(
                'list-windows','-t',self.session_target,'-F','#{window_id}|#{window_name}').splitlines()]
            if not windows or any(len(row)!=2 or not re.fullmatch(r'@[0-9]+',row[0]) for row in windows):
                raise RuntimeError('Console window identity is invalid')
            selected=windows if len(windows)==1 else [row for row in windows if row[1]=='Console']
            if len(selected)!=1:
                raise RuntimeError('The dedicated Console window is absent or ambiguous')
            self.window=selected[0][0]
            return True
        return False

    def panes(self):
        fmt = '|'.join('#{' + f + '}' for f in ('pane_id','pane_pid','pane_current_command',
                      'pane_left','pane_top','pane_width','pane_height','pane_active','pane_dead',
                      '@framebridge_role'))
        rows = []
        for line in self.tmux('list-panes', '-t', self.window, '-F', fmt).splitlines():
            ident,pid,command,left,top,width,height,active,dead,role = line.split('|')
            rows.append(dict(id=ident,pid=int(pid),command=command,left=int(left),top=int(top),
                             width=int(width),height=int(height),active=active=='1',dead=dead=='1',role=role))
        return rows

    def monitor_command(self):
        command=['/usr/bin/python3', str(HERE / 'framebridge_console.py'), '--monitor', '--node', self.a.node]
        if getattr(self.a,'jobs_root',None) is not None:
            command.extend(['--jobs-root',str(self.a.jobs_root)])
        return shlex.join(command)

    def locate(self, rows):
        shell = next((p for p in rows if p['role']=='shell'), None)
        monitor = next((p for p in rows if p['role']=='monitor'), None)
        # Adopt only the known original dashboard, never an arbitrary Python pane.
        for p in rows:
            if p['role']:
                continue
            try:
                cmd = Path('/proc', str(p['pid']), 'cmdline').read_bytes().split(b'\0')
                names = {Path(x.decode(errors='replace')).name for x in cmd if x}
            except OSError:
                names = set()
            if not monitor and ('fbterm_live.py' in names or 'fbterm_dashboard.py' in names):
                monitor = p
            elif not shell and p['command']=='bash' and not p['dead']:
                shell = p
        return shell, monitor

    def layout(self, shell, monitor):
        active=next((p['id'] for p in self.panes() if p['active']),None)
        width,height = map(int, self.tmux('display-message','-p','-t',self.window,
                                        '#{window_width} #{window_height}').split())
        mode=getattr(self.a,'layout','auto')
        horizontal=mode=='horizontal' or (mode=='auto' and width>=160)
        if horizontal:
            self.tmux('select-layout','-t',self.window,'even-horizontal')
            current={p['id']:p for p in self.panes()}
            if current[monitor['id']]['left']>current[shell['id']]['left']:
                self.tmux('swap-pane','-d','-s',shell['id'],'-t',monitor['id'])
            monitor_width=max(2,min(max(2,width-21),max(24,round((width-1)*0.30))))
            self.tmux('resize-pane','-t',monitor['id'],'-x',str(monitor_width))
        else:
            self.tmux('select-layout','-t',self.window,'even-vertical')
            current={p['id']:p for p in self.panes()}
            if current[monitor['id']]['top']>current[shell['id']]['top']:
                self.tmux('swap-pane','-d','-s',shell['id'],'-t',monitor['id'])
            self.tmux('resize-pane','-t',monitor['id'],'-y',str(max(2,min(8,(height-3)//3))))
        if active:
            self.tmux('select-pane','-t',active)

    def apply(self):
        existed=self.exists()
        if not existed:
            parent=Path(self.a.socket).parent
            parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            st=parent.stat()
            if st.st_uid!=os.getuid() or st.st_mode & 0o022:
                raise RuntimeError('Console socket directory is not private to this user')
            size=os.get_terminal_size(0) if os.isatty(0) else os.terminal_size((240,67))
            self.session_target,self.window=self.tmux('new-session','-d','-P','-F','#{session_id}|#{window_id}',
                                 '-x',str(size.columns),'-y',str(size.lines),
                                 '-s',self.a.session,'-n','Console','-c',str(Path.home()),'/bin/bash --login').split('|')
        rows=self.panes()
        shell,monitor=self.locate(rows)
        if self.a.reflow:
            if shell and monitor and len(rows)==2:
                # Zoom is an explicit user choice; never undo it in a resize hook.
                if self.tmux('display-message','-p','-t',self.window,'#{window_zoomed_flag}')!='1':
                    self.layout(shell,monitor)
            return
        if not shell or shell['dead']:
            raise RuntimeError('Normal Bash pane is absent; refusing to replace another program')
        if len(rows)>2 or (len(rows)==2 and not monitor):
            raise RuntimeError('Unexpected console panes; refusing to modify them')
        original_pid=shell['pid']
        previous_focus=next((p['id'] for p in rows if p['active']),shell['id'])
        previous_zoom=self.tmux('display-message','-p','-t',self.window,'#{window_zoomed_flag}')=='1'
        if not monitor:
            ident=self.tmux('split-window','-d','-h','-l','30%','-t',shell['id'],'-P','-F','#{pane_id}',self.monitor_command())
            monitor=next(p for p in self.panes() if p['id']==ident)
        else:
            try:
                cmd=Path('/proc',str(monitor['pid']),'cmdline').read_bytes()
            except OSError:
                cmd=b''
            if self.a.refresh_monitor or monitor['dead'] or str(HERE/'framebridge_console.py').encode() not in cmd:
                self.tmux('respawn-pane','-k','-t',monitor['id'],self.monitor_command())
        for role,pane in [('shell',shell),('monitor',monitor)]:
            self.tmux('set-option','-p','-t',pane['id'],'@framebridge_role',role)
        self.tmux('set-option','-p','-t',monitor['id'],'remain-on-exit','on')
        self.tmux('select-pane','-t',shell['id'],'-T','SHELL | '+self.a.node+' | '+getpass.getuser())
        self.tmux('select-pane','-t',monitor['id'],'-T','FBTERM | ACTIVITY')
        self.tmux('set-window-option','-t',self.window,'automatic-rename','off')
        self.tmux('rename-window','-t',self.window,'Console')
        for key,value in [('pane-border-status','top'),('pane-border-style','fg=blue'),
                          ('pane-active-border-style','fg=cyan'),
                          ('pane-border-format',' #{?pane_active,>, } #{pane_title} '),
                          ('window-status-format',''),('window-status-current-format','')]:
            self.tmux('set-window-option','-t',self.window,key,value)
        for key,value in [('status','on'),('status-style','bg=blue,fg=white'),
                          ('status-left','#[bold] FRAMEBRIDGE #[default] | '+self.a.node+' | '),
                          ('status-left-length','80'),('status-right-length','110'),
                          ('status-right',' Ctrl+B, then o: switch | z: zoom | %H:%M %Z ')]:
            self.tmux('set-option','-t',self.session_target,key,value)
        self.tmux('set-option','-t',self.session_target,'mouse','off')
        # This server is dedicated to FrameBridge; do not alter global tmux configuration.
        for key in ('z','C-z','Z'):
            self.tmux('bind-key','-T','prefix',key,'resize-pane','-Z')
        # Reattachment must preserve the user's zoom and pane focus.
        if not previous_zoom:
            self.layout(shell,monitor)
        reflow=['/usr/bin/python3',str(HERE/'framebridge_console.py'),'--node',self.a.node,
                '--socket',self.a.socket,'--session',self.a.session,
                '--layout',getattr(self.a,'layout','auto'),'--reflow']
        if getattr(self.a,'jobs_root',None) is not None:
            reflow.extend(['--jobs-root',str(self.a.jobs_root)])
        command=shlex.join(reflow)
        self.tmux('set-hook','-t',self.session_target,'client-resized[90]','run-shell -b '+shlex.quote(command))
        self.tmux('set-hook','-t',self.session_target,'client-attached[90]','run-shell -b '+shlex.quote(command))
        self.tmux('select-window','-t',self.window)
        selected=previous_focus if existed else shell['id']
        self.tmux('select-pane','-t',selected)
        if previous_zoom and self.tmux('display-message','-p','-t',self.window,'#{window_zoomed_flag}')!='1':
            self.tmux('resize-pane','-Z','-t',selected)
        current=next(p for p in self.panes() if p['id']==shell['id'])
        if current['pid']!=original_pid:
            raise RuntimeError('Bash identity changed unexpectedly')


def supervise(node, jobs_root=None):
    command=['/usr/bin/python3','-u',str(HERE/'fbterm_dashboard.py'),'--node',node]
    if jobs_root is not None:
        command.extend(['--root',str(jobs_root)])
    while True:
        try:
            result=subprocess.run(command)
            print('\r\nFBTERM monitor stopped (exit '+str(result.returncode)+'). Restarting in 2 seconds.',flush=True)
            print('Ctrl+B o switches to the shell.',flush=True)
            time.sleep(2)
        except KeyboardInterrupt:
            print('\r\nFBTERM monitor restarting. Ctrl+B o switches to the shell.',flush=True)
            time.sleep(1)


def main():
    args=arguments()
    if args.monitor:
        supervise(args.node, args.jobs_root)
        return 0
    console=Console(args)
    if args.probe:
        print(json.dumps({'node':args.node,'socket':args.socket,'session':args.session,
                          'panes':console.panes() if console.exists() else [],'login':'existing-authenticated-login'}))
        return 0
    if not (args.apply or args.reflow):
        if not os.isatty(0) or os.ttyname(0)!='/dev/tty1' or os.environ.get('TMUX') or os.environ.get('SSH_TTY'):
            print('Open this console from an authenticated tty1 login. --probe is read-only.',file=sys.stderr)
            return 1
    parent=Path(args.socket).parent
    parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    info=parent.stat()
    if info.st_uid!=os.getuid() or info.st_mode & 0o022:
        raise RuntimeError('Console socket directory is not private to this user')
    fd=os.open(parent/'console-layout.lock',os.O_CREAT|os.O_RDWR|os.O_CLOEXEC|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid():
            raise RuntimeError('Invalid console layout lock')
        fcntl.flock(fd,fcntl.LOCK_EX)
        console.apply()
    finally:
        os.close(fd)
    if args.apply:
        print(json.dumps({'node':args.node,'status':'READY','panes':console.panes()}))
    elif not args.reflow:
        os.execve('/usr/bin/tmux',['tmux','-S',args.socket,'attach-session','-t',console.session_target],console.env)
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError,OSError,subprocess.TimeoutExpired) as e:
        print('FrameBridge console: '+str(e),file=sys.stderr)
        raise SystemExit(1)
