# Self-edges/loops look very awkward in state diagrams

### Description

In state diagrams where a state connects to itself, it looks, well... ugly. Like the edge was about to point to a different node, but then realized it was heading in the wrong direction and doubled-back on itself.

### Steps to reproduce

```
stateDiagram-v2
    [*] --> Node
    Node --> Node: Self Edge
```

Or, in the [live editor](https://mermaid.live/edit#pako:eNpFjrsOwjAMRX8l8oiahTEDE6ws3SAdrMZpK-VRpQ4SqvrvuEIUT_ceH1leoc-OwMDCyHSdcCgY9etsk5J5njql9UXdxfmSPR3IqJaCVzc3yBYaiFQiTk6urbttgUeKZMFIdOSxBrZg0yYqVs7tO_VguFRqoOQ6jGA8hkVand3_m4POmB45__r2AX2RPP8).

### Screenshots

> **Visual material 1:** `/testbed/assets/asset_01.png`

### Code Sample

```text

```

### Setup

- Mermaid version: v11.4.1
- Browser and Version: Chrome Version 132.0.6834.208 (Official Build) (64-bit)

### Suggested Solutions

Compare that to this graphviz digraph:

```
digraph G {
  start -> n;

  n [label= "Node"];
  n -> n [label = "Self Edge"];
}
```

> **Visual material 2:** `/testbed/assets/asset_02_reference.png`

So much more natural. The salient differences are the fact that the edge is to the _side_ of the node, and that it doesn't have the unfortunate kink in it.

### Additional Context

Issue #1443 may be related. It's complaining about rendering of self-edges, but it's less clear to me what the specific complaint is.
