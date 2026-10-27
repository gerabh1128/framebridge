from __future__ import annotations
import base64,hashlib,hmac,os,struct,time
from pathlib import Path

class TotpError(RuntimeError): pass

def b32decode_nopad(value:str)->bytes:
    s=''.join(str(value).strip().upper().split())
    if not s or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567' for c in s): raise TotpError('TOTP_SECRET_INVALID')
    s += '='*((8-len(s)%8)%8)
    try:return base64.b32decode(s,casefold=True)
    except Exception: raise TotpError('TOTP_SECRET_INVALID') from None

def hotp(secret:bytes,counter:int,digits:int=6)->str:
    digest=hmac.new(secret,struct.pack('>Q',int(counter)),hashlib.sha1).digest()
    off=digest[-1]&0x0f
    code=((digest[off]&0x7f)<<24)|((digest[off+1]&0xff)<<16)|((digest[off+2]&0xff)<<8)|(digest[off+3]&0xff)
    return str(code%(10**digits)).zfill(digits)

def verify_code(secret:bytes,code:str,when:float|None=None,window:int=1,period:int=30)->int|None:
    if not isinstance(code,str) or len(code)!=6 or not code.isdigit(): return None
    now=time.time() if when is None else float(when)
    step=int(now//period)
    for s in range(step-window,step+window+1):
        if hmac.compare_digest(hotp(secret,s),code): return s
    return None

def load_secret(path:Path)->bytes:
    p=Path(path)
    if not p.is_file() or p.is_symlink(): raise TotpError('TOTP_KEY_NOT_FOUND')
    raw=p.read_text().strip()
    secret=b32decode_nopad(raw)
    if len(secret)<20: raise TotpError('TOTP_SECRET_TOO_SHORT')
    return secret

def consume_step(used_dir:Path,key_id:str,step:int,challenge_id:str)->None:
    d=Path(used_dir);d.mkdir(parents=True,exist_ok=True,mode=0o700)
    p=d/(f'{key_id}-{step}.used')
    try:fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError: raise TotpError('TOTP_REPLAY') from None
    with os.fdopen(fd,'w') as f:f.write(str(challenge_id)+'\n')
