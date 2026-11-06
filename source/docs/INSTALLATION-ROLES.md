# FrameBridge Original Expanded 0.3.7 · installation roles

APT installs one package name, `framebridge-original-expanded`. Use the build
that matches the operating system and Python ABI. The central and nodes on
the same ABI install the same artifact.

## Fresh installation

During dpkg configuration the package selects one IPv4 LAN interface. It
uses Avahi DNS-SD on the local link to look for a central. A resolved central
selects the node role. When the browse completes without a central, a new
central must observe its own provisional announcement and the final
announcement before saving the central role. A competing claim,
ambiguous interface, or failed discovery leaves the role pending. Browse
results describe what was observed on that link. Network partitions and
multicast filtering can hide a central.

`fb installation status` reads the saved decision. After checking the LAN,
an administrator can retry a pending installation with
`sudo fb installation detect`. This can announce a new central. Authenticate
and enroll devices through the explicit enrollment procedure.

A central from an earlier version might not announce DNS-SD. Upgrade that
central first, or have the LAN administrator provision a root-owned regular
file `/etc/framebridge-expanded/lan-central` on new nodes. Its only line
contains the central's fully qualified name in local DNS. This is a
placement reservation for the default broker port 20878. It keeps fresh
nodes from promoting themselves while the reserved central is offline.
DNS-SD names are hints. The broker still needs verified HTTPS and the
central operator must approve each enrollment.

Run `fb setup` as the account that will use FrameBridge. The installed role
sets the default for a new profile. A node then uses `fb nodes enroll` and
`fb nodes poll` with approval at the central before managed startup.
Configure the central's existing TLS and broker settings in `fb setup`
before enrolling nodes. ChatGPT account authorization remains separate.

## Updates and rollback

Package transitions retain saved profiles, TLS material, credentials, and
startup policy. The package snapshots previously running profiles. On an
upgrade from 0.3.6, a captured central announces its broker port and a
captured node keeps node mode. An inactive or mixed legacy installation keeps
its existing profiles. To capture an inactive profile, run the intended
profile before an authorized reinstall.

Debian orders 0.3.7 below 0.4.0~rc1 through 0.4.0~rc3. Moving from those release
candidates to 0.3.7 is an explicit package downgrade. The 0.3.7 hook snapshots
those profiles as a compatible transition. Back up the active profile and
retain the matching previous DEB before installing. A rollback to 0.3.6
withdraws the central announcement. The earlier supervision journal is user
data in its existing profile.

## Routing response

In Fleet READ, FBEXEC and the shared authenticated transport, `target`,
`node` and `resolved_target` name the same resolved equipment. The input is
preserved in `requested_target`. Local dispatch remains identifiable as
`transport=local` and `transport_target=LOCAL`. The alias and UUID in the
routing envelope come from the local profile or confirmed Fleet registry.
