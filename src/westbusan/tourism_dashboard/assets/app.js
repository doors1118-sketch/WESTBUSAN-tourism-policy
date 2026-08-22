const fmt = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 1 });
const colors = { west: "#176bff", east: "#19b6c9", other: "#9aa8ba" };
let dashboardData = null;

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
    kpi("2021년 이후 신규 인허가", value(west.recentLicenseShare, "%"), "최초 인허가일 2021.1.1 이후", relativeToEast(west.recentLicenseShare, east.recentLicenseShare), "시설별 연결 인허가 가운데 가장 이른 인허가일을 기준으로, 2021.1.1 이후인 시설이 전체 시설에서 차지하는 비율입니다."),
    kpi("방문량 대비 관광소비 원천지표", value(west.consumptionIndex), "관광공사 원천지표 · 2026.07 구 평균", relativeToEast(west.consumptionIndex, east.consumptionIndex), "한국관광공사 ‘방문량 대비 방문 소비액’ 원천지표를 2026.07 권역 내 구 단위로 평균한 값입니다. 원화 금액으로 해석하지 않습니다."),
    kpi("건축연령 20년 이상 시설", value(west.old20Share, "%"), `사용승인일 기준 · 자료 확인률 ${west.buildingAgeCoverageShare}%`, relativeToEast(west.old20Share, east.old20Share), "건축물대장 사용승인일이 확인된 시설만 분모로 하여 기준일 현재 20년 이상인 시설 비율입니다. 내부 리모델링 상태를 뜻하지 않습니다."),
    kpi("평균 인허가 경과연수", value(west.licenseAgeAverageYears, "년"), "최초 인허가일~기준일 평균", relativeToEast(west.licenseAgeAverageYears, east.licenseAgeAverageYears), "시설별 연결 인허가 중 가장 이른 인허가일부터 기준일까지의 평균 경과연수입니다. 건축물 연령이나 동일 사업자의 영업기간과 다릅니다."),
    kpi("외국인 대상 관광등록", value(west.foreignCapableShare, "%"), "관광숙박·외국인관광 도시민박", relativeToEast(west.foreignCapableShare, east.foreignCapableShare))
  );

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
  document.querySelector("[data-core-evidence]").textContent = `관광숙박 ${west.tourismFacilityShare}% · 외국인수용 ${west.foreignCapableShare}% · 신규 ${west.recentLicenseShare}%`;

  const tourism = document.querySelector("[data-tourism-kpis]");
  tourism.append(
    kpi("서부산 일평균 방문수요", value(west.visitorDailyAverage), "일별 방문인원 평균 · 최근 355일"),
    kpi("방문수요 대비 객실공급 압력", value(west.demandPer100Rooms), "숙박객·객실점유율이 아닌 정책 검토용 파생지표"),
    kpi("소비효율 원천지표", value(west.consumptionIndex), "2026-07 · 통화금액 아님"),
    kpi("3박 방문 원천지표", value(west.stay3Index), "2026-07 · 실제 명수 아님")
  );
  renderBarGroup(document.querySelector("[data-demand-bars]"), data.regions, "visitorDailyAverage", "", 1000000);
  const indices = document.querySelector("[data-source-indices]");
  data.regions.forEach((region) => {
    const box = node("div", "source-index");
    box.append(node("span", "", region.name), node("strong", "", `${region.consumptionIndex} / ${region.stay3Index}`), node("small", "", "소비효율 / 3박 방문"));
    indices.append(box);
  });

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
    details.append(
      node("h3", "", region.name),
      node("p", "", `숙박시설 ${value(region.facilities, "개")} · 확인 객실 ${value(region.rooms, "실")}`),
      node("p", "", `2021년 이후 신규 ${value(region.recentLicenseShare, "%")} · 20년+ ${value(region.old20Share, "%")}`),
    );
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

document.querySelectorAll("[data-tab-target]").forEach((button) => {
  button.addEventListener("click", () => {
    const target = button.dataset.tabTarget;
    if (target === "map") {
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

const initialTarget = location.hash.slice(1);
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

async function requestInsight(region) {
  const response = await fetch("api/insights", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ region, period: "latest", published_run: dashboardData.publishedRun })
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
    const area = { tourism_overview: "관광 종합현황", supply_gap: "공급 격차", private_investment: "민간투자 유도" }[finding.decision_area];
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
