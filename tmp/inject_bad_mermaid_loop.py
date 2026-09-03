from pathlib import Path


path = Path("packages/mermaid/src/rendering-util/layout-algorithms/dagre/benchmark-self-loop.spec.js")
source = path.read_text()
needle = "const paths = [...document.querySelectorAll('.edgePaths path.flowchart-link')];"
bad_path = (
    "M80,58 L80,400 L500,400 L500,20 L100,33 "
    "C90,20 75,30 80,58"
)
source = source.replace(
    needle,
    f"{needle}\n    paths[0]?.setAttribute('d', '{bad_path}');",
    1,
)
path.write_text(source)
