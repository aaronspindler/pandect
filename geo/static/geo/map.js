(() => {
  "use strict";
  const section = document.querySelector("[data-case-map]");
  if (!section) return;
  const status = section.querySelector("[data-map-status]");
  const tripStatus = section.querySelector("[data-trip-status]");
  const canvas = document.getElementById("investigation-map");
  if (!window.L) {
    status.textContent = "The interactive map could not start. Reload the page to try again, or use the trip summaries and sources below.";
    return;
  }
  const points = JSON.parse(document.getElementById("case-map-points").textContent);
  const map = L.map(canvas, { center: [20, 0], zoom: 2, scrollWheelZoom: false, zoomAnimation: false, fadeAnimation: false, markerZoomAnimation: false, worldCopyJump: true });
  canvas.hidden = false;
  map.invalidateSize({ pan: false });
  map.attributionControl.setPrefix(false);
  map.on("resize", () => map.eachLayer((layer) => {
    const popup = layer.getPopup?.();
    if (!popup) return;
    popup.options.maxWidth = Math.min(300, canvas.clientWidth - 80);
    popup.options.maxHeight = Math.min(224, canvas.clientHeight - 180);
    if (layer.isPopupOpen()) popup.update();
  }));
  const markers = new Map();
  const endpoints = L.layerGroup().addTo(map);
  const poiBounds = L.latLngBounds(points.map((point) => [point.latitude, point.longitude]));
  let tripBounds;
  const fit = () => {
    const bounds = L.latLngBounds([]);
    if (poiBounds.isValid()) bounds.extend(poiBounds);
    if (tripBounds?.isValid()) bounds.extend(tripBounds);
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [32, 32], maxZoom: 16, animate: false });
  };
  const popupOptions = () => ({ maxWidth: Math.min(300, canvas.clientWidth - 80), maxHeight: Math.min(224, canvas.clientHeight - 180), autoPanPaddingTopLeft: [16, 112], autoPanPaddingBottomRight: [16, 16], keepInView: true });
  for (const point of points) {
    const content = document.createElement("div");
    const heading = document.createElement("strong");
    heading.textContent = "Investigator POI";
    const link = document.createElement("a");
    try {
      const target = new URL(point.url, window.location.origin);
      if ((target.protocol === "https:" || target.protocol === "http:") && target.origin === window.location.origin) {
        link.href = target.href;
      }
    } catch {
      // A malformed source URL remains readable as text.
    }
    link.textContent = `${point.number}. ${point.title}`;
    content.append(heading, document.createElement("br"), link);
    const icon = document.createElement("span");
    const number = document.createElement("b");
    number.textContent = point.number;
    icon.append(number);
    const marker = L.marker([point.latitude, point.longitude], {
      title: `${point.number}. ${point.title}`,
      alt: point.title,
      icon: L.divIcon({ className: "case-map-marker case-map-poi", html: icon, iconSize: [44, 44], iconAnchor: [22, 22] }),
    }).addTo(map).bindPopup(content, popupOptions());
    marker.getElement().setAttribute("aria-label", `${point.number}. ${point.title}`);
    markers.set(String(point.number), marker);
  }
  for (const button of document.querySelectorAll("[data-map-show]")) {
    const marker = markers.get(button.dataset.mapShow);
    button.hidden = !marker;
    button.addEventListener("click", () => {
      if (!marker) return;
      map.setView(marker.getLatLng(), 16, { animate: false });
      marker.openPopup();
      canvas.scrollIntoView({ block: "center" });
      canvas.focus({ preventScroll: true });
    });
  }
  const fitButton = section.querySelector("[data-map-fit]");
  fitButton.hidden = false;
  fitButton.addEventListener("click", fit);
  if (section.dataset.tileUrl) {
    let failed = false;
    const tiles = L.tileLayer(section.dataset.tileUrl, {
      maxZoom: 19, referrerPolicy: "origin", updateWhenIdle: true, keepBuffer: 0,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" rel="noreferrer">OpenStreetMap contributors</a>',
    });
    if (section.dataset.tileAttribution) {
      const label = document.createElement("span");
      label.textContent = section.dataset.tileAttribution;
      map.attributionControl.addAttribution(label.outerHTML);
    }
    tiles.on("loading", () => { failed = false; status.textContent = "Loading map tiles…"; status.classList.remove("sr-only"); });
    tiles.on("tileerror", () => { failed = true; status.textContent = "Some map tiles could not load. The trip path, POIs, and sources remain available. Pan or zoom to retry."; status.classList.remove("sr-only"); });
    tiles.on("load", () => { if (!failed) { status.textContent = "Map loaded."; status.classList.add("sr-only"); } });
    tiles.addTo(map);
  } else {
    status.textContent = "No background map configured. Trip paths and POIs remain available.";
  }
  if (section.dataset.poiCreateUrl) {
    map.on("click", (event) => {
      const position = event.latlng.wrap();
      const url = new URL(section.dataset.poiCreateUrl, window.location.origin);
      url.searchParams.set("latitude", position.lat.toFixed(6));
      url.searchParams.set("longitude", position.lng.toFixed(6));
      const link = document.createElement("a");
      link.href = url.pathname + url.search;
      link.textContent = "Add POI here";
      L.popup(popupOptions()).setLatLng(event.latlng).setContent(link).openOn(map);
    });
  }
  fit();
  if (!section.dataset.geometryUrl) return;

  // The overlay retains only viewport pixels and the preceding position. Each
  // bounded response is discarded; settled pan/zoom replays the complete trip.
  const drawing = L.DomUtil.create("canvas", "trip-path-canvas", map.getPane("overlayPane"));
  const context = drawing.getContext("2d");
  let controller;
  let stopped = false;
  let replayFrame;
  let firstReplay = true;
  const attributions = new Set();
  const endpoint = (position, label, markerLabel) => {
    const text = document.createElement("span");
    text.textContent = label;
    L.marker(position, { title: label, alt: label, icon: L.divIcon({ className: "case-map-marker case-map-endpoint", html: `<span>${markerLabel}</span>`, iconSize: [44, 44], iconAnchor: [22, 22] }) }).addTo(endpoints).bindPopup(text, popupOptions());
  };
  const replay = async () => {
    if (stopped) return;
    controller?.abort();
    const active = new AbortController();
    controller = active;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const size = map.getSize();
    drawing.width = size.x * ratio;
    drawing.height = size.y * ratio;
    drawing.style.width = `${size.x}px`;
    drawing.style.height = `${size.y}px`;
    L.DomUtil.setPosition(drawing, map.containerPointToLayerPoint([0, 0]));
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.lineWidth = 4;
    context.lineJoin = "round";
    context.lineCap = "round";
    // Tile imagery and the unconfigured basemap retain a light background.
    context.strokeStyle = "#0066cc";
    endpoints.clearLayers();
    let cursor = "0";
    let revision;
    let previous;
    let first;
    let last;
    let mapped = 0;
    let estimates = 0;
    let read = 0;
    const bounds = L.latLngBounds([]);
    section.dataset.tripComplete = "false";
    tripStatus.textContent = "Loading the complete trip path…";
    try {
      do {
        const url = new URL(section.dataset.geometryUrl, window.location.origin);
        url.searchParams.set("cursor", cursor);
        if (revision) url.searchParams.set("revision", revision);
        const response = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store", signal: active.signal });
        if (!response.ok) throw new Error(response.status === 409 ? "changed" : "unavailable");
        const chunk = await response.json();
        if (active.signal.aborted) return;
        revision = chunk.revision;
        context.beginPath();
        for (const source of chunk.positions) {
          read += 1;
          if (source.attribution && !attributions.has(source.attribution)) {
            const label = document.createElement("span");
            label.textContent = source.attribution;
            map.attributionControl.addAttribution(label.outerHTML);
            attributions.add(source.attribution);
          }
          const position = source.position;
          if (!position) { previous = null; continue; }
          mapped += 1;
          if (source.label === "Reviewed address estimate") estimates += 1;
          bounds.extend(position);
          if (!first) first = position;
          last = position;
          const point = map.latLngToContainerPoint(position);
          // Do not draw a false line across the projection's date-line seam.
          if (previous && Math.abs(previous[1] - position[1]) <= 180) {
            const before = map.latLngToContainerPoint(previous);
            context.moveTo(before.x, before.y);
            context.lineTo(point.x, point.y);
          }
          previous = position;
        }
        context.stroke();
        cursor = chunk.next_cursor;
      } while (cursor !== null);
      tripBounds = bounds;
      if (first) {
        if (mapped === 1) {
          endpoint(first, "Mapped observation", "A");
        } else if (first[0] === last[0] && first[1] === last[1]) {
          endpoint(first, "First and last mapped observations", "A/B");
        } else {
          endpoint(first, "First mapped observation", "A");
          endpoint(last, "Last mapped observation", "B");
        }
      }
      tripStatus.textContent = mapped ? `Complete trip loaded: ${mapped} of ${read} observations have mapped positions${estimates ? `, including ${estimates} reviewed address estimate${estimates === 1 ? "" : "s"}` : ""}. Missing positions interrupt the path.` : `Complete trip loaded: ${read} observations. Review address estimates from the observations below to map this trip.`;
      section.dataset.tripComplete = "true";
      section.dataset.tripSources = String(read);
      if (firstReplay) { firstReplay = false; fit(); }
    } catch (error) {
      if (active.signal.aborted) return;
      context.clearRect(0, 0, size.x, size.y);
      endpoints.clearLayers();
      section.dataset.tripComplete = "false";
      tripStatus.textContent = error.message === "changed" ? "Trip positions changed during loading. Reload the page to see the current path." : "The complete trip path could not load. Reload the page to try again. Trip summaries and sources remain available.";
    } finally {
      if (controller === active) controller = null;
    }
  };
  const scheduleReplay = () => {
    if (stopped) return;
    section.dataset.tripComplete = "false";
    cancelAnimationFrame(replayFrame);
    replayFrame = requestAnimationFrame(replay);
  };
  const stop = () => {
    stopped = true;
    cancelAnimationFrame(replayFrame);
    controller?.abort();
  };
  map.on("movestart zoomstart", () => {
    cancelAnimationFrame(replayFrame);
    controller?.abort();
  });
  // Leaflet can emit resize and moveend together; request a single replay.
  map.on("moveend resize", scheduleReplay);
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[href]");
    if (!link || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || link.target || link.hasAttribute("download")) return;
    const target = new URL(link.href, window.location.href);
    if (target.origin === window.location.origin && (target.pathname !== window.location.pathname || target.search !== window.location.search)) stop();
  });
  window.addEventListener("pagehide", stop);
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) { stopped = false; scheduleReplay(); }
  });
  replay();
})();
