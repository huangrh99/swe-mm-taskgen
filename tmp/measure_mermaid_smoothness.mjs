import { chromium } from 'playwright';

const paths = {
  gold: 'M80.092,208L76.827,212C73.561,216,67.031,224,77.099,228C87.167,232,113.833,232,123.901,228C133.969,224,127.439,216,124.173,212L120.908,208',
  smooth_independent: 'M100,0 C165,0 165,100 100,100',
  pointed_v: 'M100,0 L165,50 L100,100',
  sharp_double_back: 'M100,0 C150,0 150,40 130,50 L165,50 L100,100',
};

const baseSegments = [
  'M103,208L95.5,216.333C88,224.667,73,241.333,65.5,258C58,274.667,58,291.333,58,299.667L58,308',
  'M58,358L58,366.333C58,374.667,58,391.333,65.5,408C73,424.667,88,441.333,95.5,449.667L103,458',
  'M148,458L155.5,449.667C163,441.333,178,424.667,185.5,403.833C193,383,193,358,193,333C193,308,193,283,185.5,262.167C178,241.333,163,224.667,155.5,216.333L148,208',
];

const browser = await chromium.launch({ executablePath: '/usr/bin/chromium', headless: true });
const page = await browser.newPage();

for (const [name, d] of Object.entries(paths)) {
  await page.setContent(`<svg xmlns="http://www.w3.org/2000/svg"><path id="p" d="${d}"/></svg>`);
  const metrics = await page.$eval('#p', (path) => {
    const count = 80;
    const length = path.getTotalLength();
    const points = Array.from({ length: count + 1 }, (_, index) => {
      const point = path.getPointAtLength((length * index) / count);
      return { x: point.x, y: point.y };
    });
    const turns = [];
    for (let index = 2; index < points.length - 2; index += 1) {
      const before = {
        x: points[index].x - points[index - 2].x,
        y: points[index].y - points[index - 2].y,
      };
      const after = {
        x: points[index + 2].x - points[index].x,
        y: points[index + 2].y - points[index].y,
      };
      const dot = before.x * after.x + before.y * after.y;
      const magnitude = Math.hypot(before.x, before.y) * Math.hypot(after.x, after.y);
      turns.push((Math.acos(Math.max(-1, Math.min(1, dot / magnitude))) * 180) / Math.PI);
    }
    turns.sort((a, b) => b - a);
    return {
      length,
      maximum_local_turn_degrees: turns[0],
      second_local_turn_degrees: turns[1],
      p95_local_turn_degrees: turns[Math.floor(turns.length * 0.05)],
    };
  });
  console.log(JSON.stringify({ name, ...metrics }));
}

await page.setContent(
  `<svg xmlns="http://www.w3.org/2000/svg">${baseSegments
    .map((d, index) => `<path id="base-${index}" d="${d}"/>`)
    .join('')}</svg>`
);
const baseJoinMetrics = await page.$$eval('path', (segments) => {
  const unit = (vector) => {
    const length = Math.hypot(vector.x, vector.y);
    return { x: vector.x / length, y: vector.y / length };
  };
  const angle = (a, b) =>
    (Math.acos(Math.max(-1, Math.min(1, a.x * b.x + a.y * b.y))) * 180) / Math.PI;
  return segments.slice(0, -1).map((segment, index) => {
    const next = segments[index + 1];
    const length = segment.getTotalLength();
    const nextLength = next.getTotalLength();
    const before = segment.getPointAtLength(length * 0.98);
    const end = segment.getPointAtLength(length);
    const start = next.getPointAtLength(0);
    const after = next.getPointAtLength(nextLength * 0.02);
    return {
      join: `${index}->${index + 1}`,
      gap: Math.hypot(start.x - end.x, start.y - end.y),
      turn_degrees: angle(
        unit({ x: end.x - before.x, y: end.y - before.y }),
        unit({ x: after.x - start.x, y: after.y - start.y })
      ),
    };
  });
});
console.log(JSON.stringify({ name: 'base_segment_joins', joins: baseJoinMetrics }));

await browser.close();
