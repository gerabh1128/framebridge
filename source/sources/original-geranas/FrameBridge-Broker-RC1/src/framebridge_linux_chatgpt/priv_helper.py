#!/usr/bin/python3
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import struct
import subprocess
import time
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

PLAN_ID_RE = re.compile(r'^[A-Za-z0-9._-]{1,80}$')
SHA256_RE = re.compile(r'^[a-f0-9]{64}$')
PACKAGE_RE = re.compile(r'^[a-z0-9][a-z0-9+.-]{0,127}$')
SERVICE_RE = re.compile(r'^[A-Za-z0-9@_.-]{1,128}$')
ARGS_SHA_RE = re.compile(r'^[a-f0-9]{64}$')
SERVICE_ACTIONS = {'start','stop','restart','enable','disable'}
IFACE_RE = re.compile(r'^[A-Za-z0-9_.:-]{1,32}$')
FORWARD_PROTOCOLS = {'any','tcp','udp'}
FORWARD_SOURCE_NETS = (
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.0.0/16'),
    ipaddress.ip_network('100.64.0.0/10'),
)
IPTABLES = '/usr/sbin/iptables'
SYSCTL = '/usr/sbin/sysctl'
SYSTEMCTL = '/usr/bin/systemctl'
IP = '/usr/sbin/ip'
SYSTEMD_RUN = '/usr/bin/systemd-run'
PRIV_HELPER_PATH = '/usr/local/libexec/framebridge-seed-priv'
PAIRING_WINDOW_STATE = Path('/run/framebridge-seed/pairing-window.json')
PAIRING_WINDOW_MIN_TTL = 30
PAIRING_WINDOW_MAX_TTL = 600
R3_CORE_CAPABILITIES = {
    'package.refresh','package.upgrade','system.file.write_text',
    'system.directory.ensure','system.file.mode','system.daemon_reload',
    'network.forward',
}
CHANGE_CAPABILITIES = {'package.remove','service.manage','system.hostname','system.timezone'} | R3_CORE_CAPABILITIES
ZONEINFO_ROOT = Path('/usr/share/zoneinfo')
MODE_RE = re.compile(r'^[0-7]{3,4}$')
MAX_SYSTEM_TEXT_BYTES = 65536
SYSTEM_PATH_FORBIDDEN_PREFIXES = (
    '/proc','/sys','/dev','/run','/boot','/var/lib/framebridge-seed',
)
SYSTEM_PATH_FORBIDDEN_EXACT = {'/etc/shadow','/etc/gshadow','/etc/sudoers'}
CHALLENGE_RE = re.compile(r'^[a-f0-9]{32}$')
KEY_ID_RE = re.compile(r'^[A-Za-z0-9._-]{1,80}$')
REMOTE_SOURCE = 'REMOTE_HUMAN'
TOTP_SOURCE = 'REMOTE_HUMAN_TOTP'

STATE_ROOT = Path('/var/lib/framebridge-seed')
APPROVAL_DIR = STATE_ROOT / 'priv-approvals'
TX_DIR = STATE_ROOT / 'priv-transactions'
REMOTE_CHALLENGE_DIR = STATE_ROOT / 'remote-human-challenges'
REMOTE_KEY_DIR = STATE_ROOT / 'remote-human-keys'
TOTP_KEY_DIR = STATE_ROOT / 'totp-keys'
TOTP_USED_DIR = STATE_ROOT / 'totp-used'


class PrivError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_plan_id(v: str) -> str:
    v=str(v).strip()
    if not PLAN_ID_RE.fullmatch(v): raise PrivError('PLAN_ID_INVALID')
    return v


def _validate_sha(v: str) -> str:
    v=str(v).strip().lower()
    if not SHA256_RE.fullmatch(v): raise PrivError('PLAN_SHA256_INVALID')
    return v


def _validate_package(v: str) -> str:
    v=str(v).strip()
    if not PACKAGE_RE.fullmatch(v): raise PrivError('PACKAGE_NAME_INVALID')
    return v


def _validate_args_sha(v: str) -> str:
    v=str(v).strip().lower()
    if not ARGS_SHA_RE.fullmatch(v): raise PrivError('ARGS_SHA256_INVALID')
    return v

def _validate_capability(v: str) -> str:
    v=str(v).strip()
    if v not in CHANGE_CAPABILITIES: raise PrivError('CHANGE_CAPABILITY_INVALID')
    return v

def _validate_service(v: str) -> str:
    v=str(v).strip()
    if not SERVICE_RE.fullmatch(v): raise PrivError('SERVICE_NAME_INVALID')
    return v

def _validate_service_action(v: str) -> str:
    v=str(v).strip()
    if v not in SERVICE_ACTIONS: raise PrivError('SERVICE_ACTION_INVALID')
    return v

def _validate_interface(v: str) -> str:
    v=str(v).strip()
    if not IFACE_RE.fullmatch(v): raise PrivError('NETWORK_INTERFACE_INVALID')
    return v


def _validate_forward_network(v: str, source: bool=False) -> str:
    try:
        net=ipaddress.ip_network(str(v),strict=False)
    except ValueError:
        raise PrivError('NETWORK_CIDR_INVALID') from None
    if net.version!=4: raise PrivError('NETWORK_IPV4_REQUIRED')
    if net.is_multicast or net.is_loopback or net.is_link_local:
        raise PrivError('NETWORK_CIDR_UNSAFE')
    if source and not any(net.subnet_of(x) for x in FORWARD_SOURCE_NETS):
        raise PrivError('NETWORK_FORWARD_SOURCE_NOT_PRIVATE')
    return str(net)


def _validate_network_forward_args(args: dict[str, Any]) -> dict[str, Any]:
    required={'ingress_interface','egress_interface','source_subnet','destination_subnet','protocol','destination_port','nat','lifetime'}
    if set(args)!=required: raise PrivError('NETWORK_FORWARD_ARGS_INVALID')
    ingress=_validate_interface(args.get('ingress_interface',''))
    egress=_validate_interface(args.get('egress_interface',''))
    if ingress==egress: raise PrivError('NETWORK_FORWARD_INTERFACES_MUST_DIFFER')
    source=_validate_forward_network(args.get('source_subnet',''),source=True)
    destination=_validate_forward_network(args.get('destination_subnet',''),source=False)
    protocol=str(args.get('protocol') or '').lower()
    if protocol not in FORWARD_PROTOCOLS: raise PrivError('NETWORK_FORWARD_PROTOCOL_INVALID')
    port=args.get('destination_port')
    if isinstance(port,bool) or not isinstance(port,int) or not (0<=port<=65535): raise PrivError('NETWORK_FORWARD_PORT_INVALID')
    if protocol=='any' and port!=0: raise PrivError('NETWORK_FORWARD_PORT_REQUIRES_PROTOCOL')
    nat=args.get('nat')
    if not isinstance(nat,bool): raise PrivError('NETWORK_FORWARD_NAT_INVALID')
    lifetime=str(args.get('lifetime') or '')
    if lifetime!='runtime': raise PrivError('NETWORK_FORWARD_LIFETIME_INVALID')
    return {'ingress_interface':ingress,'egress_interface':egress,'source_subnet':source,'destination_subnet':destination,'protocol':protocol,'destination_port':port,'nat':nat,'lifetime':'runtime'}


def _forward_rule_specs(plan_id: str, args: dict[str, Any]) -> tuple[list[str], list[str] | None]:
    args=_validate_network_forward_args(args)
    comment='FrameBridge:'+_validate_plan_id(plan_id)+':forward'
    common=['-i',args['ingress_interface'],'-o',args['egress_interface'],'-s',args['source_subnet'],'-d',args['destination_subnet']]
    proto=[]
    if args['protocol']!='any':
        proto+=['-p',args['protocol']]
        if args['destination_port']>0:
            proto+=['--dport',str(args['destination_port'])]
    filt=common+proto+['-m','comment','--comment',comment,'-j','ACCEPT']
    nat=None
    if args['nat']:
        nat=['-s',args['source_subnet'],'-d',args['destination_subnet'],'-o',args['egress_interface']]+proto+['-m','comment','--comment',comment+':nat','-j','MASQUERADE']
    return filt,nat


def _run_cmd(runner, argv: list[str]):
    return runner(argv,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False,timeout=15)


def _ip_forward_state(runner) -> int:
    p=_run_cmd(runner,[SYSCTL,'-n','net.ipv4.ip_forward'])
    v=(p.stdout or '').strip()
    if p.returncode!=0 or v not in ('0','1'): raise PrivError('NETWORK_IP_FORWARD_READ_FAILED')
    return int(v)


def _set_ip_forward(runner, value: int) -> None:
    p=_run_cmd(runner,[SYSCTL,'-w','net.ipv4.ip_forward='+str(int(value))])
    if p.returncode!=0: raise PrivError('NETWORK_IP_FORWARD_SET_FAILED')
    if _ip_forward_state(runner)!=int(value): raise PrivError('NETWORK_IP_FORWARD_VERIFY_FAILED')


def _iptables_exists(runner, table: str | None, chain: str, spec: list[str]) -> bool:
    prefix=[IPTABLES,'-w','5']
    if table: prefix+=['-t',table]
    result=_run_cmd(runner,[*prefix,'-C',chain,*spec])
    if result.returncode==0:
        return True
    if result.returncode!=1:
        raise PrivError('FIREWALL_RULE_QUERY_FAILED')
    # A missing chain/backend is not evidence that a particular rule is absent.
    probe=_run_cmd(runner,[*prefix,'-S',chain])
    if probe.returncode!=0:
        raise PrivError('FIREWALL_CHAIN_QUERY_FAILED')
    return False


def _iptables_add(runner, table: str | None, chain: str, spec: list[str], insert: bool=True) -> None:
    argv=[IPTABLES,'-w','5']
    if table: argv+=['-t',table]
    argv+=(['-I',chain,'1',*spec] if insert else ['-A',chain,*spec])
    if _run_cmd(runner,argv).returncode!=0: raise PrivError('NETWORK_FORWARD_RULE_ADD_FAILED')


def _iptables_delete_if_present(runner, table: str | None, chain: str, spec: list[str]) -> None:
    if not _iptables_exists(runner,table,chain,spec): return
    argv=[IPTABLES,'-w','5']
    if table: argv+=['-t',table]
    argv+=['-D',chain,*spec]
    if _run_cmd(runner,argv).returncode!=0: raise PrivError('NETWORK_FORWARD_RULE_DELETE_FAILED')


def _ufw_config_enabled_priv(path: Path=Path('/etc/ufw/ufw.conf')) -> bool:
    try:
        text=path.read_text(encoding='utf-8',errors='replace')
    except OSError:
        return False
    for raw in text.splitlines():
        line=raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.upper().startswith('ENABLED='):
            return line.split('=',1)[1].strip().lower() in {'yes','true','1'}
    return False


def _serialize_pairing_window(fn):
    import functools, inspect
    @functools.wraps(fn)
    def wrapped(*args,**kwargs):
        import fcntl
        signature=inspect.signature(fn)
        bound=signature.bind(*args,**kwargs); bound.apply_defaults()
        state=Path(bound.arguments['state_path'])
        state.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        lock=state.with_name(state.name+'.lock')
        flags=os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0)
        fd=os.open(lock,flags,0o600)
        try:
            # Timer and monitor serialize with an opening in progress.
            fcntl.flock(fd,fcntl.LOCK_EX)
            return fn(*args,**kwargs)
        finally:
            os.close(fd)
    return wrapped


def _validate_pairing_window_args(interface: str, source_subnet: str, destination_address: str, port: int, network_fingerprint: str, ttl_seconds: int) -> dict[str, Any]:
    interface=str(interface or '').strip()
    if not IFACE_RE.fullmatch(interface): raise PrivError('PAIRING_WINDOW_INTERFACE_INVALID')
    try: source=ipaddress.ip_network(str(source_subnet),strict=False)
    except ValueError: raise PrivError('PAIRING_WINDOW_SOURCE_INVALID') from None
    if source.version!=4 or source.is_loopback or source.is_link_local or source.is_multicast:
        raise PrivError('PAIRING_WINDOW_SOURCE_INVALID')
    if not any(source.subnet_of(allowed) for allowed in FORWARD_SOURCE_NETS):
        raise PrivError('PAIRING_WINDOW_SOURCE_NOT_PRIVATE')
    try: dest=ipaddress.ip_address(str(destination_address))
    except ValueError: raise PrivError('PAIRING_WINDOW_DESTINATION_INVALID') from None
    if dest.version!=4 or dest.is_loopback or dest.is_link_local or dest.is_multicast or dest not in source:
        raise PrivError('PAIRING_WINDOW_DESTINATION_INVALID')
    if isinstance(port,bool) or not isinstance(port,int) or not (1 <= port <= 65535):
        raise PrivError('PAIRING_WINDOW_PORT_INVALID')
    fp=str(network_fingerprint or '').strip().lower()
    if not SHA256_RE.fullmatch(fp): raise PrivError('PAIRING_WINDOW_FINGERPRINT_INVALID')
    if isinstance(ttl_seconds,bool) or not isinstance(ttl_seconds,int) or not (PAIRING_WINDOW_MIN_TTL <= ttl_seconds <= PAIRING_WINDOW_MAX_TTL):
        raise PrivError('PAIRING_WINDOW_TTL_INVALID')
    return {
        'interface':interface,
        'source_subnet':str(source),
        'destination_address':str(dest),
        'port':port,
        'network_fingerprint':fp,
        'ttl_seconds':ttl_seconds,
    }


def _pairing_window_rule_spec(window_id: str, args: dict[str, Any]) -> list[str]:
    comment=f'FrameBridge:{window_id}:pairing'
    return [
        '-i',args['interface'],
        '-s',args['source_subnet'],
        '-d',args['destination_address']+'/32',
        '-p','tcp','--dport',str(args['port']),
        '-m','comment','--comment',comment,
        '-j','ACCEPT',
    ]


def _interface_has_ipv4(runner, interface: str, address: str) -> bool:
    p=_run_cmd(runner,[IP,'-4','-j','addr','show','dev',interface])
    if p.returncode!=0: return False
    try: value=json.loads(p.stdout or '[]')
    except Exception: return False
    for item in value if isinstance(value,list) else []:
        for ai in item.get('addr_info') or []:
            if ai.get('family')=='inet' and str(ai.get('local') or '')==address:
                return True
    return False


def _pairing_window_rule_present(runner, state: dict[str, Any]) -> bool:
    spec=state.get('rule_spec') or []
    if not isinstance(spec,list) or not all(isinstance(x,str) for x in spec): return False
    return _iptables_exists(runner,None,'ufw-user-input',spec)


def _pairing_window_rule_add(runner, spec: list[str]) -> None:
    argv=[IPTABLES,'-w','5','-I','ufw-user-input','1',*spec]
    if _run_cmd(runner,argv).returncode!=0: raise PrivError('PAIRING_WINDOW_RULE_ADD_FAILED')


def _pairing_window_rule_delete_if_present(runner, spec: list[str]) -> None:
    if not _iptables_exists(runner,None,'ufw-user-input',spec): return
    argv=[IPTABLES,'-w','5','-D','ufw-user-input',*spec]
    if _run_cmd(runner,argv).returncode!=0: raise PrivError('PAIRING_WINDOW_RULE_DELETE_FAILED')


@_serialize_pairing_window
def pairing_window_status(state_path: Path=PAIRING_WINDOW_STATE, runner=subprocess.run) -> dict[str, Any]:
    if not state_path.exists():
        return {'ok':True,'schema':1,'state':'closed','active':False,'rule_present':False}
    state=_read_json(state_path)
    rule_present=_pairing_window_rule_present(runner,state)
    result=dict(state)
    result.update({'ok':True,'rule_present':rule_present,'active':state.get('state')=='active' and rule_present})
    expires=str(state.get('expires_at') or '')
    try:
        exp=datetime.fromisoformat(expires)
        if exp.tzinfo is None: exp=exp.replace(tzinfo=timezone.utc)
        result['expired']=datetime.now(timezone.utc)>=exp.astimezone(timezone.utc)
    except Exception:
        result['expired']=False
    return result


@_serialize_pairing_window
def pairing_window_open(
    interface: str, source_subnet: str, destination_address: str, port: int,
    network_fingerprint: str, ttl_seconds: int,
    state_path: Path=PAIRING_WINDOW_STATE, runner=subprocess.run,
    stdin_isatty: bool | None=None,
) -> dict[str, Any]:
    require_interactive_tty(stdin_isatty)
    args=_validate_pairing_window_args(interface,source_subnet,destination_address,port,network_fingerprint,ttl_seconds)
    previous=_read_json(state_path) if state_path.exists() else None
    if previous and previous.get('state') in {'active','opening'}:
        raise PrivError('PAIRING_WINDOW_ALREADY_ACTIVE')
    if not _ufw_config_enabled_priv():
        raise PrivError('PAIRING_WINDOW_UFW_NOT_ENABLED')
    if _run_cmd(runner,[IPTABLES,'-w','5','-S','ufw-user-input']).returncode!=0:
        raise PrivError('PAIRING_WINDOW_UFW_CHAIN_MISSING')
    if not _interface_has_ipv4(runner,args['interface'],args['destination_address']):
        raise PrivError('PAIRING_WINDOW_DESTINATION_NOT_ON_INTERFACE')
    window_id=secrets.token_hex(16)
    spec=_pairing_window_rule_spec(window_id,args)
    if _iptables_exists(runner,None,'ufw-user-input',spec):
        raise PrivError('PAIRING_WINDOW_RULE_ALREADY_EXISTS')
    now=datetime.now(timezone.utc)
    expires=now+timedelta(seconds=args['ttl_seconds'])
    unit='framebridge-pairing-window-'+window_id[:12]
    state={
        'schema':1,'window_id':window_id,'state':'opening',**args,
        'opened_at':now.isoformat(),'expires_at':expires.isoformat(),
        'timer_unit':unit,'rule_spec':spec,'rule_present':False,'reboot_fail_closed':True,
    }
    # A storage failure here happens BEFORE any exposure is created.
    _atomic_json(state_path,state)
    try:
        timer=_run_cmd(runner,[
            SYSTEMD_RUN,'--quiet',f'--unit={unit}',f'--on-active={args["ttl_seconds"]}s',
            '--timer-property=AccuracySec=1s',PRIV_HELPER_PATH,
            'pairing-window-expire','--window-id',window_id,
        ])
        if timer.returncode!=0:
            raise PrivError('PAIRING_WINDOW_TIMER_SCHEDULE_FAILED')
        _pairing_window_rule_add(runner,spec)
        if not _iptables_exists(runner,None,'ufw-user-input',spec):
            raise PrivError('PAIRING_WINDOW_RULE_VERIFY_FAILED')
        state.update(state='active',rule_present=True)
        _atomic_json(state_path,state)
    except Exception as original:
        # Every catchable failure attempts exact rollback. Never report a clean
        # result if the read or deletion fails. Keep timer/state for recovery.
        try:
            _pairing_window_rule_delete_if_present(runner,spec)
            if _iptables_exists(runner,None,'ufw-user-input',spec):
                raise PrivError('PAIRING_WINDOW_RULE_DELETE_FAILED')
        except Exception as cleanup_error:
            raise PrivError('PAIRING_WINDOW_CLEANUP_UNVERIFIED') from cleanup_error
        # No exposure remains. A late timer sees closed/no state, never reopens.
        try:
            _run_cmd(runner,[SYSTEMCTL,'stop',unit+'.timer'])
        except Exception:
            pass
        try:
            if previous is not None: _atomic_json(state_path,previous)
            else: state_path.unlink(missing_ok=True)
        except Exception as state_error:
            raise PrivError('PAIRING_WINDOW_STATE_RECOVERY_FAILED') from state_error
        raise original
    return {
        'ok':True,'schema':1,'state':'active','active':True,
        'window_id':window_id,**args,'opened_at':state['opened_at'],
        'expires_at':state['expires_at'],'timer_unit':unit,
        'rule_present':True,'reboot_fail_closed':True,
    }


@_serialize_pairing_window
def _pairing_window_close_internal(window_id: str | None, reason: str, state_path: Path, runner) -> dict[str, Any]:
    if not state_path.exists():
        return {'ok':True,'schema':1,'state':'closed','active':False,'rule_present':False,'close_reason':'already_absent'}
    state=_read_json(state_path)
    current_id=str(state.get('window_id') or '')
    if window_id is not None and current_id!=str(window_id): raise PrivError('PAIRING_WINDOW_ID_MISMATCH')
    if state.get('state') not in {'active','opening'}:
        result=dict(state); result.update({'ok':True,'active':False,'rule_present':_pairing_window_rule_present(runner,state)})
        return result

    spec=state.get('rule_spec') or []
    if not isinstance(spec,list) or not all(isinstance(x,str) for x in spec): raise PrivError('PAIRING_WINDOW_STATE_INVALID')
    _pairing_window_rule_delete_if_present(runner,spec)
    if _iptables_exists(runner,None,'ufw-user-input',spec): raise PrivError('PAIRING_WINDOW_RULE_DELETE_FAILED')

    unit=str(state.get('timer_unit') or '')
    if unit and reason!='expired':
        _run_cmd(runner,[SYSTEMCTL,'stop',unit+'.timer'])

    state['state']='expired' if reason=='expired' else 'closed'
    state['closed_at']=datetime.now(timezone.utc).isoformat()
    state['close_reason']=reason
    state['rule_present']=False
    _atomic_json(state_path,state)
    result=dict(state); result.update({'ok':True,'active':False,'rule_present':False})
    return result


def pairing_window_close(reason: str='user', state_path: Path=PAIRING_WINDOW_STATE, runner=subprocess.run) -> dict[str, Any]:
    reason=str(reason or '').strip().lower()
    if reason not in {'user','cancelled','paired','denied','network_change','service_stop','reconcile'}:
        raise PrivError('PAIRING_WINDOW_CLOSE_REASON_INVALID')
    return _pairing_window_close_internal(None,reason,state_path,runner)


def pairing_window_expire(window_id: str, state_path: Path=PAIRING_WINDOW_STATE, runner=subprocess.run) -> dict[str, Any]:
    window_id=str(window_id or '').strip().lower()
    if not re.fullmatch(r'[a-f0-9]{32}',window_id): raise PrivError('PAIRING_WINDOW_ID_INVALID')
    return _pairing_window_close_internal(window_id,'expired',state_path,runner)


def _active_forward_transaction_exists(tx_dir: Path) -> bool:
    if not tx_dir.exists(): return False
    for path in tx_dir.glob('*.json'):
        try: tx=_read_json(path)
        except Exception: continue
        if tx.get('capability')=='network.forward' and tx.get('rollback_available') is True: return True
    return False


def _apply_network_forward_runtime(plan_id: str, args: dict[str, Any], runner=subprocess.run, interface_root: Path=Path('/sys/class/net')) -> tuple[dict[str, Any],dict[str, Any],dict[str, Any]]:
    args=_validate_network_forward_args(args)
    for name in (args['ingress_interface'],args['egress_interface']):
        if not (interface_root/name).exists(): raise PrivError('NETWORK_FORWARD_INTERFACE_NOT_PRESENT')
    if not _ufw_config_enabled_priv(): raise PrivError('NETWORK_FORWARD_UFW_NOT_ENABLED')
    if _run_cmd(runner,[IPTABLES,'-w','5','-S','ufw-user-forward']).returncode!=0: raise PrivError('NETWORK_FORWARD_UFW_CHAIN_MISSING')
    filt,nat=_forward_rule_specs(plan_id,args)
    if _iptables_exists(runner,None,'ufw-user-forward',filt): raise PrivError('NETWORK_FORWARD_RULE_ALREADY_EXISTS')
    if nat is not None and _iptables_exists(runner,'nat','POSTROUTING',nat): raise PrivError('NETWORK_FORWARD_NAT_RULE_ALREADY_EXISTS')
    before_ip=_ip_forward_state(runner)
    added_filter=False; added_nat=False; changed_ip=False
    try:
        _iptables_add(runner,None,'ufw-user-forward',filt,insert=True); added_filter=True
        if nat is not None:
            _iptables_add(runner,'nat','POSTROUTING',nat,insert=False); added_nat=True
        if before_ip!=1:
            _set_ip_forward(runner,1); changed_ip=True
        if not _iptables_exists(runner,None,'ufw-user-forward',filt): raise PrivError('NETWORK_FORWARD_RULE_VERIFY_FAILED')
        if nat is not None and not _iptables_exists(runner,'nat','POSTROUTING',nat): raise PrivError('NETWORK_FORWARD_NAT_VERIFY_FAILED')
        if _ip_forward_state(runner)!=1: raise PrivError('NETWORK_IP_FORWARD_VERIFY_FAILED')
    except Exception:
        if added_nat: _iptables_delete_if_present(runner,'nat','POSTROUTING',nat or [])
        if added_filter: _iptables_delete_if_present(runner,None,'ufw-user-forward',filt)
        if changed_ip: _set_ip_forward(runner,before_ip)
        raise
    before={'ip_forward':before_ip,'filter_rule':filt,'nat_rule':nat,'runtime_only':True}
    after={'ip_forward':1,'filter_rule_present':True,'nat_rule_present':nat is not None}
    verification={'ip_forward':1,'filter_rule_present':True,'nat_rule_present':nat is not None,'runtime_only':True}
    return before,after,verification


def _rollback_network_forward_runtime(before: dict[str, Any], runner=subprocess.run) -> dict[str, Any]:
    filt=list(before.get('filter_rule') or [])
    nat_raw=before.get('nat_rule'); nat=list(nat_raw) if isinstance(nat_raw,list) else None
    prior=int(before.get('ip_forward',0))
    if nat is not None: _iptables_delete_if_present(runner,'nat','POSTROUTING',nat)
    if filt: _iptables_delete_if_present(runner,None,'ufw-user-forward',filt)
    if _ip_forward_state(runner)!=prior: _set_ip_forward(runner,prior)
    filter_present=_iptables_exists(runner,None,'ufw-user-forward',filt) if filt else False
    nat_present=_iptables_exists(runner,'nat','POSTROUTING',nat) if nat is not None else False
    current_ip=_ip_forward_state(runner)
    return {'verified':(not filter_present and not nat_present and current_ip==prior),'ip_forward':current_ip,'filter_rule_present':filter_present,'nat_rule_present':nat_present}


def _validate_hostname(v: str) -> str:
    v=str(v).strip()
    if not v or len(v)>253 or v.endswith('.') or '..' in v:
        raise PrivError('HOSTNAME_INVALID')
    for label in v.split('.'):
        if not (1 <= len(label) <= 63):
            raise PrivError('HOSTNAME_INVALID')
        if label[0]=='-' or label[-1]=='-' or re.fullmatch(r'[A-Za-z0-9-]+',label) is None:
            raise PrivError('HOSTNAME_INVALID')
    return v

def _validate_timezone(v: str, zoneinfo_root: Path=ZONEINFO_ROOT) -> str:
    v=str(v).strip()
    if not v or len(v)>128 or v.startswith('/') or '\x00' in v:
        raise PrivError('TIMEZONE_INVALID')
    if any(part in ('','.','..') for part in Path(v).parts):
        raise PrivError('TIMEZONE_INVALID')
    if re.fullmatch(r'[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*',v) is None:
        raise PrivError('TIMEZONE_INVALID')
    root=zoneinfo_root.resolve(strict=False)
    candidate=(root/v).resolve(strict=False)
    try:
        if os.path.commonpath([str(root),str(candidate)]) != str(root):
            raise PrivError('TIMEZONE_INVALID')
    except ValueError:
        raise PrivError('TIMEZONE_INVALID') from None
    if not candidate.is_file():
        raise PrivError('TIMEZONE_NOT_FOUND')
    return v

def canonical_args_sha(args: dict[str, Any]) -> str:
    raw=json.dumps(args,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _validate_mode(v: str) -> str:
    v=str(v or '').strip()
    if MODE_RE.fullmatch(v) is None: raise PrivError('MODE_INVALID')
    mode=int(v,8)
    if mode<0 or mode>0o7777: raise PrivError('MODE_INVALID')
    return v


def _validate_system_path(v: str) -> Path:
    v=str(v or '').strip()
    if not v or len(v)>4096 or not v.startswith('/'): raise PrivError('SYSTEM_PATH_INVALID')
    p=Path(v)
    if any(part in ('','.','..') for part in p.parts[1:]): raise PrivError('SYSTEM_PATH_INVALID')
    norm=os.path.normpath(v)
    if norm in SYSTEM_PATH_FORBIDDEN_EXACT: raise PrivError('SYSTEM_PATH_FORBIDDEN')
    if norm.startswith('/etc/sudoers.d/') or norm.startswith('/etc/ssl/private/') or norm.startswith('/root/.ssh/') or norm.startswith('/etc/ssh/ssh_host_'):
        raise PrivError('SYSTEM_PATH_FORBIDDEN')
    for prefix in SYSTEM_PATH_FORBIDDEN_PREFIXES:
        if norm==prefix or norm.startswith(prefix+'/'): raise PrivError('SYSTEM_PATH_FORBIDDEN')
    path=Path(norm)
    if path.is_symlink(): raise PrivError('SYSTEM_PATH_SYMLINK_FORBIDDEN')
    parent=path.parent
    while parent != Path('/'):
        if parent.exists() and parent.is_symlink(): raise PrivError('SYSTEM_PATH_PARENT_SYMLINK_FORBIDDEN')
        parent=parent.parent
    return path


def package_candidate(package: str, runner: Callable[..., subprocess.CompletedProcess[str]]=subprocess.run) -> str | None:
    package=_validate_package(package)
    p=runner(['/usr/bin/apt-cache','policy',package],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False)
    if p.returncode!=0: return None
    for line in p.stdout.splitlines():
        m=re.match(r'^\s*Candidate:\s*(\S+)\s*$',line)
        if m:
            value=m.group(1)
            return None if value=='(none)' else value
    return None


def _file_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {'exists':False}
    if path.is_symlink() or not path.is_file(): raise PrivError('SYSTEM_FILE_NOT_REGULAR')
    data=path.read_bytes()
    if len(data)>MAX_SYSTEM_TEXT_BYTES: raise PrivError('SYSTEM_FILE_BACKUP_TOO_LARGE')
    st=path.stat()
    return {
        'exists':True,
        'content_b64':base64.b64encode(data).decode('ascii'),
        'sha256':hashlib.sha256(data).hexdigest(),
        'mode':st.st_mode & 0o7777,
        'uid':st.st_uid,
        'gid':st.st_gid,
    }


def _atomic_write_system_file(path: Path, data: bytes, before: dict[str, Any]) -> None:
    if not path.parent.is_dir(): raise PrivError('SYSTEM_FILE_PARENT_MISSING')
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.framebridge-',dir=str(path.parent))
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        if before.get('exists'):
            os.chmod(tmp,int(before['mode']))
            os.chown(tmp,int(before['uid']),int(before['gid']))
        else:
            os.chmod(tmp,0o644)
            os.chown(tmp,os.geteuid(),os.getegid())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def _directory_state(path: Path) -> dict[str, Any]:
    if not path.exists(): return {'exists':False}
    if path.is_symlink() or not path.is_dir(): raise PrivError('SYSTEM_DIRECTORY_NOT_DIRECTORY')
    st=path.stat()
    return {'exists':True,'mode':st.st_mode & 0o7777,'uid':st.st_uid,'gid':st.st_gid}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp=path.with_name('.'+path.name+'.tmp')
    tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+'\n')
    os.chmod(tmp,0o600)
    os.replace(tmp,path)


def _read_json(path: Path) -> dict[str, Any]:
    try: v=json.loads(path.read_text())
    except FileNotFoundError: raise PrivError('STATE_NOT_FOUND')
    except Exception: raise PrivError('STATE_INVALID_JSON')
    if not isinstance(v,dict): raise PrivError('STATE_NOT_OBJECT')
    return v


def require_root() -> None:
    if os.geteuid()!=0: raise PrivError('ROOT_REQUIRED')


def require_interactive_tty(stdin_isatty: bool | None = None) -> None:
    ok=sys.stdin.isatty() if stdin_isatty is None else bool(stdin_isatty)
    if not ok: raise PrivError('INTERACTIVE_TTY_REQUIRED')


def _now(value: datetime | None = None) -> datetime:
    result=value or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None: raise PrivError('TIMEZONE_REQUIRED')
    return result.astimezone(timezone.utc)


def _validate_challenge(v: str) -> str:
    v=str(v).strip().lower()
    if not CHALLENGE_RE.fullmatch(v): raise PrivError('REMOTE_CHALLENGE_ID_INVALID')
    return v


def _validate_key_id(v: str) -> str:
    v=str(v).strip()
    if not KEY_ID_RE.fullmatch(v): raise PrivError('REMOTE_KEY_ID_INVALID')
    return v


def _approval_record_valid(ap: dict[str, Any], kind: str, plan_sha256: str, package: str) -> bool:
    common=(ap.get('kind')==kind and ap.get('one_shot') is True and ap.get('consumed') is False and ap.get('plan_sha256')==plan_sha256 and ap.get('package')==package)
    if not common: return False
    if ap.get('source')=='LOCAL_TTY': return ap.get('schema')==1
    if ap.get('source')==REMOTE_SOURCE:
        return (ap.get('schema')==2 and ap.get('auth_method')=='ecdsa-p256' and CHALLENGE_RE.fullmatch(str(ap.get('challenge_id') or '')) is not None and KEY_ID_RE.fullmatch(str(ap.get('key_id') or '')) is not None)
    if ap.get('source')==TOTP_SOURCE:
        return (ap.get('schema')==3 and ap.get('auth_method')=='totp-sha1-6-30' and CHALLENGE_RE.fullmatch(str(ap.get('challenge_id') or '')) is not None and KEY_ID_RE.fullmatch(str(ap.get('key_id') or '')) is not None and type(ap.get('totp_step')) is int)
    return False


def package_status(package: str, runner: Callable[..., subprocess.CompletedProcess[str]]=subprocess.run) -> dict[str, Any]:
    package=_validate_package(package)
    p=runner(['/usr/bin/dpkg-query','-W','-f=${Status}|${Version}',package],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False)
    if p.returncode!=0: return {'installed':False,'version':None}
    out=p.stdout.strip()
    if not out.startswith('install ok installed|'): return {'installed':False,'version':None}
    return {'installed':True,'version':out.split('|',1)[1] if '|' in out else None}


def approve_install(plan_id: str, plan_sha256: str, package: str, approval_dir: Path=APPROVAL_DIR, stdin_isatty: bool | None=None) -> dict[str, Any]:
    require_interactive_tty(stdin_isatty)
    plan_id=_validate_plan_id(plan_id); plan_sha256=_validate_sha(plan_sha256); package=_validate_package(package)
    rec={'schema':1,'kind':'package.install','plan_id':plan_id,'plan_sha256':plan_sha256,'package':package,'source':'LOCAL_TTY','one_shot':True,'consumed':False,'approved_at':utc_now()}
    _atomic_json(approval_dir/(plan_id+'.install.json'),rec)
    return rec


def apply_install(plan_id: str, plan_sha256: str, package: str, approval_dir: Path=APPROVAL_DIR, tx_dir: Path=TX_DIR, runner: Callable[..., subprocess.CompletedProcess[str]]=subprocess.run) -> dict[str, Any]:
    plan_id=_validate_plan_id(plan_id); plan_sha256=_validate_sha(plan_sha256); package=_validate_package(package)
    ap_path=approval_dir/(plan_id+'.install.json'); ap=_read_json(ap_path)
    checks={
      'kind':ap.get('kind')=='package.install',
      'approval':_approval_record_valid(ap,'package.install',plan_sha256,package),
      'plan_sha':ap.get('plan_sha256')==plan_sha256,
      'package':ap.get('package')==package,
    }
    if not all(checks.values()): raise PrivError('INSTALL_APPROVAL_PRECONDITION_FAILED')
    before=package_status(package,runner)
    if before['installed']: raise PrivError('PACKAGE_ALREADY_INSTALLED')
    env=dict(os.environ); env['DEBIAN_FRONTEND']='noninteractive'
    p=runner(['/usr/bin/apt-get','-y','--no-install-recommends','install',package],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False,env=env)
    if p.returncode!=0: raise PrivError('APT_INSTALL_FAILED')
    after=package_status(package,runner)
    if not after['installed']: raise PrivError('PACKAGE_INSTALL_VERIFY_FAILED')
    ap['consumed']=True; ap['consumed_at']=utc_now(); _atomic_json(ap_path,ap)
    tx={'schema':1,'plan_id':plan_id,'plan_sha256':plan_sha256,'package':package,'before':before,'after':after,'applied_at':utc_now(),'rollback_available':True}
    _atomic_json(tx_dir/(plan_id+'.json'),tx)
    return {'ok':True,'package':package,'version':after['version'],'approval_consumed':True,'rollback_available':True}


def approve_rollback(plan_id: str, plan_sha256: str, package: str, approval_dir: Path=APPROVAL_DIR, tx_dir: Path=TX_DIR, stdin_isatty: bool | None=None) -> dict[str, Any]:
    require_interactive_tty(stdin_isatty)
    plan_id=_validate_plan_id(plan_id); plan_sha256=_validate_sha(plan_sha256); package=_validate_package(package)
    tx=_read_json(tx_dir/(plan_id+'.json'))
    if tx.get('plan_sha256')!=plan_sha256 or tx.get('package')!=package or tx.get('rollback_available') is not True: raise PrivError('ROLLBACK_TRANSACTION_PRECONDITION_FAILED')
    rec={'schema':1,'kind':'package.rollback','plan_id':plan_id,'plan_sha256':plan_sha256,'package':package,'source':'LOCAL_TTY','one_shot':True,'consumed':False,'approved_at':utc_now()}
    _atomic_json(approval_dir/(plan_id+'.rollback.json'),rec)
    return rec


def apply_rollback(plan_id: str, plan_sha256: str, package: str, approval_dir: Path=APPROVAL_DIR, tx_dir: Path=TX_DIR, runner: Callable[..., subprocess.CompletedProcess[str]]=subprocess.run) -> dict[str, Any]:
    plan_id=_validate_plan_id(plan_id); plan_sha256=_validate_sha(plan_sha256); package=_validate_package(package)
    ap_path=approval_dir/(plan_id+'.rollback.json'); ap=_read_json(ap_path); tx_path=tx_dir/(plan_id+'.json'); tx=_read_json(tx_path)
    checks={
      'approval':_approval_record_valid(ap,'package.rollback',plan_sha256,package),
      'plan_sha':ap.get('plan_sha256')==plan_sha256==tx.get('plan_sha256'),'package':ap.get('package')==package==tx.get('package'),
      'rollback_available':tx.get('rollback_available') is True,'before_absent':(tx.get('before') or {}).get('installed') is False,
    }
    if not all(checks.values()): raise PrivError('ROLLBACK_APPROVAL_PRECONDITION_FAILED')
    current=package_status(package,runner)
    expected_version=(tx.get('after') or {}).get('version')
    if not current['installed']: raise PrivError('PACKAGE_NOT_INSTALLED')
    if current.get('version')!=expected_version: raise PrivError('PACKAGE_VERSION_CHANGED')
    env=dict(os.environ); env['DEBIAN_FRONTEND']='noninteractive'
    p=runner(['/usr/bin/apt-get','-y','remove',package],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False,env=env)
    if p.returncode!=0: raise PrivError('APT_REMOVE_FAILED')
    after=package_status(package,runner)
    if after['installed']: raise PrivError('PACKAGE_ROLLBACK_VERIFY_FAILED')
    ap['consumed']=True; ap['consumed_at']=utc_now(); _atomic_json(ap_path,ap)
    tx['rollback_available']=False; tx['rolled_back_at']=utc_now(); tx['rollback_verified']=True; _atomic_json(tx_path,tx)
    return {'ok':True,'package':package,'absent_after_rollback':True,'rollback_approval_consumed':True,'rollback_verified':True}

def enroll_totp_key(key_id: str, node_label: str, issuer: str='FrameBridge', key_dir: Path=TOTP_KEY_DIR, stdin_isatty: bool | None=None) -> dict[str,Any]:
    require_interactive_tty(stdin_isatty)
    key_id=_validate_key_id(key_id)
    node_label=str(node_label).strip(); issuer=str(issuer).strip()
    if not node_label or len(node_label)>128 or any(c in node_label for c in "\r\n\x00"): raise PrivError('TOTP_NODE_LABEL_INVALID')
    if not issuer or len(issuer)>64 or any(c in issuer for c in "\r\n\x00"): raise PrivError('TOTP_ISSUER_INVALID')
    key_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(key_dir,0o700)
    path=key_dir/(key_id+'.b32')
    if path.exists(): raise PrivError('TOTP_KEY_ALREADY_EXISTS')
    secret=secrets.token_bytes(20)
    secret_b32=base64.b32encode(secret).decode('ascii').rstrip('=')
    tmp=path.with_name('.'+path.name+'.tmp')
    tmp.write_text(secret_b32+'\n'); os.chmod(tmp,0o600); os.replace(tmp,path)
    label=quote(f'{issuer}:{node_label}',safe='')
    uri=f'otpauth://totp/{label}?secret={secret_b32}&issuer={quote(issuer,safe="")}&algorithm=SHA1&digits=6&period=30'
    return {'ok':True,'schema':1,'key_id':key_id,'node_label':node_label,'issuer':issuer,'otpauth_uri':uri,'secret_sha256':hashlib.sha256(secret).hexdigest(),'enrolled_at':utc_now()}


def _b32decode_nopad(value: str) -> bytes:
    s=''.join(str(value).strip().upper().split())
    if not s or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567' for c in s): raise PrivError('TOTP_SECRET_INVALID')
    s += '='*((8-len(s)%8)%8)
    try: return base64.b32decode(s,casefold=True)
    except Exception: raise PrivError('TOTP_SECRET_INVALID') from None


def _load_totp_secret(key_id: str, key_dir: Path=TOTP_KEY_DIR) -> bytes:
    key_id=_validate_key_id(key_id); p=key_dir/(key_id+'.b32')
    if not p.is_file() or p.is_symlink(): raise PrivError('TOTP_KEY_NOT_FOUND')
    secret=_b32decode_nopad(p.read_text())
    if len(secret)<20: raise PrivError('TOTP_SECRET_TOO_SHORT')
    return secret


def _hotp(secret: bytes, counter: int) -> str:
    digest=hmac.new(secret,struct.pack('>Q',int(counter)),hashlib.sha1).digest(); off=digest[-1]&0x0f
    value=((digest[off]&0x7f)<<24)|((digest[off+1]&0xff)<<16)|((digest[off+2]&0xff)<<8)|(digest[off+3]&0xff)
    return str(value%1000000).zfill(6)


def _verify_totp(secret: bytes, code: str, when: float|None=None, window: int=1) -> int|None:
    if not isinstance(code,str) or len(code)!=6 or not code.isdigit(): return None
    step=int((time.time() if when is None else float(when))//30)
    for s in range(step-window,step+window+1):
        if hmac.compare_digest(_hotp(secret,s),code): return s
    return None


def _consume_totp_step(key_id: str, step: int, challenge_id: str, used_dir: Path=TOTP_USED_DIR) -> None:
    used_dir.mkdir(parents=True,exist_ok=True,mode=0o700); p=used_dir/(f'{key_id}-{step}.used')
    try: fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError: raise PrivError('TOTP_REPLAY') from None
    with os.fdopen(fd,'w') as f: f.write(challenge_id+'\n')


def create_totp_challenge(kind: str, plan_id: str, plan_sha256: str, package: str, key_id: str, challenge_dir: Path=REMOTE_CHALLENGE_DIR, tx_dir: Path=TX_DIR, key_dir: Path=TOTP_KEY_DIR, ttl_seconds: int=120, now: datetime|None=None) -> dict[str,Any]:
    key_id=_validate_key_id(key_id); _load_totp_secret(key_id,key_dir)
    if kind not in ('package.install','package.rollback'): raise PrivError('REMOTE_CHALLENGE_KIND_INVALID')
    plan_id=_validate_plan_id(plan_id); plan_sha256=_validate_sha(plan_sha256); package=_validate_package(package)
    if not (30<=int(ttl_seconds)<=300): raise PrivError('REMOTE_CHALLENGE_TTL_INVALID')
    if kind=='package.rollback':
        tx=_read_json(tx_dir/(plan_id+'.json'))
        if tx.get('plan_sha256')!=plan_sha256 or tx.get('package')!=package or tx.get('rollback_available') is not True: raise PrivError('ROLLBACK_TRANSACTION_PRECONDITION_FAILED')
    created=_now(now); challenge_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    for path in challenge_dir.glob('*.json'):
        try: existing=_read_json(path)
        except PrivError: continue
        if existing.get('totp_key_id')==key_id and existing.get('state')=='PENDING':
            try: exp=datetime.fromisoformat(str(existing.get('expires_at')).replace('Z','+00:00'))
            except Exception: continue
            if exp.tzinfo is not None and created < exp.astimezone(timezone.utc): raise PrivError('PENDING_TOTP_CHALLENGE_EXISTS')
    cid=secrets.token_hex(16)
    rec={'schema':1,'kind':kind,'plan_id':plan_id,'plan_sha256':plan_sha256,'package':package,'challenge_id':cid,'totp_key_id':key_id,'created_at':created.isoformat(),'expires_at':(created+timedelta(seconds=int(ttl_seconds))).isoformat(),'state':'PENDING'}
    _atomic_json(challenge_dir/(cid+'.json'),rec)
    return {'ok':True,**rec}


def approve_totp_challenge(challenge_id: str, key_id: str, code: str, challenge_dir: Path=REMOTE_CHALLENGE_DIR, key_dir: Path=TOTP_KEY_DIR, used_dir: Path=TOTP_USED_DIR, approval_dir: Path=APPROVAL_DIR, now: datetime|None=None) -> dict[str,Any]:
    challenge_id=_validate_challenge(challenge_id); key_id=_validate_key_id(key_id); ch_path=challenge_dir/(challenge_id+'.json'); ch=_read_json(ch_path)
    if ch.get('schema')!=1 or ch.get('challenge_id')!=challenge_id or ch.get('state')!='PENDING' or ch.get('totp_key_id')!=key_id: raise PrivError('TOTP_CHALLENGE_STATE_INVALID')
    try: expires=datetime.fromisoformat(str(ch.get('expires_at')).replace('Z','+00:00'))
    except Exception: raise PrivError('REMOTE_CHALLENGE_INVALID') from None
    current=_now(now)
    if current>=expires.astimezone(timezone.utc): raise PrivError('REMOTE_CHALLENGE_EXPIRED')
    secret=_load_totp_secret(key_id,key_dir); step=_verify_totp(secret,code,current.timestamp(),window=1)
    if step is None: raise PrivError('TOTP_INVALID')
    _consume_totp_step(key_id,step,challenge_id,used_dir)
    kind=str(ch.get('kind')); suffix='.install.json' if kind=='package.install' else '.rollback.json'
    rec={'schema':3,'kind':kind,'plan_id':ch.get('plan_id'),'plan_sha256':ch.get('plan_sha256'),'package':ch.get('package'),'source':TOTP_SOURCE,'auth_method':'totp-sha1-6-30','key_id':key_id,'challenge_id':challenge_id,'totp_step':step,'one_shot':True,'consumed':False,'approved_at':utc_now()}
    _atomic_json(approval_dir/(str(ch.get('plan_id'))+suffix),rec)
    ch['state']='APPROVED'; ch['approved_at']=rec['approved_at']; ch['key_id']=key_id; ch['totp_step']=step; _atomic_json(ch_path,ch)
    return rec


def _challenge_message(challenge: dict[str, Any]) -> bytes:
    fields={
        'schema':1,
        'kind':challenge.get('kind'),
        'plan_id':challenge.get('plan_id'),
        'plan_sha256':challenge.get('plan_sha256'),
        'package':challenge.get('package'),
        'challenge_id':challenge.get('challenge_id'),
        'expires_at':challenge.get('expires_at'),
    }
    return json.dumps(fields,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('utf-8')


def create_remote_challenge(kind: str, plan_id: str, plan_sha256: str, package: str, challenge_dir: Path=REMOTE_CHALLENGE_DIR, tx_dir: Path=TX_DIR, ttl_seconds: int=300, now: datetime | None=None) -> dict[str, Any]:
    if kind not in ('package.install','package.rollback'): raise PrivError('REMOTE_CHALLENGE_KIND_INVALID')
    plan_id=_validate_plan_id(plan_id); plan_sha256=_validate_sha(plan_sha256); package=_validate_package(package)
    if not (30 <= int(ttl_seconds) <= 900): raise PrivError('REMOTE_CHALLENGE_TTL_INVALID')
    if kind=='package.rollback':
        tx=_read_json(tx_dir/(plan_id+'.json'))
        if tx.get('plan_sha256')!=plan_sha256 or tx.get('package')!=package or tx.get('rollback_available') is not True:
            raise PrivError('ROLLBACK_TRANSACTION_PRECONDITION_FAILED')
    created=_now(now)
    challenge_id=secrets.token_hex(16)
    rec={
        'schema':1,'kind':kind,'plan_id':plan_id,'plan_sha256':plan_sha256,'package':package,
        'challenge_id':challenge_id,'created_at':created.isoformat(),
        'expires_at':(created+timedelta(seconds=int(ttl_seconds))).isoformat(),'state':'PENDING',
    }
    _atomic_json(challenge_dir/(challenge_id+'.json'),rec)
    result=dict(rec)
    result['message_b64']=base64.b64encode(_challenge_message(rec)).decode('ascii')
    result['ok']=True
    return result


def _load_public_key(key_id: str, key_dir: Path) -> Path:
    key_id=_validate_key_id(key_id)
    p=key_dir/(key_id+'.pem')
    if not p.is_file() or p.is_symlink(): raise PrivError('REMOTE_KEY_NOT_FOUND')
    return p


def _verify_signature(public_key: Path, message: bytes, signature: bytes, runner: Callable[..., subprocess.CompletedProcess[bytes]]=subprocess.run) -> bool:
    if len(signature)<8 or len(signature)>256: return False
    with tempfile.TemporaryDirectory(prefix='framebridge-remote-verify-') as td:
        root=Path(td)
        msg=root/'message.bin'; sig=root/'signature.der'
        msg.write_bytes(message); sig.write_bytes(signature)
        p=runner(['/usr/bin/openssl','dgst','-sha256','-verify',str(public_key),'-signature',str(sig),str(msg)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
        return p.returncode==0


def approve_remote_challenge(challenge_id: str, key_id: str, signature_b64: str, challenge_dir: Path=REMOTE_CHALLENGE_DIR, key_dir: Path=REMOTE_KEY_DIR, approval_dir: Path=APPROVAL_DIR, now: datetime | None=None, runner: Callable[..., subprocess.CompletedProcess[bytes]]=subprocess.run) -> dict[str, Any]:
    challenge_id=_validate_challenge(challenge_id); key_id=_validate_key_id(key_id)
    ch_path=challenge_dir/(challenge_id+'.json'); ch=_read_json(ch_path)
    if ch.get('schema')!=1 or ch.get('challenge_id')!=challenge_id or ch.get('state')!='PENDING':
        raise PrivError('REMOTE_CHALLENGE_STATE_INVALID')
    try:
        expires=datetime.fromisoformat(str(ch.get('expires_at')).replace('Z','+00:00'))
    except Exception:
        raise PrivError('REMOTE_CHALLENGE_INVALID') from None
    if expires.tzinfo is None or expires.utcoffset() is None: raise PrivError('REMOTE_CHALLENGE_INVALID')
    if _now(now) >= expires.astimezone(timezone.utc): raise PrivError('REMOTE_CHALLENGE_EXPIRED')
    try:
        signature=base64.b64decode(str(signature_b64),validate=True)
    except Exception:
        raise PrivError('REMOTE_SIGNATURE_INVALID') from None
    public_key=_load_public_key(key_id,key_dir)
    if not _verify_signature(public_key,_challenge_message(ch),signature,runner):
        raise PrivError('REMOTE_SIGNATURE_INVALID')
    kind=str(ch.get('kind'))
    suffix='.install.json' if kind=='package.install' else '.rollback.json'
    rec={
        'schema':2,'kind':kind,'plan_id':ch.get('plan_id'),'plan_sha256':ch.get('plan_sha256'),
        'package':ch.get('package'),'source':REMOTE_SOURCE,'auth_method':'ecdsa-p256',
        'key_id':key_id,'challenge_id':challenge_id,'one_shot':True,'consumed':False,'approved_at':utc_now(),
    }
    _atomic_json(approval_dir/(str(ch.get('plan_id'))+suffix),rec)
    ch['state']='APPROVED'; ch['approved_at']=rec['approved_at']; ch['key_id']=key_id
    _atomic_json(ch_path,ch)
    return rec


def install_remote_public_key(key_id: str, public_key_pem: str, key_dir: Path=REMOTE_KEY_DIR, allow_replace: bool=False) -> dict[str, Any]:
    key_id=_validate_key_id(key_id)
    if not isinstance(public_key_pem,str) or len(public_key_pem)>4096: raise PrivError('REMOTE_KEY_INVALID')
    if 'BEGIN PUBLIC KEY' not in public_key_pem or 'PRIVATE KEY' in public_key_pem: raise PrivError('REMOTE_KEY_INVALID')
    key_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=key_dir/(key_id+'.pem')
    if path.exists() and not allow_replace: raise PrivError('REMOTE_KEY_ALREADY_EXISTS')
    tmp=path.with_name('.'+path.name+'.tmp')
    tmp.write_text(public_key_pem if public_key_pem.endswith('\n') else public_key_pem+'\n')
    os.chmod(tmp,0o600)
    check=subprocess.run(['/usr/bin/openssl','pkey','-pubin','-in',str(tmp),'-noout'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
    if check.returncode!=0:
        tmp.unlink(missing_ok=True)
        raise PrivError('REMOTE_KEY_INVALID')
    os.replace(tmp,path)
    import hashlib
    return {'ok':True,'key_id':key_id,'public_key_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


def _change_approval_path(plan_id: str, rollback: bool, approval_dir: Path=APPROVAL_DIR) -> Path:
    suffix='.change-rollback.json' if rollback else '.change.json'
    return approval_dir/(plan_id+suffix)

def approve_change(
    plan_id: str,
    plan_sha256: str,
    capability: str,
    args_sha256: str,
    approval_dir: Path=APPROVAL_DIR,
    stdin_isatty: bool | None=None,
) -> dict[str, Any]:
    require_interactive_tty(stdin_isatty)
    plan_id=_validate_plan_id(plan_id)
    plan_sha256=_validate_sha(plan_sha256)
    capability=_validate_capability(capability)
    args_sha256=_validate_args_sha(args_sha256)
    rec={
      'schema':2,'kind':'system.change','plan_id':plan_id,
      'plan_sha256':plan_sha256,'capability':capability,'args_sha256':args_sha256,
      'source':'LOCAL_TTY','one_shot':True,'consumed':False,'approved_at':utc_now(),
    }
    _atomic_json(_change_approval_path(plan_id,False,approval_dir),rec)
    return rec

def _require_change_approval(
    plan_id: str,
    plan_sha256: str,
    capability: str,
    args: dict[str, Any],
    approval_dir: Path=APPROVAL_DIR,
) -> tuple[Path, dict[str, Any], str]:
    plan_id=_validate_plan_id(plan_id)
    plan_sha256=_validate_sha(plan_sha256)
    capability=_validate_capability(capability)
    args_sha=canonical_args_sha(args)
    ap_path=_change_approval_path(plan_id,False,approval_dir)
    ap=_read_json(ap_path)
    checks={
      'schema':ap.get('schema')==2,
      'kind':ap.get('kind')=='system.change',
      'source':ap.get('source')=='LOCAL_TTY',
      'one_shot':ap.get('one_shot') is True,
      'not_consumed':ap.get('consumed') is False,
      'plan_sha':ap.get('plan_sha256')==plan_sha256,
      'capability':ap.get('capability')==capability,
      'args_sha':ap.get('args_sha256')==args_sha,
    }
    if not all(checks.values()): raise PrivError('CHANGE_APPROVAL_PRECONDITION_FAILED')
    return ap_path,ap,args_sha

def _consume_approval(path: Path, ap: dict[str, Any]) -> None:
    ap['consumed']=True
    ap['consumed_at']=utc_now()
    _atomic_json(path,ap)

def package_version_available(
    package: str,
    version: str,
    runner: Callable[..., subprocess.CompletedProcess[str]]=subprocess.run,
) -> bool:
    package=_validate_package(package)
    version=str(version or '').strip()
    if not version: return False
    p=runner(['/usr/bin/apt-cache','policy',package],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False)
    if p.returncode!=0: return False
    for line in p.stdout.splitlines():
        m=re.match(r'^\s*(?:\*{3}\s*)?(\S+)\s+\d+\s*$',line)
        if m and m.group(1)==version:
            return True
    return False

def apply_remove(
    plan_id: str,
    plan_sha256: str,
    package: str,
    approval_dir: Path=APPROVAL_DIR,
    tx_dir: Path=TX_DIR,
    runner: Callable[..., subprocess.CompletedProcess[str]]=subprocess.run,
) -> dict[str, Any]:
    package=_validate_package(package)
    args={'package':package}
    ap_path,ap,args_sha=_require_change_approval(plan_id,plan_sha256,'package.remove',args,approval_dir)
    before=package_status(package,runner)
    if not before['installed']: raise PrivError('PACKAGE_NOT_INSTALLED')
    if not package_version_available(package,str(before.get('version') or ''),runner):
        raise PrivError('PACKAGE_ROLLBACK_VERSION_UNAVAILABLE')
    env=dict(os.environ); env['DEBIAN_FRONTEND']='noninteractive'
    p=runner(['/usr/bin/apt-get','-y','remove',package],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False,env=env)
    if p.returncode!=0: raise PrivError('APT_REMOVE_FAILED')
    after=package_status(package,runner)
    if after['installed']: raise PrivError('PACKAGE_REMOVE_VERIFY_FAILED')
    _consume_approval(ap_path,ap)
    tx={
      'schema':2,'plan_id':plan_id,'plan_sha256':plan_sha256,
      'capability':'package.remove','args_sha256':args_sha,'args':args,
      'before':before,'after':after,'applied_at':utc_now(),'rollback_available':True,
    }
    _atomic_json(tx_dir/(plan_id+'.json'),tx)
    return {'ok':True,'package':package,'removed':True,'approval_consumed':True,'rollback_available':True}

def service_state(
    service: str,
    runner: Callable[..., subprocess.CompletedProcess[str]]=subprocess.run,
) -> dict[str, Any]:
    service=_validate_service(service)
    a=runner(['/usr/bin/systemctl','is-active',service],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False)
    e=runner(['/usr/bin/systemctl','is-enabled',service],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False)
    return {
      'active':a.stdout.strip() or ('inactive' if a.returncode else 'active'),
      'active_rc':a.returncode,
      'enabled':e.stdout.strip() or 'unknown',
      'enabled_rc':e.returncode,
    }

def apply_service(
    plan_id: str,
    plan_sha256: str,
    service: str,
    action: str,
    approval_dir: Path=APPROVAL_DIR,
    tx_dir: Path=TX_DIR,
    runner: Callable[..., subprocess.CompletedProcess[str]]=subprocess.run,
) -> dict[str, Any]:
    service=_validate_service(service); action=_validate_service_action(action)
    args={'service':service,'action':action}
    ap_path,ap,args_sha=_require_change_approval(plan_id,plan_sha256,'service.manage',args,approval_dir)
    before=service_state(service,runner)
    p=runner(['/usr/bin/systemctl',action,service],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False)
    if p.returncode!=0: raise PrivError('SYSTEMCTL_ACTION_FAILED')
    after=service_state(service,runner)
    if action in ('start','restart') and after['active']!='active': raise PrivError('SERVICE_ACTIVE_VERIFY_FAILED')
    if action=='stop' and after['active']=='active': raise PrivError('SERVICE_STOP_VERIFY_FAILED')
    if action=='enable' and after['enabled']!='enabled': raise PrivError('SERVICE_ENABLE_VERIFY_FAILED')
    if action=='disable' and after['enabled']=='enabled': raise PrivError('SERVICE_DISABLE_VERIFY_FAILED')
    _consume_approval(ap_path,ap)
    tx={
      'schema':2,'plan_id':plan_id,'plan_sha256':plan_sha256,
      'capability':'service.manage','args_sha256':args_sha,'args':args,
      'before':before,'after':after,'applied_at':utc_now(),'rollback_available':True,
    }
    _atomic_json(tx_dir/(plan_id+'.json'),tx)
    return {'ok':True,'service':service,'action':action,'after':after,'approval_consumed':True,'rollback_available':True}

def hostname_state(runner: Callable[..., subprocess.CompletedProcess[str]]=subprocess.run) -> str:
    p=runner(['/usr/bin/hostnamectl','--static'],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False)
    if p.returncode!=0 or not p.stdout.strip(): raise PrivError('HOSTNAME_READ_FAILED')
    return p.stdout.strip()

def apply_hostname(
    plan_id: str,
    plan_sha256: str,
    hostname: str,
    approval_dir: Path=APPROVAL_DIR,
    tx_dir: Path=TX_DIR,
    runner: Callable[..., subprocess.CompletedProcess[str]]=subprocess.run,
) -> dict[str, Any]:
    hostname=_validate_hostname(hostname)
    args={'hostname':hostname}
    ap_path,ap,args_sha=_require_change_approval(plan_id,plan_sha256,'system.hostname',args,approval_dir)
    before=hostname_state(runner)
    p=runner(['/usr/bin/hostnamectl','set-hostname',hostname],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False)
    if p.returncode!=0: raise PrivError('HOSTNAME_SET_FAILED')
    after=hostname_state(runner)
    if after!=hostname: raise PrivError('HOSTNAME_VERIFY_FAILED')
    _consume_approval(ap_path,ap)
    tx={
      'schema':2,'plan_id':plan_id,'plan_sha256':plan_sha256,
      'capability':'system.hostname','args_sha256':args_sha,'args':args,
      'before':{'hostname':before},'after':{'hostname':after},
      'applied_at':utc_now(),'rollback_available':True,
    }
    _atomic_json(tx_dir/(plan_id+'.json'),tx)
    return {'ok':True,'hostname':after,'approval_consumed':True,'rollback_available':True}

def timezone_state(runner: Callable[..., subprocess.CompletedProcess[str]]=subprocess.run) -> str:
    p=runner(['/usr/bin/timedatectl','show','-p','Timezone','--value'],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False)
    if p.returncode!=0 or not p.stdout.strip(): raise PrivError('TIMEZONE_READ_FAILED')
    return p.stdout.strip()

def apply_timezone(
    plan_id: str,
    plan_sha256: str,
    timezone: str,
    approval_dir: Path=APPROVAL_DIR,
    tx_dir: Path=TX_DIR,
    runner: Callable[..., subprocess.CompletedProcess[str]]=subprocess.run,
    zoneinfo_root: Path=ZONEINFO_ROOT,
) -> dict[str, Any]:
    timezone=_validate_timezone(timezone,zoneinfo_root)
    args={'timezone':timezone}
    ap_path,ap,args_sha=_require_change_approval(plan_id,plan_sha256,'system.timezone',args,approval_dir)
    before=timezone_state(runner)
    p=runner(['/usr/bin/timedatectl','set-timezone',timezone],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False)
    if p.returncode!=0: raise PrivError('TIMEZONE_SET_FAILED')
    after=timezone_state(runner)
    if after!=timezone: raise PrivError('TIMEZONE_VERIFY_FAILED')
    _consume_approval(ap_path,ap)
    tx={
      'schema':2,'plan_id':plan_id,'plan_sha256':plan_sha256,
      'capability':'system.timezone','args_sha256':args_sha,'args':args,
      'before':{'timezone':before},'after':{'timezone':after},
      'applied_at':utc_now(),'rollback_available':True,
    }
    _atomic_json(tx_dir/(plan_id+'.json'),tx)
    return {'ok':True,'timezone':after,'approval_consumed':True,'rollback_available':True}

def apply_admin(
    plan_id: str,
    plan_sha256: str,
    capability: str,
    args: dict[str, Any],
    approval_dir: Path=APPROVAL_DIR,
    tx_dir: Path=TX_DIR,
    runner: Callable[..., subprocess.CompletedProcess[str]]=subprocess.run,
) -> dict[str, Any]:
    plan_id=_validate_plan_id(plan_id)
    plan_sha256=_validate_sha(plan_sha256)
    capability=_validate_capability(capability)
    if capability not in R3_CORE_CAPABILITIES: raise PrivError('CHANGE_CAPABILITY_INVALID')
    if not isinstance(args,dict): raise PrivError('CHANGE_ARGS_INVALID')
    ap_path,ap,args_sha=_require_change_approval(plan_id,plan_sha256,capability,args,approval_dir)
    tx_path=tx_dir/(plan_id+'.json')
    if tx_path.exists(): raise PrivError('CHANGE_TRANSACTION_EXISTS')
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    rollback_available=False
    verification: dict[str, Any] = {}

    if capability=='package.refresh':
        if args: raise PrivError('PACKAGE_REFRESH_ARGS_INVALID')
        env=dict(os.environ); env['DEBIAN_FRONTEND']='noninteractive'
        proc=runner(['/usr/bin/apt-get','update'],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False,env=env)
        if proc.returncode!=0: raise PrivError('APT_UPDATE_FAILED')
        after={'refreshed':True}
        verification={'refreshed':True}

    elif capability=='package.upgrade':
        if set(args)!={'package'}: raise PrivError('PACKAGE_UPGRADE_ARGS_INVALID')
        package=_validate_package(args.get('package',''))
        before=package_status(package,runner)
        if not before.get('installed'): raise PrivError('PACKAGE_NOT_INSTALLED')
        old=str(before.get('version') or '')
        candidate=package_candidate(package,runner)
        if not candidate or candidate==old: raise PrivError('PACKAGE_NO_UPGRADE_AVAILABLE')
        if not package_version_available(package,old,runner): raise PrivError('PACKAGE_ROLLBACK_VERSION_UNAVAILABLE')
        env=dict(os.environ); env['DEBIAN_FRONTEND']='noninteractive'
        proc=runner(['/usr/bin/apt-get','-y','--no-install-recommends','--only-upgrade','install',package],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False,env=env)
        if proc.returncode!=0: raise PrivError('APT_UPGRADE_FAILED')
        after=package_status(package,runner)
        if not after.get('installed') or after.get('version')==old: raise PrivError('PACKAGE_UPGRADE_VERIFY_FAILED')
        rollback_available=True
        verification={'package':package,'version':after.get('version')}

    elif capability=='system.file.write_text':
        if set(args)!={'path','text'}: raise PrivError('SYSTEM_FILE_ARGS_INVALID')
        path=_validate_system_path(args.get('path',''))
        text=args.get('text')
        if not isinstance(text,str): raise PrivError('SYSTEM_FILE_TEXT_INVALID')
        data=text.encode('utf-8')
        if len(data)>MAX_SYSTEM_TEXT_BYTES: raise PrivError('SYSTEM_FILE_TEXT_TOO_LARGE')
        before=_file_state(path)
        _atomic_write_system_file(path,data,before)
        after=_file_state(path)
        wanted=hashlib.sha256(data).hexdigest()
        if after.get('sha256')!=wanted: raise PrivError('SYSTEM_FILE_VERIFY_FAILED')
        rollback_available=True
        verification={'path':str(path),'sha256':wanted}

    elif capability=='system.directory.ensure':
        if set(args)!={'path','mode'}: raise PrivError('SYSTEM_DIRECTORY_ARGS_INVALID')
        path=_validate_system_path(args.get('path',''))
        mode_s=_validate_mode(args.get('mode','')); mode=int(mode_s,8)
        before=_directory_state(path)
        if not before.get('exists'):
            if not path.parent.is_dir(): raise PrivError('SYSTEM_DIRECTORY_PARENT_MISSING')
            os.mkdir(path,mode=mode)
            os.chmod(path,mode)
        else:
            os.chmod(path,mode)
        after=_directory_state(path)
        if not after.get('exists') or int(after.get('mode',-1))!=mode: raise PrivError('SYSTEM_DIRECTORY_VERIFY_FAILED')
        rollback_available=(not before.get('exists')) or int(before.get('mode',mode))!=mode
        verification={'path':str(path),'mode':after.get('mode')}

    elif capability=='system.file.mode':
        if set(args)!={'path','mode'}: raise PrivError('SYSTEM_FILE_MODE_ARGS_INVALID')
        path=_validate_system_path(args.get('path',''))
        if not path.exists() or path.is_symlink(): raise PrivError('SYSTEM_FILE_NOT_FOUND')
        mode_s=_validate_mode(args.get('mode','')); mode=int(mode_s,8)
        st=path.stat(); before={'exists':True,'mode':st.st_mode & 0o7777,'uid':st.st_uid,'gid':st.st_gid}
        os.chmod(path,mode)
        st2=path.stat(); after={'exists':True,'mode':st2.st_mode & 0o7777,'uid':st2.st_uid,'gid':st2.st_gid}
        if after['mode']!=mode: raise PrivError('SYSTEM_FILE_MODE_VERIFY_FAILED')
        rollback_available=before['mode']!=mode
        verification={'path':str(path),'mode':after['mode']}

    elif capability=='system.daemon_reload':
        if args: raise PrivError('DAEMON_RELOAD_ARGS_INVALID')
        proc=runner(['/usr/bin/systemctl','daemon-reload'],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False)
        if proc.returncode!=0: raise PrivError('SYSTEMD_DAEMON_RELOAD_FAILED')
        after={'reloaded':True}
        verification={'reloaded':True}

    elif capability=='network.forward':
        if _active_forward_transaction_exists(tx_dir): raise PrivError('NETWORK_FORWARD_TRANSACTION_ALREADY_ACTIVE')
        before,after,verification=_apply_network_forward_runtime(plan_id,args,runner)
        rollback_available=True

    else:
        raise PrivError('CHANGE_CAPABILITY_INVALID')

    _consume_approval(ap_path,ap)
    tx={
      'schema':3,'plan_id':plan_id,'plan_sha256':plan_sha256,
      'capability':capability,'args_sha256':args_sha,'args':args,
      'before':before,'after':after,'applied_at':utc_now(),
      'rollback_available':rollback_available,
    }
    _atomic_json(tx_path,tx)
    return {
      'ok':True,'capability':capability,'approval_consumed':True,
      'rollback_available':rollback_available,'verification':verification,
    }


def approve_change_rollback(
    plan_id: str,
    plan_sha256: str,
    capability: str,
    args_sha256: str,
    approval_dir: Path=APPROVAL_DIR,
    tx_dir: Path=TX_DIR,
    stdin_isatty: bool | None=None,
) -> dict[str, Any]:
    require_interactive_tty(stdin_isatty)
    plan_id=_validate_plan_id(plan_id); plan_sha256=_validate_sha(plan_sha256)
    capability=_validate_capability(capability); args_sha256=_validate_args_sha(args_sha256)
    tx=_read_json(tx_dir/(plan_id+'.json'))
    checks={
      'plan_sha':tx.get('plan_sha256')==plan_sha256,
      'capability':tx.get('capability')==capability,
      'args_sha':tx.get('args_sha256')==args_sha256,
      'rollback_available':tx.get('rollback_available') is True,
    }
    if not all(checks.values()): raise PrivError('CHANGE_ROLLBACK_TRANSACTION_PRECONDITION_FAILED')
    rec={
      'schema':2,'kind':'system.change.rollback','plan_id':plan_id,
      'plan_sha256':plan_sha256,'capability':capability,'args_sha256':args_sha256,
      'source':'LOCAL_TTY','one_shot':True,'consumed':False,'approved_at':utc_now(),
    }
    _atomic_json(_change_approval_path(plan_id,True,approval_dir),rec)
    return rec

def _restore_service(
    before: dict[str, Any],
    service: str,
    action: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, Any]:
    if action in ('enable','disable'):
        prior=before.get('enabled')
        if prior=='enabled':
            p=runner(['/usr/bin/systemctl','enable',service],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False)
            if p.returncode!=0: raise PrivError('SERVICE_ROLLBACK_ENABLE_FAILED')
        elif prior=='disabled':
            p=runner(['/usr/bin/systemctl','disable',service],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False)
            if p.returncode!=0: raise PrivError('SERVICE_ROLLBACK_DISABLE_FAILED')
    prior_active=before.get('active')
    desired='start' if prior_active=='active' else 'stop'
    p=runner(['/usr/bin/systemctl',desired,service],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False)
    if p.returncode!=0: raise PrivError('SERVICE_ROLLBACK_ACTIVE_FAILED')
    return service_state(service,runner)

def apply_change_rollback(
    plan_id: str,
    plan_sha256: str,
    capability: str,
    args_sha256: str,
    approval_dir: Path=APPROVAL_DIR,
    tx_dir: Path=TX_DIR,
    runner: Callable[..., subprocess.CompletedProcess[str]]=subprocess.run,
    zoneinfo_root: Path=ZONEINFO_ROOT,
) -> dict[str, Any]:
    plan_id=_validate_plan_id(plan_id); plan_sha256=_validate_sha(plan_sha256)
    capability=_validate_capability(capability); args_sha256=_validate_args_sha(args_sha256)
    ap_path=_change_approval_path(plan_id,True,approval_dir); ap=_read_json(ap_path)
    tx_path=tx_dir/(plan_id+'.json'); tx=_read_json(tx_path)
    checks={
      'schema':ap.get('schema')==2,'kind':ap.get('kind')=='system.change.rollback',
      'source':ap.get('source')=='LOCAL_TTY','one_shot':ap.get('one_shot') is True,
      'not_consumed':ap.get('consumed') is False,
      'plan_sha':ap.get('plan_sha256')==plan_sha256==tx.get('plan_sha256'),
      'capability':ap.get('capability')==capability==tx.get('capability'),
      'args_sha':ap.get('args_sha256')==args_sha256==tx.get('args_sha256'),
      'rollback_available':tx.get('rollback_available') is True,
    }
    if not all(checks.values()): raise PrivError('CHANGE_ROLLBACK_APPROVAL_PRECONDITION_FAILED')

    args=tx.get('args') or {}
    before=tx.get('before') or {}

    if capability=='package.remove':
        package=_validate_package(args.get('package',''))
        version=str(before.get('version') or '')
        if not version: raise PrivError('PACKAGE_ROLLBACK_VERSION_MISSING')
        env=dict(os.environ); env['DEBIAN_FRONTEND']='noninteractive'
        p=runner(['/usr/bin/apt-get','-y','--no-install-recommends','install',package+'='+version],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False,env=env)
        if p.returncode!=0: raise PrivError('PACKAGE_REINSTALL_FAILED')
        current=package_status(package,runner)
        verified=current['installed'] and current.get('version')==version
        verification={'package':package,'installed':current['installed'],'version':current.get('version')}
    elif capability=='service.manage':
        service=_validate_service(args.get('service',''))
        action=_validate_service_action(args.get('action',''))
        current=_restore_service(before,service,action,runner)
        verified=(current.get('active')==before.get('active'))
        if action in ('enable','disable') and before.get('enabled') in ('enabled','disabled'):
            verified=verified and current.get('enabled')==before.get('enabled')
        verification={'service':service,'state':current}
    elif capability=='system.hostname':
        old=_validate_hostname(before.get('hostname',''))
        p=runner(['/usr/bin/hostnamectl','set-hostname',old],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False)
        if p.returncode!=0: raise PrivError('HOSTNAME_ROLLBACK_FAILED')
        current=hostname_state(runner); verified=current==old
        verification={'hostname':current}
    elif capability=='system.timezone':
        old=_validate_timezone(before.get('timezone',''),zoneinfo_root)
        p=runner(['/usr/bin/timedatectl','set-timezone',old],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False)
        if p.returncode!=0: raise PrivError('TIMEZONE_ROLLBACK_FAILED')
        current=timezone_state(runner); verified=current==old
        verification={'timezone':current}
    elif capability=='package.upgrade':
        package=_validate_package(args.get('package',''))
        version=str(before.get('version') or '')
        if not version: raise PrivError('PACKAGE_ROLLBACK_VERSION_MISSING')
        env=dict(os.environ); env['DEBIAN_FRONTEND']='noninteractive'
        p=runner(['/usr/bin/apt-get','-y','--allow-downgrades','--no-install-recommends','install',package+'='+version],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False,shell=False,env=env)
        if p.returncode!=0: raise PrivError('PACKAGE_DOWNGRADE_FAILED')
        current=package_status(package,runner)
        verified=current.get('installed') is True and current.get('version')==version
        verification={'package':package,'version':current.get('version')}
    elif capability=='system.file.write_text':
        path=_validate_system_path(args.get('path',''))
        if before.get('exists'):
            data=base64.b64decode(str(before.get('content_b64') or ''),validate=True)
            _atomic_write_system_file(path,data,before)
            current=_file_state(path)
            verified=current.get('sha256')==before.get('sha256')
            verification={'path':str(path),'sha256':current.get('sha256')}
        else:
            if path.exists():
                if path.is_symlink() or not path.is_file(): raise PrivError('SYSTEM_FILE_ROLLBACK_TARGET_INVALID')
                path.unlink()
            verified=not path.exists()
            verification={'path':str(path),'exists':path.exists()}
    elif capability=='system.directory.ensure':
        path=_validate_system_path(args.get('path',''))
        if before.get('exists'):
            if not path.is_dir() or path.is_symlink(): raise PrivError('SYSTEM_DIRECTORY_ROLLBACK_TARGET_INVALID')
            os.chmod(path,int(before['mode']))
            st=path.stat()
            verified=(st.st_mode & 0o7777)==int(before['mode'])
            verification={'path':str(path),'mode':st.st_mode & 0o7777}
        else:
            if path.exists():
                try: os.rmdir(path)
                except OSError: raise PrivError('SYSTEM_DIRECTORY_ROLLBACK_NOT_EMPTY') from None
            verified=not path.exists()
            verification={'path':str(path),'exists':path.exists()}
    elif capability=='system.file.mode':
        path=_validate_system_path(args.get('path',''))
        if not path.exists() or path.is_symlink(): raise PrivError('SYSTEM_FILE_ROLLBACK_TARGET_INVALID')
        os.chmod(path,int(before['mode']))
        st=path.stat()
        verified=(st.st_mode & 0o7777)==int(before['mode'])
        verification={'path':str(path),'mode':st.st_mode & 0o7777}
    elif capability=='network.forward':
        verification=_rollback_network_forward_runtime(before,runner)
        verified=verification.get('verified') is True
    else:
        raise PrivError('CHANGE_CAPABILITY_INVALID')

    if not verified: raise PrivError('CHANGE_ROLLBACK_VERIFY_FAILED')
    _consume_approval(ap_path,ap)
    tx['rollback_available']=False
    tx['rolled_back_at']=utc_now()
    tx['rollback_verified']=True
    _atomic_json(tx_path,tx)
    return {'ok':True,'capability':capability,'rollback_verified':True,'rollback_approval_consumed':True,'verification':verification}


def emit(v: Any) -> None: print(json.dumps(v,sort_keys=True,indent=2))


def main() -> int:
    require_root()
    p=argparse.ArgumentParser(prog='framebridge-seed-priv')
    sp=p.add_subparsers(dest='cmd',required=True)
    for name in ('approve-install','apply-install','approve-rollback','apply-rollback'):
        x=sp.add_parser(name); x.add_argument('--plan-id',required=True); x.add_argument('--plan-sha256',required=True); x.add_argument('--package',required=True)
    c=sp.add_parser('remote-challenge')
    c.add_argument('--kind',required=True,choices=('package.install','package.rollback')); c.add_argument('--plan-id',required=True); c.add_argument('--plan-sha256',required=True); c.add_argument('--package',required=True); c.add_argument('--ttl',type=int,default=300)
    r=sp.add_parser('approve-remote-challenge')
    r.add_argument('--challenge-id',required=True); r.add_argument('--key-id',required=True); r.add_argument('--signature-b64',required=True)
    te=sp.add_parser('totp-enroll')
    te.add_argument('--key-id',required=True); te.add_argument('--node-label',required=True); te.add_argument('--issuer',default='FrameBridge')
    tc=sp.add_parser('totp-challenge')
    tc.add_argument('--kind',required=True,choices=('package.install','package.rollback')); tc.add_argument('--plan-id',required=True); tc.add_argument('--plan-sha256',required=True); tc.add_argument('--package',required=True); tc.add_argument('--key-id',required=True); tc.add_argument('--ttl',type=int,default=120)
    ta=sp.add_parser('approve-totp-challenge')
    ta.add_argument('--challenge-id',required=True); ta.add_argument('--key-id',required=True)
    for name in ('approve-change','approve-change-rollback','apply-change-rollback'):
        x=sp.add_parser(name)
        x.add_argument('--plan-id',required=True)
        x.add_argument('--plan-sha256',required=True)
        x.add_argument('--capability',required=True)
        x.add_argument('--args-sha256',required=True)

    x=sp.add_parser('apply-remove')
    x.add_argument('--plan-id',required=True); x.add_argument('--plan-sha256',required=True); x.add_argument('--package',required=True)

    x=sp.add_parser('apply-service')
    x.add_argument('--plan-id',required=True); x.add_argument('--plan-sha256',required=True)
    x.add_argument('--service',required=True); x.add_argument('--action',required=True)

    x=sp.add_parser('apply-hostname')
    x.add_argument('--plan-id',required=True); x.add_argument('--plan-sha256',required=True); x.add_argument('--hostname',required=True)

    x=sp.add_parser('apply-timezone')
    x.add_argument('--plan-id',required=True); x.add_argument('--plan-sha256',required=True); x.add_argument('--timezone',required=True)

    x=sp.add_parser('apply-admin')
    x.add_argument('--plan-id',required=True)
    x.add_argument('--plan-sha256',required=True)
    x.add_argument('--capability',required=True)
    x.add_argument('--args-sha256',required=True)

    pw=sp.add_parser('pairing-window-open')
    pw.add_argument('--interface',required=True)
    pw.add_argument('--source-subnet',required=True)
    pw.add_argument('--destination-address',required=True)
    pw.add_argument('--port',required=True,type=int)
    pw.add_argument('--network-fingerprint',required=True)
    pw.add_argument('--ttl',required=True,type=int)

    sp.add_parser('pairing-window-status')
    pwc=sp.add_parser('pairing-window-close')
    pwc.add_argument('--reason',default='user')
    pwe=sp.add_parser('pairing-window-expire')
    pwe.add_argument('--window-id',required=True)

    a=p.parse_args()
    try:
        if a.cmd=='approve-install': emit(approve_install(a.plan_id,a.plan_sha256,a.package))
        elif a.cmd=='apply-install': emit(apply_install(a.plan_id,a.plan_sha256,a.package))
        elif a.cmd=='approve-rollback': emit(approve_rollback(a.plan_id,a.plan_sha256,a.package))
        elif a.cmd=='apply-rollback': emit(apply_rollback(a.plan_id,a.plan_sha256,a.package))
        elif a.cmd=='apply-admin':
            try:
                args_value=json.loads(sys.stdin.read() or '{}')
            except Exception:
                raise PrivError('CHANGE_ARGS_INVALID_JSON')
            if not isinstance(args_value,dict): raise PrivError('CHANGE_ARGS_INVALID')
            if canonical_args_sha(args_value)!=_validate_args_sha(a.args_sha256):
                raise PrivError('CHANGE_ARGS_HASH_MISMATCH')
            emit(apply_admin(a.plan_id,a.plan_sha256,a.capability,args_value))
        elif a.cmd=='pairing-window-open': emit(pairing_window_open(a.interface,a.source_subnet,a.destination_address,a.port,a.network_fingerprint,a.ttl))
        elif a.cmd=='pairing-window-status': emit(pairing_window_status())
        elif a.cmd=='pairing-window-close': emit(pairing_window_close(a.reason))
        elif a.cmd=='pairing-window-expire': emit(pairing_window_expire(a.window_id))
        elif a.cmd=='approve-change': emit(approve_change(a.plan_id,a.plan_sha256,a.capability,a.args_sha256))
        elif a.cmd=='approve-change-rollback': emit(approve_change_rollback(a.plan_id,a.plan_sha256,a.capability,a.args_sha256))
        elif a.cmd=='apply-change-rollback': emit(apply_change_rollback(a.plan_id,a.plan_sha256,a.capability,a.args_sha256))
        elif a.cmd=='apply-remove': emit(apply_remove(a.plan_id,a.plan_sha256,a.package))
        elif a.cmd=='apply-service': emit(apply_service(a.plan_id,a.plan_sha256,a.service,a.action))
        elif a.cmd=='apply-hostname': emit(apply_hostname(a.plan_id,a.plan_sha256,a.hostname))
        elif a.cmd=='apply-timezone': emit(apply_timezone(a.plan_id,a.plan_sha256,a.timezone))
        elif a.cmd=='remote-challenge': emit(create_remote_challenge(a.kind,a.plan_id,a.plan_sha256,a.package,ttl_seconds=a.ttl))
        elif a.cmd=='approve-remote-challenge': emit(approve_remote_challenge(a.challenge_id,a.key_id,a.signature_b64))
        elif a.cmd=='totp-enroll': emit(enroll_totp_key(a.key_id,a.node_label,a.issuer))
        elif a.cmd=='totp-challenge': emit(create_totp_challenge(a.kind,a.plan_id,a.plan_sha256,a.package,a.key_id,ttl_seconds=a.ttl))
        elif a.cmd=='approve-totp-challenge':
            code=sys.stdin.readline().strip()
            emit(approve_totp_challenge(a.challenge_id,a.key_id,code))
        return 0
    except PrivError as e:
        emit({'ok':False,'error':str(e)}); return 2


if __name__=='__main__': raise SystemExit(main())
