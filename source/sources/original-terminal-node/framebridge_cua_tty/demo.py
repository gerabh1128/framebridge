"""No-side-effects simulated interactive menu. Not a shell."""
import os
import sys
import tty

OPTIONS = ('Estado del sistema (demo)', 'Almacenamiento (demo)', 'Red (demo)', 'Salir')


def show(i):
    sys.stdout.write('\x1b[2J\x1b[H')
    sys.stdout.write('FrameBridge CUA-TTY - DEMOSTRACION AISLADA\r\n')
    sys.stdout.write('NO ejecuta comandos; NO modifica el servidor.\r\n\r\n')
    for n, label in enumerate(OPTIONS):
        sys.stdout.write(('> ' if n == i else '  ') + label + '\r\n')
    sys.stdout.write('\r\nUse flechas arriba/abajo, Enter y q.\r\n')
    sys.stdout.flush()


def main():
    tty.setcbreak(sys.stdin.fileno())
    i = 0
    show(i)
    while True:
        b = os.read(0, 1)
        if not b: break
        if b in (b'q', b'Q', b'\x03'): break
        if b == b'\x1b':
            seq = os.read(0, 2)
            if seq == b'[A': i = (i - 1) % len(OPTIONS)
            elif seq == b'[B': i = (i + 1) % len(OPTIONS)
        elif b in (b'j', b'J'): i = (i + 1) % len(OPTIONS)
        elif b in (b'k', b'K'): i = (i - 1) % len(OPTIONS)
        elif b in (b'\r', b'\n'):
            if i == len(OPTIONS) - 1: break
            sys.stdout.write('\r\nSeleccion simulada: ' + OPTIONS[i] + '\r\n')
            sys.stdout.flush()
        show(i)
    sys.stdout.write('\r\nDEMOSTRACION_FINALIZADA\r\n')
    sys.stdout.flush()


if __name__ == '__main__':
    main()
