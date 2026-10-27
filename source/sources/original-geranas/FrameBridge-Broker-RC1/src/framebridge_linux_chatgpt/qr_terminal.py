from __future__ import annotations
import sys
from .vendor.qrcodegen import QrCode

class TerminalQrError(RuntimeError):
    pass

def _prepare(text, border, stream, require_tty):
    if not isinstance(text,str) or not text: raise TerminalQrError('QR_TEXT_INVALID')
    if not (2 <= int(border) <= 8): raise TerminalQrError('QR_BORDER_INVALID')
    out=stream or sys.stdout
    if require_tty and not getattr(out,'isatty',lambda:False)(): raise TerminalQrError('INTERACTIVE_TTY_REQUIRED')
    return QrCode.encode_text(text,QrCode.Ecc.MEDIUM),out

def _dark(qr,border,x,y):
    size=qr.get_size()
    if x<border or y<border or x>=border+size or y>=border+size: return False
    return qr.get_module(x-border,y-border)

def render_qr_compact(text, *, border=4, stream=None, require_tty=True):
    qr,out=_prepare(text,border,stream,require_tty)
    total=qr.get_size()+border*2
    for y in range(0,total,2):
        row=[]
        for x in range(total):
            top=_dark(qr,border,x,y)
            bottom=_dark(qr,border,x,y+1) if y+1<total else False
            if top and bottom: row.append('\x1b[40m ')
            elif top: row.append('\x1b[30;47m▀')
            elif bottom: row.append('\x1b[30;47m▄')
            else: row.append('\x1b[47m ')
        out.write(''.join(row)+'\x1b[0m\n')
    out.flush()

def render_qr_mono(text, *, border=4, stream=None, require_tty=True):
    qr,out=_prepare(text,border,stream,require_tty)
    total=qr.get_size()+border*2
    for y in range(0,total,2):
        row=[]
        for x in range(total):
            top=_dark(qr,border,x,y)
            bottom=_dark(qr,border,x,y+1) if y+1<total else False
            row.append('█' if top and bottom else '▀' if top else '▄' if bottom else ' ')
        out.write(''.join(row)+'\n')
    out.flush()

def render_qr_ansi(text, *, border=4, stream=None, require_tty=True):
    qr,out=_prepare(text,border,stream,require_tty)
    size=qr.get_size(); black='\x1b[40m  '; white='\x1b[47m  '; reset='\x1b[0m'; quiet=white*(size+border*2)+reset+'\n'
    for _ in range(border): out.write(quiet)
    for y in range(size):
        row=[white]*border
        for x in range(size): row.append(black if qr.get_module(x,y) else white)
        row.extend([white]*border); out.write(''.join(row)+reset+'\n')
    for _ in range(border): out.write(quiet)
    out.flush()

def render_qr_unicode(text, *, border=4, stream=None, require_tty=True):
    qr,out=_prepare(text,border,stream,require_tty)
    size=qr.get_size(); pad='  '*(size+border*2)
    for _ in range(border): out.write(pad+'\n')
    for y in range(size):
        out.write('  '*border)
        for x in range(size): out.write('██' if qr.get_module(x,y) else '  ')
        out.write('  '*border+'\n')
    for _ in range(border): out.write(pad+'\n')
    out.flush()
