(() => {
  "use strict";

  const TILE_SIZE = 256;
  const MIN_ZOOM = 10;
  const MAX_ZOOM = 18;
  const INITIAL = { lon: 128.977, lat: 35.178, zoom: 12 };
  const map = document.getElementById("river-map");
  const tileLayer = document.getElementById("tile-layer");
  const overlay = document.getElementById("river-overlay");
  const externalOverlay = document.getElementById("external-regulation-overlay");
  const labelsLayer = document.getElementById("park-labels");
  const clickMarker = document.getElementById("click-marker");
  const activitySelect = document.getElementById("activity-select");
  const structureHeight = document.getElementById("structure-height");
  const roofType = document.getElementById("roof-type");
  const parcelAddress = document.getElementById("parcel-address");
  const parcelSearch = document.getElementById("parcel-search");
  const parcelSearchState = document.getElementById("parcel-search-state");
  const policyInsightButton = document.getElementById("policy-insight-button");
  const policyInsightPanel = document.getElementById("policy-insight-panel");
  const tileNodes = new Map();
  const pathNodes = [];
  const externalPathNodes = [];
  let features = [];
  let selectedPoint = null;
  let selectedPnu = null;
  let dragOrigin = null;
  let regulationController = null;
  let regulationSequence = 0;
  let policyInsightController = null;

  const parks = {
    hwamyeong: { name: "화명생태공원", lon: 129.00547, lat: 35.23847, zoom: 14 },
    daejeo: { name: "대저생태공원", lon: 128.98894, lat: 35.21074, zoom: 14 },
    samrak: { name: "삼락생태공원", lon: 128.9763, lat: 35.1711, zoom: 14 },
    maekdo: { name: "맥도생태공원", lon: 128.95709, lat: 35.15138, zoom: 14 },
    eulsukdo: { name: "을숙도생태공원", lon: 128.9523, lat: 35.1172, zoom: 14 },
  };
  const activityLabels = {
    walking: "산책·탐방", ecology: "생태관찰·복원", festival: "축제·행사",
    sports: "체육·레저", camping: "야영·캠핑", food: "판매·음식시설",
    culture: "공연·문화시설", lodging: "숙박시설", parking: "주차장·진입도로",
  };
  const zoneLabels = {
    waterfront: "근린친수지구", general_conservation: "일반보전지구",
    restoration: "복원지구", river_area_unclassified: "하천구역·세부지구 미확인",
    outside_river_area: "조회 기준 하천구역 외",
  };
  const gradeLabels = {
    conditional: "관리청 협의 전제 검토",
    principally_restricted: "원칙적 불가 가능성 높음",
    outside_scope: "하천구역 외·별도 법령 검토",
  };
  const regulationLabels = {
    wetland: "습지보호구역",
    heritage: "국가유산",
    urban_park: "도시공원",
    land_use: "용도지역",
  };
  const regulationStatusLabels = {
    no_overlap: "선택 지점 중첩 없음",
    provider_error: "공간서비스 조회 실패·미판정",
    invalid_response: "공간서비스 응답 오류·미판정",
  };
  const planningCategoryLabels = {
    development_restriction: "개발행위허가 제한",
    district_unit_plan: "지구단위계획",
    urban_planning_facility: "도시계획시설",
    land_use_zone: "용도지역",
    land_use_district: "용도지구",
    land_use_area: "용도구역",
    other_law_restriction: "개별법 규제",
    unclassified: "기타 지정",
  };

  function worldPixel(lon, lat, zoom) {
    const size = TILE_SIZE * (2 ** zoom);
    const clipped = Math.max(-85.05112878, Math.min(85.05112878, lat));
    const sin = Math.sin(clipped * Math.PI / 180);
    return [(lon + 180) / 360 * size, (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * size];
  }

  function lonLatFromWorld(x, y, zoom) {
    const size = TILE_SIZE * (2 ** zoom);
    const lon = x / size * 360 - 180;
    const n = Math.PI - 2 * Math.PI * y / size;
    return [lon, 180 / Math.PI * Math.atan(Math.sinh(n))];
  }

  const initialWorld = worldPixel(INITIAL.lon, INITIAL.lat, INITIAL.zoom);
  const state = { zoom: INITIAL.zoom, centerX: initialWorld[0], centerY: initialWorld[1] };

  function screenPoint(lon, lat) {
    const [x, y] = worldPixel(lon, lat, state.zoom);
    return [x - state.centerX + map.clientWidth / 2, y - state.centerY + map.clientHeight / 2];
  }

  function pathForRing(ring) {
    return ring.map((point, index) => {
      const [x, y] = screenPoint(point[0], point[1]);
      return `${index ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ") + " Z";
  }

  function pathForGeometry(geometry) {
    if (geometry.type === "Polygon") return geometry.coordinates.map(pathForRing).join(" ");
    if (geometry.type === "MultiPolygon") return geometry.coordinates.flatMap((polygon) => polygon.map(pathForRing)).join(" ");
    return "";
  }

  function renderTiles() {
    const width = map.clientWidth;
    const height = map.clientHeight;
    const left = state.centerX - width / 2;
    const top = state.centerY - height / 2;
    const limit = 2 ** state.zoom;
    const visible = new Set();
    for (let ty = Math.max(0, Math.floor(top / TILE_SIZE)); ty <= Math.min(limit - 1, Math.floor((top + height) / TILE_SIZE)); ty += 1) {
      for (let tx = Math.floor(left / TILE_SIZE); tx <= Math.floor((left + width) / TILE_SIZE); tx += 1) {
        const wx = ((tx % limit) + limit) % limit;
        const key = `${state.zoom}/${wx}/${ty}`;
        visible.add(key);
        let image = tileNodes.get(key);
        if (!image) {
          image = new Image(); image.alt = ""; image.decoding = "async"; image.draggable = false;
          image.addEventListener("error", () => { image.style.visibility = "hidden"; });
          image.addEventListener("load", () => { image.style.visibility = "visible"; });
          image.src = map.dataset.tileTemplate.replace("{z}", state.zoom).replace("{x}", wx).replace("{y}", ty);
          tileNodes.set(key, image); tileLayer.append(image);
        }
        image.style.left = `${tx * TILE_SIZE - left}px`; image.style.top = `${ty * TILE_SIZE - top}px`;
      }
    }
    tileNodes.forEach((image, key) => { if (!visible.has(key)) { image.remove(); tileNodes.delete(key); } });
  }

  function renderOverlay() {
    overlay.setAttribute("viewBox", `0 0 ${map.clientWidth} ${map.clientHeight}`);
    externalOverlay.setAttribute("viewBox", `0 0 ${map.clientWidth} ${map.clientHeight}`);
    pathNodes.forEach(({ node, feature }) => node.setAttribute("d", pathForGeometry(feature.geometry)));
    externalPathNodes.forEach(({ node, feature }) => node.setAttribute("d", pathForGeometry(feature.geometry)));
    labelsLayer.replaceChildren();
    Object.values(parks).forEach((park) => {
      const [x, y] = screenPoint(park.lon, park.lat);
      if (x < -80 || x > map.clientWidth + 80 || y < -30 || y > map.clientHeight + 30) return;
      const label = document.createElement("span"); label.className = "park-label"; label.textContent = park.name;
      label.style.left = `${x}px`; label.style.top = `${y}px`; labelsLayer.append(label);
    });
    if (selectedPoint) {
      const [x, y] = screenPoint(selectedPoint[0], selectedPoint[1]);
      clickMarker.hidden = false; clickMarker.style.left = `${x}px`; clickMarker.style.top = `${y}px`;
    }
  }

  function render() { renderTiles(); renderOverlay(); }

  function pointInRing(point, ring) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
      const [xi, yi] = ring[i]; const [xj, yj] = ring[j];
      const crosses = ((yi > point[1]) !== (yj > point[1])) &&
        (point[0] < (xj - xi) * (point[1] - yi) / ((yj - yi) || Number.EPSILON) + xi);
      if (crosses) inside = !inside;
    }
    return inside;
  }

  function pointInPolygon(point, polygon) {
    if (!pointInRing(point, polygon[0])) return false;
    return !polygon.slice(1).some((hole) => pointInRing(point, hole));
  }

  function pointInFeature(point, feature) {
    const geometry = feature.geometry;
    if (geometry.type === "Polygon") return pointInPolygon(point, geometry.coordinates);
    if (geometry.type === "MultiPolygon") return geometry.coordinates.some((polygon) => pointInPolygon(point, polygon));
    return false;
  }

  function zoneAt(point) {
    const hits = features.filter((feature) => pointInFeature(point, feature));
    const specific = hits.filter((feature) => feature.properties.zone_type !== "river_area")
      .sort((a, b) => b.properties.priority - a.properties.priority);
    if (specific[0]) return specific[0].properties.zone_type;
    return hits.some((feature) => feature.properties.zone_type === "river_area") ? "river_area_unclassified" : "outside_river_area";
  }

  function distanceKm(a, b) {
    const rad = Math.PI / 180; const dLat = (b.lat - a[1]) * rad; const dLon = (b.lon - a[0]) * rad;
    const q = Math.sin(dLat / 2) ** 2 + Math.cos(a[1] * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
    return 6371 * 2 * Math.atan2(Math.sqrt(q), Math.sqrt(1 - q));
  }

  function nearestPark(point) {
    return Object.values(parks).map((park) => ({ ...park, distance: distanceKm(point, park) }))
      .sort((a, b) => a.distance - b.distance)[0];
  }

  function assessSelection(zone, activity) {
    if (zone === "outside_river_area") return { grade: "outside_scope", reason: "선택지가 조회 기준 하천구역 도형 밖입니다. 이 결과는 행위 가능 판정이 아니며 도시계획·공원·문화유산·습지 규제가 별도로 적용될 수 있습니다.", next: "토지이용계획확인서와 해당 공원·보호구역 관리청의 공식 도면을 확인하십시오." };
    if (zone === "waterfront") {
      if (activity === "lodging") return { grade: "principally_restricted", reason: "친수지구라도 숙박시설은 일상적 친수활동 범위를 넘는 영구적 건축·점용으로 판단될 가능성이 높습니다.", next: "하천정비기본계획, 하천점용허가, 홍수소통·치수안전, 공원조성계획 반영 여부를 사전협의하십시오." };
      return { grade: "conditional", reason: "친수지구는 이용 활동을 검토할 수 있지만 규모·영구성·홍수 영향에 따라 허가 가능성이 달라집니다.", next: "임시·가설 여부, 점용면적, 홍수기 철거계획, 차량진입, 하천관리청 사전협의를 확인하십시오." };
    }
    if (zone === "general_conservation") {
      if (["walking", "ecology"].includes(activity)) return { grade: "conditional", reason: "보전 기능을 훼손하지 않는 저강도 탐방·생태활동은 검토할 수 있지만 서식지·출입제한·점용 요건을 확인해야 합니다.", next: "습지·철새·문화유산 중첩, 탐방로 기조성 여부, 관리청 시기별 제한을 확인하십시오." };
      return { grade: "principally_restricted", reason: "일반보전지구에서 구조물·차량진입·영업·집객을 수반하는 행위는 보전목적과 충돌할 가능성이 높습니다.", next: "사업지를 친수지구로 조정하거나 비구조물·최소 점용 대안으로 변경한 후 관리청과 사전협의하십시오." };
    }
    if (zone === "restoration") {
      if (activity === "ecology") return { grade: "conditional", reason: "생태복원·관찰 목적이 관리방향과 일치하더라도 시설·동선은 복원계획을 훼손하지 않아야 합니다.", next: "복원 목표종·서식처, 출입 시기, 데크·안내시설 설치범위를 관리청과 협의하십시오." };
      return { grade: "principally_restricted", reason: "복원지구의 생태·지형 회복 목적과 충돌할 가능성이 높은 행위입니다.", next: "복원계획상 위치와 목표를 확인하고 사업지 변경 또는 비구조물·최소개입 대안을 검토하십시오." };
    }
    return { grade: "conditional", reason: "하천구역 내이지만 현재 스냅샷에서 세부 관리지구가 중첩되지 않은 위치입니다. 이를 행위 허용으로 해석하면 안 됩니다.", next: "최신 하천정비기본계획 원도면과 관리청 공식 의견으로 세부 지구를 확인하십시오." };
  }

  function setRegulationResultsLoading() {
    document.querySelectorAll("[data-regulation-result]").forEach((card) => {
      card.className = "is-loading";
      card.querySelector("strong").textContent = "조회 중";
      card.querySelector("small").textContent = "VWorld 규제 레이어 교차조회";
    });
    const planningStatus = document.getElementById("parcel-planning-status");
    planningStatus.className = "";
    planningStatus.textContent = selectedPnu ? "필지 규제 조회 중" : "주소·지번 입력 필요";
  }

  function clearExternalRegulations() {
    externalPathNodes.splice(0).forEach(({ node }) => node.remove());
  }

  function regulationLayerEnabled(category) {
    const input = document.querySelector(`[data-regulation-layer="${category}"]`);
    return !input || input.checked;
  }

  function renderRegulationCards(review) {
    const matches = Array.isArray(review.matches) ? review.matches : [];
    const statuses = Array.isArray(review.layer_statuses) ? review.layer_statuses : [];
    Object.keys(regulationLabels).forEach((category) => {
      const card = document.querySelector(`[data-regulation-result="${category}"]`);
      if (!card) return;
      const categoryMatches = matches.filter((match) => match.category === category);
      const status = statuses.find((item) => item.category === category);
      const statusCode = status ? status.status : "";
      card.className = statusCode === "matched" ? "is-matched" : statusCode === "no_overlap" ? "is-clear" : "is-missing";
      card.querySelector("strong").textContent = categoryMatches.length
        ? categoryMatches.map((match) => match.label).join(" · ")
        : regulationStatusLabels[statusCode] || "응답 없음·미판정";
      card.querySelector("small").textContent = statusCode === "matched"
        ? `${categoryMatches.length}개 도형 중첩`
        : "조회 결과와 법정 고시도면은 다를 수 있음";
    });
    const heritage = review.heritage_criteria;
    const heritageCard = document.querySelector('[data-regulation-result="heritage"]');
    if (heritage && heritageCard) {
      const restricted = ["direct_designation_overlap", "individual_review_required", "exceeds_published_criteria"].includes(heritage.code);
      const clear = ["within_published_criteria", "no_snapshot_overlap"].includes(heritage.code);
      heritageCard.className = restricted ? "is-matched" : clear ? "is-clear" : "is-missing";
      heritageCard.querySelector("strong").textContent = heritage.label || "국가유산 기준 미판정";
      const checked = heritage.source_checked_at ? heritage.source_checked_at.slice(0, 10) : "기준일 미확인";
      const zone = heritage.zone_name ? ` · ${heritage.zone_name}` : "";
      heritageCard.querySelector("small").textContent = `${checked} 승인 스냅샷${zone}`;
    }
  }

  function renderExternalRegulations(collection) {
    clearExternalRegulations();
    const externalFeatures = collection && Array.isArray(collection.features) ? collection.features : [];
    externalFeatures.forEach((feature) => {
      const category = feature && feature.properties ? feature.properties.category : "";
      if (!regulationLabels[category] || !feature.geometry) return;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("class", `external-feature external-${category}`);
      path.setAttribute("fill-rule", "evenodd");
      path.classList.toggle("is-hidden", !regulationLayerEnabled(category));
      externalOverlay.append(path);
      externalPathNodes.push({ node: path, feature });
    });
    renderOverlay();
  }

  function renderParcelPlanning(review) {
    const status = document.getElementById("parcel-planning-status");
    const root = document.getElementById("parcel-planning-results");
    root.replaceChildren();
    status.className = review && review.grade === "principally_restricted"
      ? "is-restricted"
      : review && review.grade === "conditional" ? "is-conditional" : "";
    status.textContent = review && review.label ? review.label : "필지 규제 미판정";
    if (!review || review.status !== "matched") {
      const message = document.createElement("p");
      message.textContent = review && review.reason
        ? review.reason
        : "승인된 필지 규제 결과를 확인하지 못했습니다.";
      root.append(message);
      return;
    }
    const designations = document.createElement("div");
    designations.className = "planning-designations";
    (review.designations || []).forEach((item) => {
      const badge = document.createElement("span");
      badge.textContent = `${planningCategoryLabels[item.category] || "지정"} · ${item.name}`;
      designations.append(badge);
    });
    if (!designations.childElementCount) {
      const badge = document.createElement("span");
      badge.textContent = "공식 지정명 없음·원문 재확인";
      designations.append(badge);
    }
    root.append(designations);
    const characteristics = review.characteristics || {};
    const facts = [
      ["PNU", review.pnu || "-"],
      ["지목", characteristics.land_category || "미확인"],
      ["필지면적", Number.isFinite(characteristics.parcel_area) ? `${characteristics.parcel_area.toLocaleString("ko-KR")}㎡` : "미확인"],
      ["도로접면", characteristics.road_side || "미확인"],
      ["이용상황", characteristics.land_use_situation || "미확인"],
      ["자료기준", review.source_date || (review.checked_at || "").slice(0, 10) || "미확인"],
    ];
    const grid = document.createElement("div");
    grid.className = "planning-grid";
    facts.forEach(([label, value]) => {
      const card = document.createElement("div");
      const caption = document.createElement("span"); caption.textContent = label;
      const strong = document.createElement("strong"); strong.textContent = value;
      card.append(caption, strong); grid.append(card);
    });
    root.append(grid);
    const copy = document.createElement("div");
    copy.className = "planning-copy";
    const reason = document.createElement("p"); reason.textContent = `판정 근거: ${review.reason}`;
    const next = document.createElement("p"); next.textContent = `선행 확인: ${review.next_check}`;
    copy.append(reason, next); root.append(copy);
  }

  function markRegulationsUnavailable(message) {
    clearExternalRegulations();
    document.querySelectorAll("[data-regulation-result]").forEach((card) => {
      card.className = "is-missing";
      card.querySelector("strong").textContent = "조회 실패·미판정";
      card.querySelector("small").textContent = message;
    });
    renderParcelPlanning({
      status: "request_failed",
      grade: "unreviewed",
      label: "필지 규제 조회 실패·미판정",
      reason: message,
    });
  }

  async function queryRegulations(point, zone, activity, sequence) {
    if (regulationController) regulationController.abort();
    regulationController = new AbortController();
    const query = new URLSearchParams({
      longitude: String(point[0]),
      latitude: String(point[1]),
      activity,
      river_zone: zone,
    });
    const height = Number(structureHeight.value);
    if (structureHeight.value !== "" && Number.isFinite(height)) query.set("height_m", String(height));
    query.set("roof_type", roofType.value);
    if (selectedPnu) query.set("pnu", selectedPnu);
    try {
      const response = await fetch(`/tourism/api/regulations/point?${query}`, {
        headers: { Accept: "application/json" },
        signal: regulationController.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const review = await response.json();
      if (sequence !== regulationSequence) return;
      const resolution = review.parcel_resolution || {};
      if (!selectedPnu && resolution.status === "matched" && resolution.pnu) {
        selectedPnu = resolution.pnu;
        parcelSearchState.textContent = `지도 클릭 좌표 · PNU 자동연결 완료 · 도형 ${resolution.matched_count}/${resolution.target_count}개 적재`;
      } else if (!selectedPnu && resolution.status === "boundary_ambiguous") {
        parcelSearchState.textContent = "필지 경계선 좌표입니다. 후보 PNU가 둘 이상이므로 주소·지번으로 확정하십시오.";
      } else if (!selectedPnu && resolution.status === "scope_not_published") {
        parcelSearchState.textContent = "현재 발행된 검토대상 필지도형 밖입니다. 주소·지번 검색으로 별도 확인하십시오.";
      }
      renderRegulationCards(review);
      renderParcelPlanning(review.parcel_planning);
      renderExternalRegulations(review.feature_collection);
      const grade = document.getElementById("assessment-grade");
      grade.className = `grade ${review.grade}`;
      grade.textContent = review.label || gradeLabels[review.grade] || "추가 확인 필요";
      document.getElementById("assessment-reason").textContent = review.reason;
      document.getElementById("assessment-next").textContent = review.next_check;
      policyInsightButton.disabled = false;
    } catch (error) {
      if (error.name === "AbortError" || sequence !== regulationSequence) return;
      markRegulationsUnavailable("하천구역 판정만 유지·외부 규제 재조회 필요");
      policyInsightButton.disabled = true;
    }
  }

  function fillList(element, values) {
    element.replaceChildren();
    (Array.isArray(values) ? values : []).forEach((value) => {
      const item = document.createElement("li");
      item.textContent = value;
      element.append(item);
    });
  }

  async function queryPolicyInsight() {
    if (!selectedPoint || policyInsightButton.disabled) return;
    if (policyInsightController) policyInsightController.abort();
    policyInsightController = new AbortController();
    policyInsightPanel.hidden = false;
    policyInsightButton.disabled = true;
    document.getElementById("policy-insight-status").textContent = "법령 근거·정책대안 생성 중";
    document.getElementById("policy-insight-title").textContent = "선택지 정책인사이트";
    document.getElementById("policy-insight-copy").textContent = "공간판정과 내부 법령근거 캐시를 확인하고 있습니다.";
    fillList(document.getElementById("policy-options"), []);
    fillList(document.getElementById("required-consultations"), []);
    document.getElementById("legal-source-links").replaceChildren();
    document.getElementById("policy-insight-limit").textContent = "";
    const zone = zoneAt(selectedPoint);
    const body = {
      longitude: selectedPoint[0],
      latitude: selectedPoint[1],
      activity: activitySelect.value,
      river_zone: zone,
      roof_type: roofType.value,
    };
    const height = Number(structureHeight.value);
    if (structureHeight.value !== "" && Number.isFinite(height)) body.height_m = height;
    if (selectedPnu) body.pnu = selectedPnu;
    try {
      const response = await fetch("/tourism/api/regulations/insight", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(body),
        signal: policyInsightController.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const insight = await response.json();
      document.getElementById("policy-insight-title").textContent = insight.headline;
      document.getElementById("policy-insight-copy").textContent = insight.policy_insight;
      document.getElementById("policy-insight-status").textContent = insight.legal_evidence_status === "retrieved"
        ? `${insight.cached ? "캐시 재사용" : "신규 생성"} · 법령근거 조회`
        : "결정규칙 설명 · 법령근거 미연결";
      fillList(document.getElementById("policy-options"), insight.policy_options);
      fillList(document.getElementById("required-consultations"), insight.required_consultations);
      const sourceRoot = document.getElementById("legal-source-links");
      sourceRoot.replaceChildren();
      (insight.legal_source_urls || []).forEach((url, index) => {
        const link = document.createElement("a");
        link.href = url; link.target = "_blank"; link.rel = "noopener";
        link.textContent = `공식 법령근거 ${index + 1} ↗`;
        sourceRoot.append(link);
      });
      document.getElementById("policy-insight-limit").textContent = insight.limitations;
    } catch (error) {
      if (error.name === "AbortError") return;
      document.getElementById("policy-insight-status").textContent = "생성 실패";
      document.getElementById("policy-insight-copy").textContent = "법령 MCP 또는 AI 서비스에 연결하지 못했습니다. 위 공간판정과 선행 확인사항만 사용하십시오.";
    } finally {
      policyInsightButton.disabled = false;
    }
  }

  function updateAssessment(point) {
    const zone = zoneAt(point); const activity = activitySelect.value; const result = assessSelection(zone, activity); const park = nearestPark(point);
    document.getElementById("assessment-title").textContent = `${park.name} 일원·${activityLabels[activity]} 1차 검토`;
    const grade = document.getElementById("assessment-grade"); grade.className = `grade ${result.grade}`; grade.textContent = gradeLabels[result.grade];
    document.getElementById("selected-park").textContent = `${park.name} 약 ${park.distance.toFixed(1)}km`;
    document.getElementById("selected-zone").textContent = zoneLabels[zone];
    document.getElementById("selected-activity").textContent = activityLabels[activity];
    document.getElementById("selected-coordinate").textContent = `${point[1].toFixed(6)}, ${point[0].toFixed(6)}`;
    document.getElementById("assessment-reason").textContent = result.reason;
    document.getElementById("assessment-next").textContent = result.next;
    policyInsightButton.disabled = true;
    policyInsightPanel.hidden = true;
    setRegulationResultsLoading();
    regulationSequence += 1;
    void queryRegulations(point, zone, activity, regulationSequence);
  }

  function setView(lon, lat, zoom) {
    state.zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom));
    [state.centerX, state.centerY] = worldPixel(lon, lat, state.zoom); render();
  }

  map.addEventListener("pointerdown", (event) => { map.setPointerCapture(event.pointerId); dragOrigin = { x: event.clientX, y: event.clientY, cx: state.centerX, cy: state.centerY, moved: false }; });
  map.addEventListener("pointermove", (event) => { if (!dragOrigin) return; const dx = event.clientX - dragOrigin.x; const dy = event.clientY - dragOrigin.y; if (Math.abs(dx) + Math.abs(dy) > 4) dragOrigin.moved = true; state.centerX = dragOrigin.cx - dx; state.centerY = dragOrigin.cy - dy; render(); });
  map.addEventListener("pointerup", (event) => {
    if (!dragOrigin) return; const moved = dragOrigin.moved; dragOrigin = null;
    if (moved) return;
    const rect = map.getBoundingClientRect(); const worldX = state.centerX - map.clientWidth / 2 + event.clientX - rect.left; const worldY = state.centerY - map.clientHeight / 2 + event.clientY - rect.top;
    selectedPnu = null;
    parcelSearchState.textContent = "지도 클릭 위치입니다. 필지 도시계획 상세는 주소·지번 검색이 필요합니다.";
    selectedPoint = lonLatFromWorld(worldX, worldY, state.zoom); updateAssessment(selectedPoint); renderOverlay();
  });
  map.addEventListener("wheel", (event) => { event.preventDefault(); const [lon, lat] = lonLatFromWorld(state.centerX, state.centerY, state.zoom); setView(lon, lat, state.zoom + (event.deltaY < 0 ? 1 : -1)); }, { passive: false });
  document.getElementById("zoom-in").addEventListener("click", () => { const [lon, lat] = lonLatFromWorld(state.centerX, state.centerY, state.zoom); setView(lon, lat, state.zoom + 1); });
  document.getElementById("zoom-out").addEventListener("click", () => { const [lon, lat] = lonLatFromWorld(state.centerX, state.centerY, state.zoom); setView(lon, lat, state.zoom - 1); });
  document.getElementById("zoom-reset").addEventListener("click", () => setView(INITIAL.lon, INITIAL.lat, INITIAL.zoom));
  document.querySelectorAll("[data-park]").forEach((button) => button.addEventListener("click", () => {
    document.querySelectorAll("[data-park]").forEach((item) => item.classList.toggle("is-active", item === button));
    const park = parks[button.dataset.park]; setView(park.lon, park.lat, park.zoom);
  }));
  document.querySelectorAll("[data-layer]").forEach((input) => input.addEventListener("change", () => {
    pathNodes.filter((item) => item.feature.properties.zone_type === input.dataset.layer).forEach((item) => item.node.classList.toggle("is-hidden", !input.checked));
  }));
  document.querySelectorAll("[data-regulation-layer]").forEach((input) => input.addEventListener("change", () => {
    externalPathNodes.filter((item) => item.feature.properties.category === input.dataset.regulationLayer)
      .forEach((item) => item.node.classList.toggle("is-hidden", !input.checked));
  }));
  activitySelect.addEventListener("change", () => { document.getElementById("selected-activity").textContent = activityLabels[activitySelect.value]; if (selectedPoint) updateAssessment(selectedPoint); });
  structureHeight.addEventListener("change", () => { if (selectedPoint) updateAssessment(selectedPoint); });
  roofType.addEventListener("change", () => { if (selectedPoint) updateAssessment(selectedPoint); });
  async function searchParcel() {
    const address = parcelAddress.value.trim();
    if (address.length < 8 || !address.includes("부산")) {
      parcelSearchState.textContent = "부산광역시와 자치구를 포함한 주소·지번을 입력하십시오.";
      return;
    }
    parcelSearch.disabled = true;
    parcelSearchState.textContent = "주소와 PNU를 확인하는 중입니다.";
    try {
      const response = await fetch("/tourism/api/vworld/geocode", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ address }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const geocode = await response.json();
      if (geocode.status !== "matched" || !geocode.pnu || !Number.isFinite(geocode.longitude) || !Number.isFinite(geocode.latitude)) {
        selectedPnu = null;
        parcelSearchState.textContent = "주소 좌표 또는 19자리 PNU를 확인하지 못했습니다.";
        return;
      }
      selectedPnu = geocode.pnu;
      selectedPoint = [geocode.longitude, geocode.latitude];
      parcelSearchState.textContent = `${geocode.district || "부산"} · PNU 확인 완료`;
      setView(selectedPoint[0], selectedPoint[1], Math.max(state.zoom, 16));
      updateAssessment(selectedPoint);
      renderOverlay();
    } catch (_error) {
      selectedPnu = null;
      parcelSearchState.textContent = "주소 검색 서비스에 연결하지 못했습니다. 잠시 후 다시 확인하십시오.";
    } finally {
      parcelSearch.disabled = false;
    }
  }
  parcelSearch.addEventListener("click", () => { void searchParcel(); });
  policyInsightButton.addEventListener("click", () => { void queryPolicyInsight(); });
  parcelAddress.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); void searchParcel(); }
  });
  window.addEventListener("resize", render);

  Promise.all([
    fetch("river_layers.geojson", { cache: "no-store" }).then((response) => response.json()),
    fetch("source_metadata.json", { cache: "no-store" }).then((response) => response.json()),
  ]).then(([collection, metadata]) => {
    features = collection.features;
    document.getElementById("source-date").textContent = `${metadata.retrieved_at.slice(0, 10)} 스냅샷·${metadata.feature_count}개 도형`;
    features.slice().sort((a, b) => a.properties.priority - b.properties.priority).forEach((feature) => {
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("class", `zone-feature zone-${feature.properties.zone_type}`); path.setAttribute("fill-rule", "evenodd");
      overlay.append(path); pathNodes.push({ node: path, feature });
    });
    render();
  }).catch(() => {
    document.querySelector(".map-instruction").textContent = "규제도형을 불러오지 못했습니다. 발행파일 경로를 확인하십시오.";
    renderTiles();
  });
})();
