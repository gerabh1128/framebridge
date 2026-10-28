# Private X11 text candidate 0.3.2+private.20261028.2

This candidate is private and uninstalled. It does not change the frozen 0.3.2 source or the six installed private.20261028.1 packages. No signing, publication or public activation is part of this preparation.

The X11 KEYS backend chooses keycodes only from validated keyboard group 0 and sends direct XTEST events. It never invokes the libxdo group-switching sender, changes a keyboard map/group, or reads or writes the clipboard. The captured original source remains byte-for-byte unchanged. The existing action authorization, target-window and geometry checks still run before the project-owned primitive hook. Every permission batch repeats the display guard; the child rechecks the fixed context after permission and before events. A bounded isolated child prevents a stalled Xlib roundtrip from indefinitely blocking the native host.

Supported native context is x86-64 LP64, libxdo version 3.20160805.1, active/locked keyboard group 0, no held/latched modifiers or group latch, and conventional Caps/Num locked bits only. All characters, action table indices and generated key behaviors are checked before the first input batch. Modifier actions must be exact SetMods for their physical modifier bit; ClearLocks is accepted only if that bit is not locked. Other groups, native ABIs, remapped actions and unrepresentable text are refused before events. No legacy text-sender fallback is permitted.

An attempted input batch remains conservatively marked as possible input after a failure or timeout; automatic replay is forbidden. Cleanup attempts to release all attempted keys. A stalled or terminated X server can prevent confirmation of cleanup, so the result must remain uncertain.

## Evidence and acceptance scope

- Fix source commit: b283999f5aef615b2acee7754e438cdd01cc75bd.
- Approved helper SHA-256: d0c8a1f56a6a8e3d16a7d676bcc0a858318cbe590ea825f76bceb6a1d7045470.
- Approved native host SHA-256: 9a2f2af559793aa93c0623d307f6383494fb0b4863db2c425f73ed67ed7ae65f.
- The fix worktree passed all 800 tests; remote receipt gerameg-mcp-20261028-090452-f2ada16c, stdout SHA-256 b073bf80ad84bb1aeb6105fd8d8796106c231f20c0ce060ffc66d320d4e62e42.
- ASUS read-only preflight for a 169-character command-shaped ASCII probe passed with group 0, no held modifiers, Num Lock mask 16 and slash keycode 61; event emission was disabled. Receipt asus-mcp-20261028-085936-a8426ef7, stdout SHA-256 235d7cc42b8313fccdf49c604530c47e6b14c4505ba4f3d324d0b0e98fc1aca1.
- The root GUI controller then tested the reviewed standalone development helper in its own foreground cat receiver: 169 characters exactly matched, 169 permission batches, 5.704268 seconds, with keyboard map/group/held/Num Lock state unchanged. Production EXEC receipt 090055-db27c436d046 and receiver receipt 090127-6ceba9d8e540.
- That live result validates the development helper, not the installed MCP pipeline. This candidate still requires a private installed pilot and exact receiver comparison through the final package before any broader activation.

## Primary implementation references

- Public 2016 libxdo source and ABI: https://sources.debian.org/src/xdotool/1%3A3.20160805.1-5/
- XKB public structures: https://gitlab.freedesktop.org/xorg/proto/xorgproto/-/blob/master/include/X11/extensions/XKBstr.h
- X.Org XKB library specification, action and behavior tables: https://www.x.org/archive/X11R7.7/doc/libX11/XKB/xkblib.html
