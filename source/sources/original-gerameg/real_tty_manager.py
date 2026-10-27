"""Real terminals for the existing FrameBridge CUA-TTY Manager.
Transport identity and same-UID socket policy are unchanged. No Owner prompts.
Only this module's tmux sessions are operated on; no existing tty is hijacked.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import subprocess
import time
import uuid
from framebridge_cua_tty.core import TerminalDenied

TMUX_SOCKET = '/run/user/1000/framebridge-alpha01-cua/real-tmux.sock'
KEY_NAMES = {'UP':'Up','DOWN':'Down','LEFT':'Left','RIGHT':'Right',
 'ENTER':'Enter','RETURN':'Enter','TAB':'Tab','SHIFT_TAB':'BTab','ESC':'Escape',
 'BACKSPACE':'BSpace','DELETE':'DC','INSERT':'IC','HOME':'Home','END':'End',
 'PAGE_UP':'PPage','PAGE_DOWN':'NPage','SPACE':'Space'}
KEY_NAMES.update({'CTRL_'+c:'C-'+c.lower() for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'})
KEY_NAMES.update({'F'+str(i):'F'+str(i) for i in range(1,13)})

@dataclass
class RealSession:
    session_id: str
    name: str
    pane: str
    born: float
    program: str = 'bash'
    sequence: int = 0

class RealTerminalMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._real = {}

    def _tmux(self, *args, data=None, optional=False):
        env={k:v for k,v in os.environ.items() if k not in ('TMUX','TMUX_PANE')}
        env.update({'HOME':str(Path.home()),'USER':'gerabh','LOGNAME':'gerabh',
                    'LANG':'C.UTF-8','TERM':'xterm-256color','HISTFILE':'/dev/null'})
        try:
            p=subprocess.run(['/usr/bin/tmux','-S',TMUX_SOCKET,'-f','/dev/null',*args],
                input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,timeout=3)
        except subprocess.TimeoutExpired:
            raise TerminalDenied('TMUX_COMMAND_TIMEOUT') from None
        except OSError:
            raise TerminalDenied('TMUX_NOT_AVAILABLE') from None
        if p.returncode and not optional:
            raise TerminalDenied('TMUX_COMMAND_FAILED')
        return p

    def _info(self, session):
        keys=['pane_width','pane_height','cursor_x','cursor_y','pane_dead',
              'pane_dead_status','pane_pid']
        fmt='|'.join('#{'+key+'}' for key in keys)
        p=self._tmux('display-message','-p','-t',session.pane,fmt)
        parts=p.stdout.decode('utf-8','replace').rstrip('\n').split('|')
        if len(parts)!=len(keys):raise TerminalDenied('TMUX_METADATA_INVALID')
        info=dict(zip(keys,parts))
        for key in ['pane_current_path','pane_current_command','pane_tty']:
            p=self._tmux('display-message','-p','-t',session.pane,'#{'+key+'}')
            info[key]=p.stdout.decode('utf-8','replace').rstrip('\n')
        return info

    def open(self, program='demo-readonly', rows=24, cols=80):
        if program!='bash':
            with self._lock:
                self.reap()
                if len(self._sessions)+len(self._real)>=self.max_sessions:
                    raise TerminalDenied('SESSION_LIMIT')
                return super().open(program,rows,cols)
        if not (5<=rows<=100 and 20<=cols<=220):raise TerminalDenied('SIZE_INVALID')
        with self._lock:
            self.reap()
            if len(self._sessions)+len(self._real)>=self.max_sessions:
                raise TerminalDenied('SESSION_LIMIT')
            sid=uuid.uuid4().hex
            name='fb-real-'+sid
            pane=None
            try:
                p=self._tmux('new-session','-d','-s',name,'-x',str(cols),'-y',str(rows),
                    '-c',str(Path.home()),'-P','-F','#{pane_id}',
                    '/usr/bin/bash --noprofile --norc -i')
                pane=p.stdout.decode().strip()
                if not re.fullmatch(r'%[0-9]+',pane):raise TerminalDenied('PANE_ID_INVALID')
                self._tmux('set-option','-p','-t',pane,'remain-on-exit','on')
                self._tmux('set-option','-t',name,'status','off')
                self._tmux('set-option','-t',name,'history-limit','10000')
                s=RealSession(sid,name,pane,time.monotonic())
                self._real[sid]=s
                info=self._info(s)
                return {'session_id':sid,'program':'bash','state':'OPEN','backend':'tmux',
                    'real_terminal':True,'tty':info['pane_tty'],'pid':int(info['pane_pid']),
                    'cwd':info['pane_current_path'],'persistent_between_chat_calls':True,
                    'attach_command':'tmux -S '+TMUX_SOCKET+' attach-session -t '+name}
            except BaseException:
                self._real.pop(sid,None)
                self._tmux('kill-session','-t',name,optional=True)
                raise

    def status(self):
        with self._lock:
            value=super().status()
            sessions=[]
            for sid,s in list(self._real.items()):
                try:
                    info=self._info(s)
                except TerminalDenied:
                    self._real.pop(sid,None)
                    continue
                sessions.append({'session_id':sid,'program':'bash',
                    'running':info['pane_dead']!='1','tty':info['pane_tty'],
                    'cwd':info['pane_current_path'],'command':info['pane_current_command']})
            value.update({'active_sessions':value['active_sessions']+len(sessions),
                'real_sessions':sessions,'real_terminal_enabled':True,
                'real_programs':['bash'],'input_policy':'local-uid-1000',
                'owner_device_connected':False,'owner_signature_required':False,
                'input_enabled':'same-uid-1000','real_idle_timeout_s':None})
            return value

    def observe(self, sid):
        with self._lock:
            if sid not in self._real:return super().observe(sid)
            s=self._real[sid];info=self._info(s)
            text=self._tmux('capture-pane','-p','-t',s.pane).stdout.decode('utf-8','replace')
            lines=text.splitlines()
            # The original RPC response has a finite frame size, not an approval gate.
            limited=False
            import json
            while len(json.dumps(lines,ensure_ascii=True).encode())>48000:
                lines=lines[:-1];limited=True
            return {'session_id':sid,'program':'bash','real_terminal':True,'backend':'tmux',
                'running':info['pane_dead']!='1','exit_code':int(info['pane_dead_status']) if info['pane_dead_status'] else None,
                'cwd':info['pane_current_path'],'current_command':info['pane_current_command'],
                'tty':info['pane_tty'],'pid':int(info['pane_pid']),'sequence':s.sequence,
                'screen':{'rows':int(info['pane_height']),'cols':int(info['pane_width']),
                    'cursor':{'row':int(info['cursor_y']),'col':int(info['cursor_x'])},'lines':lines},
                'screen_truncated':limited,'screen_sha256':hashlib.sha256(text.encode()).hexdigest()}

    def text(self, sid, text, approved_context=None):
        with self._lock:
            if sid not in self._real:return super().text(sid,text,approved_context=approved_context)
            if not isinstance(text,str) or not text or '\x00' in text or len(text.encode())>16384:
                raise TerminalDenied('TEXT_INVALID_OR_TOO_LARGE')
            s=self._real[sid];payload=text.encode('utf-8')
            self._authorize(s,'text.type',payload,approved_context)
            if self._info(s)['pane_dead']=='1':raise TerminalDenied('SESSION_EXITED')
            name='fb-input-'+sid
            self._tmux('load-buffer','-b',name,'-',data=payload)
            try:self._tmux('paste-buffer','-d','-r','-b',name,'-t',s.pane)
            finally:self._tmux('delete-buffer','-b',name,optional=True)
            s.sequence+=1
            return {'session_id':sid,'state':'TYPED','submitted_chars':len(text),'sequence':s.sequence}

    def key(self, sid, key, approved_context=None):
        with self._lock:
            if sid not in self._real:return super().key(sid,key,approved_context=approved_context)
            if not isinstance(key,str) or key not in KEY_NAMES:raise TerminalDenied('KEY_NOT_ALLOWLISTED')
            s=self._real[sid]
            self._authorize(s,'key.send',key.encode(),approved_context)
            if self._info(s)['pane_dead']=='1':raise TerminalDenied('SESSION_EXITED')
            self._tmux('send-keys','-t',s.pane,KEY_NAMES[key]);s.sequence+=1
            return {'session_id':sid,'state':'KEY_SENT','submitted':key,'sequence':s.sequence}

    def resize(self, sid, rows, cols):
        with self._lock:
            if sid not in self._real:return super().resize(sid,rows,cols)
            if type(rows) is not int or type(cols) is not int or not (5<=rows<=100 and 20<=cols<=220):
                raise TerminalDenied('SIZE_INVALID')
            self._tmux('resize-window','-t',self._real[sid].pane,'-x',str(cols),'-y',str(rows))
            self._real[sid].sequence+=1
            return {'session_id':sid,'rows':rows,'cols':cols}

    def abort(self, sid):
        with self._lock:
            if sid not in self._real:return super().abort(sid)
            s=self._real[sid]
            self._tmux('kill-session','-t',s.name)
            self._real.pop(sid,None)
            probe=self._tmux('has-session','-t',s.name,optional=True)
            if probe.returncode==0:raise TerminalDenied('SESSION_CLOSE_NOT_VERIFIED')
            return {'session_id':sid,'state':'CLOSED','cleanup_verified':True}

    def shutdown(self):
        with self._lock:
            for sid in list(self._real):
                try:self.abort(sid)
                except TerminalDenied:pass
            super().shutdown()
