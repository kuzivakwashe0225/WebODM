import L from 'leaflet';
import ReactDOM from 'ReactDOM';
import React from 'React';
import $ from 'jquery';
import './app.scss';
import PreciseAgricPanel from './PreciseAgricPanel';
import CropButton from 'webodm/components/CropButton';
import { _ } from 'webodm/classes/gettext';

const BOUNDARIES_URL = '/api/agri/boundaries/';

// Red (low / stressed) -> yellow -> green (healthy). Used by both the heatmap
// legend and the grid cell coloring so the whole map reads consistently.
function rygColor(t){
    t = Math.max(0, Math.min(1, t));
    // 0 -> red(215,48,39), 0.5 -> yellow(255,255,191), 1 -> green(26,152,80)
    let r, g, b;
    if (t < 0.5){
        const k = t / 0.5;
        r = Math.round(215 + (255 - 215) * k);
        g = Math.round(48 + (255 - 48) * k);
        b = Math.round(39 + (191 - 39) * k);
    }else{
        const k = (t - 0.5) / 0.5;
        r = Math.round(255 + (26 - 255) * k);
        g = Math.round(255 + (152 - 255) * k);
        b = Math.round(191 + (80 - 191) * k);
    }
    return `rgb(${r},${g},${b})`;
}

// Plain-language reading of a value within a field's own [lo, hi] range.
export function interpretIndex(t){
    if (t < 0.34) return _("Low — sparse or stressed vegetation");
    if (t < 0.67) return _("Moderate — patchy or developing");
    return _("Healthy — dense, vigorous growth");
}

// A custom Leaflet control: a single toggle button that opens/closes the
// Precise Agric side panel (matches the WebODM plugin convention).
const PreciseAgricControl = L.Control.extend({
    options: { position: 'topright' },
    onAdd: function(){
        const container = L.DomUtil.create('div', 'leaflet-bar leaflet-control precise-agric-control');
        const link = L.DomUtil.create('a', '', container);
        link.href = '#';
        link.title = _("Precise Agric");
        link.innerHTML = '<i class="fa fa-seedling"></i>';
        L.DomEvent.disableClickPropagation(container);
        L.DomEvent.on(link, 'click', L.DomEvent.stop).on(link, 'click', function(){
            if (this.onToggle) this.onToggle();
        }, this);
        return container;
    }
});

export default class App{
    constructor(map, task){
        this.map = map;
        this.task = task;
        this.panelShowed = false;
        this.pendingGeoJSON = null;
        this.boundaryLayers = {};   // boundary id -> L.GeoJSON layer
        this.boundaryData = {};     // boundary id -> boundary object (geom/status/name)
        this.boundariesVisible = true;
        this.selectedId = null;
        this.heatmapLayer = null;
        this.gridLayer = null;
        this.legendControl = null;
        this.onSelect = null;       // set by the panel to react to map clicks

        this.$panelContainer = $('<div class="precise-agric-panel-container"></div>').appendTo(map.getContainer());

        this.control = new PreciseAgricControl();
        this.control.onToggle = () => this.togglePanel();
        map.addControl(this.control);

        // Reuse WebODM's own polygon-drawing tool to capture a boundary's shape.
        // We never crop the orthophoto -- onPolygonChange just hands us the GeoJSON.
        this.drawControl = new CropButton({
            position: 'topright',
            title: _("Draw Field Boundary"),
            color: '#2e7d32',
            onPolygonChange: (feature) => this.onBoundaryDrawn(feature)
        });
        map.addControl(this.drawControl);

        // Draw existing boundaries immediately -- independent of the panel being
        // open. This is what makes saved boundaries persist + be clickable.
        this.loadBoundaries();
    }

    loadBoundaries(){
        $.getJSON(`${BOUNDARIES_URL}?task=${this.task.id}`).done(boundaries => {
            this.syncBoundaries(boundaries);
        });
    }

    togglePanel(){ this.panelShowed = !this.panelShowed; this.renderPanel(); }
    closePanel(){ this.panelShowed = false; this.renderPanel(); }

    onBoundaryDrawn(feature){
        if (!feature){
            this.pendingGeoJSON = null;
        }else{
            this.pendingGeoJSON = feature.geometry;
            this.panelShowed = true;
        }
        this.renderPanel();
    }

    clearDrawnPolygon(){ this.drawControl.deletePolygon(); }

    renderPanel(){
        if (!this.panelShowed){
            ReactDOM.unmountComponentAtNode(this.$panelContainer.get(0));
            return;
        }
        ReactDOM.render(<PreciseAgricPanel
                            app={this}
                            task={this.task}
                            pendingGeoJSON={this.pendingGeoJSON}
                            onClose={() => this.closePanel()}
                            onDiscardPending={() => { this.clearDrawnPolygon(); this.onBoundaryDrawn(null); }}
                            onPendingSaved={() => { this.clearDrawnPolygon(); this.onBoundaryDrawn(null); }}
                        />, this.$panelContainer.get(0));
    }

    // --- Boundary overlays (App owns the layer lifecycle) ---

    static STATUS_COLORS = {DRAFT: '#95a5a6', APPROVED: '#2e7d32', REJECTED: '#e74c3c'};

    syncBoundaries(boundaries){
        const currentIds = new Set(boundaries.map(b => b.id));
        // Drop layers for boundaries that no longer exist
        Object.keys(this.boundaryLayers).forEach(id => {
            if (!currentIds.has(Number(id)) && !currentIds.has(id)){
                this.map.removeLayer(this.boundaryLayers[id]);
                delete this.boundaryLayers[id];
                delete this.boundaryData[id];
            }
        });
        boundaries.forEach(b => this._drawBoundary(b));
    }

    _drawBoundary(b){
        this.boundaryData[b.id] = b;
        if (this.boundaryLayers[b.id]) this.map.removeLayer(this.boundaryLayers[b.id]);

        const selected = this.selectedId === b.id;
        const color = App.STATUS_COLORS[b.status] || '#2e7d32';
        const layer = L.geoJSON(b.geom, {
            style: {
                color,
                weight: selected ? 5 : 3,
                fillColor: color,
                fillOpacity: selected ? 0.15 : 0.08,
                dashArray: selected ? null : '4,4'
            }
        }).bindTooltip(b.name || '', {permanent: true, direction: 'center', className: 'boundary-label'});

        layer.on('click', () => this.selectBoundary(b.id, {fromMap: true}));
        this.boundaryLayers[b.id] = layer;
        if (this.boundariesVisible) layer.addTo(this.map);
    }

    setBoundariesVisible(visible){
        this.boundariesVisible = visible;
        Object.keys(this.boundaryLayers).forEach(id => {
            const layer = this.boundaryLayers[id];
            if (visible){ if (!this.map.hasLayer(layer)) layer.addTo(this.map); }
            else if (this.map.hasLayer(layer)) this.map.removeLayer(layer);
        });
    }

    selectBoundary(id, opts = {}){
        this.selectedId = id;
        // Restyle all boundaries so only the selected one is emphasized
        Object.keys(this.boundaryData).forEach(bid => this._drawBoundary(this.boundaryData[bid]));

        const layer = this.boundaryLayers[id];
        if (layer && layer.getBounds && layer.getBounds().isValid()){
            this.map.fitBounds(layer.getBounds(), {maxZoom: 19, padding: [40, 40]});
        }
        if (!this.panelShowed){ this.panelShowed = true; }
        this.renderPanel();
        if (opts.fromMap && this.onSelect) this.onSelect(id);
    }

    // --- Plant-health heatmap (reuses WebODM's tiler; url from the API) ---

    showHeatmap(tileUrl, indexName, opacity = 0.85){
        this.hideHeatmap();
        if (!tileUrl) return;
        this.heatmapLayer = L.tileLayer(tileUrl, {opacity, zIndex: 500, maxNativeZoom: 24}).addTo(this.map);
        this._showLegend(indexName);
    }
    hideHeatmap(){
        if (this.heatmapLayer){ this.map.removeLayer(this.heatmapLayer); this.heatmapLayer = null; }
        this._hideLegend();
    }
    setHeatmapOpacity(v){ if (this.heatmapLayer) this.heatmapLayer.setOpacity(v); }

    _showLegend(indexName){
        this._hideLegend();
        const legend = L.control({position: 'bottomright'});
        legend.onAdd = () => {
            const div = L.DomUtil.create('div', 'precise-agric-legend');
            div.innerHTML =
                `<div class="legend-title">${indexName || _("Plant health")}</div>` +
                `<div class="legend-bar"></div>` +
                `<div class="legend-labels"><span>${_("Poor")}</span><span>${_("Fair")}</span><span>${_("Healthy")}</span></div>`;
            return div;
        };
        legend.addTo(this.map);
        this.legendControl = legend;
    }
    _hideLegend(){ if (this.legendControl){ this.map.removeControl(this.legendControl); this.legendControl = null; } }

    // --- Grid analysis zones (clickable cells) ---

    showGrid(geojson, indexName){
        this.hideGrid();
        if (!geojson || !geojson.features || !geojson.features.length) return;

        const vals = geojson.features
            .map(f => f.properties && f.properties.mean_index)
            .filter(v => typeof v === 'number');
        const lo = Math.min.apply(null, vals);
        const hi = Math.max.apply(null, vals);
        const span = (hi - lo) || 1;

        this.gridLayer = L.geoJSON(geojson, {
            style: (feature) => {
                const t = ((feature.properties.mean_index) - lo) / span;
                return {color: '#333', weight: 0.5, fillColor: rygColor(t), fillOpacity: 0.6};
            },
            onEachFeature: (feature, layer) => {
                const v = feature.properties.mean_index;
                const t = (v - lo) / span;
                layer.bindPopup(
                    `<div class="grid-popup"><div class="gp-title">${indexName || _("Zone")}</div>` +
                    `<div class="gp-value">${Number(v).toFixed(3)}</div>` +
                    `<div class="gp-note">${interpretIndex(t)}</div></div>`);
            }
        }).addTo(this.map);
    }
    hideGrid(){ if (this.gridLayer){ this.map.removeLayer(this.gridLayer); this.gridLayer = null; } }
}
