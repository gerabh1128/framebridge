# Desktop console upgrade

The desktop console and its authenticated tty1 view share the existing local Bash and FBTERM activity dashboard. A desktop attachment preserves its Bash process, other work windows and login. The dashboard is read-only; the Bash pane is the command prompt.

Older private installations used a user-level desktop launcher. That launcher could hide the package launcher and reject a valid console after other work windows were added. Version 0.3.3 targets the existing named `Console` window, while preserving the other windows.

During an upgrade, a running profile can migrate the exact recognized old launcher to the packaged CLI. Custom launchers and changed legacy components are preserved. The old desktop entry is backed up privately in the profile; the console socket/session binding is recorded there. No login or boot policy changes.

For an explicit owner repair, use the real configured profile:

```sh
fb --state-dir /absolute/path/to/profile --json console desktop migrate-legacy
```

Then open **FrameBridge Console** from the desktop. The command does not open a generic replacement terminal, recreate the Bash session or delete additional work windows.
