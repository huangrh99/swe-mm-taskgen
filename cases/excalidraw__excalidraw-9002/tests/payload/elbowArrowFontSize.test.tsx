import React from "react";
import { Excalidraw } from "../index";
import { redrawTextBoundingBox } from "../element";
import { actionDecreaseFontSize } from "../actions/actionProperties";
import { LinearElementEditor } from "../element/linearElementEditor";
import { mutateElbowArrow } from "../element/routing";
import type {
  ExcalidrawElbowArrowElement,
  ExcalidrawTextElement,
} from "../element/types";
import Scene from "../scene/Scene";
import { pointFrom } from "../../math";
import { API } from "./helpers/api";
import { render } from "./test-utils";

const { h } = window;

describe("font size and elbow arrow binding", () => {
  it(
    "[excalidraw__excalidraw-9002::constraint_001] reroutes an elbow arrow when bound text shrinks",
    async () => {
      localStorage.clear();

      const text = API.createElement({
        type: "text",
        id: "font-size-text",
        x: 100,
        y: 100,
        width: 180,
        height: 100,
        text: "Elbow arrow label\nneeds a fresh route",
        fontSize: 40,
        textAlign: "left",
        verticalAlign: "top",
        boundElements: [{ id: "font-size-arrow", type: "arrow" }],
      });
      const rectangle = API.createElement({
        type: "rectangle",
        id: "font-size-target",
        x: 400,
        y: 350,
        width: 100,
        height: 100,
        boundElements: [{ id: "font-size-arrow", type: "arrow" }],
      });
      const arrow = API.createElement({
        type: "arrow",
        id: "font-size-arrow",
        x: 0,
        y: 0,
        width: 500,
        height: 400,
        points: [pointFrom(0, 0), pointFrom(500, 400)],
        startBinding: {
          elementId: text.id,
          focus: 0,
          gap: 5,
          fixedPoint: [0.5001, -0.05],
        },
        endBinding: {
          elementId: rectangle.id,
          focus: 0,
          gap: 5,
          fixedPoint: [-0.05, 0.5001],
        },
        elbowed: true,
      }) as ExcalidrawElbowArrowElement;

      const elements = [text, arrow, rectangle];
      const fixtureScene = new Scene();
      fixtureScene.insertElements(elements);
      redrawTextBoundingBox(
        text,
        null,
        fixtureScene.getNonDeletedElementsMap(),
        false,
      );
      mutateElbowArrow(
        arrow,
        fixtureScene.getNonDeletedElementsMap(),
        arrow.points,
      );

      await render(<Excalidraw initialData={{ elements }} />);

      const beforeText = h.elements.find(
        (element) => element.id === text.id,
      ) as ExcalidrawTextElement;
      const beforeArrow = h.elements.find(
        (element) => element.id === arrow.id,
      ) as ExcalidrawElbowArrowElement;
      const beforeMap = h.scene.getNonDeletedElementsMap();
      const beforePoints = beforeArrow.points.map((_, index) =>
        LinearElementEditor.getPointAtIndexGlobalCoordinates(
          beforeArrow,
          index,
          beforeMap,
        ),
      );
      const beforeFontSize = beforeText.fontSize;
      const beforeHeight = beforeText.height;

      expect(beforeArrow.elbowed).toBe(true);
      expect(beforeArrow.startBinding?.elementId).toBe(beforeText.id);
      expect(beforeArrow.endBinding?.elementId).toBe(rectangle.id);
      expect(beforePoints.length).toBeGreaterThan(2);

      API.setSelectedElements([beforeText]);
      API.executeAction(actionDecreaseFontSize);

      const afterText = h.elements.find(
        (element) => element.id === text.id,
      ) as ExcalidrawTextElement;
      const afterArrow = h.elements.find(
        (element) => element.id === arrow.id,
      ) as ExcalidrawElbowArrowElement;
      const afterMap = h.scene.getNonDeletedElementsMap();
      const afterPoints = afterArrow.points.map((_, index) =>
        LinearElementEditor.getPointAtIndexGlobalCoordinates(
          afterArrow,
          index,
          afterMap,
        ),
      );

      expect(afterText.fontSize).toBeLessThan(beforeFontSize);
      expect(afterText.height).toBeLessThan(beforeHeight);
      expect(afterArrow.elbowed).toBe(true);
      expect(afterArrow.startBinding?.elementId).toBe(afterText.id);
      expect(afterPoints.length).toBeGreaterThan(2);

      const binding = afterArrow.startBinding;
      if (!binding) {
        throw new Error("The elbow arrow lost its text binding");
      }
      const expectedStart = [
        afterText.x + afterText.width * binding.fixedPoint[0],
        afterText.y + afterText.height * binding.fixedPoint[1],
      ];
      const actualStart = afterPoints[0];

      expect(Math.abs(actualStart[0] - expectedStart[0])).toBeLessThan(1);
      expect(Math.abs(actualStart[1] - expectedStart[1])).toBeLessThan(1);
      expect(Math.abs(actualStart[1] - beforePoints[0][1])).toBeGreaterThan(1);

      afterPoints.slice(1).forEach((point, index) => {
        const previous = afterPoints[index];
        const isHorizontal = Math.abs(point[1] - previous[1]) < 0.00001;
        const isVertical = Math.abs(point[0] - previous[0]) < 0.00001;
        expect(isHorizontal || isVertical).toBe(true);
      });
    },
  );

  it(
    "[excalidraw__excalidraw-9002::p2p_text_resize] resizes standalone text itself",
    async () => {
      localStorage.clear();

      const text = API.createElement({
        type: "text",
        id: "standalone-font-size-text",
        x: 100,
        y: 100,
        width: 240,
        height: 100,
        text: "Standalone text keeps resizing",
        fontSize: 40,
        textAlign: "left",
        verticalAlign: "top",
      });

      await render(<Excalidraw initialData={{ elements: [text] }} />);
      const beforeText = h.elements.find(
        (element) => element.id === text.id,
      ) as ExcalidrawTextElement;
      const beforeFontSize = beforeText.fontSize;
      const beforeHeight = beforeText.height;

      API.setSelectedElements([beforeText]);
      API.executeAction(actionDecreaseFontSize);

      const afterText = h.elements.find(
        (element) => element.id === text.id,
      ) as ExcalidrawTextElement;
      expect(afterText.fontSize).toBeLessThan(beforeFontSize);
      expect(afterText.height).toBeLessThan(beforeHeight);
    },
  );
});
