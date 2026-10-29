# Historical RC7 artifact provenance

The following identifiers describe the accepted historical RC7 artifacts, not the current community candidate.

| Field | Historical value |
| --- | --- |
| Public installer filename | `FrameBridge-0.3.0-RC7.deb` |
| Debian package | `framebridge-original-expanded` |
| Debian version | `0.3.0~expanded.20261024.7` |
| Source archive | `framebridge-source-0.3.0~expanded.20261024.7.tar.gz` |

Installer SHA-256:

```text
55f391ed169626b24f589d26774f13e89083badaf7fa6af830f4776a14452e47
```

Original source archive SHA-256:

```text
59e5e34a35a3e9f235a4cb2747b993b78af7e37c7407435dbdfa594b975d2557
```

The historical `framebridge.deb` name and the public versioned installer refer to the same RC7 bytes. The original acceptance evidence, export and installer remain preserved.

## Current source is a separate candidate

Community Preview 1 (`0.3.1~community.20261025.1`) changed 12 baseline paths and added a test to the 1,070-file RC7 export. It passed 368 tests and a reproducible package build.

The current `source/` tree targets Preview 2, `0.3.1~community.20261026.2`. It adds an explicit development-broker peer policy, current installation instructions and R2 source/notice records. Current manifests, rather than historical file counts, describe the actual release set. Preview 2 passed 376 tests in 22.351 seconds; package and VM validation are pending at this documentation freeze.

`SOURCE-SNAPSHOT-SHA256.json` is retained as a historical baseline record. It must not be used as a claim that every current file is unchanged. The candidate's source and installer require their own artifact hashes and validation.

Running `source/packaging/build_expanded.py` from the current tree builds the community candidate. To reproduce RC7, use a separate extraction of the exact original source archive above and its original builder. Do not overwrite historical artifacts during that comparison.

The owner selected GPL-3.0-only for original FrameBridge material on October 25, 2026. Repository licensing records and the candidate's new legal material do not claim that those files were already present inside the historical RC7 package. Third-party obligations and the candidate's corresponding-source arrangements remain distinct release responsibilities.
