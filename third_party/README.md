# Vendored third-party sources

Git submodules for dependencies linked directly into an xEdge component,
as opposed to a pip/npm package. Each entry's licensing rationale is
recorded in `docs/planning/license-audit.md`, not here.

## bacnet-stack

`github.com/bacnet-stack/bacnet-stack`, pinned to tag `bacnet-stack-1.6.0`.
Provides the BACnet MS/TP datalink and application layers for Sprint P7
(XEDGE-166-171/289) — `bacpypes3` (the library used for BACnet/IP) has no
MS/TP implementation. Linked into a standalone C daemon
(`xedge/drivers/bacnet/mstp_daemon/`, added in a later Sprint P7 PR), never
into the main xEdge Python process. Kept unmodified; see
`docs/planning/license-audit.md` §4 item 11 for the license basis and the
policy for if it ever needs patching.

To update the pin:

```sh
cd third_party/bacnet-stack
git fetch --tags
git checkout <new-tag>
cd ../..
git add third_party/bacnet-stack
```
