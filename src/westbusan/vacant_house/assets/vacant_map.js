(() => {
  "use strict";
  const mapElement = document.getElementById("vacant-slippy-map");
  const hubCandidateList = document.getElementById("hub-candidate-list");
  const standaloneCandidateList = document.getElementById("standalone-candidate-list");
  const districtFilter = document.getElementById("district-filter");
  const WEST_DISTRICTS = ["강서구", "북구", "사상구", "사하구"];
  const guide = document.getElementById("zoom-guide");
  const map = L.map(mapElement, {
    minZoom: Number(mapElement.dataset.minZoom),
    maxZoom: Number(mapElement.dataset.maxZoom),
  }).setView(
    mapElement.dataset.mapCenter.split(",").reverse().map(Number),
    Number(mapElement.dataset.mapZoom),
  );
  L.tileLayer(mapElement.dataset.tileTemplate, {
    minZoom: Number(mapElement.dataset.minZoom),
    maxZoom: Number(mapElement.dataset.maxZoom),
    attribution: "국토교통부 VWorld",
  }).addTo(map);

  const data = {
    hubs: [], standalone: [], parcels: [], houses: [], access: [], summary: null,
  };
  const layers = {
    hubs: L.layerGroup().addTo(map),
    standalone: L.layerGroup().addTo(map),
    parcels: L.layerGroup(),
    houses: L.layerGroup(),
    transport: L.layerGroup().addTo(map),
    tourismPois: L.layerGroup().addTo(map),
  };
  const featureLayers = {
    hubs: new Map(), standalone: new Map(),
    parcels: new Map(), houses: new Map(),
  };
  const contextLabels = {
    district_visitor_demand: "자치구 방문수요",
    nearby_attractions: "인근 관광지",
    station_proximity: "역 접근성",
    transport_flow: "교통량",
  };
  let selected = null;

  function candidateAccess(identifier) {
    return data.access.find((item) => item.properties.kind === "candidate_accessibility"
      && item.properties.candidate_id === identifier)?.properties || null;
  }
  function tourismWeight(feature) {
    const type = String(feature.properties.content_type_id
      || feature.properties.category_name || feature.properties.category_code || "");
    if (["12", "14", "15", "25", "28"].includes(type)) return 1;
    if (["38", "39"].includes(type)) return 0.35;
    return 0;
  }
  function haversineMetres(left, right) {
    const radians = (value) => value * Math.PI / 180;
    const earthRadius = 6371008.8;
    const latitudeDelta = radians(right[1] - left[1]);
    const longitudeDelta = radians(right[0] - left[0]);
    const a = Math.sin(latitudeDelta / 2) ** 2
      + Math.cos(radians(left[1])) * Math.cos(radians(right[1]))
      * Math.sin(longitudeDelta / 2) ** 2;
    return earthRadius * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }
  function geometryCoordinate(feature) {
    if (feature.geometry?.type === "Point") return feature.geometry.coordinates.map(Number);
    const coordinates = [];
    const collect = (value) => {
      if (Array.isArray(value) && value.length >= 2
        && Number.isFinite(Number(value[0])) && Number.isFinite(Number(value[1]))) {
        coordinates.push([Number(value[0]), Number(value[1])]);
      } else if (Array.isArray(value)) value.forEach(collect);
    };
    collect(feature.geometry?.coordinates || []);
    if (!coordinates.length) return null;
    return [
      (Math.min(...coordinates.map((item) => item[0])) + Math.max(...coordinates.map((item) => item[0]))) / 2,
      (Math.min(...coordinates.map((item) => item[1])) + Math.max(...coordinates.map((item) => item[1]))) / 2,
    ];
  }
  function accessibilityForFeature(feature, identifier) {
    const direct = candidateAccess(identifier);
    const properties = feature.properties || {};
    const coordinate = geometryCoordinate(feature);
    const pois = coordinate ? data.access.filter((item) => item.properties.kind === "tourism_poi"
      && tourismWeight(item) > 0
      && item.geometry?.type === "Point").map((item) => ({
        feature: item,
        distance: haversineMetres(coordinate, item.geometry.coordinates.map(Number)),
      })).sort((a, b) => a.distance - b.distance) : [];
    const nearest = pois[0] || null;
    const transport = data.access.filter((item) => {
      const context = item.properties || {};
      return context.kind === "transport_dong"
        && context.district_name === properties.district_name
        && context.dong_name === properties.dong_name;
    }).sort((a, b) => String(b.properties.period || "").localeCompare(String(a.properties.period || "")))[0];
    const transportInbound = properties.transport_inbound ?? direct?.transport_inbound ?? (transport
      ? Number(transport.properties.inbound_other_district || transport.properties.inbound_other_dong || 0)
      : null);
    const nearestPoiName = properties.nearest_tourism_poi_name
      || direct?.nearest_tourism_poi_name || nearest?.feature.properties.title || null;
    const nearestPoiDistance = properties.nearest_tourism_poi_distance_m
      ?? direct?.nearest_tourism_poi_distance_m ?? nearest?.distance ?? null;
    const poiCount1000m = properties.tourism_poi_count_1000m
      ?? direct?.tourism_poi_count_1000m ?? pois.filter((item) => item.distance <= 1000).length;
    return {
      transportInbound,
      transportPeriod: properties.transport_period
        || direct?.transport_period || transport?.properties.period || null,
      nearestPoiName,
      nearestPoiDistance,
      poiCount1000m,
      hasTransport: transportInbound !== null,
      hasTourism: poiCount1000m > 0,
      rankingEligible: Boolean(properties.ranking_eligible ?? direct?.ranking_eligible),
      weightedScore: properties.weighted_score ?? null,
      parcelScore: properties.parcel_score ?? null,
      transportScore: properties.transport_score ?? null,
      tourismScore: properties.tourism_score ?? null,
      visitorScore: properties.visitor_score ?? null,
    };
  }
  function accessScoreBreakdown(feature, identifier) {
    const access = accessibilityForFeature(feature, identifier);
    if (!access.rankingEligible || access.weightedScore === null) {
      return "접근성 보완순위 미적용(필수 자료 미결합)";
    }
    return (
      `접근성 보완점수 ${Number(access.weightedScore).toFixed(1)}점`
      + ` · 필지 45% ${Number(access.parcelScore).toFixed(1)}점`
      + ` · 교통 20% ${Number(access.transportScore).toFixed(1)}점`
      + ` · 관광 20% ${Number(access.tourismScore).toFixed(1)}점`
      + ` · 방문수요 15% ${Number(access.visitorScore).toFixed(1)}점`
    );
  }
  function accessibilityText(feature, identifier) {
    const access = accessibilityForFeature(feature, identifier);
    const parts = [];
    if (access.transportInbound !== null) parts.push(`동 단위 타 자치구 대중교통 유입 ${Number(access.transportInbound).toLocaleString("ko-KR")}통행 (${access.transportPeriod})`);
    if (access.nearestPoiName) parts.push(`1km 내 관광지 ${Number(access.poiCount1000m).toLocaleString("ko-KR")}개`);
    if (access.nearestPoiName) parts.push(`최근접 관광지 ${access.nearestPoiName} ${Math.round(access.nearestPoiDistance).toLocaleString("ko-KR")}m`);
    if (access.hasTransport && access.hasTourism) parts.push("1km 내 관광지와 교통유입 신호가 함께 확인되어 사업성 추가검토 가치가 있음");
    else if (access.hasTransport || access.hasTourism) parts.push("접근성 신호가 일부 확인되어 추가 자료와 함께 사업성을 검토할 필요가 있음");
    else parts.push("교통·관광지 접근성 자료 미결합으로 사업성 신호를 판단할 수 없음");
    if (!access.rankingEligible) parts.push("현재 후보순위에는 접근성 근거가 완전 반영되지 않음");
    else parts.push(accessScoreBreakdown(feature, identifier));
    return parts.join(" · ");
  }
  function renderAccessibility(feature, identifier) {
    const access = accessibilityForFeature(feature, identifier);
    document.getElementById("detail-nearest-poi").textContent = access.nearestPoiName
      ? `${access.nearestPoiName} · ${Math.round(access.nearestPoiDistance).toLocaleString("ko-KR")}m`
      : "자료 없음";
    document.getElementById("detail-poi-count").textContent = access.nearestPoiName
      ? `1km 내 관광지 ${Number(access.poiCount1000m).toLocaleString("ko-KR")}개`
      : "관광지 자료 미결합";
    document.getElementById("detail-transport-inflow").textContent = access.transportInbound === null
      ? "자료 없음"
      : `${Number(access.transportInbound).toLocaleString("ko-KR")}통행`;
    document.getElementById("detail-transport-period").textContent = access.transportPeriod
      ? `${access.transportPeriod} · 타 자치구 유입`
      : "교통 자료 미결합";
    document.getElementById("detail-access-signal").textContent = access.hasTransport && access.hasTourism
      ? "추가검토 가치 있음"
      : access.hasTransport || access.hasTourism ? "보완검토" : "판단 보류";
    return access;
  }

  function textElement(tagName, text, className = "") {
    const element = document.createElement(tagName);
    element.className = className;
    element.textContent = text;
    return element;
  }
  function featureMatches(feature) {
    const districts = feature.properties.district_names
      || [feature.properties.district_name];
    return !districtFilter.value || districts.includes(districtFilter.value);
  }
  function selectionKey(kind, identifier) {
    return `${kind}:${identifier}`;
  }
  function isSelected(kind, identifier) {
    return selected === selectionKey(kind, identifier);
  }
  function candidateHouses(kind, feature) {
    if (kind === "hub") {
      return data.houses.filter(
        (item) => item.properties.hub_id === feature.properties.hub_id,
      );
    }
    return data.houses.filter(
      (item) => item.properties.pnu === feature.properties.pnu,
    );
  }
  function houseDetailNode(feature) {
    const properties = feature.properties;
    const wrapper = document.createElement("div");
    wrapper.append(
      textElement(
        "strong",
        properties.exact_address || properties.road_address || "주소 미확인",
      ),
      textElement("p", [
        properties.housing_type || "주택유형 미확인",
        properties.construction_year
          ? `${properties.construction_year}년`
          : "건축연도 미확인",
        properties.vacant_grade
          ? `${properties.vacant_grade}등급`
          : "등급 미확인",
      ].join(" · ")),
    );
    return wrapper;
  }
  function updateSelectionStyles() {
    document.querySelectorAll(".candidate-list button").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.selection === selected);
    });
    featureLayers.hubs.forEach((entry, hubId) => entry.shape.setStyle({
      fillOpacity: isSelected("hub", hubId) ? 0.42 : 0.2,
      weight: isSelected("hub", hubId) ? 4 : 2,
    }));
    featureLayers.standalone.forEach((entry, candidateId) => entry.shape.setStyle({
      fillOpacity: isSelected("standalone", candidateId) ? 0.52 : 0.24,
      weight: isSelected("standalone", candidateId) ? 4 : 2,
    }));
  }
  function renderHouseCards(houses) {
    const list = document.getElementById("house-list");
    list.replaceChildren();
    houses.forEach((house) => {
      const card = document.createElement("article");
      card.append(
        textElement(
          "strong",
          house.properties.exact_address
            || house.properties.road_address
            || "주소 미확인",
        ),
        textElement("span", [
          house.properties.housing_type || "주택유형 미확인",
          house.properties.construction_year
            ? `${house.properties.construction_year}년`
            : "건축연도 미확인",
          house.properties.vacant_grade
            ? `${house.properties.vacant_grade}등급`
            : "등급 미확인",
        ].join(" · ")),
        textElement("span", parcelPlanningText(house.properties)),
      );
      list.append(card);
    });
  }
  function parcelPlanningText(properties) {
    const planning = [
      properties.land_use_zone,
      properties.land_use_district,
      properties.land_use_area,
    ].filter(Boolean).join(" · ") || "용도지역·지구 미확인";
    const parcelArea = properties.parcel_area === null
      || properties.parcel_area === undefined
      ? "필지면적 미확인"
      : `필지 ${Number(properties.parcel_area).toLocaleString("ko-KR")}㎡`;
    const road = properties.road_side
      ? `도로접면 ${properties.road_side}`
      : "도로접면 미확인";
    const terrain = [properties.terrain_height, properties.terrain_shape]
      .filter(Boolean).join("·") || "지형 미확인";
    const use = properties.land_use_situation
      ? `토지이용 ${properties.land_use_situation}`
      : "토지이용상황 미확인";
    return `${planning} · ${properties.land_category || "지목 미확인"} · ${parcelArea} · ${road} · ${terrain} · ${use}`;
  }
  function selectHouse(feature) {
    const properties = feature.properties;
    selected = selectionKey("house", properties.record_id);
    updateSelectionStyles();
    const area = Number(properties.land_area || 0);
    const address = properties.exact_address
      || properties.road_address
      || `${properties.district_name} ${properties.dong_name}`;
    document.getElementById("detail-title").textContent = address;
    document.getElementById("detail-summary").textContent = (
      "개발후보와 별도로 게시된 일반 빈집 전수현황입니다."
    );
    document.getElementById("detail-type").textContent = "일반 빈집";
    document.getElementById("detail-rank").textContent = "미선정";
    document.getElementById("detail-parcels").textContent = "1필지";
    document.getElementById("detail-houses").textContent = "1개소";
    document.getElementById("detail-area").textContent = area > 0
      ? `${area.toLocaleString("ko-KR")}㎡(원천)`
      : "자료 없음";
    renderAccessibility(feature, properties.record_id);
    document.getElementById("detail-evidence").textContent = (
      `PNU ${properties.pnu || "미확인"} · 건물면적 ${properties.building_area || "자료 없음"}㎡. `
      + `${parcelPlanningText(properties)}. `
      + "현재 A형 연속필지군 또는 B형 자치구별 상위 5개에 포함되지 않은 빈집입니다. "
      + "B형은 검증 지적면적 300㎡ 이상 단독주택형 가운데 자치구별로 최대 5개를 게시합니다. "
      + `${accessibilityText(feature, properties.record_id)}. `
      + "접근성은 사업성 추가검토 신호이며 수익성·인허가의 확정 판정이 아닙니다."
    );
    renderHouseCards([feature]);
  }
  function selectHub(feature) {
    const properties = feature.properties;
    selected = selectionKey("hub", properties.hub_id);
    updateSelectionStyles();
    const houses = candidateHouses("hub", feature);
    document.getElementById("detail-title").textContent = (
      `${properties.district_names.join("·")} ${properties.dong_names.join("·") || "연속필지군"}`
    );
    document.getElementById("detail-summary").textContent = (
      `${properties.parcel_count}개 빈집 필지가 경계를 맞댄 A형 거점개발 후보입니다. ${accessScoreBreakdown(feature, properties.hub_id)}`
    );
    document.getElementById("detail-type").textContent = "A형";
    document.getElementById("detail-rank").textContent = (
      `A${properties.candidate_rank}`
    );
    document.getElementById("detail-parcels").textContent = (
      `${properties.parcel_count}필지`
    );
    document.getElementById("detail-houses").textContent = `${houses.length}개소`;
    document.getElementById("detail-area").textContent = (
      `${Math.round(properties.union_area).toLocaleString("ko-KR")}㎡`
    );
    renderAccessibility(feature, properties.hub_id);
    document.getElementById("detail-evidence").textContent = (
      `지적필지 경계 접촉이 확인된 물리적 연속필지군입니다. 용도지역 ${properties.land_use_zones.join("·") || "미확인"}, 용도지구 ${properties.land_use_districts.join("·") || "미확인"}, 도로접면 ${properties.road_sides.join("·") || "미확인"}입니다. ${accessibilityText(feature, properties.hub_id)}. 소유권·구조안전·소방·주차와 수익성은 별도 검토 대상입니다.`
    );
    renderHouseCards(houses);
    const entry = featureLayers.hubs.get(properties.hub_id);
    entry.shape.bringToFront();
    map.fitBounds(entry.shape.getBounds(), { padding: [55, 55], maxZoom: 17 });
  }
  function selectStandalone(feature) {
    const properties = feature.properties;
    selected = selectionKey("standalone", properties.candidate_id);
    updateSelectionStyles();
    const houses = candidateHouses("standalone", feature);
    const demand = properties.district_demand_score === null
      ? "자치구 방문수요 자료 미결합"
      : `자치구 방문수요 점수 ${Number(properties.district_demand_score).toFixed(1)}점`;
    const access = renderAccessibility(feature, properties.candidate_id);
    const resolved = new Set([
      ...(access.hasTourism ? ["nearby_attractions"] : []),
      ...(access.hasTransport ? ["transport_access", "station_proximity", "transport_flow"] : []),
    ]);
    const gaps = properties.missing_context.filter((code) => !resolved.has(code)).map(
      (code) => `${contextLabels[code] || code} 자료 미결합`,
    );
    document.getElementById("detail-title").textContent = (
      `${properties.district_name} ${properties.dong_name || "단일필지"}`
    );
    document.getElementById("detail-summary").textContent = (
      `연속필지군은 아니지만 검증 지적면적 300㎡ 이상인 단독주택형 B형 예비후보입니다. ${accessScoreBreakdown(feature, properties.candidate_id)}`
    );
    document.getElementById("detail-type").textContent = "B형";
    document.getElementById("detail-rank").textContent = (
      `B${properties.preliminary_rank} 예비`
    );
    document.getElementById("detail-parcels").textContent = "1필지";
    document.getElementById("detail-houses").textContent = `${houses.length}개소`;
    document.getElementById("detail-area").textContent = (
      `${Math.round(properties.parcel_area).toLocaleString("ko-KR")}㎡`
    );
    document.getElementById("detail-evidence").textContent = (
      `${demand}. ${parcelPlanningText(properties)}. ${gaps.length ? `${gaps.join(" · ")}. ` : ""}${accessibilityText(feature, properties.candidate_id)}. B형 번호는 최종 투자순위가 아니라 현재 가용근거 기준의 예비검토 순서입니다.`
    );
    renderHouseCards(houses);
    const entry = featureLayers.standalone.get(properties.candidate_id);
    entry.shape.bringToFront();
    map.fitBounds(entry.shape.getBounds(), { padding: [65, 65], maxZoom: 18 });
  }
  function candidateButton(kind, feature) {
    const properties = feature.properties;
    const isHub = kind === "hub";
    const identifier = isHub ? properties.hub_id : properties.candidate_id;
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.selection = selectionKey(kind, identifier);
    if (isSelected(kind, identifier)) button.classList.add("is-active");
    const label = document.createElement("span");
    const area = isHub ? properties.union_area : properties.parcel_area;
    const place = isHub
      ? `${properties.district_names.join("·")} ${properties.dong_names.join("·") || "후보지"}`
      : `${properties.district_name} ${properties.dong_name || "단일필지"}`;
    const baseEvidence = isHub
      ? `${properties.parcel_count}개 연속필지 · ${Math.round(area).toLocaleString("ko-KR")}㎡`
      : `단독주택형 · ${Math.round(area).toLocaleString("ko-KR")}㎡ · 자치구 내 예비 ${properties.preliminary_rank}순위`;
    const access = accessibilityForFeature(feature, identifier);
    const accessEvidence = access.hasTransport && access.hasTourism
      ? ` · 관광·교통 근거 확인`
      : access.hasTransport || access.hasTourism ? " · 접근성 일부 확인" : " · 접근성 보완 필요";
    const scoreEvidence = access.rankingEligible && access.weightedScore !== null
      ? ` · 보완점수 ${Number(access.weightedScore).toFixed(1)}점`
      : "";
    const evidence = `${baseEvidence}${accessEvidence}${scoreEvidence}`;
    label.append(textElement("strong", place), textElement("small", evidence));
    const markerLabel = isHub
      ? `A${Number(feature.properties.candidate_rank)}`
      : `B${Number(feature.properties.preliminary_rank)}`;
    button.append(textElement("b", markerLabel), label);
    button.addEventListener("click", () => {
      if (isHub) selectHub(feature);
      else selectStandalone(feature);
    });
    item.append(button);
    return item;
  }
  function renderCandidates() {
    hubCandidateList.replaceChildren();
    standaloneCandidateList.replaceChildren();
    const hubs = data.hubs.filter(featureMatches);
    const standalone = data.standalone.filter(featureMatches);
    hubs.forEach((feature) => hubCandidateList.append(candidateButton("hub", feature)));
    standalone.forEach((feature) => (
      standaloneCandidateList.append(candidateButton("standalone", feature))
    ));
    document.getElementById("hub-candidate-count").textContent = `${hubs.length}개`;
    document.getElementById("standalone-candidate-count").textContent = (
      `${standalone.length}개`
    );
    renderInventoryStatus();
  }
  function renderInventoryStatus() {
    if (!data.summary) return;
    const district = districtFilter.value;
    const houseCount = district
      ? data.summary.district_house_counts[district]
      : data.summary.exact_location_count;
    const parcelCount = district
      ? data.summary.district_parcel_counts[district]
      : data.summary.distinct_parcel_count;
    document.getElementById("inventory-house-count").textContent = (
      `${Number(houseCount || 0).toLocaleString("ko-KR")}개소`
    );
    document.getElementById("inventory-parcel-count").textContent = (
      `${Number(parcelCount || 0).toLocaleString("ko-KR")}필지`
    );
    const counts = district
      ? data.summary.district_candidate_counts[district]
      : {
        contiguous_hubs: data.summary.candidate_count,
        standalone_candidates: data.summary.standalone_candidate_count,
        supplemental_candidates: 0,
      };
    const noPrimaryCandidates = (
      "연속필지 개발후보 0개 / 현재 게시된 단독개발 상위후보 0개"
    );
    const primaryStatus = counts.contiguous_hubs === 0
      && counts.standalone_candidates === 0
      ? noPrimaryCandidates
      : `연속필지 개발후보 ${counts.contiguous_hubs}개 / 현재 게시된 단독개발 상위후보 ${counts.standalone_candidates}개`;
    const reason = district && counts.contiguous_hubs === 0
      ? " 연속필지 후보는 3개 이상 지적필지의 경계 접촉 기준을 충족하지 못해 미선정되었습니다."
      : "";
    document.getElementById("district-candidate-status").textContent = (
      `${district || "서부산 전체"}: ${primaryStatus}.${reason}`
    );
  }
  function refreshLayers() {
    Object.values(layers).forEach((layer) => layer.clearLayers());
    Object.values(featureLayers).forEach((entries) => entries.clear());
    data.hubs.filter(featureMatches).forEach((feature) => {
      const shape = L.geoJSON(feature, { style: {
        color: "#105bd1", weight: 2, fillColor: "#146cff", fillOpacity: 0.2,
      } }).on("click", () => selectHub(feature));
      const marker = L.marker(shape.getBounds().getCenter(), { icon: L.divIcon({
        className: "candidate-marker",
        html: `<span>A${Number(feature.properties.candidate_rank)}</span>`,
        iconSize: [40, 40], iconAnchor: [20, 20],
      }) }).on("click", () => selectHub(feature));
      shape.addTo(layers.hubs); marker.addTo(layers.hubs);
      featureLayers.hubs.set(feature.properties.hub_id, { shape, marker });
    });
    data.standalone.filter(featureMatches).forEach((feature) => {
      const shape = L.geoJSON(feature, { style: {
        color: "#9b6400", weight: 2, fillColor: "#e5a61f", fillOpacity: 0.24,
      } }).on("click", () => selectStandalone(feature));
      const marker = L.marker(shape.getBounds().getCenter(), { icon: L.divIcon({
        className: "standalone-marker",
        html: `<span>B${Number(feature.properties.preliminary_rank)}</span>`,
        iconSize: [38, 38], iconAnchor: [19, 19],
      }) }).on("click", () => selectStandalone(feature));
      shape.addTo(layers.standalone); marker.addTo(layers.standalone);
      featureLayers.standalone.set(
        feature.properties.candidate_id, { shape, marker },
      );
    });
    data.parcels.filter(featureMatches).forEach((feature) => {
      const standalone = data.standalone.find(
        (item) => item.properties.pnu === feature.properties.pnu,
      );
      const shape = L.geoJSON(feature, { style: {
        color: standalone ? "#9b6400" : "#a64613",
        weight: 2,
        fillColor: standalone ? "#e5a61f" : "#ed7d31",
        fillOpacity: 0.5,
      } });
      shape.on("click", () => {
        const hub = data.hubs.find(
          (item) => item.properties.hub_id === feature.properties.hub_id,
        );
        if (hub) selectHub(hub);
        else if (standalone) selectStandalone(standalone);
        else {
          const house = data.houses.find(
            (item) => item.properties.pnu === feature.properties.pnu,
          );
          if (house) selectHouse(house);
        }
      });
      shape.addTo(layers.parcels);
      featureLayers.parcels.set(feature.properties.pnu, shape);
    });
    data.houses.filter(featureMatches).forEach((feature) => {
      const marker = L.circleMarker(
        [feature.geometry.coordinates[1], feature.geometry.coordinates[0]],
        {
          radius: 7, color: "#fff", weight: 2,
          fillColor: "#d9475a", fillOpacity: 1,
        },
      ).bindPopup(houseDetailNode(feature)).on("click", () => selectHouse(feature));
      marker.addTo(layers.houses);
      featureLayers.houses.set(feature.properties.record_id, marker);
    });
    const visibleHouses = data.houses.filter(featureMatches);
    data.access.filter((feature) => feature.properties.kind === "tourism_poi" && featureMatches(feature)).forEach((feature) => {
      const [longitude, latitude] = feature.geometry.coordinates;
      const popup = document.createElement("div");
      const title = document.createElement("strong");
      const type = document.createElement("div");
      const location = document.createElement("small");
      title.textContent = feature.properties.title || "관광정보";
      type.textContent = `유형: ${feature.properties.content_type_name || "유형 미확인"}`;
      location.textContent = [feature.properties.district_name, feature.properties.dong_name].filter(Boolean).join(" ") || "소재지역 미확인";
      popup.append(title, type, location);
      L.marker([latitude, longitude], { icon: L.divIcon({
        className: "tourism-poi-icon", html: "관", iconSize: [28, 28], iconAnchor: [14, 14],
      }) }).bindPopup(popup).addTo(layers.tourismPois);
    });
    data.access.filter((feature) => feature.properties.kind === "transport_dong" && featureMatches(feature)).forEach((feature) => {
      const matching = visibleHouses.filter((house) => house.properties.dong_name === feature.properties.dong_name);
      if (!matching.length) return;
      const latitude = matching.reduce((sum, house) => sum + Number(house.geometry.coordinates[1]), 0) / matching.length;
      const longitude = matching.reduce((sum, house) => sum + Number(house.geometry.coordinates[0]), 0) / matching.length;
      const value = Number(feature.properties.inbound_other_district || feature.properties.inbound_other_dong || 0);
      L.circleMarker([latitude, longitude], {
        radius: Math.max(7, Math.min(24, 7 + Math.sqrt(value) / 6)),
        color: "#8e0152", weight: 2, fillColor: "#d95f72", fillOpacity: .35,
      }).bindPopup(`<strong>${feature.properties.district_name} ${feature.properties.dong_name}</strong><br>타 자치구 대중교통 유입 ${value.toLocaleString("ko-KR")}통행<br><small>관광객 수가 아니며 통행 목적·중복 이용자를 구분하지 못함</small>`).addTo(layers.transport);
    });
    updateSelectionStyles(); updateLayerVisibility(); renderCandidates();
  }
  function updateLayerVisibility() {
    const zoom = map.getZoom();
    if (!map.hasLayer(layers.hubs)) layers.hubs.addTo(map);
    if (!map.hasLayer(layers.standalone)) layers.standalone.addTo(map);
    if (zoom >= 14 && !map.hasLayer(layers.parcels)) layers.parcels.addTo(map);
    if (zoom < 14 && map.hasLayer(layers.parcels)) map.removeLayer(layers.parcels);
    const showHouses = zoom >= 17 || Boolean(districtFilter.value);
    if (showHouses && !map.hasLayer(layers.houses)) layers.houses.addTo(map);
    if (!showHouses && map.hasLayer(layers.houses)) map.removeLayer(layers.houses);
    featureLayers.houses.forEach((marker) => marker.setRadius(zoom >= 17 ? 7 : 4));
    mapElement.classList.toggle("parcel-detail-mode", zoom >= 14);
    mapElement.classList.toggle("street-detail-mode", zoom >= 17);
    [
      featureLayers.hubs, featureLayers.standalone,
    ].forEach((entries) => {
      entries.forEach((entry) => {
        const layer = entries === featureLayers.hubs
          ? layers.hubs
          : entries === featureLayers.standalone
            ? layers.standalone
            : layers.standalone;
        if (zoom >= 17) layer.removeLayer(entry.marker);
        else if (!layer.hasLayer(entry.marker)) layer.addLayer(entry.marker);
      });
    });
    guide.textContent = zoom >= 17
      ? "개별 빈집 점을 누르면 정확주소·주택유형·건축연도를 확인합니다."
      : districtFilter.value
        ? `${districtFilter.value} 빈집 전수 위치를 표시합니다. 확대하면 지적필지 경계를 확인할 수 있습니다.`
      : zoom >= 14
        ? "지적필지 경계가 표시됩니다. 더 확대하면 개별 빈집 위치가 보입니다."
        : "A형 또는 B형 후보를 선택하면 해당 필지로 확대됩니다.";
  }
  function visibleCandidateFeatures() {
    return [...data.hubs, ...data.standalone].filter(featureMatches);
  }
  function fitVisibleCandidates(maxZoom = 12) {
    const visible = visibleCandidateFeatures();
    if (!visible.length) return;
    const bounds = L.latLngBounds(visible.flatMap((feature) => {
      const featureBounds = L.geoJSON(feature).getBounds();
      return [featureBounds.getSouthWest(), featureBounds.getNorthEast()];
    }));
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [35, 35], maxZoom });
  }
  function fitVisibleInventory(maxZoom = 13) {
    const visible = data.parcels.filter(featureMatches);
    if (!visible.length) return;
    const bounds = L.latLngBounds(visible.flatMap((feature) => {
      const featureBounds = L.geoJSON(feature).getBounds();
      return [featureBounds.getSouthWest(), featureBounds.getNorthEast()];
    }));
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [35, 35], maxZoom });
  }
  map.on("zoomend", updateLayerVisibility);
  districtFilter.addEventListener("change", () => {
    selected = null;
    refreshLayers();
    if (districtFilter.value) fitVisibleInventory(13);
    else fitVisibleCandidates(12);
  });
  document.querySelectorAll("[data-layer]").forEach((control) => control.addEventListener("change", () => {
    const layer = control.dataset.layer === "transport_inflow" ? layers.transport : layers.tourismPois;
    if (control.checked && !map.hasLayer(layer)) layer.addTo(map);
    if (!control.checked && map.hasLayer(layer)) map.removeLayer(layer);
  }));
  document.getElementById("address-analysis-form").addEventListener(
    "submit",
    async (event) => {
      event.preventDefault();
      const result = document.getElementById("address-analysis-result");
      result.hidden = false;
      result.textContent = "현재 게시 빈집·연속필지군을 확인하고 있습니다.";
      try {
        const response = await fetch("/tourism/api/vacant/address-analysis", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            address: document.getElementById("address-input").value,
          }),
        });
        if (!response.ok) throw new Error("address_analysis_failed");
        const analysis = await response.json();
        result.textContent = (
          `${analysis.interpretation}${analysis.hub_rank ? ` 후보 ${analysis.hub_rank}위, ${analysis.hub_parcel_count}개 연속필지군입니다.` : ""} ${analysis.limitation}`
        );
      } catch (_) {
        result.textContent = (
          "주소 분석을 불러오지 못했습니다. 주소 형식과 서버 상태를 확인해 주세요."
        );
      }
    },
  );
  Promise.all(
    [
      "hubs.geojson",
      "standalone-candidates.geojson",
      "bukgu-supplemental-candidates.geojson",
      "parcels.geojson",
      "vacant-houses.geojson",
      "accessibility-context.geojson",
      "summary.json",
    ].map((url) => fetch(url).then((response) => {
      if (!response.ok) throw new Error("map_data_failed");
      return response.json();
    })),
  ).then(([hubs, standalone, _supplemental, parcels, houses, access, summary]) => {
    data.hubs = hubs.features;
    data.standalone = standalone.features;
    data.parcels = parcels.features;
    data.houses = houses.features;
    data.access = access.features;
    data.summary = summary;
    WEST_DISTRICTS.forEach(
      (district) => districtFilter.add(new Option(district, district)),
    );
    refreshLayers(); fitVisibleCandidates(12);
  }).catch(() => {
    hubCandidateList.textContent = "게시 지도자료를 불러오지 못했습니다.";
    standaloneCandidateList.textContent = "게시 지도자료를 불러오지 못했습니다.";
  });
})();
