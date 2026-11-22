export interface OrbitChartTheme {
  series: string[];
  text: string;
  axis: string;
  grid: string;
  slider: string;
  selection: string;
  tooltipBackground: string;
  tooltipBorder: string;
  tooltipText: string;
  warning: string;
  danger: string;
}

const fallback: OrbitChartTheme = {
  series: [
    "#58c2ae",
    "#7f9de8",
    "#a68bdd",
    "#c79a55",
    "#6faaa1",
    "#8c99a5",
    "#c07777",
    "#8cb77f",
  ],
  text: "#788682",
  axis: "#33413e",
  grid: "#1d2826",
  slider: "#0d1211",
  selection: "#173b35",
  tooltipBackground: "#121817",
  tooltipBorder: "#2b3936",
  tooltipText: "#edf4f2",
  warning: "#d5a34c",
  danger: "#df6868",
};

function token(style: CSSStyleDeclaration, name: string, fallbackValue: string): string {
  return style.getPropertyValue(name).trim() || fallbackValue;
}

/**
 * ECharts renders into canvas, so CSS custom properties cannot be passed to
 * its options reliably. Resolve the current Orbit tokens whenever the theme
 * changes and give the canvas concrete, contrast-safe colours instead.
 */
export function readOrbitChartTheme(): OrbitChartTheme {
  if (typeof document === "undefined" || typeof getComputedStyle !== "function") {
    return fallback;
  }

  const style = getComputedStyle(document.documentElement);
  return {
    series: fallback.series.map((value, index) =>
      token(style, `--rv-chart-series-${index + 1}`, value)
    ),
    text: token(style, "--rv-chart-text", fallback.text),
    axis: token(style, "--rv-chart-axis", fallback.axis),
    grid: token(style, "--rv-chart-grid", fallback.grid),
    slider: token(style, "--rv-chart-slider", fallback.slider),
    selection: token(style, "--rv-chart-selection", fallback.selection),
    tooltipBackground: token(style, "--rv-chart-tooltip-bg", fallback.tooltipBackground),
    tooltipBorder: token(style, "--rv-chart-tooltip-border", fallback.tooltipBorder),
    tooltipText: token(style, "--rv-chart-tooltip-text", fallback.tooltipText),
    warning: token(style, "--rv-amber", fallback.warning),
    danger: token(style, "--rv-red", fallback.danger),
  };
}

export function orbitChartAxis(theme: OrbitChartTheme) {
  return {
    axisLine: { lineStyle: { color: theme.axis } },
    axisLabel: { color: theme.text },
    splitLine: { lineStyle: { color: theme.grid } },
  };
}

export function orbitChartTooltip(theme: OrbitChartTheme) {
  return {
    trigger: "axis",
    backgroundColor: theme.tooltipBackground,
    borderColor: theme.tooltipBorder,
    textStyle: { color: theme.tooltipText },
  };
}
