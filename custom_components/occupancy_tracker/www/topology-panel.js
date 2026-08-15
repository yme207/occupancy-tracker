// Occupancy Tracker topology editor panel (docs/SPEC.md §7.3). Registered by
// panel.py via panel_custom, served at
// /occupancy_tracker_static/topology-panel.js. Talks only to the websocket
// commands in websocket_api.py (occupancy_tracker/topology/get + /save) and
// the core config_entries/get command (to resolve this integration's single
// config entry id without hardcoding it into the panel's static
// registration, which would go stale if the entry were ever removed and
// re-added — see docs/DECISIONS.md).
//
// Connectors are drawn/removed via a "Draw connector" toolbar mode
// (click one room, then another — not drag, see docs/DECISIONS.md's
// 2026-08-09 connector-interaction entry) rather than dragging a node onto
// another, since node-drag already means "reposition" here; a click
// sequence keeps the two gestures unambiguous and stays keyboard/touch
// friendly per docs/UX_GUIDELINES.md §6. Egress-point flagging and per-area
// entity selection (SPEC.md §5.2) are both per-area concerns instead, so
// they live in the click-to-inspect detail panel as two independent
// checklists over the same area.entity_ids list: an Area *is* an egress
// point exactly when its crossing-entity list is non-empty (matches the
// backend's own validation, so there's no separate on/off flag to keep in
// sync), while entity selection has no such derived meaning — it's just
// "which of this room's entities count as occupancy evidence at all."
import { LitElement, html, svg, css, nothing } from "./vendor/lit-core.min.js";

const NODE_RADIUS = 22;
const GRID_SIZE = 40;
const MIN_VIEWBOX_W = 240;
const MAX_VIEWBOX_W = 4000;
const AUTO_LAYOUT_CELL = 140;
const AUTO_LAYOUT_BAND_GAP = 70;

// Mirrors occupancy_engine.py's StateQuality/ProvenanceTier enum member
// names (the websocket API serializes them via `.name`, see
// websocket_api.py's _engine_state_json) — plain-language labels for the
// explainability inspector (SPEC.md §7.3).
const QUALITY_LABELS = {
  CONFIRMED: "Confirmed",
  LATCHED: "Probably occupied",
  AMBIGUOUS: "Checking…",
};
const PROVENANCE_LABELS = {
  USER_CONFIRMED: "someone directly using a device",
  AMBIGUOUS_PHYSICAL: "a sensor picking up activity",
};

// binary_sensor device classes that mean "someone is physically here" (HA's
// own device-class list — verified against developers.home-assistant.io's
// binary_sensor entry, not guessed) — used to suggest likely occupancy
// evidence per room (docs/UX_GUIDELINES.md §3's "confident, sensible
// defaults"). Deliberately excludes weaker/ambiguous classes like "moving"
// (that's for the device itself moving, e.g. a vehicle, not room presence).
const OCCUPANCY_EVIDENCE_DEVICE_CLASSES = new Set(["motion", "occupancy", "presence"]);

// device_class only exists on binary_sensor, and even there a real-world
// motion sensor integration doesn't always set it — and this project's own
// dev-instance fixtures (docs/STATUS.md) use input_boolean helpers to
// simulate a motion sensor precisely *because* input_boolean is toggleable
// from the UI, unlike a real binary_sensor. Neither has a device_class, so a
// name fallback is needed for the suggestion to ever fire for them: a
// whole-word match on the object id, restricted to these two domains (the
// only ones signal_ingestion.py-relevant "on/off occupancy signal" domains
// this heuristic should guess at — a numeric sensor.*_motion_battery or a
// switch.*_motion_override would be a false positive under a looser check).
const OCCUPANCY_EVIDENCE_ENTITY_DOMAINS = new Set(["binary_sensor", "input_boolean"]);
// Not \b-based: JavaScript regex treats "_" as a word character, so \b never
// breaks between "landing" and "motion" in "landing_motion" — the entity
// object id format virtually every HA entity actually uses — which silently
// never matched anything. Anchoring on "_"/start/end explicitly instead.
const OCCUPANCY_EVIDENCE_NAME_PATTERN = /(?:^|_)(?:motion|occupancy|presence)(?:_|$)/i;

// signal_ingestion.py's _ACTIVE_STATE only ever matches a literal state
// string of "on" (see docs/DECISIONS.md) — broader than the suggestion
// domains above (switch/light are reasonable manual picks even though the
// suggestion heuristic doesn't auto-guess them), but still only domains
// that actually report "on"/"off". A media_player (state "playing"/"idle"),
// a cover (state "open"/"closed"), or a numeric sensor would be pickable in
// an unfiltered checklist but would then silently never register as
// evidence, with no error anywhere — checklists below filter to this set
// instead of promising something the backend can't classify yet.
const SELECTABLE_EVIDENCE_ENTITY_DOMAINS = new Set(["binary_sensor", "switch", "light", "input_boolean"]);

// Must match the setTimeout delay in _selectArea's closing branch — the
// detail card stays mounted this long after deselection so its CSS
// transition can actually finish playing before Lit removes it from the DOM.
const DETAIL_TRANSITION_MS = 180;

// crypto.randomUUID() requires a secure context (https or localhost) and is
// unavailable on a plain-http LAN address — exactly how this project's own
// dev HA instance is reached (docs/STATUS.md), and a common way real HA
// installs are reached too. Avoid it rather than assume it.
function generateConnectorId() {
  return `connector-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

// Connector/egress lines are conceptually drawn "from node to node," but a
// line whose endpoints are the node *centers* runs straight across each
// circle's interior — visible through it unless the circle sits perfectly
// opaque and on top in z-order (which an inactive/dimmed node's reduced
// opacity, or a translucent theme --card-background-color, can't guarantee).
// Trimming each endpoint back to the circle's edge fixes this unconditionally,
// regardless of fill opacity or paint order, and is also the visually correct
// way to draw a node-link diagram in the first place.
function pointTowardsEdge(from, to, radius) {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const dist = Math.hypot(dx, dy) || 1;
  return { x: from.x + (dx / dist) * radius, y: from.y + (dy / dist) * radius };
}
// Must match .graph-wrap's CSS `aspect-ratio` below exactly. The viewBox is
// kept at this same ratio at all times (computeViewBox, wheel-zoom) so the
// SVG never letterboxes (preserveAspectRatio="xMidYMid meet" by default) —
// letterboxing was silently breaking the screen<->user-space coordinate
// math pan/zoom/drag all depend on, which is what read as "zoom also pans".
const VIEWPORT_ASPECT = 640 / 460;

class OccupancyTrackerTopologyPanel extends LitElement {
  static properties = {
    hass: { type: Object },
    narrow: { type: Boolean },
    panel: { type: Object },
    _loading: { state: true },
    _error: { state: true },
    _houseShape: { state: true },
    _topology: { state: true },
    _selectedAreaId: { state: true },
    _positions: { state: true },
    _viewBox: { state: true },
    _gridEnabled: { state: true },
    _panning: { state: true },
    _connectMode: { state: true },
    _connectSourceAreaId: { state: true },
    _connectPreviewPoint: { state: true },
    _selectedConnectorId: { state: true },
    _engineState: { state: true },
    _detailPhase: { state: true },
  };

  constructor() {
    super();
    this._loading = true;
    this._error = null;
    this._houseShape = null;
    this._topology = null;
    this._selectedAreaId = null;
    this._entryId = null;
    this._dataRequested = false;
    this._positions = new Map();
    this._viewBox = { x: 0, y: 0, w: 640, h: 460 };
    this._gridEnabled = true;
    this._panning = false;
    this._connectMode = false;
    this._connectSourceAreaId = null;
    this._connectPreviewPoint = null;
    this._selectedConnectorId = null;
    // Live occupancy belief for every Area (SPEC.md §7.3's explainability
    // inspector) — fetched once on load, then kept fresh for the panel's
    // whole lifetime via a push subscription (not per-room-selection; see
    // _subscribeEngineState's own comment for why one subscription covers
    // every room). Not a reactive Lit property — nothing renders it
    // directly, it's just a handle for cleanup.
    this._engineState = null;
    this._engineStateUnsub = null;
    // Drives the detail card's open/close CSS transition
    // (docs/UX_GUIDELINES.md §2) — see _selectArea for the state machine.
    this._detailPhase = "closed";
    this._detailCloseTimer = null;
    this._onKeyDown = this._onKeyDown.bind(this);
  }

  connectedCallback() {
    super.connectedCallback();
    window.addEventListener("keydown", this._onKeyDown);
  }

  disconnectedCallback() {
    window.removeEventListener("keydown", this._onKeyDown);
    this._engineStateUnsub?.();
    this._engineStateUnsub = null;
    if (this._detailCloseTimer) clearTimeout(this._detailCloseTimer);
    super.disconnectedCallback();
  }

  _onKeyDown(e) {
    if (e.key !== "Escape" || !this._connectMode) return;
    if (this._connectSourceAreaId) {
      this._connectSourceAreaId = null;
      this._connectPreviewPoint = null;
    } else {
      this._connectMode = false;
    }
  }

  updated() {
    // Guarded by _dataRequested rather than done in firstUpdated(): `hass`
    // is reassigned by the HA frontend on essentially every state change
    // elsewhere in the house, so this must fire once total, not once per
    // `hass` update, and must tolerate `hass` not being set yet on the very
    // first render pass.
    if (this.hass && !this._dataRequested) {
      this._dataRequested = true;
      this._loadData();
    }
  }

  async _loadData() {
    this._loading = true;
    this._error = null;
    try {
      const entries = await this.hass.callWS({
        type: "config_entries/get",
        domain: "occupancy_tracker",
      });
      if (!entries.length) {
        this._error = "not_configured";
        this._loading = false;
        return;
      }
      this._entryId = entries[0].entry_id;
      const result = await this.hass.callWS({
        type: "occupancy_tracker/topology/get",
        entry_id: this._entryId,
      });
      this._houseShape = result.house_shape;
      this._topology = result.topology;
      this._initPositions();
      await this._loadEngineState();
      this._subscribeEngineState();
    } catch (err) {
      this._error = err.message || "unknown_error";
    } finally {
      this._loading = false;
    }
  }

  // -- Layout: positions, auto-arrange, viewBox ---------------------------

  _initPositions() {
    const areas = this._areaEntries();
    const positions = this._autoLayout(areas);
    for (const [areaId, position] of Object.entries(this._topology.area_positions ?? {})) {
      if (positions.has(areaId)) positions.set(areaId, position);
    }
    this._positions = positions;
    this._viewBox = this._computeViewBox([...positions.values()]);
  }

  _floorsById() {
    return new Map((this._houseShape?.floors ?? []).map((f) => [f.floor_id, f]));
  }

  _autoLayout(areas) {
    // Group by floor, order floor bands top-to-bottom by the floor's own
    // `level` (unset floors and "no floor" areas sort last), and arrange
    // each band as a fixed-spacing grid — deterministic and overlap-free by
    // construction, rather than a force-directed layout that needs its own
    // collision-resolution logic.
    const floorsById = this._floorsById();
    const groups = new Map();
    for (const area of areas) {
      const key = area.floor_id ?? "__no_floor__";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(area);
    }
    const bandKeys = [...groups.keys()].sort((a, b) => {
      if (a === "__no_floor__") return 1;
      if (b === "__no_floor__") return -1;
      const levelA = floorsById.get(a)?.level;
      const levelB = floorsById.get(b)?.level;
      if (levelA == null && levelB == null) return 0;
      if (levelA == null) return 1;
      if (levelB == null) return -1;
      return levelA - levelB;
    });

    const positions = new Map();
    let y = 0;
    for (const key of bandKeys) {
      const bandAreas = groups.get(key);
      const cols = Math.max(1, Math.ceil(Math.sqrt(bandAreas.length)));
      const rows = Math.ceil(bandAreas.length / cols);
      const bandWidth = (cols - 1) * AUTO_LAYOUT_CELL;
      bandAreas.forEach((area, i) => {
        const col = i % cols;
        const row = Math.floor(i / cols);
        positions.set(area.area_id, {
          x: col * AUTO_LAYOUT_CELL - bandWidth / 2,
          y: y + row * AUTO_LAYOUT_CELL,
        });
      });
      y += rows * AUTO_LAYOUT_CELL + AUTO_LAYOUT_BAND_GAP;
    }
    // AUTO_LAYOUT_CELL (140) isn't a multiple of GRID_SIZE (40), so an
    // auto-arranged layout would otherwise never actually sit on the same
    // points a manual grid-snapped drag lands on — snap here too so the two
    // ways of positioning a node agree with each other and with the visible
    // dot grid.
    if (this._gridEnabled) {
      for (const [areaId, position] of positions) {
        positions.set(areaId, {
          x: Math.round(position.x / GRID_SIZE) * GRID_SIZE,
          y: Math.round(position.y / GRID_SIZE) * GRID_SIZE,
        });
      }
    }
    return positions;
  }

  _autoArrangeAll() {
    const positions = this._autoLayout(this._areaEntries());
    this._positions = positions;
    this._viewBox = this._computeViewBox([...this._positions.values()]);
    this._saveTopology();
  }

  _computeViewBox(points) {
    const MIN_W = 320;
    if (!points.length) {
      const w = MIN_W;
      const h = w / VIEWPORT_ASPECT;
      return { x: -w / 2, y: -h / 2, w, h };
    }
    const pad = NODE_RADIUS + 24;
    const labelPad = 28;
    const minX = Math.min(...points.map((p) => p.x)) - pad;
    const maxX = Math.max(...points.map((p) => p.x)) + pad;
    const minY = Math.min(...points.map((p) => p.y)) - pad;
    const maxY = Math.max(...points.map((p) => p.y)) + pad + labelPad;
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;

    // Fit both dimensions of the content, then grow whichever is short so
    // the box ends up at exactly VIEWPORT_ASPECT — never a different ratio
    // than the container actually renders at (see the constant's comment).
    let w = Math.max(maxX - minX, MIN_W);
    let h = Math.max(maxY - minY, MIN_W / VIEWPORT_ASPECT);
    if (w / h > VIEWPORT_ASPECT) {
      h = w / VIEWPORT_ASPECT;
    } else {
      w = h * VIEWPORT_ASPECT;
    }
    return { x: cx - w / 2, y: cy - h / 2, w, h };
  }

  _fitView() {
    this._viewBox = this._computeViewBox([...this._positions.values()]);
  }

  _toggleGrid() {
    this._gridEnabled = !this._gridEnabled;
  }

  // -- Connector drawing --------------------------------------------------

  _toggleConnectMode() {
    this._connectMode = !this._connectMode;
    this._connectSourceAreaId = null;
    this._connectPreviewPoint = null;
    this._selectedConnectorId = null;
  }

  _onNodeConnectClick(areaId) {
    if (!this._connectSourceAreaId) {
      this._connectSourceAreaId = areaId;
      return;
    }
    if (this._connectSourceAreaId === areaId) {
      // Clicking the pending source again cancels it, rather than drawing a
      // self-loop (the backend rejects those anyway, see websocket_api.py).
      this._connectSourceAreaId = null;
      this._connectPreviewPoint = null;
      return;
    }
    this._addConnector(this._connectSourceAreaId, areaId);
    this._connectSourceAreaId = null;
    this._connectPreviewPoint = null;
  }

  _onGraphPointerMove(e) {
    if (!this._connectMode || !this._connectSourceAreaId) return;
    const { scale, rect } = this._screenScale(e.currentTarget);
    this._connectPreviewPoint = {
      x: this._viewBox.x + (e.clientX - rect.left) * scale,
      y: this._viewBox.y + (e.clientY - rect.top) * scale,
    };
  }

  _addConnector(areaIdA, areaIdB) {
    const connectors = this._topology.connectors ?? [];
    const alreadyConnected = connectors.some(
      (c) =>
        (c.area_id_a === areaIdA && c.area_id_b === areaIdB) ||
        (c.area_id_a === areaIdB && c.area_id_b === areaIdA)
    );
    if (alreadyConnected) return;
    const connector = {
      connector_id: generateConnectorId(),
      area_id_a: areaIdA,
      area_id_b: areaIdB,
    };
    this._topology = { ...this._topology, connectors: [...connectors, connector] };
    this._saveTopology();
  }

  _removeConnector(connectorId) {
    this._topology = {
      ...this._topology,
      connectors: (this._topology.connectors ?? []).filter(
        (c) => c.connector_id !== connectorId
      ),
    };
    if (this._selectedConnectorId === connectorId) this._selectedConnectorId = null;
    this._saveTopology();
  }

  _toggleConnectorSelected(connectorId) {
    // Touch devices have no hover state, so tapping a connector is the
    // alternative way to reveal its delete control (desktop can also just
    // hover — see the `.connector:hover`/`.connector--selected` CSS).
    this._selectedConnectorId = this._selectedConnectorId === connectorId ? null : connectorId;
  }

  // -- Egress-point flagging -----------------------------------------------

  // An Area *is* an egress point exactly when it has a non-empty crossing-
  // entity list (matches the backend's own validation, which rejects an
  // egress point with zero entities) — so there's no separate on/off flag to
  // keep in sync, just this list.
  _setEgressEntities(areaId, entityIds) {
    const rest = (this._topology.egress_points ?? []).filter((e) => e.area_id !== areaId);
    const egress_points = entityIds.length ? [...rest, { area_id: areaId, entity_ids: entityIds }] : rest;
    this._topology = { ...this._topology, egress_points };
    this._saveTopology();
  }

  _toggleEgressEntity(areaId, entityId, checked) {
    const current =
      (this._topology.egress_points ?? []).find((e) => e.area_id === areaId)?.entity_ids ?? [];
    const next = checked ? [...current, entityId] : current.filter((id) => id !== entityId);
    this._setEgressEntities(areaId, next);
  }

  // -- Per-area entity selection (SPEC.md §5.2) ----------------------------

  // Independent of the access-point crossing-entity list above — an entity
  // can be in neither, either, or both (e.g. a door sensor can double as
  // general activity evidence for the room it's in) since the backend
  // places no exclusivity constraint between the two lists (websocket_api.py
  // validates them separately), so neither does this UI.
  _setAreaEntitySelections(areaId, entityIds) {
    const current = { ...(this._topology.area_entity_selections ?? {}) };
    if (entityIds.length) {
      current[areaId] = entityIds;
    } else {
      delete current[areaId];
    }
    this._topology = { ...this._topology, area_entity_selections: current };
    this._saveTopology();
  }

  _toggleAreaEntitySelection(areaId, entityId, checked) {
    const current = this._topology.area_entity_selections?.[areaId] ?? [];
    const next = checked ? [...current, entityId] : current.filter((id) => id !== entityId);
    this._setAreaEntitySelections(areaId, next);
  }

  // -- Persistence ----------------------------------------------------------

  async _saveTopology() {
    if (!this._entryId || !this.hass) return;
    const area_positions = {};
    for (const [areaId, position] of this._positions) {
      area_positions[areaId] = { x: position.x, y: position.y };
    }
    try {
      const result = await this.hass.callWS({
        type: "occupancy_tracker/topology/save",
        entry_id: this._entryId,
        connectors: this._topology.connectors ?? [],
        egress_points: this._topology.egress_points ?? [],
        area_entity_selections: this._topology.area_entity_selections ?? {},
        area_positions,
        // The "Outside" node/edges were removed from the graph (see
        // docs/DECISIONS.md's 2026-08-09 entry) — access points are shown as
        // a dashed ring on the room itself instead. outside_position stays a
        // required field in the backend's save schema (accepts null), so it's
        // always sent as null now rather than deleting it from the schema
        // just for this — no reason to force a storage migration over a
        // field that's simply unused going forward.
        outside_position: null,
      });
      this._topology = result.topology;
      // A save can reload the entry (e.g. toggling an access-point entity),
      // which replaces the engine — resubscribe unconditionally rather than
      // trying to guess whether this particular save actually triggered
      // one; a redundant resubscribe when it didn't is cheap and harmless.
      this._resubscribeEngineState();
    } catch (err) {
      // Optimistic UI already applied the change locally (docs/UX_GUIDELINES.md
      // §2) — a failed background save is logged, not thrown back in the
      // user's face for something as low-stakes as a node position.
      console.error("Occupancy Tracker: failed to save topology", err);
    }
  }

  // -- Pan & zoom -------------------------------------------------------

  _screenScale(svgEl) {
    const rect = svgEl.getBoundingClientRect();
    return { scale: this._viewBox.w / rect.width, rect };
  }

  _onWheel(e) {
    e.preventDefault();
    const { scale, rect } = this._screenScale(e.currentTarget);
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const ux = this._viewBox.x + mx * scale;
    const uy = this._viewBox.y + my * scale;
    const factor = e.deltaY > 0 ? 1.15 : 1 / 1.15;
    const newW = Math.min(MAX_VIEWBOX_W, Math.max(MIN_VIEWBOX_W, this._viewBox.w * factor));
    const newH = newW * (this._viewBox.h / this._viewBox.w);
    this._viewBox = {
      x: ux - (mx / rect.width) * newW,
      y: uy - (my / rect.height) * newH,
      w: newW,
      h: newH,
    };
  }

  _onBackgroundPointerDown(e) {
    this._selectedConnectorId = null;
    const { scale } = this._screenScale(e.currentTarget);
    const startClientX = e.clientX;
    const startClientY = e.clientY;
    const startViewBox = { ...this._viewBox };
    this._panning = true;

    const onMove = (moveEvent) => {
      const dx = (moveEvent.clientX - startClientX) * scale;
      const dy = (moveEvent.clientY - startClientY) * scale;
      this._viewBox = { ...startViewBox, x: startViewBox.x - dx, y: startViewBox.y - dy };
    };
    const onUp = () => {
      this._panning = false;
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  // -- Node dragging ------------------------------------------------------

  _onNodePointerDown(e, areaId) {
    e.stopPropagation();
    if (this._connectMode) return; // clicking, not dragging, is how connect mode works
    const svgEl = e.currentTarget.closest("svg");
    const { scale } = this._screenScale(svgEl);
    const start = this._positions.get(areaId) ?? { x: 0, y: 0 };
    const startClientX = e.clientX;
    const startClientY = e.clientY;

    const onMove = (moveEvent) => {
      const dx = (moveEvent.clientX - startClientX) * scale;
      const dy = (moveEvent.clientY - startClientY) * scale;
      let x = start.x + dx;
      let y = start.y + dy;
      if (this._gridEnabled) {
        x = Math.round(x / GRID_SIZE) * GRID_SIZE;
        y = Math.round(y / GRID_SIZE) * GRID_SIZE;
      }
      const next = new Map(this._positions);
      next.set(areaId, { x, y });
      this._positions = next;
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      const final = this._positions.get(areaId);
      if (final.x !== start.x || final.y !== start.y) {
        this._saveTopology();
      }
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  // Detail-card open/close is a small state machine, not a plain boolean,
  // so the closing transition has a frame to actually play: Lit removes the
  // card from the DOM the instant _selectedAreaId goes falsy, which would
  // otherwise skip the CSS transition entirely (no time to animate an
  // element that's already gone). "entering" needs its own extra rAF too —
  // committing "entering" and "open" styles in the same paint would collapse
  // them into one frame with nothing to transition from.
  _selectArea(areaId) {
    if (areaId === this._selectedAreaId) return;
    if (this._detailCloseTimer) {
      clearTimeout(this._detailCloseTimer);
      this._detailCloseTimer = null;
    }
    if (areaId) {
      this._selectedAreaId = areaId;
      this._detailPhase = "entering";
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          this._detailPhase = "open";
        });
      });
    } else {
      this._detailPhase = "closing";
      this._detailCloseTimer = setTimeout(() => {
        this._selectedAreaId = null;
        this._detailPhase = "closed";
        this._detailCloseTimer = null;
      }, DETAIL_TRANSITION_MS);
    }
  }

  // Suggests likely occupancy evidence for a room that has none selected yet
  // (docs/UX_GUIDELINES.md §3's "confident, sensible defaults" /
  // docs/STATUS.md's noted setup-friction item) — a one-click accept rather
  // than a silent auto-apply, since area_entity_selections has no way to
  // distinguish "never configured" from "user explicitly cleared it" (an
  // absent key means either), and silently reapplying a suggestion after
  // someone deliberately emptied a room's list would be a real correctness
  // bug, not a nicety.
  _suggestedEvidenceEntityIds(area) {
    if (!this.hass) return [];
    return area.entity_ids.filter((id) => {
      const dotIndex = id.indexOf(".");
      const domain = id.slice(0, dotIndex);
      if (!OCCUPANCY_EVIDENCE_ENTITY_DOMAINS.has(domain)) return false;
      const deviceClass = this.hass.states[id]?.attributes?.device_class;
      if (OCCUPANCY_EVIDENCE_DEVICE_CLASSES.has(deviceClass)) return true;
      return OCCUPANCY_EVIDENCE_NAME_PATTERN.test(id.slice(dotIndex + 1));
    });
  }

  // Both checklists below (egress crossing-sensors and "what counts as
  // activity") must only offer entities the backend can actually act on —
  // see SELECTABLE_EVIDENCE_ENTITY_DOMAINS's own comment.
  _selectableEntityIds(area) {
    return area.entity_ids.filter((id) =>
      SELECTABLE_EVIDENCE_ENTITY_DOMAINS.has(id.slice(0, id.indexOf(".")))
    );
  }

  async _loadEngineState() {
    if (!this._entryId || !this.hass) return;
    try {
      this._engineState = await this.hass.callWS({
        type: "occupancy_tracker/engine/get_state",
        entry_id: this._entryId,
      });
    } catch (err) {
      console.error("Occupancy Tracker: failed to load engine state", err);
      this._engineState = null;
    }
  }

  // One subscription for the panel's whole lifetime, not one per selected
  // room: the backend already returns every Area's state in a single
  // snapshot (occupancy_engine.py's engine has no per-Area subscribe
  // granularity, and there's no reason to invent one just for this), so a
  // single always-on subscription keeps _engineState fresh regardless of
  // which room happens to be selected when a change arrives — the bug this
  // was built to fix was exactly that a *stale* detail panel didn't update
  // until closed and reopened.
  async _subscribeEngineState() {
    if (!this._entryId || !this.hass) return;
    try {
      this._engineStateUnsub = await this.hass.connection.subscribeMessage(
        (event) => {
          this._engineState = event;
        },
        { type: "occupancy_tracker/engine/subscribe_updates", entry_id: this._entryId }
      );
    } catch (err) {
      console.error("Occupancy Tracker: failed to subscribe to engine state", err);
    }
  }

  // A topology save can trigger a full entry reload (websocket_api.py's
  // engine_relevant_change check — e.g. toggling an access-point entity),
  // which replaces the engine instance entirely. The backend's own
  // subscription cleanup (entry.async_on_unload) stops forwarding from the
  // now-dead engine at that point, but nothing re-subscribes to the *new*
  // one on its own — the websocket connection itself outlives any single
  // reload, so this side has to notice and re-establish it.
  async _resubscribeEngineState() {
    this._engineStateUnsub?.();
    this._engineStateUnsub = null;
    await this._loadEngineState();
    await this._subscribeEngineState();
  }

  render() {
    return html`
      <div class="toolbar">
        <ha-icon-button label="Back" @click=${() => history.back()}>
          <ha-icon icon="mdi:arrow-left"></ha-icon>
        </ha-icon-button>
        <div class="titles">
          <div class="title">Occupancy Tracker</div>
          <div class="subtitle">House topology</div>
        </div>
      </div>
      <div class="content">${this._renderContent()}</div>
    `;
  }

  _renderContent() {
    if (this._loading) {
      return html`<div class="state">
        <ha-circular-progress indeterminate></ha-circular-progress>
        <p>Loading your rooms…</p>
      </div>`;
    }
    if (this._error === "not_configured") {
      return html`<div class="state">
        <ha-icon icon="mdi:home-search-outline"></ha-icon>
        <p>Occupancy Tracker isn't set up yet.</p>
        <p class="muted">Add it from Settings → Devices & Services first.</p>
      </div>`;
    }
    if (this._error) {
      return html`<div class="state">
        <ha-icon icon="mdi:alert-circle-outline"></ha-icon>
        <p>Couldn't load your rooms.</p>
        <p class="muted">${this._error}</p>
      </div>`;
    }
    if (this._areaEntries().length === 0) {
      return html`<div class="state">
        <ha-icon icon="mdi:floor-plan"></ha-icon>
        <p>No rooms found.</p>
        <p class="muted">
          Set up your rooms in Settings → Areas &amp; Zones first, then come back here to connect
          them.
        </p>
      </div>`;
    }
    const connectors = this._topology.connectors ?? [];
    const egressPoints = this._topology.egress_points ?? [];
    const hasTopology = connectors.length > 0 || egressPoints.length > 0;

    return html`
      <div class="layout ${this.narrow ? "layout--narrow" : ""}">
        <ha-card class="graph-card">
          <div class="card-header">
            <h1>Areas &amp; connections</h1>
            ${this._renderLiveStats()}
          </div>
          <p class="card-subtitle">
            Your rooms, pulled directly from Home Assistant. Drag a room to move it, or click one
            to choose which sensors to use, flag it as an access point (a door to outside), and
            see what's happening in it right now.
          </p>
          ${
            hasTopology
              ? nothing
              : html`<div class="empty-topology-notice">
                  <ha-icon icon="mdi:vector-line"></ha-icon>
                  <span>
                    You haven't connected any rooms yet. Use "Draw connector" below to link two
                    rooms that are next to each other, or click a room to flag it as an access
                    point (a door to outside).
                  </span>
                </div>`
          }
          <div class="graph-toolbar">
            <button class="tool-btn" @click=${() => this._autoArrangeAll()}>
              <ha-icon icon="mdi:auto-fix"></ha-icon>
              Auto-arrange
            </button>
            <button class="tool-btn" @click=${() => this._fitView()}>
              <ha-icon icon="mdi:fit-to-page-outline"></ha-icon>
              Fit view
            </button>
            <button
              class="tool-btn ${this._gridEnabled ? "tool-btn--active" : ""}"
              @click=${() => this._toggleGrid()}
            >
              <ha-icon icon=${this._gridEnabled ? "mdi:grid" : "mdi:grid-off"}></ha-icon>
              Grid
            </button>
            <button
              class="tool-btn ${this._connectMode ? "tool-btn--active" : ""}"
              @click=${() => this._toggleConnectMode()}
            >
              <ha-icon icon="mdi:link-plus"></ha-icon>
              ${this._connectMode ? "Drawing connector…" : "Draw connector"}
            </button>
          </div>
          <div class="graph-wrap">${this._renderGraph()}</div>
          <div class="graph-footer">
            <p class="caption">
              ${this._connectMode
                ? this._connectSourceAreaId
                  ? "Click another room to connect it, or press Esc to cancel."
                  : "Click a room to start a connector, or press Esc to stop drawing."
                : "Drag a room to move it, scroll to zoom, drag the background to pan. Hover or tap a connector, then click × to remove it."}
            </p>
            ${
              hasTopology
                ? html`<div class="legend">
                    <span class="legend-item"
                      ><span class="legend-swatch"></span>Line = these rooms are connected</span
                    >
                    <span class="legend-item"
                      ><span class="legend-swatch legend-swatch--egress"></span>Dashed ring = this
                      room has an access point (a door to outside)</span
                    >
                    <span class="legend-item">
                      <span class="legend-dots">
                        <span class="legend-dot legend-dot--confirmed"></span>
                        <span class="legend-dot legend-dot--latched"></span>
                        <span class="legend-dot legend-dot--ambiguous"></span>
                      </span>
                      Ring color = confirmed / latched / ambiguous
                    </span>
                  </div>`
                : nothing
            }
          </div>
        </ha-card>
        ${this._selectedAreaId ? this._renderDetail() : nothing}
      </div>
    `;
  }

  _areaEntries() {
    // house_shape.areas is a JSON array (see websocket_api.py's
    // _house_shape_json), not a map — keep a single helper so layout/render
    // code doesn't re-derive this shape in more than one place.
    return this._houseShape?.areas ?? [];
  }

  // Mirrors topology_store.py's active_area_ids(): a room only gets real HA
  // entities (and is only actually tracked) once it has at least one
  // activity-evidence entity selected or is an access point — project-owner
  // feedback that an untouched room's sensors were pure clutter. Used here
  // to visually dim untracked rooms in the graph and explain why in the
  // detail panel, so the backend's housekeeping doesn't look like rooms are
  // silently missing.
  _isAreaActive(areaId) {
    const hasEvidence = (this._topology.area_entity_selections?.[areaId] ?? []).length > 0;
    const isAccessPoint = (this._topology.egress_points ?? []).some((e) => e.area_id === areaId);
    return hasEvidence || isAccessPoint;
  }

  // Total occupancy + pending-transit count, always visible on the main
  // graph page rather than buried in a per-room click-through — added for
  // walking-the-house live debugging (project-owner feedback): both values
  // are already pushed live via _subscribeEngineState's subscription, this
  // just surfaces what was already being fetched.
  _renderLiveStats() {
    if (!this._engineState) return nothing;
    const total = this._engineState.total_occupant_count ?? 0;
    const pending = this._engineState.pending_transits?.length ?? 0;
    return html`
      <div class="live-stats">
        <span class="badge badge--stat">
          <ha-icon icon="mdi:account-multiple"></ha-icon>
          ${total} total occupant${total === 1 ? "" : "s"}
        </span>
        ${pending
          ? html`<span class="badge badge--stat badge--pending">
              <ha-icon icon="mdi:transit-connection-variant"></ha-icon>
              ${pending} pending transit${pending === 1 ? "" : "s"}
            </span>`
          : nothing}
      </div>
    `;
  }

  _renderGraph() {
    const areas = this._areaEntries();
    const connectors = this._topology.connectors ?? [];
    const egressPoints = this._topology.egress_points ?? [];
    const egressAreaIds = new Set(egressPoints.map((e) => e.area_id));
    const positions = this._positions;

    const vb = this._viewBox;

    return svg`
      <svg
        viewBox="${vb.x} ${vb.y} ${vb.w} ${vb.h}"
        class="graph ${this._panning ? "graph--panning" : ""} ${
          this._connectMode ? "graph--connecting" : ""
        }"
        @wheel=${(e) => this._onWheel(e)}
        @pointerdown=${(e) => this._onBackgroundPointerDown(e)}
        @pointermove=${(e) => this._onGraphPointerMove(e)}
      >
        <defs>
          <pattern id="ot-grid" width=${GRID_SIZE} height=${GRID_SIZE} patternUnits="userSpaceOnUse">
            <circle cx="0" cy="0" r="1.4" class="grid-dot"></circle>
          </pattern>
        </defs>
        ${
          this._gridEnabled
            ? svg`<rect x=${vb.x} y=${vb.y} width=${vb.w} height=${vb.h} fill="url(#ot-grid)"></rect>`
            : nothing
        }
        ${connectors.map((connector) => {
          const a = positions.get(connector.area_id_a);
          const b = positions.get(connector.area_id_b);
          if (!a || !b) return nothing;
          // The delete control sits at the true midpoint between the node
          // centers — trimming both ends by the same radius doesn't move
          // that midpoint, so it's computed from a/b, not the trimmed points.
          const mx = (a.x + b.x) / 2;
          const my = (a.y + b.y) / 2;
          const aEdge = pointTowardsEdge(a, b, NODE_RADIUS);
          const bEdge = pointTowardsEdge(b, a, NODE_RADIUS);
          const selected = connector.connector_id === this._selectedConnectorId;
          return svg`
            <g
              class="connector ${selected ? "connector--selected" : ""}"
              tabindex="0"
              role="button"
              aria-label="Remove connector"
              @keydown=${(e) => {
                if (e.key === "Enter" || e.key === "Delete" || e.key === "Backspace") {
                  e.preventDefault();
                  this._removeConnector(connector.connector_id);
                }
              }}
            >
              <line
                class="edge-hit"
                x1=${aEdge.x}
                y1=${aEdge.y}
                x2=${bEdge.x}
                y2=${bEdge.y}
                @click=${(e) => {
                  e.stopPropagation();
                  if (!this._connectMode) this._toggleConnectorSelected(connector.connector_id);
                }}
              ></line>
              <line class="edge" x1=${aEdge.x} y1=${aEdge.y} x2=${bEdge.x} y2=${bEdge.y}></line>
              <g
                class="edge-delete"
                transform="translate(${mx}, ${my})"
                @click=${(e) => {
                  e.stopPropagation();
                  this._removeConnector(connector.connector_id);
                }}
              >
                <circle r="9"></circle>
                <path d="M-4,-4 L4,4 M-4,4 L4,-4"></path>
              </g>
            </g>
          `;
        })}
        ${
          this._connectSourceAreaId && this._connectPreviewPoint && positions.get(this._connectSourceAreaId)
            ? (() => {
                const src = positions.get(this._connectSourceAreaId);
                const start = pointTowardsEdge(src, this._connectPreviewPoint, NODE_RADIUS);
                return svg`<line
                  class="edge edge--preview"
                  x1=${start.x}
                  y1=${start.y}
                  x2=${this._connectPreviewPoint.x}
                  y2=${this._connectPreviewPoint.y}
                ></line>`;
              })()
            : nothing
        }
        ${areas.map((area) => {
          const p = positions.get(area.area_id);
          if (!p) return nothing;
          const isEgress = egressAreaIds.has(area.area_id);
          const selected = area.area_id === this._selectedAreaId;
          const isConnectSource = area.area_id === this._connectSourceAreaId;
          const isActive = this._isAreaActive(area.area_id);
          const state = isActive ? this._engineState?.areas?.[area.area_id] : null;
          // Quality (SPEC.md §6.8: confirmed/latched/ambiguous) as a ring
          // color, readable at a glance while walking the house — same
          // colors as the detail panel's own quality chip (chip--confirmed
          // etc.), just applied to the node's stroke instead of a dot, so
          // there's nothing new to learn between the two views.
          //
          // LATCHED only means "not fresh, but the last real evidence
          // stands" (SPEC.md §6.2) — the engine's quality tier tracks
          // freshness only, not the count's sign, so a room that's never had
          // any evidence at all (occupant_count 0, never confirmed) also
          // reports LATCHED, identically to a room latched *occupied*.
          // Coloring both the same muted gray erases the plain "this room
          // is tracked" signal a fresh, still-empty room used to show (the
          // default ring color) and reads as "probably occupied" for a room
          // that's actually empty. Only color the ring by quality once
          // there's an actual occupant it's describing.
          const quality = state && (state.quality !== "LATCHED" || state.occupant_count > 0)
            ? state.quality
            : null;
          const classes = [
            "node",
            isEgress ? "node--egress" : "",
            selected ? "node--selected" : "",
            isConnectSource ? "node--connect-source" : "",
            isActive ? "" : "node--inactive",
            quality ? `node--quality-${quality.toLowerCase()}` : "",
          ]
            .filter(Boolean)
            .join(" ");
          const onActivate = () =>
            this._connectMode ? this._onNodeConnectClick(area.area_id) : this._selectArea(area.area_id);
          return svg`
            <g
              class=${classes}
              transform="translate(${p.x}, ${p.y})"
              tabindex="0"
              role="button"
              aria-label=${area.name}
              @pointerdown=${(e) => this._onNodePointerDown(e, area.area_id)}
              @click=${onActivate}
              @keydown=${(e) => {
                if (e.key === "Enter" || e.key === " ") onActivate();
              }}
            >
              <circle r=${NODE_RADIUS}></circle>
              ${
                isActive
                  ? svg`<text class="node-count" dy="0.35em">${this._engineState?.areas?.[area.area_id]?.occupant_count ?? 0}</text>`
                  : nothing
              }
              <text dy=${NODE_RADIUS + 16}>${area.name}</text>
            </g>
          `;
        })}
      </svg>
    `;
  }

  // The explainability inspector (SPEC.md §7.3's "how did it know that"
  // moment) — live signals, confidence tier, and transit reasoning for the
  // selected Area, fetched from occupancy_tracker/engine/get_state.
  _renderExplainability(area) {
    const state = this._engineState?.areas?.[area.area_id];
    if (!state) {
      return html`<p class="muted">Loading current state…</p>`;
    }
    const pendingTransit = (this._engineState.pending_transits ?? []).find(
      (t) => t.area_id_a === area.area_id || t.area_id_b === area.area_id
    );
    const qualityKey = state.quality.toLowerCase();
    // LATCHED with zero occupants means "empty, and not fresh evidence of
    // that either" — not "probably occupied" (see _renderGraph's identical
    // reasoning for the node ring color; same underlying quirk of the
    // engine's quality tier, fixed the same way here).
    const qualityLabel =
      state.quality === "LATCHED" && state.occupant_count === 0
        ? "Not occupied"
        : (QUALITY_LABELS[state.quality] ?? state.quality);

    return html`
      <div class="explain">
        <div class="explain-row">
          <span class="chip chip--${qualityKey}"
            ><span class="chip-dot"></span>${qualityLabel}</span
          >
          <span class="occupant-count">
            ${state.occupant_count} occupant${state.occupant_count === 1 ? "" : "s"}
          </span>
        </div>
        <p class="muted">
          Last confirmed:
          ${state.last_confirmed ? this._formatRelativeTime(state.last_confirmed) : "never"}
          ${state.last_provenance
            ? html` — ${PROVENANCE_LABELS[state.last_provenance] ?? state.last_provenance}`
            : nothing}
        </p>
        ${pendingTransit
          ? html`<p class="explain-pending">
              <ha-icon icon="mdi:transit-connection-variant"></ha-icon>
              Unconfirmed transit ${this._transitOtherSideLabel(pendingTransit, area.area_id)}
            </p>`
          : nothing}
      </div>
    `;
  }

  _transitOtherSideLabel(transit, areaId) {
    const otherId = transit.area_id_a === areaId ? transit.area_id_b : transit.area_id_a;
    if (otherId === "outside") return "with Outside";
    const other = this._areaEntries().find((a) => a.area_id === otherId);
    return other ? `with ${other.name}` : "";
  }

  _formatRelativeTime(isoString) {
    const diffMs = new Date(isoString).getTime() - Date.now();
    const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
    const diffSeconds = Math.round(diffMs / 1000);
    if (Math.abs(diffSeconds) < 60) return rtf.format(diffSeconds, "second");
    const diffMinutes = Math.round(diffSeconds / 60);
    if (Math.abs(diffMinutes) < 60) return rtf.format(diffMinutes, "minute");
    const diffHours = Math.round(diffMinutes / 60);
    if (Math.abs(diffHours) < 24) return rtf.format(diffHours, "hour");
    return rtf.format(Math.round(diffHours / 24), "day");
  }

  _renderDetail() {
    const area = this._areaEntries().find((a) => a.area_id === this._selectedAreaId);
    if (!area) return nothing;
    const entitiesById = new Map((this._houseShape.entities ?? []).map((e) => [e.entity_id, e]));
    const egressPoint = (this._topology.egress_points ?? []).find(
      (e) => e.area_id === area.area_id
    );
    const selectedEntityIds = this._topology.area_entity_selections?.[area.area_id] ?? [];
    const suggestedEntityIds = selectedEntityIds.length
      ? []
      : this._suggestedEvidenceEntityIds(area);
    const selectableEntityIds = this._selectableEntityIds(area);
    const unsupportedNotice = html`<p class="muted">
      None of this room's Home Assistant entities can be used yet — only motion/contact sensors,
      switches, lights, and helpers are currently supported.
    </p>`;

    return html`
      <ha-card class="detail-card detail-card--${this._detailPhase}">
        <div class="detail-header">
          <h2>${area.name}</h2>
          <ha-icon-button label="Close" @click=${() => this._selectArea(null)}>
            <ha-icon icon="mdi:close"></ha-icon>
          </ha-icon-button>
        </div>
        <div class="detail-body">
          ${
            this._isAreaActive(area.area_id)
              ? nothing
              : html`<div class="empty-topology-notice">
                  <ha-icon icon="mdi:sleep"></ha-icon>
                  <span
                    >Not tracked yet — pick an access point or something under "What counts as
                    activity" below to start counting occupancy in this room.</span
                  >
                </div>`
          }
          ${this._renderExplainability(area)}
          <p class="row">
            <ha-icon icon=${egressPoint ? "mdi:door-open" : "mdi:door-closed"}></ha-icon>
            ${egressPoint ? "Access point" : "Not an access point"}
          </p>
          <p class="muted">
            Check the door or window sensor(s) in this room that would notice someone coming in
            or going out.
          </p>
          ${selectableEntityIds.length
            ? html`<ul class="checklist">
                ${selectableEntityIds.map((id) => {
                  const checked = egressPoint?.entity_ids.includes(id) ?? false;
                  return html`
                    <li>
                      <label class="checklist-item">
                        <input
                          type="checkbox"
                          .checked=${checked}
                          @change=${(e) =>
                            this._toggleEgressEntity(area.area_id, id, e.target.checked)}
                        />
                        <span title=${id}>${this._entityLabel(id, entitiesById)}</span>
                      </label>
                    </li>
                  `;
                })}
              </ul>`
            : area.entity_ids.length
              ? unsupportedNotice
              : html`<p class="muted">
                  This room has no sensors or devices in Home Assistant yet.
                </p>`}
          <p class="row"><ha-icon icon="mdi:eye-check-outline"></ha-icon> What counts as activity</p>
          <p class="muted">
            Check any sensors or devices that mean someone's in this room — a motion sensor, a
            light turning on, a smart plug switching on. This room is only ever counted as
            occupied because of these.
          </p>
          ${
            suggestedEntityIds.length
              ? html`<div class="suggestion-row">
                  <ha-icon icon="mdi:auto-fix"></ha-icon>
                  <span>
                    ${suggestedEntityIds.length === 1
                      ? "This room already has a motion sensor Home Assistant knows about."
                      : "This room already has motion sensors Home Assistant knows about."}
                  </span>
                  <button
                    class="tool-btn"
                    @click=${() =>
                      this._setAreaEntitySelections(area.area_id, suggestedEntityIds)}
                  >
                    Use ${suggestedEntityIds.length === 1 ? "it" : "them"}
                  </button>
                </div>`
              : nothing
          }
          ${selectableEntityIds.length
            ? html`<ul class="checklist">
                ${selectableEntityIds.map((id) => {
                  const checked = selectedEntityIds.includes(id);
                  return html`
                    <li>
                      <label class="checklist-item">
                        <input
                          type="checkbox"
                          .checked=${checked}
                          @change=${(e) =>
                            this._toggleAreaEntitySelection(area.area_id, id, e.target.checked)}
                        />
                        <span title=${id}>${this._entityLabel(id, entitiesById)}</span>
                      </label>
                    </li>
                  `;
                })}
              </ul>`
            : area.entity_ids.length
              ? unsupportedNotice
              : html`<p class="muted">
                  This room has no sensors or devices in Home Assistant yet.
                </p>`}
        </div>
      </ha-card>
    `;
  }

  _entityLabel(entityId, entitiesById) {
    const entity = entitiesById.get(entityId);
    const label = entity?.name || entityId;
    return entity?.disabled ? `${label} (disabled)` : label;
  }

  static styles = css`
    :host {
      display: block;
      height: 100%;
      background: var(--primary-background-color);
      color: var(--primary-text-color);
      box-sizing: border-box;
    }
    .toolbar {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 16px;
      background: var(--app-header-background-color, var(--primary-color));
      color: var(--app-header-text-color, white);
      box-shadow: 0 1px 4px rgba(0, 0, 0, 0.2);
    }
    .titles {
      display: flex;
      flex-direction: column;
      line-height: 1.2;
    }
    .title {
      font-size: 20px;
      font-weight: 400;
    }
    .subtitle {
      font-size: 13px;
      opacity: 0.8;
    }
    .content {
      padding: 16px;
      max-width: 1100px;
      margin: 0 auto;
    }
    .state {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 8px;
      padding: 64px 16px;
      text-align: center;
      color: var(--secondary-text-color);
    }
    .state ha-icon {
      --mdc-icon-size: 48px;
      color: var(--secondary-text-color);
    }
    .muted {
      color: var(--secondary-text-color);
      font-size: 14px;
    }
    .layout {
      display: flex;
      gap: 16px;
      align-items: flex-start;
    }
    .layout--narrow {
      flex-direction: column;
    }
    .graph-card {
      flex: 1;
      padding: 16px;
      box-sizing: border-box;
    }
    .card-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      flex-wrap: wrap;
    }
    .card-header h1 {
      font-size: 18px;
      font-weight: 500;
      margin: 0;
    }
    .badge {
      font-size: 12px;
      font-weight: 500;
      color: var(--secondary-text-color);
      background: var(--secondary-background-color, rgba(127, 127, 127, 0.15));
      border-radius: 12px;
      padding: 2px 10px;
      white-space: nowrap;
    }
    .card-subtitle {
      color: var(--secondary-text-color);
      font-size: 14px;
      margin: 4px 0 16px;
    }
    .live-stats {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .badge--stat {
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    .badge--stat ha-icon {
      --mdc-icon-size: 15px;
    }
    .badge--pending {
      color: var(--warning-color, #ff9800);
    }
    .graph-footer {
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      gap: 8px;
      margin-top: 8px;
    }
    .legend {
      display: flex;
      flex-wrap: wrap;
      gap: 16px;
    }
    .legend-item {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      color: var(--secondary-text-color);
    }
    .legend-swatch {
      display: inline-block;
      width: 20px;
      height: 0;
      border-top: 2px solid var(--divider-color, #e0e0e0);
    }
    /* Mirrors .node--egress circle's own dashed ring, not a line — access
       points are shown as a style on the room's own node, not a separate
       edge (see docs/DECISIONS.md's 2026-08-09 "Outside" node removal). */
    .legend-swatch--egress {
      width: 14px;
      height: 14px;
      border: 2px dashed var(--primary-color);
      border-radius: 50%;
    }
    .legend-dots {
      display: inline-flex;
      gap: 3px;
    }
    .legend-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
    }
    .legend-dot--confirmed {
      background: var(--success-color, #4caf50);
    }
    .legend-dot--latched {
      background: var(--secondary-text-color, #888);
    }
    .legend-dot--ambiguous {
      background: var(--warning-color, #ff9800);
    }
    .empty-topology-notice {
      display: flex;
      align-items: flex-start;
      gap: 8px;
      background: var(--secondary-background-color, rgba(127, 127, 127, 0.1));
      border-radius: var(--ha-card-border-radius, 12px);
      padding: 10px 12px;
      margin-bottom: 12px;
      font-size: 13px;
      color: var(--secondary-text-color);
    }
    .empty-topology-notice ha-icon {
      --mdc-icon-size: 20px;
      flex-shrink: 0;
      margin-top: 1px;
    }
    .suggestion-row {
      display: flex;
      align-items: center;
      gap: 8px;
      background: var(--secondary-background-color, rgba(127, 127, 127, 0.1));
      border-radius: var(--ha-card-border-radius, 12px);
      padding: 8px 10px;
      margin: 4px 0 12px;
      font-size: 13px;
      color: var(--secondary-text-color);
    }
    .suggestion-row ha-icon {
      --mdc-icon-size: 20px;
      color: var(--primary-color);
      flex-shrink: 0;
    }
    .suggestion-row span {
      flex: 1;
    }
    .graph-toolbar {
      display: flex;
      gap: 8px;
      margin-bottom: 8px;
      flex-wrap: wrap;
    }
    .tool-btn {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font: inherit;
      font-size: 13px;
      font-weight: 500;
      color: var(--primary-text-color);
      background: var(--secondary-background-color, rgba(127, 127, 127, 0.12));
      border: none;
      border-radius: 16px;
      padding: 6px 12px;
      cursor: pointer;
    }
    .tool-btn ha-icon {
      --mdc-icon-size: 18px;
    }
    .tool-btn:hover {
      background: var(--divider-color, rgba(127, 127, 127, 0.25));
    }
    .tool-btn--active {
      color: var(--text-primary-color, white);
      background: var(--primary-color);
    }
    .caption {
      color: var(--secondary-text-color);
      font-size: 12px;
      margin: 0;
    }
    .detail-card {
      width: 320px;
      flex-shrink: 0;
      box-sizing: border-box;
      opacity: 1;
      transform: translateY(0);
      /* Duration must match DETAIL_TRANSITION_MS, which keeps the card
         mounted this long after deselection so this can actually play. */
      transition: opacity 180ms ease, transform 180ms ease;
    }
    .detail-card--entering,
    .detail-card--closing {
      opacity: 0;
      transform: translateY(6px);
    }
    @media (prefers-reduced-motion: reduce) {
      .detail-card {
        transition: none;
      }
    }
    .layout--narrow .detail-card {
      width: 100%;
    }
    .graph-wrap {
      width: 100%;
      max-width: 640px;
      /* Must match the VIEWPORT_ASPECT JS constant exactly — see its comment. */
      aspect-ratio: 640 / 460;
      margin: 0 auto;
      overflow: hidden;
      border-radius: var(--ha-card-border-radius, 12px);
      border: 1px solid var(--divider-color, #e0e0e0);
      touch-action: none;
    }
    .graph {
      width: 100%;
      height: 100%;
      display: block;
      cursor: grab;
    }
    .graph--panning {
      cursor: grabbing;
    }
    .graph--connecting .node circle {
      cursor: pointer;
    }
    .grid-dot {
      fill: var(--secondary-text-color);
      opacity: 0.3;
    }
    .edge {
      /* A lighter tint of the same color used for area nodes, not an
         unrelated neutral grey — --rgb-primary-color is a real HA theme
         token (verified present in the installed hass_frontend bundle). */
      stroke: rgba(var(--rgb-primary-color), 0.5);
      stroke-width: 2;
    }
    .edge--preview {
      stroke: var(--primary-color);
      stroke-width: 2;
      stroke-dasharray: 5 4;
      pointer-events: none;
    }
    .connector {
      outline: none;
    }
    .connector:hover .edge,
    .connector:focus-visible .edge,
    .connector--selected .edge {
      stroke: var(--error-color, #db4437);
      stroke-width: 3;
    }
    .edge-hit {
      stroke: transparent;
      stroke-width: 16;
      pointer-events: stroke;
      cursor: pointer;
    }
    .edge-delete {
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.15s ease;
      cursor: pointer;
    }
    .connector:hover .edge-delete,
    .connector:focus-visible .edge-delete,
    .connector--selected .edge-delete {
      opacity: 1;
      pointer-events: all;
    }
    .edge-delete circle {
      fill: var(--error-color, #db4437);
    }
    .edge-delete path {
      stroke: white;
      stroke-width: 1.6;
    }
    .node circle {
      fill: var(--card-background-color);
      stroke: var(--primary-color);
      stroke-width: 2;
      cursor: grab;
    }
    .node text {
      fill: var(--primary-text-color);
      font-size: 13px;
      text-anchor: middle;
      pointer-events: none;
    }
    .node text.node-count {
      font-size: 15px;
      font-weight: 600;
    }
    .node--egress circle {
      stroke-dasharray: 3 2;
    }
    /* Not tracked yet — no activity evidence and not an access point, so
       this room has no HA entities at all (project-owner feedback: an
       untouched room shouldn't get sensors, but should still look visibly
       different so that's not mistaken for a bug). Dims via stroke-opacity,
       not element-level opacity on the whole circle — opacity there would
       make the node's own fill translucent too, letting a connector line
       drawn underneath bleed through it (the line endpoints are also now
       trimmed to the circle's edge, so this is belt-and-braces, not the only
       fix — see pointTowardsEdge's comment). */
    .node--inactive circle {
      stroke: var(--secondary-text-color);
      stroke-opacity: 0.6;
    }
    .node--inactive text {
      opacity: 0.6;
    }
    /* Quality ring color (SPEC.md §6.8) — same tokens as the detail panel's
       chip--confirmed/latched/ambiguous dots, applied to the node's stroke
       instead so a room's current belief is readable without clicking in.
       Placed before .node--selected/.node--connect-source below so those
       still win when a node is simultaneously mid-interaction. */
    .node--quality-confirmed circle {
      stroke: var(--success-color, #4caf50);
    }
    .node--quality-latched circle {
      stroke: var(--secondary-text-color, #888);
    }
    .node--quality-ambiguous circle {
      stroke: var(--warning-color, #ff9800);
      stroke-width: 3;
    }
    .node--selected circle {
      fill: var(--primary-color);
    }
    .node--selected text.node-count {
      fill: var(--text-primary-color, white);
    }
    .node--connect-source circle {
      stroke: var(--primary-color);
      stroke-width: 4;
    }
    .node:focus-visible circle {
      stroke-width: 4;
    }
    .detail-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 8px 16px 0;
    }
    .detail-header h2 {
      font-size: 18px;
      font-weight: 500;
      margin: 0;
    }
    .detail-body {
      padding: 0 16px 16px;
    }
    .detail-body .row {
      display: flex;
      align-items: center;
      gap: 8px;
      font-weight: 500;
    }
    .explain {
      margin-bottom: 12px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--divider-color, #e0e0e0);
    }
    .explain-row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 4px;
    }
    /* A neutral pill (the same tokens .badge already uses) carrying the
       state's color as a small dot rather than as the pill's own fill:
       solid --success-color/--warning-color behind white text both fail
       WCAG contrast outright (verified against the installed
       home-assistant-frontend bundle's actual values, #43a047/#ffa600 —
       around 3.3:1 and 2:1, need 4.5:1), and --secondary-text-color flips
       from readable to nearly invisible between themes since the token
       itself inverts brightness by theme (#5e5e5e light / #ccc dark — white
       text on #ccc is ~1.6:1). Pairing a solid dot with normal
       --primary-text-color text sidesteps per-token contrast tuning
       entirely, since that pairing is the one HA's own themes already
       guarantee stays legible. */
    .chip {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      font-weight: 500;
      padding: 2px 10px 2px 8px;
      border-radius: 12px;
      background: var(--secondary-background-color, rgba(127, 127, 127, 0.15));
      color: var(--primary-text-color);
    }
    .chip-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      flex-shrink: 0;
    }
    .chip--confirmed .chip-dot {
      background: var(--success-color, #4caf50);
    }
    .chip--latched .chip-dot {
      background: var(--secondary-text-color, #888);
    }
    .chip--ambiguous .chip-dot {
      background: var(--warning-color, #ff9800);
    }
    .occupant-count {
      font-weight: 500;
    }
    .explain-pending {
      display: flex;
      align-items: center;
      gap: 6px;
      margin: 4px 0 0;
      font-size: 13px;
      color: var(--warning-color, #ff9800);
    }
    .explain-pending ha-icon {
      --mdc-icon-size: 16px;
    }
    .detail-body ul {
      margin: 4px 0 16px;
      padding-left: 16px;
      color: var(--secondary-text-color);
      font-size: 14px;
    }
    .checklist {
      list-style: none;
      padding-left: 0;
    }
    .checklist-item {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 3px 0;
      color: var(--primary-text-color);
      cursor: pointer;
    }
    .checklist-item input[type="checkbox"] {
      width: 16px;
      height: 16px;
      accent-color: var(--primary-color);
      cursor: pointer;
      flex-shrink: 0;
    }
  `;
}

customElements.define("occupancy-tracker-topology-panel", OccupancyTrackerTopologyPanel);
