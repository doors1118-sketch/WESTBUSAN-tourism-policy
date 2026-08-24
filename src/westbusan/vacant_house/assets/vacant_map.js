(() => {
  "use strict";
  const mapElement = document.getElementById("vacant-slippy-map");
  const hubCandidateList = document.getElementById("hub-candidate-list");
  const standaloneCandidateList = document.getElementById("standalone-candidate-list");
  const supplementalCandidateList = document.getElementById("supplemental-candidate-list");
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
    hubs: [], standalone: [], supplemental: [], parcels: [], houses: [], summary: null,
  };
  const layers = {
    hubs: L.layerGroup().addTo(map),
    standalone: L.layerGroup().addTo(map),
    supplemental: L.layerGroup().addTo(map),
    parcels: L.layerGroup(),
    houses: L.layerGroup(),
  };
  const featureLayers = {
    hubs: new Map(), standalone: new Map(), supplemental: new Map(),
    parcels: new Map(), houses: new Map(),
  };
  const contextLabels = {
    district_visitor_demand: "자치구 방문수요",
    nearby_attractions: "인근 관광지",
    station_proximity: "역 접근성",
    transport_flow: "교통량",
  };
  let selected = null;

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
    featureLayers.supplemental.forEach((entry, candidateId) => entry.shape.setStyle({
      fillOpacity: isSelected("supplemental", candidateId) ? 0.56 : 0.28,
      weight: isSelected("supplemental", candidateId) ? 4 : 2,
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
      );
      list.append(card);
    });
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
      `${properties.parcel_count}개 빈집 필지가 경계를 맞댄 A형 거점개발 후보입니다.`
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
    document.getElementById("detail-evidence").textContent = (
      "지적필지 경계 접촉이 확인된 물리적 연속필지군입니다. 소유권·접도·용도지역·구조안전·소방·주차와 사업성은 별도 검토 대상입니다."
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
    const gaps = properties.missing_context.map(
      (code) => `${contextLabels[code] || code} 자료 미결합`,
    );
    document.getElementById("detail-title").textContent = (
      `${properties.district_name} ${properties.dong_name || "단일필지"}`
    );
    document.getElementById("detail-summary").textContent = (
      "연속필지군은 아니지만 검증 지적면적 300㎡ 이상인 단독주택형 B형 예비후보입니다."
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
      `${demand}. ${gaps.join(" · ")}. B형 번호는 최종 투자순위가 아니라 현재 가용근거 기준의 예비검토 순서입니다.`
    );
    renderHouseCards(houses);
    const entry = featureLayers.standalone.get(properties.candidate_id);
    entry.shape.bringToFront();
    map.fitBounds(entry.shape.getBounds(), { padding: [65, 65], maxZoom: 18 });
  }
  function selectSupplemental(feature) {
    const properties = feature.properties;
    selected = selectionKey("supplemental", properties.candidate_id);
    updateSelectionStyles();
    const houses = candidateHouses("supplemental", feature);
    document.getElementById("detail-title").textContent = (
      `${properties.district_name} ${properties.dong_name || "단일필지"}`
    );
    document.getElementById("detail-summary").textContent = (
      "북구의 300㎡ 이상 단독주택형 비연속 필지에 역·관광지 접근성과 자치구 방문수요를 결합한 C형 보완검토 후보입니다."
    );
    document.getElementById("detail-type").textContent = "C형";
    document.getElementById("detail-rank").textContent = (
      `C${properties.preliminary_rank} 예비`
    );
    document.getElementById("detail-parcels").textContent = "1필지";
    document.getElementById("detail-houses").textContent = `${houses.length}개소`;
    document.getElementById("detail-area").textContent = (
      `${Math.round(properties.parcel_area).toLocaleString("ko-KR")}㎡`
    );
    document.getElementById("detail-evidence").textContent = (
      `종합점수 ${Number(properties.composite_score).toFixed(1)}점 · ${properties.nearest_station} 직선거리 ${Math.round(properties.station_distance_metres).toLocaleString("ko-KR")}m · ${properties.nearest_attraction} 직선거리 ${Math.round(properties.attraction_distance_metres).toLocaleString("ko-KR")}m · 자치구 방문수요 ${Number(properties.district_demand_score).toFixed(1)}점. ${properties.limitation}`
    );
    renderHouseCards(houses);
    const entry = featureLayers.supplemental.get(properties.candidate_id);
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
    const evidence = isHub
      ? `${properties.parcel_count}개 연속필지 · ${Math.round(area).toLocaleString("ko-KR")}㎡`
      : kind === "supplemental"
        ? `${properties.nearest_station} ${Math.round(properties.station_distance_metres).toLocaleString("ko-KR")}m · ${Math.round(area).toLocaleString("ko-KR")}㎡`
        : `단독주택형 · ${Math.round(area).toLocaleString("ko-KR")}㎡ · 예비검토`;
    label.append(textElement("strong", place), textElement("small", evidence));
    const markerLabel = isHub
      ? `A${Number(feature.properties.candidate_rank)}`
      : kind === "supplemental"
        ? `C${Number(feature.properties.preliminary_rank)}`
        : `B${Number(feature.properties.preliminary_rank)}`;
    button.append(textElement("b", markerLabel), label);
    button.addEventListener("click", () => {
      if (isHub) selectHub(feature);
      else if (kind === "supplemental") selectSupplemental(feature);
      else selectStandalone(feature);
    });
    item.append(button);
    return item;
  }
  function renderCandidates() {
    hubCandidateList.replaceChildren();
    standaloneCandidateList.replaceChildren();
    supplementalCandidateList.replaceChildren();
    const hubs = data.hubs.filter(featureMatches);
    const standalone = data.standalone.filter(featureMatches);
    const supplemental = data.supplemental.filter(featureMatches);
    hubs.forEach((feature) => hubCandidateList.append(candidateButton("hub", feature)));
    standalone.forEach((feature) => (
      standaloneCandidateList.append(candidateButton("standalone", feature))
    ));
    supplemental.forEach((feature) => (
      supplementalCandidateList.append(candidateButton("supplemental", feature))
    ));
    document.getElementById("hub-candidate-count").textContent = `${hubs.length}개`;
    document.getElementById("standalone-candidate-count").textContent = (
      `${standalone.length}개`
    );
    document.getElementById("supplemental-candidate-count").textContent = (
      `${supplemental.length}개`
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
        supplemental_candidates: data.summary.bukgu_supplemental_candidate_count,
      };
    const noPrimaryCandidates = (
      "연속필지 개발후보 0개 / 현재 게시된 단독개발 상위후보 0개"
    );
    const primaryStatus = counts.contiguous_hubs === 0
      && counts.standalone_candidates === 0
      ? noPrimaryCandidates
      : `연속필지 개발후보 ${counts.contiguous_hubs}개 / 현재 게시된 단독개발 상위후보 ${counts.standalone_candidates}개`;
    const supplementStatus = counts.supplemental_candidates
      ? ` / 북구 보완검토 후보 ${counts.supplemental_candidates}개`
      : "";
    const reason = district && counts.contiguous_hubs === 0
      ? " 연속필지 후보는 3개 이상 지적필지의 경계 접촉 기준을 충족하지 못해 미선정되었습니다."
      : "";
    document.getElementById("district-candidate-status").textContent = (
      `${district || "서부산 전체"}: ${primaryStatus}${supplementStatus}.${reason}`
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
    data.supplemental.filter(featureMatches).forEach((feature) => {
      const shape = L.geoJSON(feature, { style: {
        color: "#4d2a91", weight: 2, fillColor: "#7a5bc7", fillOpacity: 0.28,
      } }).on("click", () => selectSupplemental(feature));
      const marker = L.marker(shape.getBounds().getCenter(), { icon: L.divIcon({
        className: "supplemental-marker",
        html: `<span>C${Number(feature.properties.preliminary_rank)}</span>`,
        iconSize: [38, 38], iconAnchor: [19, 19],
      }) }).on("click", () => selectSupplemental(feature));
      shape.addTo(layers.supplemental); marker.addTo(layers.supplemental);
      featureLayers.supplemental.set(
        feature.properties.candidate_id, { shape, marker },
      );
    });
    data.parcels.filter(featureMatches).forEach((feature) => {
      const standalone = data.standalone.find(
        (item) => item.properties.pnu === feature.properties.pnu,
      );
      const supplemental = data.supplemental.find(
        (item) => item.properties.pnu === feature.properties.pnu,
      );
      const shape = L.geoJSON(feature, { style: {
        color: supplemental ? "#4d2a91" : standalone ? "#9b6400" : "#a64613",
        weight: 2,
        fillColor: supplemental ? "#7a5bc7" : standalone ? "#e5a61f" : "#ed7d31",
        fillOpacity: 0.5,
      } });
      shape.on("click", () => {
        const hub = data.hubs.find(
          (item) => item.properties.hub_id === feature.properties.hub_id,
        );
        if (hub) selectHub(hub);
        else if (supplemental) selectSupplemental(supplemental);
        else if (standalone) selectStandalone(standalone);
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
      ).bindPopup(houseDetailNode(feature));
      marker.addTo(layers.houses);
      featureLayers.houses.set(feature.properties.record_id, marker);
    });
    updateSelectionStyles(); updateLayerVisibility(); renderCandidates();
  }
  function updateLayerVisibility() {
    const zoom = map.getZoom();
    if (!map.hasLayer(layers.hubs)) layers.hubs.addTo(map);
    if (!map.hasLayer(layers.standalone)) layers.standalone.addTo(map);
    if (!map.hasLayer(layers.supplemental)) layers.supplemental.addTo(map);
    if (zoom >= 14 && !map.hasLayer(layers.parcels)) layers.parcels.addTo(map);
    if (zoom < 14 && map.hasLayer(layers.parcels)) map.removeLayer(layers.parcels);
    const showHouses = zoom >= 17 || Boolean(districtFilter.value);
    if (showHouses && !map.hasLayer(layers.houses)) layers.houses.addTo(map);
    if (!showHouses && map.hasLayer(layers.houses)) map.removeLayer(layers.houses);
    featureLayers.houses.forEach((marker) => marker.setRadius(zoom >= 17 ? 7 : 4));
    mapElement.classList.toggle("parcel-detail-mode", zoom >= 14);
    mapElement.classList.toggle("street-detail-mode", zoom >= 17);
    [
      featureLayers.hubs, featureLayers.standalone, featureLayers.supplemental,
    ].forEach((entries) => {
      entries.forEach((entry) => {
        const layer = entries === featureLayers.hubs
          ? layers.hubs
          : entries === featureLayers.standalone
            ? layers.standalone
            : layers.supplemental;
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
    return [...data.hubs, ...data.standalone, ...data.supplemental].filter(featureMatches);
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
      "summary.json",
    ].map((url) => fetch(url).then((response) => {
      if (!response.ok) throw new Error("map_data_failed");
      return response.json();
    })),
  ).then(([hubs, standalone, supplemental, parcels, houses, summary]) => {
    data.hubs = hubs.features;
    data.standalone = standalone.features;
    data.supplemental = supplemental.features;
    data.parcels = parcels.features;
    data.houses = houses.features;
    data.summary = summary;
    WEST_DISTRICTS.forEach(
      (district) => districtFilter.add(new Option(district, district)),
    );
    refreshLayers(); fitVisibleCandidates(12);
  }).catch(() => {
    hubCandidateList.textContent = "게시 지도자료를 불러오지 못했습니다.";
    standaloneCandidateList.textContent = "게시 지도자료를 불러오지 못했습니다.";
    supplementalCandidateList.textContent = "게시 지도자료를 불러오지 못했습니다.";
  });
})();
