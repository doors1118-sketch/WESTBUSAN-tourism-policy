const fmt = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 1 });
const colors = { west: "#176bff", east: "#19b6c9", other: "#9aa8ba" };
let dashboardData = null;
const districtInsightPromises = new Map();
const supplyInsightPromises = new Map();
let activeDistrictInsightId = null;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function value(value, suffix = "") {
  return value == null ? "자료 준비 중" : `${fmt.format(value)}${suffix}`;
}

function relativeToEast(westValue, eastValue) {
  if (westValue == null || eastValue == null || eastValue === 0) return "동부산 대비 자료 준비 중";
  return `동부산 대비 ${value((westValue / eastValue) * 100, "%")}`;
}

function clear(target) {
  while (target.firstChild) target.removeChild(target.firstChild);
}

function kpi(label, main, note, delta, definition) {
  const card = node("article", "card kpi");
  const noteElement = node("small", "", note);
  if (definition) {
    noteElement.classList.add("metric-definition");
    noteElement.textContent = `${note} ⓘ`;
    noteElement.title = definition;
    noteElement.tabIndex = 0;
    noteElement.setAttribute("aria-label", `${note}. ${definition}`);
  }
  card.append(node("span", "label", label), node("strong", "", main), noteElement);
  if (delta) card.append(node("span", "delta", delta));
  return card;
}

function facilitySupplyKpi(west, east) {
  const card = node("article", "card kpi");
  const main = node("strong", "");
  const facilityMix = node("small", "facility-mix");
  west.facilityMix.forEach((item) => {
    facilityMix.append(node("span", "facility-mix-item", `${item.name} ${value(item.facilities, "개소")}`));
  });
  main.append(
    document.createTextNode(value(west.facilities, "개소")),
    document.createTextNode(" "),
    node("span", "secondary-value", `(${value(west.rooms, "실")})`),
  );
  card.append(
    node("span", "label", "서부산 숙박업체"),
    main,
    facilityMix,
    node("span", "delta", relativeToEast(west.facilities, east.facilities)),
  );
  return card;
}

function renderBarGroup(target, regions, key, suffix, ceiling) {
  clear(target);
  regions.forEach((region) => {
    const row = node("div", "bar-row");
    const track = node("div", "track");
    const fill = node("div", "fill");
    fill.style.width = `${Math.min(100, (region[key] / ceiling) * 100)}%`;
    fill.style.background = colors[region.id];
    track.append(fill);
    row.append(node("strong", "", region.name), track, node("span", "bar-value", value(region[key], suffix)));
    target.append(row);
  });
}

function renderSupplyGapSummary(target, west, east) {
  clear(target);
  const metrics = [
    {
      label: "전체 숙박업체",
      west: value(west.facilities, "개소"),
      east: value(east.facilities, "개소"),
      comparison: `서부산은 동부산의 ${value(west.facilities / east.facilities * 100, "%")}`,
      definition: "현재 영업 중인 전체 숙박업체 수",
    },
    {
      label: "객실 100실당 방문수요",
      west: value(west.demandPer100Rooms),
      east: value(east.demandPer100Rooms),
      comparison: `서부산이 동부산의 ${value(west.demandPer100Rooms / east.demandPer100Rooms, "배")}`,
      definition: "외지인+외국인 일평균 방문수요 ÷ 확인 객실 × 100",
    },
    {
      label: "관광숙박 등록",
      west: value(west.tourismFacilityShare, "%"),
      east: value(east.tourismFacilityShare, "%"),
      comparison: `서부산이 ${value(east.tourismFacilityShare - west.tourismFacilityShare, "%p 낮음")}`,
      definition: "전체 숙박시설 중 관광숙박업 등록시설 비율",
    },
    {
      label: "외국인 숙박 대응시설",
      west: value(west.foreignCapableShare, "%"),
      east: value(east.foreignCapableShare, "%"),
      comparison: `서부산이 ${value(east.foreignCapableShare - west.foreignCapableShare, "%p 낮음")}`,
      definition: "관광숙박업·외국인관광 도시민박업 등록시설 비율",
    },
    {
      label: "2021년 이후 숙박업 등록",
      west: value(west.recentLicenseShare, "%"),
      east: value(east.recentLicenseShare, "%"),
      comparison: `서부산이 ${value(east.recentLicenseShare - west.recentLicenseShare, "%p 낮음")}`,
      definition: "현재 영업시설 중 최초 인허가일 2021.1.1 이후 비율",
    },
  ];
  metrics.forEach((metric) => {
    const card = node("article", "supply-gap-stat");
    const values = node("div", "supply-gap-values");
    values.append(
      node("span", "west", `서부산 ${metric.west}`),
      node("span", "east", `동부산 ${metric.east}`),
    );
    card.append(
      node("h3", "", metric.label),
      node("small", "supply-gap-definition", metric.definition),
      values,
      node("p", "", metric.comparison),
    );
    target.append(card);
  });
}

function renderVisitorDemandComparison(target, west, east) {
  clear(target);
  const metrics = [
    { label: "외지인 방문수요", key: "nonlocalVisitorDailyAverage" },
    { label: "외국인 방문수요", key: "foreignVisitorDailyAverage" },
  ];
  metrics.forEach((metric) => {
    const group = node("section", "visitor-demand-group");
    group.append(node("h4", "", metric.label));
    const maximum = Math.max(west[metric.key], east[metric.key]);
    [west, east].forEach((region) => {
      const row = node("div", "visitor-demand-row");
      const track = node("div", "visitor-demand-bar");
      const fill = node("span", region.id);
      fill.style.width = `${(region[metric.key] / maximum) * 100}%`;
      track.append(fill);
      row.append(
        node("strong", "", region.name),
        track,
        node("span", "visitor-demand-value", value(Math.round(region[metric.key]), "명/일")),
      );
      group.append(row);
    });
    target.append(group);
  });

  const foreignDemandRatio = (west.foreignVisitorDailyAverage / east.foreignVisitorDailyAverage) * 100;
  const foreignCapacityRatio = (west.foreignCapableShare / east.foreignCapableShare) * 100;
  const insight = document.querySelector("[data-visitor-demand-insight]");
  insight.textContent = `외국인 방문수요는 동부산의 ${value(foreignDemandRatio, "%")}인 반면, 외국인 숙박 대응시설은 동부산의 ${value(foreignCapacityRatio, "%")} 수준입니다. 서부산은 방문수요 유입에 비해 관광숙박·외국인 대응 공급기반이 약해 신규 공급과 기존시설 전환을 함께 검토할 필요가 있습니다.`;
}

function renderRegistrationTypeComparison(target, registrationTypes) {
  clear(target);
  const regionOrder = [
    { id: "west", name: "서부산" },
    { id: "east", name: "동부산" },
    { id: "other", name: "기타 부산" },
  ];
  const maximum = Math.max(
    1,
    ...registrationTypes.flatMap((type) => regionOrder.map((region) => type.regions[region.id] || 0)),
  );

  registrationTypes.forEach((type) => {
    const group = node("section", "registration-type-row");
    group.append(node("h4", "", type.name));
    const bars = node("div", "registration-type-bars");
    regionOrder.forEach((region) => {
      const count = type.regions[region.id] || 0;
      const row = node("div", "registration-type-series");
      const track = node("div", "registration-type-track");
      const fill = node("span", `registration-type-bar ${region.id}`);
      fill.style.width = count === 0 ? "0" : `${Math.max(1.2, (count / maximum) * 100)}%`;
      track.append(fill);
      row.append(
        node("strong", "", region.name),
        track,
        node("b", "", value(count, "개소")),
      );
      bars.append(row);
    });
    group.append(bars);
    target.append(group);
  });
}

const districtMetricDefinitions = [
  { label: "숙박업체", key: "facilities", suffix: "개소", note: "영업 중 시설 수" },
  { label: "확인 객실", key: "rooms", suffix: "실", note: "객실 수 확인 시설 기준" },
  { label: "객실자료 확보율", key: "roomCoverageShare", suffix: "%", note: "전체 시설 중 객실 수가 확인된 시설 비율" },
  { label: "시설당 객실 중앙값", key: "roomMedian", suffix: "실", note: "시설 규모의 중앙 수준" },
  { label: "일평균 방문수요", key: "visitorDailyAverage", suffix: "명", note: "외지인+외국인 최근 355일 평균" },
  { label: "객실 100실당 방문 압력", key: "demandPer100Rooms", suffix: "", note: "숙박객·점유율이 아닌 공급 검토용 지표" },
  { label: "관광숙박업 등록시설", key: "tourismFacilityShare", suffix: "%", note: "전체 숙박시설 대비 시설 수 비율" },
  { label: "외국인 숙박 대응시설", key: "foreignCapableShare", suffix: "%", note: "관광숙박업·외국인관광 도시민박업 등록" },
  { label: "건축연령 20년 이상", key: "old20Share", suffix: "%", note: "건축물대장 사용승인일부터 산정" },
  { label: "2021년 이후 숙박업 등록", key: "recentLicenseShare", suffix: "%", note: "현재 영업시설의 최초 인허가일 기준" },
  { label: "관광소비 원천지표", key: "consumptionIndex", suffix: "", note: "2026.07 지역 비교용·원화 아님" },
  { label: "3박 방문 원천지표", key: "stay3Index", suffix: "", note: "2026.07 지역 비교용·실제 명수 아님" },
];

const districtChartDefinitions = [
  { label: "숙박업체", key: "facilities", suffix: "개소", cityShare: true },
  { label: "확인 객실", key: "rooms", suffix: "실", cityShare: true },
  { label: "방문수요", key: "visitorDailyAverage", suffix: "명", cityShare: true },
  { label: "수요압력", key: "demandPer100Rooms", suffix: "" },
  { label: "관광숙박", key: "tourismFacilityShare", suffix: "%" },
  { label: "노후시설", key: "old20Share", suffix: "%" },
  { label: "신규 진입", key: "recentLicenseShare", suffix: "%" },
];

function districtCityShare(data, district, metric) {
  if (!metric.cityShare) return null;
  const cityTotal = data.regions.reduce((sum, region) => sum + (Number(region[metric.key]) || 0), 0);
  if (!cityTotal) return null;
  return (district[metric.key] / cityTotal) * 100;
}

function districtChartValue(data, district, metric) {
  const base = value(district[metric.key], metric.suffix);
  const share = districtCityShare(data, district, metric);
  return share == null ? base : `${base} (${value(share, "%")})`;
}

function renderWestDistrictSummary(data, selectedDistrict, onSelect) {
  const target = document.querySelector("[data-west-district-summary]");
  if (!target) return;
  clear(target);
  data.westDistricts.forEach((district) => {
    const card = node("button", `card district-summary-card${district.id === selectedDistrict.id ? " active" : ""}`);
    card.type = "button";
    card.append(
      node("span", "district-summary-rank", `우선순위 ${district.rank}`),
      node("h3", "", district.name),
      node("strong", "", `${districtChartValue(data, district, districtChartDefinitions[0])} · ${districtChartValue(data, district, districtChartDefinitions[1])}`),
      node("p", "", `일평균 ${districtChartValue(data, district, districtChartDefinitions[2])} · 객실100실당 ${value(district.demandPer100Rooms)}`),
      node("small", "", district.priority),
    );
    card.addEventListener("click", () => onSelect(district));
    target.append(card);
  });
}

function renderWestDistrictChart(data, metric, selectedDistrict, onSelect) {
  const target = document.querySelector("[data-west-district-chart]");
  if (!target) return;
  clear(target);
  const benchmarkValue = data.benchmarkDistrict[metric.key];
  const scale = Math.max(benchmarkValue || 0, ...data.westDistricts.map((district) => district[metric.key] || 0), 1);
  const benchmark = node("div", "district-chart-benchmark");
  benchmark.append(
    node("span", "", "해운대구 기준"),
    node("strong", "", districtChartValue(data, data.benchmarkDistrict, metric)),
  );
  target.append(benchmark);
  data.westDistricts.forEach((district) => {
    const row = node("button", `district-chart-bar${district.id === selectedDistrict.id ? " active" : ""}`);
    row.type = "button";
    const track = node("span", "district-chart-track");
    const fill = node("i", "district-chart-fill");
    fill.style.width = `${Math.max(2, (district[metric.key] / scale) * 100)}%`;
    track.append(fill);
    row.append(node("strong", "", district.name), track, node("span", "district-chart-value", districtChartValue(data, district, metric)));
    row.addEventListener("click", () => onSelect(district));
    target.append(row);
  });
}

function districtProfileCard(district, role) {
  const card = node("article", `card district-profile-card ${role}`);
  const heading = node("div", "district-profile-heading");
  heading.append(node("span", "district-role", role === "selected" ? "선택 자치구" : "비교 기준"), node("h3", "", district.name));
  const primary = node("div", "district-profile-primary");
  primary.append(
    node("strong", "", value(district.facilities, "개소")),
    node("span", "", `확인 객실 ${value(district.rooms, "실")}`),
  );
  card.append(
    heading,
    primary,
    node("p", "", `일평균 방문수요 ${value(district.visitorDailyAverage, "명")} · 객실100실당 ${value(district.demandPer100Rooms)}`),
  );
  return card;
}

function renderDistrictDetail(selected, benchmark) {
  const profile = document.querySelector("[data-district-profile]");
  const comparison = document.querySelector("[data-district-comparison]");
  if (!profile || !comparison) return;
  clear(profile);
  clear(comparison);
  profile.append(districtProfileCard(selected, "selected"), node("div", "district-versus", "VS"), districtProfileCard(benchmark, "benchmark"));

  districtMetricDefinitions.forEach((metric) => {
    const selectedValue = selected[metric.key];
    const benchmarkValue = benchmark[metric.key];
    const scale = Math.max(selectedValue || 0, benchmarkValue || 0, 1);
    const row = node("div", "district-comparison-row");
    const label = node("div", "district-metric-label");
    label.append(node("strong", "", metric.label), node("small", "", metric.note));
    const selectedBar = node("div", "district-metric-side selected");
    const selectedTrack = node("div", "district-metric-track");
    const selectedFill = node("i", "district-metric-fill");
    selectedFill.style.width = `${Math.max(2, (selectedValue / scale) * 100)}%`;
    selectedTrack.append(selectedFill);
    selectedBar.append(node("span", "", selected.name), node("strong", "", value(selectedValue, metric.suffix)), selectedTrack);
    const benchmarkBar = node("div", "district-metric-side benchmark");
    const benchmarkTrack = node("div", "district-metric-track");
    const benchmarkFill = node("i", "district-metric-fill");
    benchmarkFill.style.width = `${Math.max(2, (benchmarkValue / scale) * 100)}%`;
    benchmarkTrack.append(benchmarkFill);
    benchmarkBar.append(node("span", "", benchmark.name), node("strong", "", value(benchmarkValue, metric.suffix)), benchmarkTrack);
    row.append(label, selectedBar, benchmarkBar);
    comparison.append(row);
  });

}

function renderDistrictPolicyLoading(selected) {
  const policy = document.querySelector("[data-district-policy]");
  if (!policy) return;
  clear(policy);
  policy.append(
    node("span", "district-policy-label", "AI 정책검토 포인트"),
    node("h3", "", `${selected.name} 분석을 준비하고 있습니다.`),
    node("p", "", "현재 발행본의 수요·공급·노후도·신규 진입 지표를 해운대구와 비교해 해석합니다."),
  );
}

function renderDistrictPolicyInsight(selected, insight) {
  const policy = document.querySelector("[data-district-policy]");
  if (!policy) return;
  clear(policy);
  const meta = node("div", "district-policy-meta");
  meta.append(
    node("span", "district-policy-label", "AI 정책검토 포인트"),
    node("span", "district-policy-cache", insight.cached ? "저장 분석" : "새 AI 분석"),
  );
  const option = insight.policy_options?.[0];
  policy.append(meta, node("h3", "", `${selected.name} · ${insight.headline}`), node("p", "", insight.executive_summary));
  if (option) {
    const action = node("div", "district-policy-action");
    action.append(node("strong", "", option.action), node("span", "", option.rationale));
    policy.append(action);
  }
  policy.append(node("small", "", "현재 DB 발행본에 근거한 정책 아이디어이며 수익성·법적 적합성·투자 확정 판단은 아닙니다."));
}

function renderDistrictPolicyFallback(selected, benchmark) {
  const policy = document.querySelector("[data-district-policy]");
  if (!policy) return;
  clear(policy);
  const pressureRatio = benchmark.demandPer100Rooms ? selected.demandPer100Rooms / benchmark.demandPer100Rooms : null;
  policy.append(
    node("span", "district-policy-label", "정책검토 포인트"),
    node("h3", "", `${selected.name} · ${selected.priority}`),
    node("p", "", `${selected.name}의 객실 100실당 방문 압력은 해운대구의 ${value(pressureRatio, "배")}입니다. 관광숙박업 등록 ${value(selected.tourismFacilityShare, "%")}, 외국인 숙박 대응시설 ${value(selected.foreignCapableShare, "%")}, 2021년 이후 숙박업 등록 ${value(selected.recentLicenseShare, "%")}를 함께 고려해 ${selected.priority}을 우선 검토할 수 있습니다.`),
    node("small", "", "AI 분석을 불러오지 못해 검증된 지표의 기본 해석을 표시합니다."),
  );
}

async function loadDistrictInsight(data, district, benchmark) {
  const cacheKey = `${data.publishedRun}:${district.id}`;
  if (!districtInsightPromises.has(cacheKey)) {
    districtInsightPromises.set(cacheKey, requestInsight("west", district.id));
  }
  try {
    const insight = await districtInsightPromises.get(cacheKey);
    if (activeDistrictInsightId === district.id) renderDistrictPolicyInsight(district, insight);
  } catch (_) {
    districtInsightPromises.delete(cacheKey);
    if (activeDistrictInsightId === district.id) renderDistrictPolicyFallback(district, benchmark);
  }
}

function initializeDistrictDetail(data) {
  const tabs = document.querySelector("[data-west-district-tabs]");
  const chartMetrics = document.querySelector("[data-district-chart-metrics]");
  if (!tabs || !chartMetrics || !data.westDistricts?.length || !data.benchmarkDistrict) return;
  clear(tabs);
  clear(chartMetrics);
  let selectedDistrict = data.westDistricts[0];
  let selectedMetric = districtChartDefinitions[0];
  const districtButtons = new Map();

  const selectDistrict = (district) => {
    selectedDistrict = district;
    activeDistrictInsightId = district.id;
    districtButtons.forEach((button, districtId) => {
      const selected = districtId === district.id;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", selected ? "true" : "false");
    });
    renderWestDistrictSummary(data, selectedDistrict, selectDistrict);
    renderWestDistrictChart(data, selectedMetric, selectedDistrict, selectDistrict);
    renderDistrictDetail(selectedDistrict, data.benchmarkDistrict);
    renderDistrictPolicyLoading(selectedDistrict);
    loadDistrictInsight(data, selectedDistrict, data.benchmarkDistrict);
  };

  data.westDistricts.forEach((district, index) => {
    const button = node("button", index === 0 ? "active" : "", district.name);
    button.type = "button";
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", index === 0 ? "true" : "false");
    button.addEventListener("click", () => selectDistrict(district));
    districtButtons.set(district.id, button);
    tabs.append(button);
  });
  districtChartDefinitions.forEach((metric, index) => {
    const button = node("button", index === 0 ? "active" : "", metric.label);
    button.type = "button";
    button.addEventListener("click", () => {
      selectedMetric = metric;
      chartMetrics.querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
      renderWestDistrictChart(data, selectedMetric, selectedDistrict, selectDistrict);
    });
    chartMetrics.append(button);
  });
  selectDistrict(selectedDistrict);
}

function renderMonthlyTrend(data) {
  const chart = document.querySelector("[data-trend-chart]");
  const summary = document.querySelector("[data-trend-summary]");
  const tooltip = document.querySelector("[data-trend-tooltip]");
  if (!chart || !summary || !tooltip || !data.monthlyTrends?.length) return;

  const series = data.monthlyTrends.map((month) => ({ period: month.period, west: month.west, east: month.east }));
  const latest = series.at(-1);
  const westEntryTotal = series.reduce((sum, item) => sum + item.west.newActiveFacilities, 0);
  const eastEntryTotal = series.reduce((sum, item) => sum + item.east.newActiveFacilities, 0);

  clear(summary);
  [
    ["서부산 최신 방문수요", value(Math.round(latest.west.visitorDailyAverage)), latest.period.replace("-", ".")],
    ["동부산 최신 방문수요", value(Math.round(latest.east.visitorDailyAverage)), latest.period.replace("-", ".")],
    ["서부산 12개월 신규 진입", value(westEntryTotal, "개소"), "현재 영업시설의 최초 인허가 월 기준"],
    ["동부산 12개월 신규 진입", value(eastEntryTotal, "개소"), "현재 영업시설의 최초 인허가 월 기준"],
  ].forEach(([label, main, note]) => {
    const item = node("article", "trend-stat");
    item.append(node("span", "", label), node("strong", "", main), node("small", "", note));
    summary.append(item);
  });

  clear(chart);
  const width = 1120;
  const height = 390;
  const frame = { left: 76, right: 36 };
  const demandPlot = { top: 46, height: 140 };
  const entryPlot = { top: 252, height: 62 };
  const plotWidth = width - frame.left - frame.right;
  const xInset = 32;
  const dataWidth = plotWidth - xInset * 2;
  const visitors = series.flatMap((item) => [item.west.visitorDailyAverage, item.east.visitorDailyAverage]);
  const visitorMin = Math.min(...visitors);
  const visitorMax = Math.max(...visitors);
  const visitorPadding = Math.max((visitorMax - visitorMin) * 0.15, visitorMax * 0.025);
  const visitorFloor = Math.max(0, visitorMin - visitorPadding);
  const visitorCeiling = visitorMax + visitorPadding;
  const entryMax = Math.max(1, ...series.flatMap((item) => [item.west.newActiveFacilities, item.east.newActiveFacilities]));
  const x = (index) => frame.left + xInset + (dataWidth * index) / (series.length - 1);
  const demandY = (amount) => demandPlot.top + demandPlot.height * (1 - ((amount - visitorFloor) / (visitorCeiling - visitorFloor)));
  const entryY = (amount) => entryPlot.top + entryPlot.height * (1 - amount / entryMax);

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("aria-hidden", "true");
  const svgNode = (tag, attributes = {}) => {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attributes).forEach(([name, content]) => element.setAttribute(name, content));
    return element;
  };

  svg.append(
    svgNode("rect", { x: frame.left, y: demandPlot.top, width: plotWidth, height: demandPlot.height, rx: 12, class: "trend-demand-panel" }),
    svgNode("rect", { x: frame.left, y: entryPlot.top, width: plotWidth, height: entryPlot.height, rx: 12, class: "trend-entry-panel" }),
  );
  const demandLabel = svgNode("text", { x: frame.left, y: demandPlot.top - 17, class: "trend-panel-label" });
  demandLabel.textContent = "방문수요(천 명)";
  const entryLabel = svgNode("text", { x: frame.left, y: entryPlot.top - 17, class: "trend-panel-label" });
  entryLabel.textContent = "신규 숙박업체 진입(개소)";
  svg.append(demandLabel, entryLabel, svgNode("line", { x1: frame.left, x2: width - frame.right, y1: 218, y2: 218, class: "trend-panel-divider" }));

  for (let tick = 0; tick <= 3; tick += 1) {
    const y = demandPlot.top + (demandPlot.height * tick) / 3;
    const amount = visitorCeiling - ((visitorCeiling - visitorFloor) * tick) / 3;
    svg.append(svgNode("line", { x1: frame.left, x2: width - frame.right, y1: y, y2: y, class: "trend-grid" }));
    const label = svgNode("text", { x: frame.left - 12, y: y + 4, class: "trend-axis-label", "text-anchor": "end" });
    label.textContent = fmt.format(Math.round(amount / 1000));
    svg.append(label);
  }

  [0, Math.ceil(entryMax / 2), entryMax].forEach((amount) => {
    const y = entryY(amount);
    svg.append(svgNode("line", { x1: frame.left, x2: width - frame.right, y1: y, y2: y, class: "trend-grid entry-grid" }));
    const label = svgNode("text", { x: frame.left - 12, y: y + 4, class: "trend-axis-label entry-axis", "text-anchor": "end" });
    label.textContent = String(amount);
    svg.append(label);
  });

  const barWidth = Math.min(20, plotWidth / series.length * 0.23);
  const barGap = 4;
  series.forEach((item, index) => {
    [["west", -barWidth - barGap / 2], ["east", barGap / 2]].forEach(([region, offset]) => {
      const amount = item[region].newActiveFacilities;
      const y = entryY(amount);
      const barClass = region === "west" ? "trend-bar west" : "trend-bar east";
      svg.append(svgNode("rect", {
        x: x(index) + offset,
        y: amount === 0 ? entryPlot.top + entryPlot.height - 2 : y,
        width: barWidth,
        height: amount === 0 ? 2 : entryPlot.top + entryPlot.height - y,
        rx: 4,
        class: amount === 0 ? `${barClass} zero` : barClass,
      }));
      if (region === "west" && amount > 0) {
        const valueLabel = svgNode("text", {
          x: x(index) + offset + barWidth / 2,
          y: Math.max(entryPlot.top + 10, y - 6),
          class: "trend-west-entry-label",
          "text-anchor": "middle",
        });
        valueLabel.textContent = String(amount);
        svg.append(valueLabel);
      }
    });
  });

  const westLinePath = series.map((item, index) => `${index === 0 ? "M" : "L"}${x(index)},${demandY(item.west.visitorDailyAverage)}`).join(" ");
  const eastLinePath = series.map((item, index) => `${index === 0 ? "M" : "L"}${x(index)},${demandY(item.east.visitorDailyAverage)}`).join(" ");
  svg.append(svgNode("path", { d: westLinePath, class: "trend-line west" }));
  svg.append(svgNode("path", { d: eastLinePath, class: "trend-line east" }));
  series.forEach((item, index) => {
    svg.append(svgNode("circle", { cx: x(index), cy: demandY(item.west.visitorDailyAverage), r: 5, class: "trend-point west" }));
    svg.append(svgNode("circle", { cx: x(index), cy: demandY(item.east.visitorDailyAverage), r: 5, class: "trend-point east" }));
    const label = svgNode("text", { x: x(index), y: height - 24, class: "trend-month", "text-anchor": "middle" });
    label.textContent = item.period.slice(2).replace("-", ".");
    svg.append(label);
  });

  function showTooltip(index, clientX) {
    const item = series[index];
    tooltip.hidden = false;
    clear(tooltip);
    tooltip.append(
      node("strong", "", item.period.replace("-", ".")),
      node("span", "west-demand", `서부산 방문수요 ${value(Math.round(item.west.visitorDailyAverage), "명")}`),
      node("span", "east-demand", `동부산 방문수요 ${value(Math.round(item.east.visitorDailyAverage), "명")}`),
      node("span", "west-entry", `서부산 신규 숙박업체 진입 ${value(item.west.newActiveFacilities, "개소")}`),
      node("span", "east-entry", `동부산 신규 숙박업체 진입 ${value(item.east.newActiveFacilities, "개소")}`),
    );
    const chartRect = chart.getBoundingClientRect();
    const proposed = clientX == null ? (x(index) / width) * chart.scrollWidth - chart.scrollLeft : clientX - chartRect.left;
    tooltip.style.left = `${Math.max(92, Math.min(chart.clientWidth - 92, proposed))}px`;
    tooltip.style.top = "54px";
  }

  series.forEach((item, index) => {
    const previousX = index === 0 ? frame.left : (x(index - 1) + x(index)) / 2;
    const nextX = index === series.length - 1 ? width - frame.right : (x(index) + x(index + 1)) / 2;
    const target = svgNode("rect", { x: previousX, y: demandPlot.top, width: nextX - previousX, height: entryPlot.top + entryPlot.height - demandPlot.top, class: "trend-hit" });
    target.setAttribute("tabindex", "0");
    target.setAttribute("aria-label", `${item.period}, 서부산 방문수요 ${Math.round(item.west.visitorDailyAverage)}명, 동부산 방문수요 ${Math.round(item.east.visitorDailyAverage)}명, 서부산 신규 숙박업체 진입 ${item.west.newActiveFacilities}개소, 동부산 신규 숙박업체 진입 ${item.east.newActiveFacilities}개소`);
    target.addEventListener("pointerenter", (event) => showTooltip(index, event.clientX));
    target.addEventListener("pointermove", (event) => showTooltip(index, event.clientX));
    target.addEventListener("focus", () => showTooltip(index));
    target.addEventListener("pointerleave", () => { tooltip.hidden = true; });
    target.addEventListener("blur", () => { tooltip.hidden = true; });
    svg.append(target);
  });
  chart.append(svg);
  chart.setAttribute("aria-label", "서부산과 동부산의 최근 12개월 일평균 방문수요 선과 신규 숙박업체 진입 병렬 막대 비교");
}

function renderDashboard(data) {
  dashboardData = data;
  document.querySelectorAll("[data-as-of]").forEach((item) => { item.textContent = data.asOf; });
  document.querySelector("[data-load-state]").textContent = "운영 DB 최신 발행본 기준";
  const west = data.regions.find((item) => item.id === "west");
  const east = data.regions.find((item) => item.id === "east");

  const overview = document.querySelector("[data-overview-kpis]");
  overview.append(
    facilitySupplyKpi(west, east),
    kpi("서부산 일평균 방문수요", value(west.visitorDailyAverage), "외지인+외국인 · 일별 방문인원 평균", relativeToEast(west.visitorDailyAverage, east.visitorDailyAverage)),
    kpi("관광숙박업 등록시설 비율", value(west.tourismFacilityShare, "%"), "전체 숙박시설 대비 · 시설 수 기준", relativeToEast(west.tourismFacilityShare, east.tourismFacilityShare), "전체 숙박시설 중 관광진흥법상 관광숙박업 등록을 보유한 시설 수의 비율입니다. 객실 비중이 아닙니다."),
    kpi("2021년 이후 숙박업 등록", value(west.recentLicenseShare, "%"), "현재 영업시설 · 최초 인허가일 기준", relativeToEast(west.recentLicenseShare, east.recentLicenseShare), "현재 영업 중인 시설 가운데 연결 인허가의 가장 이른 인허가일이 2021.1.1 이후인 시설의 비율입니다. 전체 과거 신규등록 건수는 아닙니다."),
    kpi("방문량 대비 관광소비 원천지표", value(west.consumptionIndex), "방문량 대비 방문소비 수준 · 2026.07", relativeToEast(west.consumptionIndex, east.consumptionIndex), "한국관광공사 관광데이터랩의 ‘방문량 대비 방문 소비액’ 원천값을 2026.07 권역 내 구 단위로 평균한 값입니다. 지역 간 소비 수준 비교에만 사용하며, 현재 원천 단위 계약을 검토 중이므로 원화 금액·점유율로 해석하지 않습니다."),
    kpi("건축연령 20년 이상 시설", value(west.old20Share, "%"), "건축물대장 사용승인일부터 산정", relativeToEast(west.old20Share, east.old20Share), "건축물대장 사용승인일이 확인된 시설만 분모로 하여 기준일 현재 20년 이상인 시설 비율입니다. 내부 리모델링 상태를 뜻하지 않습니다."),
    kpi("평균 인허가 경과연수", value(west.licenseAgeAverageYears, "년"), "최초 인허가일~기준일 평균", relativeToEast(west.licenseAgeAverageYears, east.licenseAgeAverageYears), "시설별 연결 인허가 중 가장 이른 인허가일부터 기준일까지의 평균 경과연수입니다. 건축물 연령이나 동일 사업자의 영업기간과 다릅니다."),
    kpi("외국인 숙박 대응시설", value(west.foreignCapableShare, "%"), "관광숙박업·외국인관광 도시민박업", relativeToEast(west.foreignCapableShare, east.foreignCapableShare), "전체 숙박시설 중 관광숙박업 또는 외국인관광 도시민박업으로 등록된 시설 비율입니다. 실제 외국인 투숙실적이나 모든 외국인 수용 가능 시설을 뜻하지 않습니다.")
  );
  renderMonthlyTrend(data);

  const summary = document.querySelector("[data-region-summary]");
  const roomDonut = document.querySelector("[data-room-donut]");
  let donutStart = 0;
  const donutStops = data.regions.map((region) => {
    const segmentStart = donutStart;
    donutStart += region.roomShare;
    return `${colors[region.id]} ${segmentStart}% ${donutStart}%`;
  });
  roomDonut.style.background = `conic-gradient(${donutStops.join(",")})`;
  data.regions.forEach((region) => {
    const row = node("div", "region-row");
    const name = node("strong", "region-name", region.name);
    const dot = node("i", "region-dot");
    dot.style.background = colors[region.id];
    name.prepend(dot);
    row.append(name, node("span", "", `객실 비중 ${value(region.roomShare, "%")}`), node("span", "", `객실 ${value(region.rooms, "실")}`), node("span", "", `수요압력 ${value(region.demandPer100Rooms)}`));
    summary.append(row);
  });
  document.querySelector("[data-core-evidence]").textContent = `관광숙박 ${west.tourismFacilityShare}% · 외국인 숙박 대응시설 ${west.foreignCapableShare}% · 2021년 이후 숙박업 등록 ${west.recentLicenseShare}%`;

  initializeDistrictDetail(data);

  renderSupplyGapSummary(
    document.querySelector("[data-supply-gap-summary]"),
    west,
    east,
  );
  renderVisitorDemandComparison(document.querySelector("[data-visitor-demand-bars]"), west, east);
  renderRegistrationTypeComparison(
    document.querySelector("[data-registration-type-bars]"),
    data.registrationTypes,
  );

  const supplyDonuts = document.querySelector("[data-supply-donuts]");
  const facilityTotal = data.regions.reduce((sum, region) => sum + region.facilities, 0);
  data.regions.forEach((region) => {
    const facilityShare = region.facilities / facilityTotal * 100;
    const card = node("article", "card region-donut-card");
    const chart = node("div", "region-donut");
    chart.style.setProperty("--region-color", colors[region.id]);
    chart.style.setProperty("--region-share", `${facilityShare.toFixed(1)}%`);
    chart.append(node("strong", "", `${facilityShare.toFixed(1)}%`), node("small", "", "부산 시설 비중"));
    const details = node("div", "region-donut-details");
    details.append(node("h3", "", region.name));
    card.append(chart, details);
    supplyDonuts.append(card);
  });

  const supplyBody = document.querySelector("[data-supply-table]");
  data.regions.forEach((region) => {
    const row = node("tr");
    [region.name, value(region.facilities, "개"), value(region.rooms, "실"), value(region.roomCoverageShare, "%"), value(region.roomMedian, "실"), value(region.buildingAgeAverageYears, "년"), value(region.old20Share, "%"), value(region.licenseAgeAverageYears, "년"), value(region.recentLicenseShare, "%")].forEach((text, index) => {
      const cell = node("td", "");
      const content = index === 0 ? node("strong", "", text) : document.createTextNode(text);
      cell.append(content);
      row.append(cell);
    });
    supplyBody.append(row);
  });
  renderBarGroup(document.querySelector("[data-tourism-bars]"), data.regions, "tourismFacilityShare", "%", 40);
  renderBarGroup(document.querySelector("[data-foreign-bars]"), data.regions, "foreignCapableShare", "%", 50);
  renderBarGroup(document.querySelector("[data-new-bars]"), data.regions, "recentLicenseShare", "%", 65);

  const districtBody = document.querySelector("[data-district-table]");
  data.westDistricts.forEach((district) => {
    const row = node("tr");
    [district.rank, district.name, value(district.rooms, "실"), value(district.demandPer100Rooms), value(district.stay3Index), value(district.old20Share, "%"), district.priority].forEach((text, index) => {
      const cell = node("td", "");
      cell.append(index === 1 ? node("strong", "", text) : document.createTextNode(text));
      row.append(cell);
    });
    districtBody.append(row);
  });
}

function resolveTabTarget(target) {
  return target === "map" ? "investment" : target;
}

document.querySelectorAll("[data-tab-target]").forEach((button) => {
  button.addEventListener("click", () => {
    const target = button.dataset.tabTarget;
    if (target === "investment") {
      const mapFrame = document.querySelector("[data-map-src]");
      if (mapFrame && !mapFrame.getAttribute("src")) {
        mapFrame.setAttribute("src", mapFrame.dataset.mapSrc);
      }
    }
    document.querySelectorAll("[data-tab-target]").forEach((item) => {
      const selected = item === button;
      item.classList.toggle("active", selected);
      item.setAttribute("aria-selected", selected ? "true" : "false");
    });
    document.querySelectorAll("[data-tab-panel]").forEach((panel) => panel.classList.toggle("active", panel.dataset.tabPanel === target));
    history.replaceState(null, "", `#${target}`);
  });
});

const initialTarget = resolveTabTarget(location.hash.slice(1));
const initialButton = [...document.querySelectorAll("[data-tab-target]")]
  .find((item) => item.dataset.tabTarget === initialTarget);
if (initialButton && !initialButton.classList.contains("active")) initialButton.click();

const insightButton = document.querySelector("[data-insight-button]");
insightButton.addEventListener("click", async () => {
  if (!dashboardData) return;
  const state = document.querySelector("[data-insight-state]");
  insightButton.disabled = true;
  state.textContent = "검증된 발행지표를 해석하고 있습니다.";
  try {
    renderInsight(await requestInsight(document.querySelector("#insight-region").value));
    state.textContent = "현재 발행본을 기준으로 정책해석을 생성했습니다.";
  } catch (_) {
    state.textContent = "정책해석을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.";
  } finally {
    insightButton.disabled = false;
  }
});

const mapInsightButton = document.querySelector("[data-map-insight-button]");
mapInsightButton.addEventListener("click", async () => {
  if (!dashboardData) return;
  mapInsightButton.disabled = true;
  mapInsightButton.textContent = "지도 해석 중…";
  try {
    renderMapInsight(await requestInsight("west"));
  } catch (_) {
    const result = document.querySelector("[data-map-insight-result]");
    result.hidden = false;
    document.querySelector("[data-map-insight-headline]").textContent = "AI 지도해석을 불러오지 못했습니다.";
    document.querySelector("[data-map-insight-summary]").textContent = "잠시 후 다시 시도해 주세요.";
  } finally {
    mapInsightButton.disabled = false;
    mapInsightButton.textContent = "AI로 지도 설명";
  }
});

const supplyInsightButton = document.querySelector("[data-supply-insight-button]");
supplyInsightButton.addEventListener("click", async () => {
  if (!dashboardData) return;
  const state = document.querySelector("[data-supply-insight-state]");
  const cacheKey = `${dashboardData.publishedRun}:east-west-supply`;
  supplyInsightButton.disabled = true;
  state.textContent = "동·서부산 공급구조를 검증된 발행지표로 해석하고 있습니다.";
  try {
    const reusedInSession = supplyInsightPromises.has(cacheKey);
    if (!reusedInSession) {
      const pending = requestInsight("all").catch((error) => {
        supplyInsightPromises.delete(cacheKey);
        throw error;
      });
      supplyInsightPromises.set(cacheKey, pending);
    }
    const insight = await supplyInsightPromises.get(cacheKey);
    renderSupplyInsight(insight, reusedInSession);
    state.textContent = insight.cached || reusedInSession ? "동일 발행본의 저장 분석을 재사용했습니다." : "현재 발행본의 공급구조 분석을 저장했습니다.";
  } catch (_) {
    state.textContent = "공급구조 분석을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.";
  } finally {
    supplyInsightButton.disabled = false;
  }
});

async function requestInsight(region, district = null) {
  const payload = { region, period: "latest", published_run: dashboardData.publishedRun };
  if (district) payload.district = district;
  const response = await fetch("api/insights", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload)
  });
  if (!response.ok) throw new Error("request_failed");
  return response.json();
}

function renderMapInsight(insight) {
  const result = document.querySelector("[data-map-insight-result]");
  result.hidden = false;
  document.querySelector("[data-map-insight-headline]").textContent = insight.headline;
  document.querySelector("[data-map-insight-summary]").textContent = insight.executive_summary;
  const findings = document.querySelector("[data-map-insight-findings]");
  clear(findings);
  insight.findings.slice(0, 3).forEach((finding) => {
    const item = node("article", "");
    item.append(node("strong", "", finding.title), node("small", "", finding.claim));
    findings.append(item);
  });
}

function renderSupplyInsight(insight, reusedInSession = false) {
  const result = document.querySelector("[data-supply-insight-result]");
  result.hidden = false;
  document.querySelector("[data-supply-insight-source]").textContent = insight.source === "openai" ? "OpenAI 정책해석" : "기본 규칙 해석";
  document.querySelector("[data-supply-insight-date]").textContent = `자료 ${insight.data_as_of}`;
  document.querySelector("[data-supply-insight-cache]").textContent = insight.cached || reusedInSession ? "저장 분석" : "새 분석";
  document.querySelector("[data-supply-insight-headline]").textContent = insight.headline;
  document.querySelector("[data-supply-insight-summary]").textContent = insight.executive_summary;
  const findings = document.querySelector("[data-supply-insight-findings]");
  clear(findings);
  insight.findings.forEach((finding) => {
    const item = node("article", "");
    item.append(node("strong", "", finding.title), node("p", "", finding.claim));
    findings.append(item);
  });
  const options = document.querySelector("[data-supply-insight-options]");
  clear(options);
  insight.policy_options.slice(0, 3).forEach((option) => {
    const item = node("article", "");
    item.append(
      node("span", "", `${option.priority_rank}순위`),
      node("strong", "", option.action),
      node("p", "", `${option.target_area} · ${option.rationale}`),
    );
    options.append(item);
  });
}

function renderInsight(insight) {
  const result = document.querySelector("[data-insight-result]");
  result.hidden = false;
  document.querySelector("[data-insight-source]").textContent = insight.source === "openai" ? "OpenAI 구조화 해석" : "기본 규칙 해석";
  document.querySelector("[data-insight-date]").textContent = `자료 ${insight.data_as_of}`;
  document.querySelector("[data-insight-cache]").textContent = insight.cached ? "저장 결과" : "새 분석";
  document.querySelector("[data-insight-headline]").textContent = insight.headline;
  document.querySelector("[data-insight-summary]").textContent = insight.executive_summary;
  const evidenceById = new Map(insight.evidence.map((metric) => [metric.metric_id, metric]));
  const findings = document.querySelector("[data-insight-findings]");
  clear(findings);
  insight.findings.forEach((finding) => {
    const card = node("article", "card finding");
    const area = { tourism_overview: "관광 종합현황", supply_gap: "동서 공급 격차", private_investment: "투자정보 제공" }[finding.decision_area];
    card.append(node("span", "area", area), node("h3", "", finding.title), node("p", "", finding.claim), node("small", "", `한계: ${finding.limitations}`));
    const chips = node("div", "metric-chips");
    finding.metric_ids.forEach((id) => {
      const metric = evidenceById.get(id);
      chips.append(node("span", "", metric ? `${metric.label} ${value(metric.value, metric.unit)}` : id));
    });
    card.append(chips);
    findings.append(card);
  });
  const options = document.querySelector("[data-insight-options]");
  clear(options);
  insight.policy_options.forEach((option) => {
    const row = node("article", "policy-option");
    const content = node("div", "");
    content.append(node("h3", "", option.action), node("p", "", `${option.rationale} · 유의: ${option.caveat}`));
    row.append(node("span", "rank", String(option.priority_rank)), node("strong", "", option.target_area), content);
    options.append(row);
  });
  const evidence = document.querySelector("[data-insight-evidence]");
  clear(evidence);
  insight.evidence.forEach((metric) => {
    const row = node("div", "evidence-row");
    row.append(node("strong", "", metric.label), node("span", "", value(metric.value, metric.unit)), node("span", "", metric.period), node("span", "", metric.quality_note));
    evidence.append(row);
  });
}

fetch("data.json", { cache: "no-store" })
  .then((response) => response.json())
  .then(renderDashboard)
  .catch(() => { document.querySelector("[data-load-state]").textContent = "지표 파일을 불러오지 못했습니다."; });
