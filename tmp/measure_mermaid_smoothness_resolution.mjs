import { chromium } from 'playwright';

const fixtures = {
  gold: 'M80.092,208L76.827,212C73.561,216,67.031,224,77.099,228C87.167,232,113.833,232,123.901,228C133.969,224,127.439,216,124.173,212L120.908,208',
  smooth_independent: 'M100,0 C165,0 165,100 100,100',
  pointed_v: 'M100,0 L165,50 L100,100',
  sharp_double_back: 'M100,0 C150,0 150,40 130,50 L165,50 L100,100',
};

const browser = await chromium.launch({ executablePath: '/usr/bin/chromium', headless: true });
const page = await browser.newPage();

for (const count of [80, 160, 320]) {
  for (const [name, d] of Object.entries(fixtures)) {
    await page.setContent(`<svg xmlns="http://www.w3.org/2000/svg"><path id="p" d="${d}"/></svg>`);
    const metrics = await page.$eval(
      '#p',
      (path, sampleCount) => {
        const length = path.getTotalLength();
        const points = Array.from({ length: sampleCount + 1 }, (_, index) => {
          const point = path.getPointAtLength((length * index) / sampleCount);
          return { x: point.x, y: point.y };
        });
        const turns = [];
        for (let index = 1; index < points.length - 1; index += 1) {
          const before = {
            x: points[index].x - points[index - 1].x,
            y: points[index].y - points[index - 1].y,
          };
          const after = {
            x: points[index + 1].x - points[index].x,
            y: points[index + 1].y - points[index].y,
          };
          const dot = before.x * after.x + before.y * after.y;
          const magnitude = Math.hypot(before.x, before.y) * Math.hypot(after.x, after.y);
          turns.push((Math.acos(Math.max(-1, Math.min(1, dot / magnitude))) * 180) / Math.PI);
        }
        return {
          arc_step: length / sampleCount,
          maximum_local_turn_degrees: Math.max(...turns),
        };
      },
      count
    );
    console.log(JSON.stringify({ count, name, ...metrics }));
  }
}

await browser.close();
