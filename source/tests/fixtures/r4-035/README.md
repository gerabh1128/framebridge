# R4 regression inputs

These are source-test inputs, never runtime profiles or authentic screen captures.

`runtime.py` and `fbterm_engine.py` preserve the exact published 0.3.5 inputs from
tree `2f62e03c1f7e89589b614cba3def37c7d8674018`. They reproduce the stale-observation
case and compare the full old/new job audit. They are imported under isolated test
names and are never installed or launched as services.

`STRUCTURAL-GUARDS.json` contains hashes of published 0.3.5 ASTs and unchanged
helper files. The tests load current product files from the full source tree.
AST fingerprints use the portable `r4_ast.py` serializer, shared with the
maintained viewer-test contract: optional empty fields are omitted, while
semantic values and placeholders inside nonempty lists are retained.
The original broker Store and signing helper are likewise loaded from their
existing public source paths, without duplicating deployment material.

Run from a complete source checkout:

```sh
python3 -B -m unittest discover -s source/tests -p 'test_r4_*.py'
```

The pixel fixtures are generated in temporary directories. They require Pillow
and ImageMagick for tests; no physical display or input device is used. Protocol
tests use disposable temporary SSHSIG signers and owned loopback listeners.
No owner's identities, credentials, privileged installer stages, private SMTP
policy, runtime observations or real captured pixels are included.

The tests preserve the complete job audit, signed nonce/expiry and single-dispatch
behavior, authorization boundaries, stable window geometry and post-action
evidence. Source-test success does not certify installation or desktop lifecycle
on any distribution.
