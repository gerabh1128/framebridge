"""Small bounded VT-like ASCII/UTF-8 text screen for staging.

This is deliberately NOT advertised as a full xterm emulator. It handles common
CSI cursor/erase controls and suppresses OSC/DCS/other escape payloads.
"""
from __future__ import annotations
import codecs
import unicodedata

class Screen:
    def __init__(self, rows: int = 24, cols: int = 80):
        if not 8 <= rows <= 80 or not 20 <= cols <= 200:
            raise ValueError('SCREEN_DIMENSIONS_INVALID')
        self.rows, self.cols = rows, cols
        self.buf = [[' '] * cols for _ in range(rows)]
        self.y = self.x = 0
        self.mode = 'normal'
        self.control = ''
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.max_control = 96

    def reset(self):
        self.buf = [[' '] * self.cols for _ in range(self.rows)]
        self.y = self.x = 0

    def resize(self, rows: int, cols: int):
        if not 8 <= rows <= 80 or not 20 <= cols <= 200:
            raise ValueError('SCREEN_DIMENSIONS_INVALID')
        new = [[' '] * cols for _ in range(rows)]
        for y in range(min(rows, self.rows)):
            for x in range(min(cols, self.cols)):
                new[y][x] = self.buf[y][x]
        self.rows, self.cols, self.buf = rows, cols, new
        self.y = min(self.y, rows - 1)
        self.x = min(self.x, cols - 1)

    def scroll(self):
        self.buf.pop(0)
        self.buf.append([' '] * self.cols)
        self.y = self.rows - 1

    def newline(self):
        self.y += 1
        if self.y >= self.rows:
            self.scroll()

    def put(self, c: str):
        if unicodedata.combining(c):
            return
        # Avoid unmodeled full-width terminal-cell ambiguity in this R1 pilot.
        if unicodedata.east_asian_width(c) in ('W', 'F'):
            c = '?'
        if self.x >= self.cols:
            self.x = 0
            self.newline()
        self.buf[self.y][self.x] = c
        self.x += 1

    def csi(self, final: str, args: str):
        if len(args) > self.max_control:
            return
        if args.startswith('?'):
            if final in ('h', 'l') and args in ('?1049', '?1047', '?47') and final == 'h':
                self.reset()
            return
        nums = [int(a) if a.isdecimal() else 0 for a in args.split(';')[:8]]
        n = nums[0] if nums else 0
        count = max(1, min(n, 200))
        if final == 'A': self.y = max(0, self.y - count)
        elif final == 'B': self.y = min(self.rows - 1, self.y + count)
        elif final == 'C': self.x = min(self.cols - 1, self.x + count)
        elif final == 'D': self.x = max(0, self.x - count)
        elif final in ('H', 'f'):
            self.y = max(0, min(self.rows - 1, (nums[0] if nums and nums[0] else 1) - 1))
            self.x = max(0, min(self.cols - 1, (nums[1] if len(nums) > 1 and nums[1] else 1) - 1))
        elif final == 'G': self.x = max(0, min(self.cols - 1, count - 1))
        elif final == 'd': self.y = max(0, min(self.rows - 1, count - 1))
        elif final == 'J':
            if n == 2: self.reset()
            elif n == 0:
                for y in range(self.y, self.rows):
                    start = self.x if y == self.y else 0
                    for x in range(start, self.cols): self.buf[y][x] = ' '
        elif final == 'K':
            start, end = (0, self.cols) if n == 2 else ((0, self.x + 1) if n == 1 else (self.x, self.cols))
            for x in range(start, min(end, self.cols)): self.buf[self.y][x] = ' '
        # SGR and unrecognized commands are intentionally ignored.

    def feed(self, data: bytes):
        for c in self.decoder.decode(data):
            if self.mode == 'normal':
                if c == '\x1b': self.mode = 'esc'
                elif c == '\r': self.x = 0
                elif c == '\n': self.newline()
                elif c == '\b': self.x = max(0, self.x - 1)
                elif c == '\t': self.x = min(self.cols - 1, (self.x // 8 + 1) * 8)
                elif c.isprintable() and c not in '\x7f': self.put(c)
            elif self.mode == 'esc':
                if c == '[': self.mode, self.control = 'csi', ''
                elif c == ']': self.mode, self.control = 'osc', ''
                elif c in ('P', '^', '_'): self.mode, self.control = 'str', ''
                elif c == 'c': self.reset(); self.mode = 'normal'
                else: self.mode = 'normal'
            elif self.mode == 'csi':
                if '@' <= c <= '~':
                    self.csi(c, self.control); self.mode = 'normal'; self.control = ''
                elif len(self.control) >= self.max_control: self.mode, self.control = 'discard_csi', ''
                else: self.control += c
            elif self.mode == 'discard_csi':
                if '@' <= c <= '~': self.mode = 'normal'
            elif self.mode in ('osc', 'str'):
                if c == '\x07' and self.mode == 'osc': self.mode = 'normal'
                elif c == '\x1b': self.mode = 'string_esc'
            elif self.mode == 'string_esc':
                if c == '\\': self.mode = 'normal'
                elif c == '\x1b': self.mode = 'string_esc'
                else: self.mode = 'str'

    def snapshot(self):
        return {'rows': self.rows, 'cols': self.cols, 'cursor': {'row': self.y, 'col': self.x},
                'lines': [''.join(row).rstrip() for row in self.buf]}
