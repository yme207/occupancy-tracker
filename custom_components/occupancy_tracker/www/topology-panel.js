// Occupancy Tracker topology editor panel (docs/SPEC.md §7.3). Registered by
// panel.py via panel_custom, served at
// /occupancy_tracker_static/topology-panel.js. Talks only to the websocket
// commands in websocket_api.py (occupancy_tracker/topology/get + /save) and
// the core config_entries/get command (to resolve this integration's single
// config entry id without hardcoding it into the panel's static
// registration, which would go stale if the entry were ever removed and
// re-added — see docs/DECISIONS.md).
//
// Connector-drawing and egress-point flagging aren't built yet (still
// read-only for those) — this slice is: live-loaded areas, pan/zoom,
// draggable + grid-snapped + persisted node layout, and a floor-aware
// auto-arrange, so the graph is actually usable to look at and organize
// before editing lands on top of it.
import { LitElement, html, svg, css, nothing } from "./vendor/lit-core.min.js";

const NODE_RADIUS = 22;
const OUTSIDE_ID = "__outside__";
const GRID_SIZE = 40;
const MIN_VIEWBOX_W = 240;
const MAX_VIEWBOX_W = 4000;
const AUTO_LAYOUT_CELL = 140;
const AUTO_LAYOUT_BAND_GAP = 70;
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
    return positions;
  }

  _autoArrangeAll() {
    this._positions = this._autoLayout(this._areaEntries());
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
      });
      this._topology = result.topology;
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

  _selectArea(areaId) {
    this._selectedAreaId = areaId;
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
        <p>Loading topology…</p>
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
        <p>Couldn't load the topology.</p>
        <p class="muted">${this._error}</p>
      </div>`;
    }
    if (this._areaEntries().length === 0) {
      return html`<div class="state">
        <ha-icon icon="mdi:floor-plan"></ha-icon>
        <p>No areas found.</p>
        <p class="muted">
          Assign entities to Areas in Settings → Areas & Zones, then come back here to draw
          connectors between them.
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
            <span class="badge">Read-only preview</span>
          </div>
          <p class="card-subtitle">
            Areas are pulled live from Home Assistant's own Areas &amp; Zones — drag a room to
            reposition it, or click one to see what Occupancy Tracker currently knows about it.
          </p>
          ${
            hasTopology
              ? html`<div class="legend">
                  <span class="legend-item"
                    ><span class="legend-swatch"></span>Connector — a passage between two areas</span
                  >
                  <span class="legend-item"
                    ><span class="legend-swatch legend-swatch--egress"></span>Egress route to
                    outside</span
                  >
                </div>`
              : html`<div class="empty-topology-notice">
                  <ha-icon icon="mdi:vector-line"></ha-icon>
                  <span>
                    No connections drawn yet. Drawing connectors between areas and marking egress
                    points is coming in an upcoming update — for now you can arrange your areas.
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
          </div>
          <div class="graph-wrap">${this._renderGraph()}</div>
          <p class="caption">Drag to move, scroll to zoom, drag the background to pan.</p>
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

  _renderGraph() {
    const areas = this._areaEntries();
    const connectors = this._topology.connectors ?? [];
    const egressPoints = this._topology.egress_points ?? [];
    const egressAreaIds = new Set(egressPoints.map((e) => e.area_id));
    const hasOutside = egressAreaIds.size > 0;
    const positions = this._positions;

    let outside = null;
    if (hasOutside && positions.size) {
      const allPoints = [...positions.values()];
      const minX = Math.min(...allPoints.map((p) => p.x));
      const maxX = Math.max(...allPoints.map((p) => p.x));
      const minY = Math.min(...allPoints.map((p) => p.y));
      outside = { x: (minX + maxX) / 2, y: minY - 100 };
    }

    const vb = this._viewBox;

    return svg`
      <svg
        viewBox="${vb.x} ${vb.y} ${vb.w} ${vb.h}"
        class="graph ${this._panning ? "graph--panning" : ""}"
        @wheel=${(e) => this._onWheel(e)}
        @pointerdown=${(e) => this._onBackgroundPointerDown(e)}
      >
        <defs>
          <pattern id="ot-grid" width=${GRID_SIZE} height=${GRID_SIZE} patternUnits="userSpaceOnUse">
            <circle cx="1" cy="1" r="1.4" class="grid-dot"></circle>
          </pattern>
        </defs>
        ${
          this._gridEnabled
            ? svg`<rect x=${vb.x} y=${vb.y} width=${vb.w} height=${vb.h} fill="url(#ot-grid)"></rect>`
            : nothing
        }
        ${
          outside
            ? [...egressAreaIds].map((areaId) => {
                const p = positions.get(areaId);
                return p
                  ? svg`<line class="edge edge--egress" x1=${outside.x} y1=${outside.y} x2=${p.x} y2=${p.y}></line>`
                  : nothing;
              })
            : nothing
        }
        ${connectors.map((connector) => {
          const a = positions.get(connector.area_id_a);
          const b = positions.get(connector.area_id_b);
          return a && b
            ? svg`<line class="edge" x1=${a.x} y1=${a.y} x2=${b.x} y2=${b.y}></line>`
            : nothing;
        })}
        ${
          outside
            ? svg`<g class="node node--outside" transform="translate(${outside.x}, ${outside.y})">
                <circle r=${NODE_RADIUS}></circle>
                <text dy=${NODE_RADIUS + 16}>Outside</text>
              </g>`
            : nothing
        }
        ${areas.map((area) => {
          const p = positions.get(area.area_id);
          if (!p) return nothing;
          const isEgress = egressAreaIds.has(area.area_id);
          const selected = area.area_id === this._selectedAreaId;
          const classes = ["node", isEgress ? "node--egress" : "", selected ? "node--selected" : ""]
            .filter(Boolean)
            .join(" ");
          return svg`
            <g
              class=${classes}
              transform="translate(${p.x}, ${p.y})"
              tabindex="0"
              role="button"
              aria-label=${area.name}
              @pointerdown=${(e) => this._onNodePointerDown(e, area.area_id)}
              @click=${() => this._selectArea(area.area_id)}
              @keydown=${(e) => {
                if (e.key === "Enter" || e.key === " ") this._selectArea(area.area_id);
              }}
            >
              <circle r=${NODE_RADIUS}></circle>
              <text dy=${NODE_RADIUS + 16}>${area.name}</text>
            </g>
          `;
        })}
      </svg>
    `;
  }

  _renderDetail() {
    const area = this._areaEntries().find((a) => a.area_id === this._selectedAreaId);
    if (!area) return nothing;
    const entitiesById = new Map((this._houseShape.entities ?? []).map((e) => [e.entity_id, e]));
    const egressPoint = (this._topology.egress_points ?? []).find(
      (e) => e.area_id === area.area_id
    );
    const selectedEntityIds = this._topology.area_entity_selections?.[area.area_id] ?? [];

    return html`
      <ha-card class="detail-card">
        <div class="detail-header">
          <h2>${area.name}</h2>
          <ha-icon-button label="Close" @click=${() => this._selectArea(null)}>
            <ha-icon icon="mdi:close"></ha-icon>
          </ha-icon-button>
        </div>
        <div class="detail-body">
          <p class="row">
            <ha-icon icon=${egressPoint ? "mdi:door-open" : "mdi:door-closed"}></ha-icon>
            ${egressPoint ? "Egress point" : "Not an egress point"}
          </p>
          ${egressPoint
            ? html`<ul>
                ${egressPoint.entity_ids.map(
                  (id) => html`<li>${this._entityLabel(id, entitiesById)}</li>`
                )}
              </ul>`
            : nothing}
          <p class="row"><ha-icon icon="mdi:cog-outline"></ha-icon> Selected entities</p>
          ${selectedEntityIds.length
            ? html`<ul>
                ${selectedEntityIds.map((id) => html`<li>${this._entityLabel(id, entitiesById)}</li>`)}
              </ul>`
            : html`<p class="muted">No entities selected for this area yet.</p>`}
        </div>
      </ha-card>
    `;
  }

  _entityLabel(entityId, entitiesById) {
    const entity = entitiesById.get(entityId);
    if (entity?.disabled) return `${entityId} (disabled)`;
    return entityId;
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
    .legend {
      display: flex;
      flex-wrap: wrap;
      gap: 16px;
      margin-bottom: 12px;
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
    .legend-swatch--egress {
      border-top-style: dashed;
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
      text-align: center;
      color: var(--secondary-text-color);
      font-size: 12px;
      margin: 8px 0 0;
    }
    .detail-card {
      width: 320px;
      flex-shrink: 0;
      box-sizing: border-box;
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
    .grid-dot {
      fill: var(--secondary-text-color);
      opacity: 0.3;
    }
    .edge {
      stroke: var(--divider-color, #e0e0e0);
      stroke-width: 2;
    }
    .edge--egress {
      stroke-dasharray: 4 4;
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
    .node--egress circle {
      stroke-dasharray: 3 2;
    }
    .node--selected circle {
      fill: var(--primary-color);
    }
    .node--outside circle {
      fill: var(--secondary-background-color, #eee);
      stroke: var(--secondary-text-color);
      stroke-dasharray: 3 2;
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
    .detail-body ul {
      margin: 4px 0 16px;
      padding-left: 16px;
      color: var(--secondary-text-color);
      font-size: 14px;
    }
  `;
}

customElements.define("occupancy-tracker-topology-panel", OccupancyTrackerTopologyPanel);
