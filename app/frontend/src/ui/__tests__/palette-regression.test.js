import { expect, test } from "bun:test";
import postcss from "postcss";
import { readFileSync } from "node:fs";

const appCss = readFileSync(new URL("../../app.css", import.meta.url), "utf8");
const bootCss = readFileSync(new URL("../../styles/boot.css", import.meta.url), "utf8");
const gateCss = readFileSync(new URL("../../styles/gate.css", import.meta.url), "utf8");
const workspaceCss = readFileSync(new URL("../../styles/workspaces.css", import.meta.url), "utf8");
const chatSource = readFileSync(new URL("../../chat.jsx", import.meta.url), "utf8");
const sourceLab = readFileSync(new URL("../../sourceLab.jsx", import.meta.url), "utf8");


function declarations(css, selector) {
  const result = {};
  postcss.parse(css).walkRules(rule => {
    if (rule.parent.type !== "root" || !rule.selectors?.map(item => item.trim()).includes(selector)) return;
    for (const node of rule.nodes) if (node.type === "decl") result[node.prop] = node.value;
  });
  return result;
}

function paletteFrom(css, theme) {
  const selectors = [
    ":root[data-dir=\"" + theme + "\"]",
    "[data-dir=\"" + theme + "\"]",
    ".oi-product[data-dir=\"" + theme + "\"]",
  ];
  let found;
  postcss.parse(css).walkRules(rule => {
    const actual = rule.selectors?.map(selector => selector.trim()) ?? [];
    if (selectors.every(selector => actual.includes(selector))) found = rule;
  });
  if (!found) return undefined;
  return Object.fromEntries(found.nodes
    .filter(node => node.type === "decl")
    .map(node => [node.prop, node.value]));
}

function luminance(hex) {
  const match = hex.match(/^#([0-9a-f]{6})$/i);
  if (!match) throw new Error("Expected an opaque six digit color, got " + hex);
  const rgb = [0, 2, 4].map(index => parseInt(match[1].slice(index, index + 2), 16) / 255);
  const linear = rgb.map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
  return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
}

function contrast(foreground, background) {
  const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
  return (values[0] + 0.05) / (values[1] + 0.05);
}

function composite(foreground, background, alpha) {
  const channels = hex => [0, 2, 4].map(index => parseInt(hex.slice(index + 1, index + 3), 16));
  const fg = channels(foreground);
  const bg = channels(background);
  return "#" + fg.map((value, index) => Math.round(value * alpha + bg[index] * (1 - alpha)).toString(16).padStart(2, "0")).join("");
}

const expected = {
  midnight: {
    "--canvas": "#151214", "--surface": "#1D191C", "--ink": "#F5F1F3",
    "--muted": "#B6A8AF", "--accent": "#B52B3F", "--accent-text": "#F08A99",
    "--accent-ink": "#FFFFFF", "--accent-soft": "#3B2027",
    "--down": "#E41424", "--down-text": "#FF8D96", "--neg": "var(--down)",
    "--up": "#6DCB9C", "--steady": "#B6A8AF",
  },
  daylight: {
    "--canvas": "#FAF8F9", "--surface": "#FFFFFF", "--ink": "#261E23",
    "--muted": "#6E5D66", "--accent": "#9D263B", "--accent-text": "#9D263B",
    "--accent-ink": "#FFFFFF", "--accent-soft": "#F8E8ED",
    "--down": "#E41424", "--down-text": "#A60815", "--neg": "var(--down)",
    "--up": "#176848", "--steady": "#6E5D66",
  },
};

test("critical CSS gives the first paint neutral surfaces before the full theme loads", () => {
  const firstPaint = declarations(bootCss, ":root");
  expect(firstPaint["--color-ink-canvas"]).toBe("#151214");
  expect(firstPaint["--color-ink-panel"]).toBe("#1D191C");
  expect(firstPaint["--color-daylight-paper"]).toBe("#FAF8F9");
  expect(firstPaint["--color-daylight-ink"]).toBe("#261E23");
  expect(firstPaint["--color-white"]).toBe("#FFFFFF");
  expect(bootCss).not.toContain(':root[data-dir="midnight"]');
  expect(bootCss).not.toContain(':root[data-dir="daylight"]');
  expect(declarations(bootCss, ".app .instrument-shell")["border-top"]).toBe("1px solid var(--accent)");
});

test("deferred palette carries the approved semantic roles", () => {
  for (const theme of Object.keys(expected)) {
    const deferred = paletteFrom(appCss, theme);
    expect(deferred).toBeDefined();
    for (const [key, value] of Object.entries(expected[theme])) expect(deferred[key]).toBe(value);
    expect(deferred["--down-soft"]).toBe("color-mix(in srgb, var(--down) 12%, transparent)");
    expect(deferred["--up-soft"]).toBe("color-mix(in srgb, var(--up) 12%, transparent)");
  }
});

test("gate foreground roles use accessible text and brand colors in both themes", () => {
  expect(declarations(gateCss, ".gate-v4").background).toBe("var(--canvas)");
  expect(declarations(gateCss, ".gate-v4").color).toBe("var(--ink)");
  expect(declarations(gateCss, ".gate-v4-access").background).toBe("var(--surface)");
  /* Restated, design audit 2 October 2026: the mast Internal tag was removed
     as a false affordance, so the lockup name carries the ink check. Was:
     .gate-v4-mast p:last-child strong color var(--ink). */
  expect(declarations(gateCss, ".gate-v4-mast p:first-child span").color).toBe("var(--ink)");
  /* Restated, design audit 2 October 2026: the dt is now the bold ink title,
     not a red numbered folio. Was: .gate-v4-system dt color var(--accent-text). */
  expect(declarations(gateCss, ".gate-v4-system dt").color).toBe("var(--ink)");
  expect(declarations(gateCss, ".gate-v4-submit:hover").color).toBe("var(--accent-ink)");
});

test("main, muted, and accent text plus filled actions clear 4.5 to 1 on their planes", () => {
  for (const theme of Object.keys(expected)) {
    const palette = paletteFrom(appCss, theme);
    for (const background of [palette["--canvas"], palette["--surface"], palette["--accent-soft"]]) {
      for (const foreground of [palette["--ink"], palette["--muted"], palette["--accent-text"]]) {
        expect(contrast(foreground, background)).toBeGreaterThanOrEqual(4.5);
      }
    }
    expect(contrast(palette["--accent-ink"], palette["--accent"])).toBeGreaterThanOrEqual(4.5);
  }
  expect(contrast(expected.daylight["--muted"], "#E8DED2")).toBeGreaterThanOrEqual(4.5);
});

test("error roles stay distinct and readable in actual text consumers", () => {
  const palette = paletteFrom(appCss, "midnight");
  const light = paletteFrom(appCss, "daylight");
  for (const theme of [palette, light]) {
    const errorText = theme["--down-text"];
    for (const background of [theme["--canvas"], theme["--surface"], theme["--accent-soft"]]) {
      expect(contrast(errorText, background)).toBeGreaterThanOrEqual(4.5);
    }
    expect(theme["--neg"]).toBe("var(--down)");
    expect(theme["--down"]).not.toBe(theme["--accent"]);
  }
  for (const selector of [".gate-err", ".refine-err", ".intel-down .id-msg", ".ct-err"]) {
    expect(declarations(appCss, selector).color).toBe("var(--down-text)");
  }
  expect(chatSource).toContain("color: 'var(--down-text)'");
});

test("status text clears 4.5 to 1 over composited status surfaces", () => {
  for (const theme of Object.keys(expected)) {
    const palette = paletteFrom(appCss, theme);
    for (const background of [palette["--canvas"], palette["--surface"], palette["--accent-soft"]]) {
      const downSoft = composite(palette["--down"], background, 0.12);
      const upSoft = composite(palette["--up"], background, 0.12);
      expect(contrast(palette["--down-text"], downSoft)).toBeGreaterThanOrEqual(4.5);
      expect(contrast(palette["--up"], upSoft)).toBeGreaterThanOrEqual(4.5);
      expect(contrast(palette["--steady"], background)).toBeGreaterThanOrEqual(4.5);
    }
  }
});

test("faded text states keep visible state cues without alpha", () => {
  const ghost = declarations(appCss, ".brd-card.ghost");
  const rejected = declarations(appCss, ".bscan-row-rejected");
  const unconfigured = declarations(workspaceCss, ".workspace-budget .workspace-row[data-lane-state=\"not_configured\"]");
  expect(ghost.opacity).toBeUndefined();
  expect(ghost.border).toBe("1px solid var(--muted)");
  expect(rejected.opacity).toBeUndefined();
  expect(rejected["border-left"]).toBe("2px solid var(--down)");
  expect(unconfigured.opacity).toBeUndefined();
  expect(sourceLab).toContain("<dt>Attribution</dt>");
  expect(sourceLab).toContain("<dt>Kill state</dt>");
});

test("focus rings use readable accent text and respect forced colors", () => {
  expect(appCss).toContain(".app :focus-visible {\n  outline-color: var(--accent-text) !important;");
  expect(appCss).toContain("@media (forced-colors: active) {\n  .app :focus-visible {\n    outline-color: Highlight !important;");
});

test("first paint keeps the thin brand rule and disabled copy opaque", () => {
  for (const css of [bootCss, appCss]) {
    const shell = declarations(css, ".app .instrument-shell");
    expect(shell["border-top"]).toBe("1px solid var(--accent)");
  }
  const disabled = declarations(appCss, ".oi-product .research-generate-btn:disabled");
  expect(disabled.opacity).toBe("1");
  expect(disabled.important).toBeFalsy();
});
