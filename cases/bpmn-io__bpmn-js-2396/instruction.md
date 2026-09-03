# Related issue 1

## Weird layout of sequence flows starting from boundary events on activity corners due to additional invalid waypoint

### Describe the Bug

When adding a sequence flow starting from a boundary event on an activity corner to an element below, the sequence flow gets aligned weirdly because an additional waypoint is added inside of the element:

> **Visual material 1:** `/testbed/assets/asset_01_44ed2f05e1eb.png`

### Steps to Reproduce

1. Add a boundary event to the corner of an activity
2. Add an element below the activity
3. Add a straight sequence flow from the boundary event to the element below

> **Visual material 2:** `/testbed/assets/asset_02_ba179e3c63f8.gif`

### Expected Behavior

The sequence flow should connect in a straight line between the boundary event and the element below without the additional waypoint.

### Environment

- https://demo.bpmn.io/
- Browser: Chrome 131
- OS: Windows 11

---

# Related issue 2

## Incorrect direction of the arrow from the boundary event to the subsequent element.

### Describe the Bug

Incorrect direction of the arrow from the boundary event to the subsequent element.

### Steps to Reproduce

1. Open any business process (BP).
2. Place a task element on the diagram and add a boundary event to the task.
3. Add another task element at the same level as the boundary event.
4. Click on the boundary event, then in the pop-up menu, select the arrow tool and drag it to the task element.

> **Visual material 3:** `/testbed/assets/asset_03_a6b818fa6019.png`

You can check it here: https://demo.bpmn.io/s/start

### Expected Behavior

> **Visual material 4:** `/testbed/assets/asset_04_1a6bc712a5d1.png`

### Environment

- Browser: Chrome 134.0.6998.166
- OS: MacOS Sequoia 15.3.2
- Library version: 18.4.0
