# Licensing scope

## Original FrameBridge material: GNU GPL version 3 only

The owner selected GNU General Public License version 3 only, SPDX identifier GPL-3.0-only. The full license is [LICENSE](LICENSE). Future GPL versions are not selected automatically.

The grant covers original FrameBridge code and project documentation that the rightsholders are entitled to license. Recipients may use, study, modify and redistribute that material under the GPL. Modified private copies and lawful independent distributions do not require submitting a pull request to this project. Original blog article text explicitly marked for that purpose uses CC-BY-SA-4.0 under [blog/LICENSING.md](blog/LICENSING.md); media and separately licensed components retain their own terms.

[licensing/SOURCE-LICENSE-SCOPE.json](licensing/SOURCE-LICENSE-SCOPE.json) records historical classification and provenance; the matching release SOURCE-ARCHIVE-MANIFEST.json identifies the complete current source set. File location is not proof of ownership. Preserve [COPYRIGHT.md](COPYRIGHT.md) and applicable notices.

## Third-party material

Vendored Python packages, incorporated native libraries, Go modules and the OpenAI tunnel-client executable retain their own licenses. The FrameBridge GPL notice does not relabel MIT, BSD, Apache, MPL, PSF, Unicode or other material.

The Python/Rust native terms and source arrangement are recorded in [docs/THIRD-PARTY-RELEASE-REVIEW.md](docs/THIRD-PARTY-RELEASE-REVIEW.md) and [licensing/third-party/NATIVE-DEPENDENCY-TERMS.md](licensing/third-party/NATIVE-DEPENDENCY-TERMS.md). The separate OpenAI executable is Apache-2.0; its retained notices, exact Go module versions and checksum-verified source archives are described in [licensing/third-party/tunnel-client/README.md](licensing/third-party/tunnel-client/README.md).

The tunnel executable identifies its own module as a development build without an embedded VCS revision. Its precise upstream source commit has not been established. The 47 incorporated Go module versions and h1 checksums were recovered and verified against publisher archives; that does not establish the executable's original build provenance. The current pinned upstream LICENSE/NOTICE are retained with this limitation stated explicitly.

## Source delivery

Distribute the matching FrameBridge 0.3.4 source archive beside each of its three distribution-profile installers, the immutable shared Python/Rust dependency-source asset, the new tunnel-client dependency-source supplement and retained notices. [Source availability](licensing/third-party/SOURCE-AVAILABILITY.md) identifies the files and hashes.

The unchanged Certifi source arrangement retains its exact upstream source assets, original notice, full MPL-2.0 text and recipient access instructions. The historical Mozilla certdata revision remains unidentified. No independent native-wheel or tunnel-client rebuild is claimed. These are provenance and reproducibility limits, not claims that unavailable proof has been obtained.

## Official releases and freedom to modify

[OFFICIAL-RELEASES.md](OFFICIAL-RELEASES.md) distinguishes maintainer-reviewed, signed releases from independent variants. Signatures and hashes authenticate official artifacts; they do not encrypt the program or prohibit modification. Installation privileges and ordinary file ownership protect the installed copy against unprivileged accidental edits. An authorized owner can still modify their own installation.

The official Git accepts proposed changes for review. Maintainers decide what enters official branches, release tags and signed packages. This workflow does not add a license condition requiring all forks or private changes to return upstream.

This is a scoped technical licensing record, not a universal legal or security certification.
