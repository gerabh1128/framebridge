# OpenAI tunnel-client and its incorporated Go modules

The retained executable source/sources/original-geranas/FrameBridge-MCP-Private/bin/tunnel-client is third-party OpenAI Secure MCP Tunnel software, licensed Apache-2.0. It is not original FrameBridge code. Its SHA-256 is 472eb9dd9dd625b4e6023c3b4a5736b3a2e5a1b6dbe9338e001887a64ec992a6.

The Go build information identifies github.com/openai/tunnel-client/cmd/client, Go 1.27.0, module version (devel), and 47 dependency modules. It does not embed an upstream VCS revision. No claim is made that its exact own-source commit or independent reproducible build has been established.

## Retained legal material

- OPENAI-LICENSE and OPENAI-NOTICE were retrieved from the official OpenAI repository at commit cc81b261aa51059f6ef464ca236ce8d0c2347382. This pins the notice evidence; it is not asserted to be the executable's build commit.
- GO-LICENSE and GO-PATENTS are the Go 1.27.0 toolchain terms.
- licenses/ contains 61 legal files from the 47 exact incorporated module versions.
- GO-BUILDINFO.json records the embedded public dependency identities.
- GO-MODULE-SOURCE-NOTICES.json records the source archive hashes, embedded h1 checksums, exact legal-file paths and hashes.
- UPSTREAM-NOTICE-PROVENANCE.json records the notice source URLs and hashes.

Every module archive was downloaded from the publisher-backed Go module proxy and its canonical h1 zip checksum was verified against the binary's embedded build information. Full license and notice files are preserved. Detected terms fall within the retained MIT, BSD and Apache-style set; GO-LICENSE-MARKERS.json is an aid to review, not a substitute for the full legal texts or a universal legal opinion.

## Source supplement

FrameBridge-0.3.1-tunnel-client-dependency-sources.tar.gz contains all 47 exact Go module source archives and the retained legal/provenance material. Size: 26,941,745 bytes. SHA-256: 6445d4f0e773aba833fe5c610fbae54102b92de2ce636438f769f21e890c2bd9.

This supplement supplies dependency sources, not a claim that the tunnel executable's own original source revision is known. The upstream project is https://github.com/openai/tunnel-client. Apache-2.0 remains the component's license; FrameBridge's GPL notice does not replace it.

The root TUNNEL-DEPENDENCY-SOURCES.json and overall release checksum manifest bind the supplement to the retained executable identity. Ship these notices in both the binary package's documentation and the matching FrameBridge source, and make the supplement downloadable beside the release.
