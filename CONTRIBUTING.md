# Contributing

Original FrameBridge code and project documentation use GPL-3.0-only within [LICENSING.md](LICENSING.md). Clearly marked original blog article text uses CC-BY-SA-4.0 under [blog/LICENSING.md](blog/LICENSING.md). Contributors keep their copyrights. No automatic copyright assignment or broad contributor agreement is created here.

You may use, change and redistribute the software under its applicable licenses. Private modifications and independent forks do not have to be submitted upstream. To improve an official release, propose the change through the project Git so maintainers and the community can review it.

Work in a separate source checkout. Describe the problem, resulting behavior, affected version and focused validation. Keep changes reviewable, identify third-party material and retain its notices. Never include credentials, runtime profiles, private keys, cookies or unrelated personal data in an issue, recording, commit or patch.

Official branches and release artifacts are maintainer-controlled. Changes enter them through review; official packages are built from identified source revisions and signed through the release process. A pull request does not itself become an official binary. See [OFFICIAL-RELEASES.md](OFFICIAL-RELEASES.md).

Preserve the historical RC7 installer and source evidence. New executable or payload changes require a separately identifiable build. Do not present a modified artifact as the unchanged certified RC7 release.

## Run focused source tests

The previously documented `tools/run_tests.py` helper is absent from the frozen 0.3.5 tree. The historical 1,011-case native run remains recorded release evidence; its full-suite harness and generated `source/qa` fixtures are not published in this snapshot. A clean checkout therefore does not currently provide that one-command full-suite reproduction.

From the repository root, the tracked connection-monitor tests can be run directly with the supported Linux system Python, as a normal user:

~~~bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=source/expanded:source/portable:source/tests python3 -m unittest -v test_connection_monitor
~~~

This focused suite uses temporary fixtures and loopback HTTP sockets. It requires permission to create those sockets; it neither connects a real FrameBridge profile nor installs a package. It exercises readiness, bounded metric parsing, private logs and foreground cleanup. It is not the full native suite or a package, desktop, upgrade or reboot acceptance test.

Other modules require additional native tools or release fixtures. Record their actual prerequisites, failures and omissions instead of substituting the historical case count for a fresh result. Publishing and validating a complete clean-checkout harness is separate follow-up work.

Release preparation follows the [reviewed source and release workflow](docs/RELEASE-WORKFLOW.md). Report the tested commit and separate native test evidence from GitHub CI results; a draft or technically mergeable PR is not approved for integration.
