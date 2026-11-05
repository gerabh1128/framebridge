# FrameBridge Original Expanded 0.4 · roles and private panel

APT installs one package name, `framebridge-original-expanded`. Use the build
that matches the operating system and Python ABI. The central and nodes on
the same ABI install the same artifact.

## Fresh installation

During dpkg configuration the package selects one IPv4 LAN interface. It
uses Avahi DNS-SD on the local link to look for a central. A resolved central
selects the node role. When the browse completes without a central, a new
central must observe its own provisional announcement and the final
announcement before installation extracts the panel. A competing claim,
ambiguous interface, or failed discovery leaves the role pending. Browse
results describe what was observed on that link. Network partitions and
multicast filtering can hide a central.

`fb installation status` reads the saved decision. After checking the LAN,
an administrator can retry a pending installation with
`sudo fb installation detect`. This can announce a new central and install
the panel. It does not authenticate or enroll any device.

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

APT keeps saved profiles, TLS material, credentials, and startup policy.
The existing package upgrade hook captures previously running profiles.
When upgrading from 0.3.6, a captured central gets the panel and an
announcement using its broker port. A captured node keeps node mode.
An inactive or mixed legacy installation is preserved without automatic
promotion. For that case, run the intended legacy profile before an
authorized reinstall so the package can capture it.

The central announcement can continue while the runtime is offline. The
panel assets are local and its private journal remains in the profile.
Downgrading to 0.3.6 removes the 0.4 advertisement and current generated
panel assets. Preserve the original signed package, a consistent backup of
the existing profile and journal, and the central access route before a
production update. Use the verified 0.3.6 Deployment Kit for rollback.

## Supervision panel

Run `fb panel` in the central profile's local terminal. It listens on
`127.0.0.1:29189` and uses the temporary passphrase entered in that terminal.
Close the browser session and press Ctrl+C when done. The packaged panel
provides no public route. A separate authenticated local-DNS access path
requires an explicit infrastructure change.

Inventory comes from the local authenticated broker when available. To
include infrastructure outside the Linux Fleet, the profile owner can add
a private mode-0600 `panel-inventory.json` containing a JSON list of names.
Those entries appear as unverified until the owner records evidence. The
panel stores incidents, checks, and approvals recorded by the owner in
`panel-bitacora.sqlite3` inside the central profile. It can only read the
local Fleet inventory and write its own journal. The standalone dashboard
journal and historical evidence must be migrated separately from a backup.

## Routing response

In Fleet READ, FBEXEC and the shared authenticated transport, `target`,
`node` and `resolved_target` name the same resolved equipment. The input is
preserved in `requested_target`. Local dispatch remains identifiable as
`transport=local` and `transport_target=LOCAL`. The alias and UUID in the
routing envelope come from the local profile or confirmed Fleet registry.
