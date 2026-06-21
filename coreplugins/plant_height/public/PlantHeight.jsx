import React from 'react';
import PropTypes from 'prop-types';
import Storage from 'webodm/classes/Storage';
import L from 'leaflet';
import './VegetationIndices.scss';
import ErrorMessage from 'webodm/components/ErrorMessage';
import Workers from 'webodm/classes/Workers';
import Utils from 'webodm/classes/Utils';
import { _ } from 'webodm/classes/gettext';

export default class PlantHeight extends React.Component {
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
        loading: true,
        task: props.tasks[0] || null,
        computing: false,
        progress: null,
        heightLayer: null,
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
              if (available_assets.indexOf("dsm.tif") === -1){
                this.setState({permanentError: _("No DSM is available. To estimate plant height you need a DSM.")});
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

    this.setState({computing: true, error: "", progress: null});

    $.post(`/api/plugins/plant-height/task/${id}/height`)
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
        this.setState({computing: false, error: _("Cannot compute plant height")});
      });
  }

  displayLayer = (geojson) => {
    const {map} = this.props;

    // Remove existing layer
    if (this.state.heightLayer){
      map.removeLayer(this.state.heightLayer);
    }

    // Create layer with points colored by height
    const layer = L.geoJSON(geojson, {
      pointToLayer: (feature, latlng) => {
        const height = feature.properties.height;
        let color = 'blue';
        if (height > 0.5) color = 'green';
        if (height > 1) color = 'yellow';
        if (height > 2) color = 'red';
        return L.circleMarker(latlng, {
          color: color,
          fillColor: color,
          fillOpacity: 0.5,
          radius: 2
        });
      }
    });

    layer.addTo(map);
    this.setState({heightLayer: layer});
  }

  handleRemoveLayer = () => {
    const {map} = this.props;
    if (this.state.heightLayer){
      map.removeLayer(this.state.heightLayer);
      this.setState({heightLayer: null});
    }
  }

  render(){
    const { loading, permanentError, heightLayer, computing, progress } = this.state;
    
    let content = "";
    if (loading) content = (<span><i className="fa fa-circle-notch fa-spin"></i> {_("Loading…")}</span>);
    else if (permanentError) content = (<div className="alert alert-warning">{permanentError}</div>);
    else{
      content = (<div>
        <button onClick={this.handleCompute} disabled={computing}
                type="button" className="btn btn-primary btn-block">
          {computing ? <span><i className="fa fa-circle-notch fa-spin"></i> {_("Estimating…")}</span> : _("Estimate Height")}
        </button>
        {progress ? <div className="progress">
          <div className="progress-bar" style={{width: progress + '%'}}></div>
        </div> : ""}
        {this.state.error ? <ErrorMessage error={this.state.error} /> : ""}
      </div>);
    }

    if (heightLayer) {
      const pointCount = heightLayer.getLayers().length;
      content = (<div>
        <div className="height-action-buttons">
          <span><strong>{_("Points:")}</strong> {pointCount}</span>
          <button onClick={this.handleRemoveLayer}
                  type="button" className="btn btn-sm btn-default">
            <i className="fa fa-trash fa-fw"/>
          </button>
        </div>
        {content}
      </div>);
    }

    return (<div className="plantheight-panel">
      <span className="close-button" onClick={this.props.onClose}/>
      <div className="title">{_("Plant Height")}</div>
      <hr/>
      {content}
    </div>);
  }
}