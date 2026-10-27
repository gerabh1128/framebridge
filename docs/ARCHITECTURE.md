# Architecture at a glance

FrameBridge connects a hosted ChatGPT conversation to tools on computers controlled by the owner. A manual connection links the configured standalone or central instance to the owner's ChatGPT account.

## One computer

A standalone installation provides execution, persistent terminal and screen interaction on its computer. FBEXEC carries out a selected task. FBTERM keeps a terminal session available so work can be resumed with its existing shell state. CUA and CUA-TTY expose the actual graphical or text console supported by that installation.

## Central and nodes

A central instance provides the fleet entry point. Approved worker nodes supply execution on their own computers; the operator selects the intended node for each task. The node workflow is described in the [private evaluation guide](PRIVATE-EVALUATION.md) and [Debian tutorial](../source/docs/DEBIAN-TUTORIAL.md).

A worker does not need a separate ChatGPT tunnel. ChatGPT remains hosted while tools run on the selected owner-controlled machines.

## Sessions and shared resources

A persistent terminal is useful for returning to an existing shell. It does not make simultaneous edits to the same files safe. Separate conversations have been used for non-overlapping FBEXEC work; isolation between those conversations has not been certified.

Graphical control shares the desktop's focus, keyboard and mouse. Use one controlling instance per desktop at a time. This is a recommendation for operation, not a certified automatic exclusion mechanism.

## Evidence and presentation

Show the complete, authentic screen when presenting a CUA capture. Label a conceptual animation as an illustration. A conceptual promotional video is separate from a real execution recording.

The recorded RC7 acceptance evidence and the current runtime state answer different questions. After a reboot, inspect the current connection and session before claiming that a live demonstration is ready.
