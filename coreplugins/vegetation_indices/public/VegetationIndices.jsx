import React from 'react';
import PropTypes from 'prop-types';
import Storage from 'webodm/classes/Storage';
import L from 'leaflet';
import './VegetationIndices.scss';
import ErrorMessage from 'webodm/components/ErrorMessage';
import Workers from 'webodm/classes/Workers';
import Utils from 'webodm/classes/Utils';
import { _ } from 'webodm/classes/gettext';

export default class VegetationIndices extends React.Component {
  static defaultProps = {
  };
  static propTypes = {
    onClose: PropTypes.func.isRequired,
    tasks: PropTypes.object.isRequired,
    isShowed: PropTypes.bool.isRequired,
    map: PropTypes.object.isRequired
  }

  constructor(props){
    super(props);

    this.state = {
        error: "",
        permanentError: "",
        index: Storage.getItem("last_vegetation_index") || "ndvi",
        loading: true,
        task: props.tasks[0] || null,
        computing: false,
        progress: null,
        vegLayer: null,
    };
  }

  componentDidMount(){
  }

  componentDidUpdate(){
    if (this.props.isShowed && this.state.loading){
      const {id, project} = this.state.task;
      
      this.loadingReq = $.getJSON(`/api/projects/${project}/tasks/${id}/`)
          .done(res => {
              const { available_assets } = res;
              if (available_assets.indexOf("orthophoto.tif") === -1){
                this.setState({permanentError: _("No orthophoto is available. To compute vegetation indices you need an orthophoto.")});
              }
          })
          .fail(() => {
            this.setState({permanentError: _("Cannot retrieve task information.")});
          })
          .always(() => {
            this.setState({loading: false});
          });
    }
  }

  handleCompute = () => {
    const {id, project} = this.state.task;
    const {index} = this.state;

    Storage.setItem("last_vegetation_index", index);

    this.setState({computing: true, error: "", progress: null});

    $.post(`/api/plugins/vegetation-indices/task/${id}/vegetation`, {index})
      .done(res => {
        if (res.celery_task_id){
          Workers.waitForCompletion(res.celery_task_id, (status) => {
            this.setState({progress: status.progress});
          }, 1000)
          .done(result => {
            this.setState({computing: false, progress: null});
            this.displayLayer(result);
          })
          .fail(error => {
            this.setState({computing: false, progress: null, error: error});
          });
        }else{
          this.setState({computing: false, error: res.error || _("Invalid response")});
        }
      })
      .fail(() => {
        this.setState({computing: false, error: _("Cannot compute vegetation index")});
      });
  }

  displayLayer = (geojson) => {
    const {map} = this.props;

    // Remove existing layer
    if (this.state.vegLayer){
      map.removeLayer(this.state.vegLayer);
    }

    // Create layer with points colored by index
    const layer = L.geoJSON(geojson, {
      pointToLayer: (feature, latlng) => {
        const index = feature.properties.index;
        let color = 'red';
        if (index > 0.5) color = 'green';
        else if (index > 0) color = 'yellow';
        return L.circleMarker(latlng, {
          color: color,
          fillColor: color,
          fillOpacity: 0.5,
          radius: 2
        });
      }
    });

    layer.addTo(map);
    this.setState({vegLayer: layer});
  }

  handleRemoveLayer = () => {
    const {map} = this.props;
    if (this.state.vegLayer){
      map.removeLayer(this.state.vegLayer);
      this.setState({vegLayer: null});
    }
  }

  render(){
    const { loading, permanentError, vegLayer, computing, index, progress } = this.state;
    const indices = [
      {label: _('NDVI'), value: 'ndvi'},
      {label: _('NGRDI'), value: 'ngrdi'},
    ]
    
    let content = "";
    if (loading) content = (<span><i className="fa fa-circle-notch fa-spin"></i> {_("Loading…")}</span>);
    else if (permanentError) content = (<div className="alert alert-warning">{permanentError}</div>);
    else{
      content = (<div>
        <div className="form-group">
          <label>{_("Vegetation Index:")}</label>
          <select className="form-control" value={index} onChange={e => this.setState({index: e.target.value})}>
            {indices.map(i => <option value={i.value}>{i.label}</option>)}
          </select>
        </div>
        <button onClick={this.handleCompute} disabled={computing}
                type="button" className="btn btn-primary btn-block">
          {computing ? <span><i className="fa fa-circle-notch fa-spin"></i> {_("Computing…")}</span> : _("Compute")}
        </button>
        {progress ? <div className="progress">
          <div className="progress-bar" style={{width: progress + '%'}}></div>
        </div> : ""}
        {this.state.error ? <ErrorMessage error={this.state.error} /> : ""}
      </div>);
    }

    if (vegLayer) {
      const pointCount = vegLayer.getLayers().length;
      content = (<div>
        <div className="veg-action-buttons">
          <span><strong>{_("Points:")}</strong> {pointCount}</span>
          <button onClick={this.handleRemoveLayer}
                  type="button" className="btn btn-sm btn-default">
            <i className="fa fa-trash fa-fw"/>
          </button>
        </div>
        {content}
      </div>);
    }

    return (<div className="vegetation-panel">
      <span className="close-button" onClick={this.props.onClose}/>
      <div className="title">{_("Vegetation Indices")}</div>
      <hr/>
      {content}
    </div>);
  }
}