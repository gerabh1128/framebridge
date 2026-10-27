# FrameBridge — private technical evaluation brief

FrameBridge connects hosted ChatGPT conversations to execution, persistent-terminal and real-screen tools on Linux computers controlled by their owner. A central instance provides access to approved worker nodes so the operator can select where a task runs.

The core workflow is to request a small task, inspect its result on the selected computer and continue in the same terminal. Separate conversations have been used for independent FBEXEC tasks when objectives and changes did not overlap. That observed use does not establish isolation or simultaneous execution of every command. A graphical desktop shares focus, keyboard and mouse; one controlling instance per desktop is an operating recommendation.

## Recorded baseline and current candidate

Original Expanded RC7 targets Debian 13 amd64 with manual account consent and connection. Its historical acceptance record includes 361 unit tests, a byte-identical package rebuild, Debian VM installation/lifecycle checks, worker enrollment and authentic console images.

The current candidate is `0.3.1~community.20261026.2`. It changes implementation and packaging files and adds a regression test. Preview 2 passed 376 unit tests in 22.351 seconds. Package and VM validation and a fresh real demonstration remain pending at documentation freeze. Historical RC7 results do not certify this build.

A dependency source and notice supplement has been collected. [Source availability](../licensing/third-party/SOURCE-AVAILABILITY.md) and [third-party review](THIRD-PARTY-RELEASE-REVIEW.md) record reviewed permissive terms, the technically accepted unchanged Certifi source route, R2 supplements and actual source-delivery conditions.

## Useful evaluation questions

- Can a tester follow the correct build's installation and connection instructions?
- Can a reviewer trace a request to the selected node, persistent console and visible result?
- Are account consent, node identity, desktop coordination and recovery behavior clear?
- What would improve the documentation and maintainability of this workflow?

Original FrameBridge material uses GPL-3.0-only within its stated scope. The project is independent. This brief remains private, has not been sent to OpenAI and claims no affiliation or endorsement.
