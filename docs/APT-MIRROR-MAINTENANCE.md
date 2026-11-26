# Stable APT mirror maintenance

The current Linux release is distributed by the permanent signed stable/main channel. Use the [official download and cold-maintenance guide](https://framebridge.betancourt.one/download/).

The stable mirror checks signed metadata daily, verifies the pinned archive key and every index, reuses an existing immutable package only when its size and SHA-256 match the signed metadata, and downloads a package from the origin only if it is missing. A mismatched cached package fails closed. APT validation uses the local mirror snapshot; the check does not make another artifact request to the public origin. The mirror never signs releases.

Historical suites and the separate fbterm15 snapshot remain available without automatic migrations or redirects. Their refresh workflows are manual-only. Previously signed metadata may expire; obtain fresh signed metadata from the origin or migrate using the documented cold-maintenance process. Never disable APT signature or validity checks.

The mirror importer declares its maintenance purpose using a request header. This is a self-declaration, not authenticated visitor identity. Origin statistics must distinguish mirror maintenance, other declared tests and unknown origin; HTTP transfers are not installations or unique users.
