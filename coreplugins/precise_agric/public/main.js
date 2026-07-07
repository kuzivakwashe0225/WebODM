PluginsAPI.Map.willAddControls([
		'precise_agric/build/app.js',
		'precise_agric/build/app.css'
	], function(args, App){
	var tasks = [];
	var ids = {};

	for (var i = 0; i < args.tiles.length; i++){
		var task = args.tiles[i].meta.task;
		if (!ids[task.id]){
			tasks.push(task);
			ids[task.id] = true;
		}
	}

	// Single-task map views only (a Capture is one task)
	if (tasks.length === 1){
		new App(args.map, tasks[0]);
	}
});

// Adds "Upload Orthophoto (Precise Agric)" to each project's existing
// "Import" dropdown (next to the native "Assets / Backups" and "External
// Data" options) -- for a capture that's already a finished, georeferenced
// GeoTIFF, bypassing NodeODM entirely (see agri/capture.py). No JSX here:
// main.js is loaded as a plain script (not webpack-bundled), so we build
// the element with React.createElement directly.
PluginsAPI.Dashboard.addImportTaskItem(function(args){
	var handleClick = function(){
		// The input must actually be in the DOM (even if hidden) for a
		// synthetic .click() to reliably open the native file picker and
		// for its change event to fire in every browser -- a detached
		// element is not guaranteed to work.
		var input = document.createElement('input');
		input.type = 'file';
		input.accept = '.tif,.tiff';
		input.style.display = 'none';
		document.body.appendChild(input);

		var cleanup = function(){
			if (input.parentNode) input.parentNode.removeChild(input);
		};

		input.onchange = function(){
			var file = input.files[0];
			console.log('[precise_agric] file input changed:', file);
			if (!file){ cleanup(); return; }

			var defaultName = file.name.replace(/\.(tif|tiff)$/i, '');
			var name = window.prompt('Name this capture:', defaultName);
			if (name === null){ cleanup(); return; } // user cancelled

			// Acquisition date drives the Season Progress graphs. Default today.
			var today = new Date().toISOString().slice(0, 10);
			var captureDate = window.prompt('Capture date (YYYY-MM-DD) — when the imagery was flown:', today);
			if (captureDate === null){ cleanup(); return; } // user cancelled

			var formData = new FormData();
			formData.append('project', args.projectId);
			formData.append('name', name || file.name);
			formData.append('capture_date', captureDate || today);
			formData.append('orthophoto', file);

			console.log('[precise_agric] uploading orthophoto to /api/agri/captures/ ...');
			jQuery.ajax({
				url: '/api/agri/captures/',
				type: 'POST',
				data: formData,
				processData: false,
				contentType: false
			}).done(function(res){
				console.log('[precise_agric] upload OK:', res);
				cleanup();
				args.onNewTaskAdded();
			}).fail(function(xhr){
				console.error('[precise_agric] upload FAILED:', xhr.status, xhr.responseText);
				cleanup();
				var detail = (xhr.responseJSON && (xhr.responseJSON.detail || xhr.responseJSON.orthophoto)) || xhr.statusText;
				window.alert('Could not upload orthophoto: ' + detail);
			});
		};
		input.click();
	};

	return React.createElement('li', {key: 'precise-agric-upload'},
		React.createElement('a', {href: 'javascript:void(0)', onClick: handleClick},
			React.createElement('i', {className: 'fa fa-seedling fa-fw'}),
			' Upload Orthophoto (Precise Agric)'
		)
	);
});
