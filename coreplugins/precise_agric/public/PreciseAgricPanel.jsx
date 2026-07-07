import React from 'react';
import $ from 'jquery';
import './PreciseAgricPanel.scss';
import ErrorMessage from 'webodm/components/ErrorMessage';
import { _ } from 'webodm/classes/gettext';

const BOUNDARIES_URL = '/api/agri/boundaries/';
const ANALYSIS_URL = '/api/agri/analysis/';
const FIELDS_URL = '/api/agri/fields/';
const POLL_INTERVAL_MS = 3000;

const RESULT_LABELS = {
    plant_health: _("Plant Health"),
    rgb_index: _("RGB Indices"),
    grid: _("Grid Analysis"),
    canopy: _("Canopy Cover"),
    weed: _("Weed Mapping"),
    report: _("Report")
};

// Friendly one-liner from a plant-health run's classification / mean.
function healthHeadline(run){
    const ph = (run.results || []).find(r => r.kind === "plant_health");
    if (!ph || !ph.stats || ph.stats.mean === undefined || ph.stats.mean === null) return null;
    const mean = ph.stats.mean;
    const idx = ph.stats.index || _("index");
    if (idx === "NDVI"){
        if (mean >= 0.5) return _("Vegetation looks healthy and vigorous. 🌿");
        if (mean >= 0.2) return _("Vegetation is moderate — some patchy or developing areas.");
        return _("Sparse or stressed vegetation — check the red zones on the map.");
    }
    return _("See the heatmap for where growth is strongest and weakest.");
}

export default class PreciseAgricPanel extends React.Component{
    constructor(props){
        super(props);
        this.state = {
            loading: true,
            error: "",
            boundaries: [],
            runsByBoundary: {},
            fields: [],
            pendingName: _("New field"),
            pendingFieldName: "",
            busyBoundaryId: null,
            selectedId: props.app.selectedId,
            heatmapBoundaryId: null,   // which boundary's heatmap is shown
            heatmapOpacity: 0.85,
            gridBoundaryId: null,      // which boundary's grid is shown
            boundariesVisible: props.app.boundariesVisible
        };
        this.pollTimers = {};
    }

    componentDidMount(){
        // React to boundary clicks originating on the map
        this.props.app.onSelect = (id) => this.handleSelect(id, {skipMap: true});
        this.refresh();
        this.loadFields();
    }

    componentWillUnmount(){
        Object.keys(this.pollTimers).forEach(id => clearTimeout(this.pollTimers[id]));
        if (this.refreshReq) this.refreshReq.abort();
        if (this.props.app.onSelect) this.props.app.onSelect = null;
    }

    loadFields = () => {
        $.getJSON(`${FIELDS_URL}?task=${this.props.task.id}`)
            .done(fields => this.setState({fields}))
            .fail(() => {});
    }

    refresh = () => {
        this.setState({loading: true, error: ""});
        this.refreshReq = $.getJSON(`${BOUNDARIES_URL}?task=${this.props.task.id}`)
            .done(boundaries => {
                this.props.app.syncBoundaries(boundaries);
                $.getJSON(ANALYSIS_URL).done(runs => {
                    const runsByBoundary = {};
                    runs.forEach(run => {
                        if (String(run.task) === String(this.props.task.id) &&
                            runsByBoundary[run.boundary] === undefined){
                            runsByBoundary[run.boundary] = run;
                        }
                    });
                    this.setState({boundaries, runsByBoundary, loading: false});
                    Object.keys(runsByBoundary).forEach(bId => {
                        const run = runsByBoundary[bId];
                        if (run && (run.status === "PENDING" || run.status === "RUNNING")){
                            this.schedulePoll(run.id);
                        }
                    });
                }).fail(() => this.setState({boundaries, loading: false}));
            })
            .fail(() => this.setState({loading: false,
                error: _("Could not load fields. Are you logged in?")}));
    }

    schedulePoll = (runId) => {
        if (this.pollTimers[runId]) return;
        this.pollTimers[runId] = setTimeout(() => this.pollRun(runId), POLL_INTERVAL_MS);
    }

    pollRun = (runId) => {
        delete this.pollTimers[runId];
        $.getJSON(`${ANALYSIS_URL}${runId}/`).done(run => {
            this.setState(prev => ({runsByBoundary: {...prev.runsByBoundary, [run.boundary]: run}}));
            if (run.status === "PENDING" || run.status === "RUNNING") this.schedulePoll(runId);
        }).fail(() => this.schedulePoll(runId));
    }

    // --- Selection + overlays ---

    handleSelect = (id, opts = {}) => {
        this.setState({selectedId: id});
        if (!opts.skipMap) this.props.app.selectBoundary(id);
    }

    latestRun = (boundaryId) => this.state.runsByBoundary[boundaryId];

    toggleBoundariesVisible = () => {
        const visible = !this.state.boundariesVisible;
        this.props.app.setBoundariesVisible(visible);
        this.setState({boundariesVisible: visible});
    }

    handleReuseFields = () => {
        this.setState({error: ""});
        $.ajax({type: 'POST', url: '/api/agri/reuse-boundaries/', contentType: 'application/json',
                data: JSON.stringify({task: this.props.task.id})})
            .done(() => this.refresh())
            .fail(xhr => this.setState({
                error: this.describeError(xhr, _("No previous fields found to reuse."))}));
    }

    toggleHeatmap = (boundary) => {
        const run = this.latestRun(boundary.id);
        if (this.state.heatmapBoundaryId === boundary.id){
            this.props.app.hideHeatmap();
            this.setState({heatmapBoundaryId: null});
        }else if (run && run.plant_health_tile_url){
            this.props.app.showHeatmap(run.plant_health_tile_url, run.index_used, this.state.heatmapOpacity);
            this.setState({heatmapBoundaryId: boundary.id});
        }
    }

    changeOpacity = (v) => {
        this.setState({heatmapOpacity: v});
        this.props.app.setHeatmapOpacity(v);
    }

    toggleGrid = (boundary) => {
        const run = this.latestRun(boundary.id);
        if (this.state.gridBoundaryId === boundary.id){
            this.props.app.hideGrid();
            this.setState({gridBoundaryId: null});
            return;
        }
        if (!run) return;
        $.getJSON(`${ANALYSIS_URL}${run.id}/grid/`).done(geojson => {
            this.props.app.showGrid(geojson, run.index_used);
            this.setState({gridBoundaryId: boundary.id});
        }).fail(() => this.setState({error: _("Could not load grid zones")}));
    }

    // --- Drawing / saving a new boundary ---

    handleDiscardPending = () => {
        this.setState({pendingName: _("New field"), pendingFieldName: ""});
        this.props.onDiscardPending();
    }

    handleSavePending = () => {
        const { task, pendingGeoJSON } = this.props;
        const { pendingName, pendingFieldName } = this.state;
        this.setState({error: ""});
        $.ajax({
            type: 'POST', url: BOUNDARIES_URL, contentType: 'application/json',
            data: JSON.stringify({
                task: task.id,
                name: pendingName || _("Field"),
                geom: pendingGeoJSON,
                field_name: pendingFieldName || pendingName || _("Field")
            })
        }).done(() => {
            this.setState({pendingName: _("New field"), pendingFieldName: ""});
            this.props.onPendingSaved();
            this.refresh();
            this.loadFields();
        }).fail(xhr => this.setState({error: this.describeError(xhr, _("Could not save field"))}));
    }

    // --- Review / delete / analyze ---

    handleReview = (boundary, action) => {
        this.setState({busyBoundaryId: boundary.id, error: ""});
        $.ajax({type: 'POST', url: `${BOUNDARIES_URL}${boundary.id}/${action}/`})
            .done(() => { this.setState({busyBoundaryId: null}); this.refresh(); })
            .fail(xhr => this.setState({busyBoundaryId: null,
                error: this.describeError(xhr, _("Could not update field"))}));
    }

    handleDelete = (boundary) => {
        const msg = boundary.status === "APPROVED"
            ? _("Delete this field? Any analysis on it will also be deleted. This cannot be undone.")
            : _("Delete this field? This cannot be undone.");
        if (!window.confirm(msg)) return;
        this.setState({busyBoundaryId: boundary.id, error: ""});
        $.ajax({type: 'DELETE', url: `${BOUNDARIES_URL}${boundary.id}/`})
            .done(() => { this.setState({busyBoundaryId: null}); this.refresh(); })
            .fail(xhr => this.setState({busyBoundaryId: null,
                error: this.describeError(xhr, _("Could not delete field"))}));
    }

    handleAnalyze = (boundary) => {
        this.setState({busyBoundaryId: boundary.id, error: ""});
        $.ajax({type: 'POST', url: ANALYSIS_URL, contentType: 'application/json',
            data: JSON.stringify({boundary: boundary.id})})
        .done(run => {
            this.setState(prev => ({busyBoundaryId: null,
                runsByBoundary: {...prev.runsByBoundary, [boundary.id]: run}}));
            this.schedulePoll(run.id);
        }).fail(xhr => this.setState({busyBoundaryId: null,
            error: this.describeError(xhr, _("Could not start analysis"))}));
    }

    handleReviewRun = (run, action) => {
        this.setState({busyBoundaryId: run.boundary, error: ""});
        $.ajax({type: 'POST', url: `${ANALYSIS_URL}${run.id}/${action}/`})
            .done(updatedRun => this.setState(prev => ({busyBoundaryId: null,
                runsByBoundary: {...prev.runsByBoundary, [updatedRun.boundary]: updatedRun}})))
            .fail(xhr => this.setState({busyBoundaryId: null,
                error: this.describeError(xhr, _("Could not update analysis"))}));
    }

    describeError(xhr, fallback){
        if (xhr.status === 403) return _("You don't have permission to do that (role restriction).");
        if (xhr.responseJSON && xhr.responseJSON.detail) return xhr.responseJSON.detail;
        return fallback;
    }

    renderRunSummary(boundary, run){
        if (!run) return null;

        if (run.status === "PENDING" || run.status === "RUNNING"){
            return (<div className="run-status running">
                <i className="fa fa-circle-notch fa-spin"></i> {_("Analyzing your field…")}
            </div>);
        }
        if (run.status === "FAILED"){
            return (<div className="run-status failed">
                <i className="fa fa-exclamation-triangle"></i> {_("Analysis failed")}: {run.error}
            </div>);
        }

        const headline = healthHeadline(run);
        const heatmapOn = this.state.heatmapBoundaryId === boundary.id;
        const gridOn = this.state.gridBoundaryId === boundary.id;
        const hasHeatmap = !!run.plant_health_tile_url;

        return (<div className="run-results">
            {headline ? <div className="run-headline">{headline}</div> : null}

            <ul className="results-list">
                {(run.results || []).map(r => (
                    <li key={r.kind}>{RESULT_LABELS[r.kind] || r.kind}
                        {r.kind === "plant_health" && r.stats && r.stats.mean !== undefined && r.stats.mean !== null ?
                            <span className="metric"> {Number(r.stats.mean).toFixed(3)} {r.stats.index || ""}</span> : ""}
                        {r.kind === "canopy" && r.stats && r.stats.canopy_pct !== undefined ?
                            <span className="metric"> {r.stats.canopy_pct}%</span> : ""}
                        {r.kind === "weed" && r.stats && r.stats.weed_count !== undefined ?
                            <span className="metric"> {r.stats.weed_count} {_("hotspots")}</span> : ""}
                    </li>
                ))}
            </ul>

            <div className="map-toggles">
                <button className={"btn btn-sm " + (heatmapOn ? "btn-success" : "btn-default")}
                        disabled={!hasHeatmap} onClick={() => this.toggleHeatmap(boundary)}>
                    <i className="fa fa-fire"></i> {heatmapOn ? _("Hide heatmap") : _("Show heatmap")}
                </button>
                <button className={"btn btn-sm " + (gridOn ? "btn-success" : "btn-default")}
                        onClick={() => this.toggleGrid(boundary)}>
                    <i className="fa fa-th"></i> {gridOn ? _("Hide zones") : _("Show zones")}
                </button>
            </div>
            {heatmapOn ? (<div className="opacity-row">
                <span>{_("Opacity")}</span>
                <input type="range" min="0.2" max="1" step="0.05" value={this.state.heatmapOpacity}
                       onChange={e => this.changeOpacity(parseFloat(e.target.value))} />
            </div>) : null}

            {run.status === "PENDING_REVIEW" ? (<div className="review-actions">
                <button className="btn btn-sm btn-primary" onClick={() => this.handleReviewRun(run, "approve")}>
                    <i className="fa fa-check"></i> {_("Approve & push")}
                </button>
                <button className="btn btn-sm btn-default" onClick={() => this.handleReviewRun(run, "reject")}>
                    <i className="fa fa-times"></i> {_("Reject")}
                </button>
            </div>) : null}
            {run.status === "APPROVED" ? <div className="run-status approved">
                <i className="fa fa-check-circle"></i> {_("Approved — pushed to AgriTrack")}
            </div> : null}
            {run.status === "REJECTED" ? <div className="run-status rejected">
                <i className="fa fa-times-circle"></i> {_("Rejected")}
            </div> : null}
        </div>);
    }

    renderBoundary(b){
        const { runsByBoundary, busyBoundaryId, selectedId } = this.state;
        const run = runsByBoundary[b.id];
        const busy = busyBoundaryId === b.id;
        const selected = selectedId === b.id;

        return (<div className={"boundary-card" + (selected ? " selected" : "")} key={b.id}
                     onClick={() => this.handleSelect(b.id)}>
            <div className="boundary-header">
                <span className={"status-pill status-" + b.status.toLowerCase()}>{b.status}</span>
                <span className="boundary-name">{b.name}</span>
                <button className="btn-delete" disabled={busy} title={_("Delete field")}
                        onClick={(e) => { e.stopPropagation(); this.handleDelete(b); }}>
                    <i className="fa fa-trash"></i>
                </button>
            </div>

            {b.status === "DRAFT" ? (<div className="boundary-actions" onClick={e => e.stopPropagation()}>
                <button className="btn btn-sm btn-primary" disabled={busy}
                        onClick={() => this.handleReview(b, "approve")}>
                    <i className="fa fa-check"></i> {_("Approve")}
                </button>
                <button className="btn btn-sm btn-default" disabled={busy}
                        onClick={() => this.handleReview(b, "reject")}>
                    <i className="fa fa-times"></i> {_("Reject")}
                </button>
            </div>) : null}

            {b.status === "APPROVED" && !run ? (<div className="boundary-actions" onClick={e => e.stopPropagation()}>
                <button className="btn btn-sm btn-success" disabled={busy}
                        onClick={() => this.handleAnalyze(b)}>
                    {busy ? <i className="fa fa-circle-notch fa-spin"></i> : <i className="fa fa-play"></i>} {_("Start Analysis")}
                </button>
            </div>) : null}

            <div onClick={e => e.stopPropagation()}>{run ? this.renderRunSummary(b, run) : null}</div>
        </div>);
    }

    render(){
        const { loading, error, boundaries, pendingName, pendingFieldName, fields, boundariesVisible } = this.state;
        const { pendingGeoJSON } = this.props;

        return (<div className="precise-agric-panel">
            <div className="panel-header">
                <span className="title"><i className="fa fa-seedling"></i> {_("Precise Agric")}</span>
                <span className="header-tools">
                    <button className={"boundaries-toggle" + (boundariesVisible ? " on" : "")}
                            title={boundariesVisible ? _("Hide field boundaries") : _("Show field boundaries")}
                            onClick={this.toggleBoundariesVisible}>
                        <i className={"fa " + (boundariesVisible ? "fa-eye" : "fa-eye-slash")}></i>
                        <span className="label">{_("Boundaries")}</span>
                    </button>
                    <span className="close-button" onClick={this.props.onClose}>&times;</span>
                </span>
            </div>

            <ErrorMessage bind={[this, "error"]} />

            {pendingGeoJSON ? (
                <div className="pending-boundary">
                    <label>{_("Field drawn! Name it and assign it to a season field:")}</label>
                    <input type="text" className="form-control" value={pendingName} autoFocus
                           placeholder={_("This capture's name for the field")}
                           onFocus={e => e.target.select()}
                           onChange={e => this.setState({pendingName: e.target.value})} />
                    <input type="text" className="form-control" value={pendingFieldName} list="pa-field-list"
                           placeholder={_("Season field (for progress tracking)")}
                           onChange={e => this.setState({pendingFieldName: e.target.value})} />
                    <datalist id="pa-field-list">
                        {fields.map(f => <option key={f.id} value={f.name} />)}
                    </datalist>
                    <div className="pending-actions">
                        <button className="btn btn-sm btn-primary" onClick={this.handleSavePending}>
                            <i className="fa fa-save"></i> {_("Save field")}
                        </button>
                        <button className="btn btn-sm btn-default" onClick={this.handleDiscardPending}>
                            <i className="fa fa-undo"></i> {_("Discard")}
                        </button>
                    </div>
                </div>
            ) : (
                <div className="draw-hint">
                    <i className="fa fa-info-circle"></i> {_("Use the \"Draw Field Boundary\" button (top-right) to map a field, then approve and analyze it.")}
                </div>
            )}

            <hr/>

            {loading ? (<div className="loading"><i className="fa fa-circle-notch fa-spin"></i> {_("Loading…")}</div>) :
             boundaries.length === 0 ? (<div className="empty">
                <div>🌱 {_("No fields on this capture yet.")}</div>
                <button className="btn btn-sm btn-success reuse-btn" onClick={this.handleReuseFields}>
                    <i className="fa fa-clone"></i> {_("Reuse fields from a previous capture")}
                </button>
                <div className="empty-hint">{_("…or draw a new field on the map (top-right).")}</div>
             </div>) :
             (<div className="boundaries-list">{boundaries.map(b => this.renderBoundary(b))}</div>)}
        </div>);
    }
}
