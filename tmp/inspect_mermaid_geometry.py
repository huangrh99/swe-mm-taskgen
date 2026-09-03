import os
from pathlib import Path


path = Path("packages/mermaid/src/rendering-util/layout-algorithms/dagre/benchmark-self-loop.spec.js")
source = path.read_text()
geometry = (
    'console.log("GEOMETRY_START", JSON.stringify({' 
    'paths: paths.map((path) => path.getAttribute("d")), '
    'nodes: [...document.querySelectorAll(".node")].map((node) => ({'
    'id: node.id, transform: node.getAttribute("transform"), '
    'shape: node.querySelector("rect,polygon,circle,ellipse")?.outerHTML}))}));'
)

if os.environ.get("MODE") == "LR":
    source = source.replace("flowchart TD\\nC -->|retry| C", "flowchart LR\\nC -->|retry| C")
    source = source.replace(
        "expect(paths.length).toBe(1);",
        f"{geometry}\n    expect(paths.length).toBe(1);",
        1,
    )
else:
    needle = "const paths = [...document.querySelectorAll('.edgePaths path')];"
    source = source.replace(needle, f"{needle}\n    {geometry}", 1)

path.write_text(source)
