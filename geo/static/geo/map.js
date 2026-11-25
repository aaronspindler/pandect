// Leaflet caches its initial viewport. Wait for the local map styles to finish loading.
window.addEventListener("load", () => {
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
  canvas.hidden = false;
  const map = L.map(canvas, { center: [20, 0], zoom: 2, scrollWheelZoom: false, zoomAnimation: false, fadeAnimation: false, markerZoomAnimation: false, worldCopyJump: true });
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
  const poiGroups = new Map();
  for (const point of points) {
    const key = `${point.latitude},${point.longitude}`;
    if (!poiGroups.has(key)) poiGroups.set(key, []);
    poiGroups.get(key).push(point);
  }
  for (const group of poiGroups.values()) {
    const [first] = group;
    const label = group.length === 1
      ? `${first.number}. ${first.title}`
      : `${group.length} investigator POIs at ${first.number}`;
    const content = document.createElement("div");
    const heading = document.createElement("strong");
    heading.textContent = group.length === 1 ? "Investigator POI" : label;
    content.append(heading);
    for (const point of group) {
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
      content.append(document.createElement("br"), link);
    }
    const icon = document.createElement("span");
    const number = document.createElement("b");
    number.textContent = group.length === 1 ? first.number : `${group.length} POIs`;
    icon.append(number);
    const marker = L.marker([first.latitude, first.longitude], {
      title: label,
      alt: label,
      icon: L.divIcon({ className: "case-map-marker case-map-poi", html: icon, iconSize: [44, 44], iconAnchor: [22, 22] }),
    }).addTo(map).bindPopup(content, popupOptions());
    marker.getElement().setAttribute("aria-label", label);
    for (const point of group) markers.set(String(point.number), marker);
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

  // Reuse only a complete response made of reported coordinates.
  // Address estimates are read fresh and longer trips stream in bounded chunks.
  const drawing = L.DomUtil.create("canvas", "trip-path-canvas", map.getPane("overlayPane"));
  const context = drawing.getContext("2d");
  const legend = section.querySelector("[data-trip-legend]");
  const styles = window.getComputedStyle(section);
  const speedStyle = (band, dash) => ({
    color: styles.getPropertyValue(`--trip-speed-${band}`).trim(),
    dash,
  });
  const slow = speedStyle("slow", []);
  const medium = speedStyle("medium", [8, 6]);
  const fast = speedStyle("fast", [2, 6]);
  const unknown = speedStyle("unknown", [8, 6, 2, 6]);
  const speedBand = (speed) => {
    if (!Number.isFinite(speed)) return unknown;
    if (speed < 30) return slow;
    if (speed < 60) return medium;
    return fast;
  };
  const arrowSpacing = 100;
  const addArrows = (path, before, point, viewport, distanceToArrow) => {
    const length = before.distanceTo(point);
    if (length <= distanceToArrow) return distanceToArrow - length;
    // Pan and zoom can project very long segments off screen. Only draw visible arrows.
    const visible = L.LineUtil.clipSegment(before, point, viewport);
    if (visible) {
      const from = before.distanceTo(visible[0]);
      const through = before.distanceTo(visible[1]);
      const start = distanceToArrow + Math.max(0, Math.ceil((from - distanceToArrow) / arrowSpacing)) * arrowSpacing;
      const dx = (point.x - before.x) / length;
      const dy = (point.y - before.y) / length;
      for (let distance = start; distance <= through && distance < length; distance += arrowSpacing) {
        const x = before.x + dx * distance;
        const y = before.y + dy * distance;
        path.moveTo(x - 8 * dx - 5 * dy, y - 8 * dy + 5 * dx);
        path.lineTo(x, y);
        path.lineTo(x - 8 * dx + 5 * dy, y - 8 * dy - 5 * dx);
      }
    }
    return (arrowSpacing - (length - distanceToArrow) % arrowSpacing) % arrowSpacing;
  };
  let controller;
  let stopped = false;
  let replayFrame;
  let firstReplay = true;
  let savedChunk;
  const attributions = new Set();
  const endpoint = (position, label, markerLabel) => {
    const text = document.createElement("span");
    text.textContent = label;
    L.marker(position, { title: label, alt: label, icon: L.divIcon({ className: "case-map-marker case-map-endpoint", html: `<span>${markerLabel}</span>`, iconSize: [44, 44], iconAnchor: [22, 22] }) }).addTo(endpoints).bindPopup(text, popupOptions());
  };
  const requestGeometry = async (active, params) => {
    const url = new URL(section.dataset.geometryUrl, window.location.origin);
    url.search = new URLSearchParams(params);
    const response = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store", signal: active.signal });
    if (!response.ok && !(params.recheck && response.status === 409)) {
      throw new Error(response.status === 409 ? "changed" : "unavailable");
    }
    if (response.ok && !window.PandectPrivateSession.matches(response.headers.get("X-Pandect-Session"))) throw new Error("Access changed");
    return response;
  };
  const replay = async () => {
    if (stopped) return;
    controller?.abort();
    const active = new AbortController();
    controller = active;
    let cursor = 0;
    let previous;
    let first;
    let last;
    let mapped = 0;
    let estimates = 0;
    let read = 0;
    let hasPath = false;
    let distanceToArrow = arrowSpacing / 2;
    const bounds = L.latLngBounds([]);
    section.dataset.tripComplete = "false";
    tripStatus.textContent = "Loading the complete trip path…";
    tripStatus.classList.remove("sr-only");
    legend.hidden = true;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const size = map.getSize();
    const viewport = L.bounds([0, 0], size);
    drawing.width = size.x * ratio;
    drawing.height = size.y * ratio;
    drawing.style.width = `${size.x}px`;
    drawing.style.height = `${size.y}px`;
    L.DomUtil.setPosition(drawing, map.containerPointToLayerPoint([0, 0]));
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.lineWidth = 4;
    context.lineJoin = "round";
    context.lineCap = "round";
    endpoints.clearLayers();
    try {
      if (savedChunk) {
        const response = await requestGeometry(active, { recheck: "1", revision: savedChunk.revision });
        if (active.signal.aborted) return;
        if (response.status === 409) {
          savedChunk = undefined;
        } else if (response.status !== 204) {
          throw new Error("unavailable");
        }
      }
      let revision;
      do {
        let chunk = savedChunk;
        if (!chunk) {
          const params = revision ? { cursor, revision } : { cursor };
          const response = await requestGeometry(active, params);
          chunk = await response.json();
        }
        if (active.signal.aborted) return;
        if (chunk.can_reuse) savedChunk = chunk;
        revision = chunk.revision;
        const arrows = new Path2D();
        let strokeBand;
        let lineOpen = false;
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
          if (!position) {
            previous = null;
            lineOpen = false;
            distanceToArrow = arrowSpacing / 2;
            continue;
          }
          mapped += 1;
          if (source.label === "Selected address estimate") estimates += 1;
          bounds.extend(position);
          if (!first) first = position;
          last = position;
          const point = map.latLngToContainerPoint(position);
          // Do not draw a false line across the projection's date-line seam.
          if (previous && Math.abs(previous.longitude - position[1]) <= 180) {
            const before = previous.point;
            if (!before.equals(point)) {
              const band = speedBand(source.speed_kmh);
              if (band !== strokeBand) {
                context.stroke();
                context.beginPath();
                context.strokeStyle = band.color;
                context.setLineDash(band.dash);
                strokeBand = band;
                lineOpen = false;
              }
              if (!lineOpen) context.moveTo(before.x, before.y);
              context.lineTo(point.x, point.y);
              lineOpen = true;
              hasPath = true;
              distanceToArrow = addArrows(arrows, before, point, viewport, distanceToArrow);
            }
          } else {
            lineOpen = false;
            distanceToArrow = arrowSpacing / 2;
          }
          previous = { point, longitude: position[1] };
        }
        context.stroke();
        context.setLineDash([]);
        context.strokeStyle = "#fff";
        context.lineWidth = 4;
        context.stroke(arrows);
        context.strokeStyle = "#14282e";
        context.lineWidth = 2;
        context.stroke(arrows);
        context.lineWidth = 4;
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
      const estimateText = estimates ? `, including ${estimates} selected address estimate${estimates === 1 ? "" : "s"}` : "";
      const missingText = mapped < read ? " Missing positions interrupt the path." : "";
      tripStatus.textContent = mapped
        ? `${mapped} of ${read} observations mapped${estimateText}.${missingText}`
        : "No mapped positions. Choose address estimates from the observations to map this trip.";
      tripStatus.classList.toggle("sr-only", mapped > 0 && mapped === read && !estimates);
      legend.hidden = !hasPath;
      section.dataset.tripComplete = "true";
      section.dataset.tripSources = String(read);
      if (firstReplay) { firstReplay = false; fit(); }
    } catch (error) {
      if (active.signal.aborted) return;
      savedChunk = undefined;
      context.resetTransform();
      context.clearRect(0, 0, drawing.width, drawing.height);
      tripBounds = null;
      endpoints.clearLayers();
      legend.hidden = true;
      section.dataset.tripComplete = "false";
      tripStatus.textContent = error.message === "changed" ? "Trip positions changed during loading. Reload the page to see the current path." : "The complete trip path could not load. Reload the page to try again. Trip summaries and sources remain available.";
      tripStatus.classList.remove("sr-only");
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
  document.addEventListener("pandect:access-lost", stop);
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) { stopped = false; scheduleReplay(); }
  });
  replay();
}, {once: true});
