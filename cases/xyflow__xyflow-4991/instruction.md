# Programmatic focusing of a node breaks the viewport

The attached Issue recording is available at:
`/testbed/assets/issue-4667-reproduction.mp4`.

At high zoom, using `Tab` or `element.focus()` to focus a node outside the
visible area disrupts the normal viewport behavior.

## Steps to reproduce

1. Open a React Flow graph.
2. Zoom in until some nodes are outside the visible area.
3. Focus a visible node and press `Tab` until an off-screen node receives
   focus.

## Expected behavior

Keyboard or programmatic focus must not cause the browser's native scrolling
to shift the React Flow viewport. Existing consumer scroll callbacks must
continue to work.
